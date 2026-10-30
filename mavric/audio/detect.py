"""Automatic sound event proposals, two-microphone measurements and emitter suggestions.

Proposals: band-limited energy in 10 ms frames, against a running noise
floor (20th percentile per 10 s); an event starts above floor + `on_db`,
lasts while above floor + `off_db`, and close events are merged.

Measurements: band-passed level on each microphone and the arrival-time
difference between the two (GCC-PHAT), i.e. which side of the cage the sound
came from.

Suggestions: birds RFID places on a perch during the event, plus the rig
itself when a played sound overlaps the event. With the microphones and the
places placed in 3D (the session's calibration), each bird is graded by how
well the sound's level and arrival-time difference fit its place
(geometry.place_fit); otherwise ranked by whether its perch is near the
louder / earlier microphone (`audio.mic_near` in rig.yaml).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .events import SYSTEM, UNKNOWN, SoundEvent

PROPOSAL_COLUMNS = ["start_s", "stop_s", "peak_db", "loudest"]


def _band_power_db(x: np.ndarray, sr: float, n: int, band: tuple[float, float]) -> np.ndarray:
    """(frames, ch) band energy in dB for consecutive frames of n samples."""
    nf = len(x) // n
    if nf == 0:
        return np.zeros((0, x.shape[1]), np.float32)
    fr = x[: nf * n].reshape(nf, n, x.shape[1]) * np.hanning(n)[None, :, None].astype(np.float32)
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    f = np.fft.rfftfreq(n, 1 / sr)
    m = (f >= band[0]) & (f <= band[1])
    return (10 * np.log10(spec[:, m, :].sum(axis=1) + 1e-20)).astype(np.float32)


def detect_events(src, band=(300.0, 8000.0), frame_s: float = 0.01, on_db: float = 12.0, off_db: float = 6.0,
                  min_dur_s: float = 0.05, merge_gap_s: float = 0.15, block_s: float = 10.0,
                  chunk_s: float = 60.0, progress=None) -> pd.DataFrame:
    """Candidate sound events over the whole recording (session times)."""
    n = max(1, int(round(frame_s * src.sr)))
    chunk = int(chunk_s * src.sr) // n * n
    parts = []
    for s0 in range(0, src.n_samples, chunk):
        parts.append(_band_power_db(src.read_samples(s0, s0 + chunk, src.mic_idx), src.sr, n, band))
        if progress:
            progress(min(1.0, (s0 + chunk) / max(src.n_samples, 1)))
    if not parts or not sum(len(p) for p in parts):
        return pd.DataFrame(columns=PROPOSAL_COLUMNS)
    E = np.concatenate(parts)
    nb = max(1, int(round(block_s / frame_s)))
    centres, floors = [], []
    for b0 in range(0, len(E), nb):
        centres.append(b0 + min(nb, len(E) - b0) / 2)
        floors.append(np.percentile(E[b0: b0 + nb], 20, axis=0))
    floors = np.array(floors)
    idx = np.arange(len(E))
    floor = np.stack([np.interp(idx, centres, floors[:, c]) for c in range(E.shape[1])], axis=1)
    rel = E - floor
    act = rel.max(axis=1)
    above_off = act > off_db
    # runs of frames above the off threshold that reach the on threshold somewhere
    edges = np.diff(np.r_[0, above_off.astype(int), 0])
    starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    runs = [(a, b) for a, b in zip(starts, stops) if act[a:b].max() > on_db]
    merged: list[list[int]] = []
    gap = int(round(merge_gap_s / frame_s))
    for a, b in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    rows = []
    for a, b in merged:
        if (b - a) * frame_s < min_dur_s:
            continue
        peak = rel[a:b].max(axis=0)
        t0, t1 = (float(v) for v in src.time_of(np.array([a * n, b * n], float)))
        rows.append((t0, t1, float(peak.max()), src.mics[int(np.argmax(peak))]))
    return pd.DataFrame(rows, columns=PROPOSAL_COLUMNS)


def _bandpass(x: np.ndarray, sr: float, band) -> np.ndarray:
    X = np.fft.rfft(x, axis=0)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    X[(f < band[0]) | (f > band[1])] = 0
    return np.fft.irfft(X, n=len(x), axis=0)


def measure_event(src, t0: float, t1: float, band=(300.0, 8000.0), max_lag_s: float = 0.003,
                  min_level_diff_db: float = 3.0, floor_db: float = 30.0) -> dict:
    """Level per microphone and arrival-time difference between the first two microphones.

    The time difference is searched within `max_lag_s` (the microphones' distance / c, when known). Frequencies
    more than `floor_db` below the strongest are left out of the correlation: whitened, the noise there would
    swamp a tonal call's few harmonics."""
    t1 = min(t1, t0 + 10.0)
    x, _ = src.read(t0, t1, src.mic_idx)
    out = {"levels_db": {}, "louder": None, "level_diff_db": None, "delay_ms": None, "first": None}
    if len(x) < 64:
        return out
    y = _bandpass(x.astype(np.float64), src.sr, band)
    rms = np.sqrt(np.mean(y ** 2, axis=0)) + 1e-12
    db = 20 * np.log10(rms)
    out["levels_db"] = {m: float(v) for m, v in zip(src.mics, db)}
    if len(src.mics) < 2:
        return out
    diff = float(db[0] - db[1])
    out["level_diff_db"] = diff
    if abs(diff) >= min_level_diff_db:
        out["louder"] = src.mics[0] if diff > 0 else src.mics[1]
    # GCC-PHAT: delay of mic 1 relative to mic 0 (positive: mic 0 heard it first)
    a, b = y[:, 0], y[:, 1]
    nfft = 1 << int(np.ceil(np.log2(2 * len(a))))
    A, B = np.fft.rfft(a, nfft), np.fft.rfft(b, nfft)
    R = B * np.conj(A)
    mag = np.abs(R)
    f = np.fft.rfftfreq(nfft, 1 / src.sr)
    keep = (f >= band[0]) & (f <= band[1])
    if keep.any():
        keep &= mag > mag[keep].max() * 10 ** (-floor_db / 10)
    R = np.where(keep, R / (mag + 1e-12), 0)
    cc = np.fft.irfft(R, nfft)
    m = int(max_lag_s * src.sr)
    cc = np.r_[cc[-m:], cc[: m + 1]]
    k = int(np.argmax(cc))
    if 0 < k < len(cc) - 1:  # parabolic peak interpolation
        y0, y1, y2 = cc[k - 1], cc[k], cc[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    else:
        frac = 0.0
    lag = (k - m + frac) / src.sr
    out["delay_ms"] = float(lag * 1e3)
    # Tonal calls (coos) correlate again one pitch period away, so the delay is only trusted when its peak
    # clearly beats every other local maximum more than 0.3 ms away (broadband sounds: claps, wing beats).
    peaks = np.flatnonzero((cc[1:-1] > cc[:-2]) & (cc[1:-1] >= cc[2:])) + 1
    others = [p for p in peaks if abs(p - k) > 0.0003 * src.sr]
    unique = not others or max(cc[p] for p in others) < 0.7 * cc[k]
    out["delay_reliable"] = bool(unique and abs(k - m) < m)  # a peak at the window edge is not a delay
    # every delay the correlation allows: the peak and, for a tonal call, the ones nearly as high
    out["delay_candidates_ms"] = ([float(lag * 1e3)] if abs(k - m) < m else []) + [
        float((p - m) / src.sr * 1e3) for p in others if cc[p] >= 0.7 * cc[k]]
    if out["delay_reliable"] and abs(lag) * 1e3 >= 0.1:
        out["first"] = src.mics[0] if lag > 0 else src.mics[1]
    return out


def occupancy(bouts: pd.DataFrame, t0: float, t1: float) -> list[tuple[int, str, str]]:
    """(antenna, roi, pigeon) of RFID bouts overlapping [t0, t1], spurious ones excluded."""
    if bouts is None or not len(bouts):
        return []
    b = bouts[(bouts["start_s"] < t1) & (bouts["stop_s"] > t0)]
    if "curated" in b:
        b = b[b["curated"] != "spurious"]
    return sorted({(int(r.antenna), str(r.roi), str(r.pigeon)) for r in b.itertuples()})


def suggest_emitters(meas: dict, perched: list[tuple[int, str, str]], mic_near: dict[str, list[int]],
                     played: list[SoundEvent] | None = None,
                     places: dict[str, float] | None = None) -> list[tuple[str, float, str]]:
    """Ranked (emitter, score, reason).

    `places` ({place: 0 to 1}, geometry.place_fit) grades each subject by how well the sound fits the place it is
    on, and "unknown" by the best place where no tag was read; without it, a subject ranks by whether its place
    is near the louder / earlier microphone (`mic_near`) and "unknown" comes last."""
    out: dict[str, tuple[float, str]] = {}
    for p in played or []:
        out[p.emitter] = (1.0, f"the rig played {p.note or 'a sound'}")
    if places:
        return _graded(out, perched, places)
    side = meas.get("louder") or meas.get("first")
    how = []
    if meas.get("louder"):
        how.append(f"louder on {meas['louder']} ({abs(meas['level_diff_db']):.0f} dB)")
    if meas.get("first"):
        how.append(f"reached {meas['first']} first ({abs(meas['delay_ms']):.1f} ms)")
    for ant, roi, pig in perched:
        if side and mic_near:
            near = ant in mic_near.get(side, [])
            score = 0.8 if near else 0.2
            reason = f"on {roi}, {'near' if near else 'away from'} {side}"
        else:
            score, reason = 0.5, f"on {roi}"
        if how and score >= 0.5:
            reason += "; " + ", ".join(how)
        if pig not in out or out[pig][0] < score:
            out[pig] = (score, reason)
    ranked = sorted(((k, v[0], v[1]) for k, v in out.items()), key=lambda r: -r[1])
    ranked.append((UNKNOWN, 0.05, "no subject at a place fits" if not ranked else "none of the above"))
    return ranked


def _graded(out: dict, perched: list[tuple[int, str, str]], places: dict[str, float]) -> list[tuple[str, float, str]]:
    for _ant, roi, pig in perched:
        f = places.get(roi)
        score, reason = (0.3, f"on {roi} (not placed in 3D)") if f is None else (
            0.05 + 0.9 * f, f"on {roi}; the sound fits it {f:.0%}")
        if pig not in out or out[pig][0] < score:
            out[pig] = (score, reason)
    occupied = {roi for _, roi, _ in perched}
    free = {r: f for r, f in places.items() if r not in occupied}
    top = max(free, key=free.get) if free else None
    if max(places.values()) < 0.05:
        out[UNKNOWN] = (0.5, "no place fits the sound: a subject elsewhere?")
    elif top is not None and free[top] > max((places.get(r, 0.0) for r in occupied), default=0.0):
        out[UNKNOWN] = (0.05 + 0.85 * free[top], f"the sound fits {top} best ({free[top]:.0%}), where no tag was read")
    else:
        out[UNKNOWN] = (0.05, "none of the above")
    return sorted(((k, v[0], v[1]) for k, v in out.items()), key=lambda r: -r[1])


def overlapping_playback(events: list[SoundEvent], t0: float, t1: float) -> list[SoundEvent]:
    return [e for e in events if e.emitter.startswith(SYSTEM) and e.start_s < t1 and e.stop_s > t0]
