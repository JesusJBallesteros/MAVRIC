"""Speaker-to-microphone distances by sound, to check and refine where the devices are.

Each speaker plays a short sweep in turn, a few times. Each microphone's
recording is matched against the sweep; the first strong peak is the direct
sound (later ones are reflections). Its time after the driver's reported
output-to-input delay is the air path plus one constant the driver does not
report (converter latency), the same for every pair:

    t(speaker, mic) = |speaker - mic| / c + L

A least-squares fit then moves the devices chosen as not well known (and L) to
agree with the sounds, starting from their typed positions, each held near it
by its own uncertainty. Four speakers at one height leave a microphone's height
poorly fixed (mirror images above and below them); the typed height keeps it on
the right side.

How strongly each sweep arrives, after the distance and the microphone's
pickup, gives each microphone's recording gain and each speaker's output
(`levels`): the place suggestions in review compare levels between the two
microphones, so their gains must be known.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field, replace

import numpy as np

from .geometry import Device, cardioid_db

BANDS = {"1-16 kHz": (1000.0, 16000.0), "12-20 kHz (above pigeon hearing)": (12000.0, 20000.0)}
MIN_SNR_DB = 15.0
PASS_RMS_MM = 30.0
PASS_MAX_MM = 60.0


def sweep(sr: float, f0: float = 1000.0, f1: float = 16000.0, dur_s: float = 0.05, level_dbfs: float = -20.0,
          fade_s: float = 0.003) -> np.ndarray:
    """A linear sweep from f0 to f1 Hz: its matched filter gives a sharp peak (about 1/(f1 - f0) wide)."""
    t = np.arange(int(dur_s * sr)) / sr
    y = np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t ** 2 / (2 * dur_s))) * 10 ** (level_dbfs / 20)
    nf = int(fade_s * sr)
    ramp = np.sin(np.linspace(0, np.pi / 2, nf)) ** 2
    y[:nf] *= ramp
    y[-nf:] *= ramp[::-1]
    return y.astype(np.float32)


def _envelope_of_match(seg: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """|analytic signal| of the cross-correlation of seg with ref, at lags 0 .. len(seg) - 1."""
    n = 1 << int(np.ceil(np.log2(len(seg) + len(ref))))
    X = np.fft.fft(seg, n) * np.conj(np.fft.fft(ref, n))
    X[n // 2 + 1:] = 0  # analytic: no negative frequencies
    X[1:n // 2] *= 2
    return np.abs(np.fft.ifft(X))[: len(seg)]


def arrival(x: np.ndarray, start: int, ref: np.ndarray, sr: float, max_s: float = 0.08) -> dict | None:
    """When the sweep `ref`, sent at sample `start`, arrives in channel `x` (the direct sound): delay in samples
    (sub-sample), SNR of the match peak, and gain (dB) relative to the sweep sent."""
    a, b = start, min(len(x), start + int(max_s * sr) + len(ref))
    if a < 0 or b - a < len(ref) + 8:
        return None
    env = _envelope_of_match(x[a:b].astype(np.float64), ref.astype(np.float64))
    env = env[: b - a - len(ref)]
    pk = float(env.max())
    floor = float(np.median(env)) + 1e-12
    i = int(np.argmax(env >= 0.5 * pk))  # the first strong peak: the direct path
    while i + 1 < len(env) and env[i + 1] > env[i]:
        i += 1
    frac = 0.0
    if 0 < i < len(env) - 1:
        y0, y1, y2 = env[i - 1], env[i], env[i + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den else 0.0
    return {"delay": i + frac, "snr_db": 20 * np.log10(pk / floor),
            "gain_db": 20 * np.log10(env[i] / float(ref.astype(np.float64) @ ref.astype(np.float64)) + 1e-12)}


@dataclass
class Ranging:
    """Delays from every speaker to every microphone (median of the repeats), in ms after the driver's round trip."""

    sample_rate: float
    round_trip_ms: float
    pairs: dict[tuple[str, str], dict] = field(default_factory=dict)  # (speaker, mic) -> delay_ms, spread_ms, snr_db...
    band: tuple[float, float] = (1000.0, 16000.0)
    date: str = ""

    def ok_pairs(self) -> list[tuple[str, str]]:
        return [k for k, v in self.pairs.items() if v.get("ok")]

    def as_dict(self) -> dict:
        return {"date": self.date, "band_hz": list(self.band), "round_trip_ms": round(self.round_trip_ms, 3),
                "pairs": {f"{s}->{m}": {k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in d.items()}
                          for (s, m), d in self.pairs.items()}}


def analyse(x: np.ndarray, starts: dict[str, list[int]], mics: dict[str, int], ref: np.ndarray, sr: float,
            round_trip: int) -> dict[tuple[str, str], dict]:
    """Delays (ms, minus the round trip) of every speaker's sweeps (`starts`: speaker -> start samples in x) in every
    microphone (`mics`: name -> column of x)."""
    out = {}
    for spk, ss in starts.items():
        for mic, j in mics.items():
            hits = [h for h in (arrival(x[:, j], s, ref, sr) for s in ss) if h is not None]
            good = [h for h in hits if h["snr_db"] >= MIN_SNR_DB]
            if not good:
                out[(spk, mic)] = {"ok": False, "n": 0, "snr_db": max((h["snr_db"] for h in hits), default=0.0)}
                continue
            d = np.array([h["delay"] for h in good]) - round_trip
            out[(spk, mic)] = {"ok": True, "n": len(good), "delay_ms": float(np.median(d) / sr * 1e3),
                               "spread_ms": float((d.max() - d.min()) / sr * 1e3),
                               "snr_db": float(np.median([h["snr_db"] for h in good])),
                               "gain_db": float(np.median([h["gain_db"] for h in good]))}
    return out


def measure(stream, band: tuple[float, float] = (1000.0, 16000.0), level_dbfs: float = -20.0, repeats: int = 3,
            gap_s: float = 0.35, progress=None) -> Ranging:
    """Play the sweep on every speaker in turn on a running AudioStream and measure it in every microphone.

    Not while recording: the recording owns the stream's input."""
    from .stream import MemorySink

    cfg = stream.cfg
    if getattr(stream, "_sink", None) is not None:
        raise RuntimeError("the audio is being recorded: stop the recording first")
    if not cfg.speakers:
        raise RuntimeError("no speakers in rig.yaml (audio: speakers:)")
    sr = cfg.sample_rate
    ref = sweep(sr, band[0], band[1], level_dbfs=level_dbfs)
    sink = MemorySink()
    stream.attach(sink)
    tag = f"ranging-{time.monotonic():.3f}"
    plan = [name for _ in range(repeats) for _, name in sorted(cfg.speakers.items())]
    try:
        time.sleep(0.3)
        for i, name in enumerate(plan):
            stream.play(ref, name, name=tag)
            time.sleep(gap_s)
            if progress:
                progress((i + 1) / len(plan))
        time.sleep(0.5)
    finally:
        stream.detach()
    x = sink.data()
    k0 = sink.k_first or 0
    starts: dict[str, list[int]] = {}
    for e in list(stream.played):
        if e.get("kind") == "start" and e.get("name") == tag:
            starts.setdefault(e["speaker"], []).append(int(e["sample"]) - k0)
    mics = {cfg.names[j]: j for j in cfg.mic_indices}
    rt = int(stream.play_offset)
    return Ranging(sr, rt / sr * 1e3, analyse(x, starts, mics, ref, sr, rt), tuple(band),
                   dt.datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- fit


@dataclass
class Fit:
    devices: dict[str, Device]  # every device; the free ones moved
    free: list[str]
    latency_ms: float  # the constant the driver does not report
    residual_mm: dict[tuple[str, str], float]  # measured minus predicted distance, per pair
    moved_mm: dict[str, float]
    c: float
    sd_xyz: dict[str, np.ndarray] = field(default_factory=dict)  # uncertainty of each free device per axis (mm)

    @property
    def rms_mm(self) -> float:
        r = np.array(list(self.residual_mm.values()))
        return float(np.sqrt(np.mean(r ** 2))) if len(r) else float("nan")

    def verdict(self) -> tuple[bool, list[str]]:
        why = []
        if len(self.residual_mm) < 4:
            why.append(f"only {len(self.residual_mm)} speaker-microphone pairs heard (need 4 or more)")
        if self.residual_mm:
            if self.rms_mm > PASS_RMS_MM:
                why.append(f"distances disagree by {self.rms_mm:.0f} mm RMS (at most {PASS_RMS_MM:.0f})")
            worst = max(self.residual_mm, key=lambda k: abs(self.residual_mm[k]))
            if abs(self.residual_mm[worst]) > PASS_MAX_MM:
                why.append(f"{worst[0]} → {worst[1]} is off by {self.residual_mm[worst]:+.0f} mm: check that "
                           f"device's position (or fit it)")
        return not why, why

    def summary(self) -> str:
        ok, _ = self.verdict()
        lines = [f"{'PASS' if ok else 'FAIL'}: {len(self.residual_mm)} pairs, distances agree within "
                 f"{self.rms_mm:.0f} mm RMS; unreported latency {self.latency_ms:.2f} ms"]
        for n in self.free:
            p = self.devices[n].position
            sd = self.sd_xyz.get(n)
            lines.append(f"{n} moved {self.moved_mm[n]:.0f} mm to x {p[0]:.0f}, y {p[1]:.0f}, z {p[2]:.0f}"
                         + (f" (± {sd[0]:.0f}, {sd[1]:.0f}, {sd[2]:.0f} mm)" if sd is not None else "")
                         + ("; its height comes mostly from the typed one" if sd is not None and sd[2] > 100 else ""))
        return "\n".join(lines)


def fit(r: Ranging, devices: dict[str, Device], free: list[str] | tuple = (), c: float = 343.0,
        prior_mm: float = 250.0, sigma_ms: float = 0.05, latency_prior: tuple[float, float] = (0.0, 3.0),
        iterations: int = 40) -> Fit:
    """Move the `free` devices (and the unreported latency) to agree with the measured delays; the others stay.

    Each free device is held near its typed position with `prior_mm` per axis; a delay counts with `sigma_ms`.
    With no free device it only finds the latency: the residuals then check the typed layout."""
    pairs = [k for k in r.ok_pairs() if k[0] in devices and k[1] in devices]
    free = [n for n in free if n in devices]
    t = np.array([r.pairs[k]["delay_ms"] for k in pairs])
    p0 = {n: devices[n].position.astype(float).copy() for n in devices}
    x = np.concatenate([[latency_prior[0]], *[p0[n] for n in free]]) if free else np.array([latency_prior[0]])

    def positions(v):
        pos = dict(p0)
        for i, n in enumerate(free):
            pos[n] = v[1 + 3 * i: 4 + 3 * i]
        return pos

    def resid(v):
        pos = positions(v)
        d = np.array([np.linalg.norm(pos[s] - pos[m]) for s, m in pairs])
        out = [(t - (d / c + v[0])) / sigma_ms, [(v[0] - latency_prior[0]) / latency_prior[1]]]
        out += [(pos[n] - p0[n]) / prior_mm for n in free]
        return np.concatenate(out)

    lam = 1e-3
    for _ in range(iterations):
        f = resid(x)
        J = np.empty((len(f), len(x)))
        for j in range(len(x)):
            h = 1e-3 if j == 0 else 0.1
            dx = np.zeros_like(x)
            dx[j] = h
            J[:, j] = (resid(x + dx) - f) / h
        A = J.T @ J
        g = J.T @ f
        step = np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-9), -g)
        if (resid(x + step) ** 2).sum() < (f ** 2).sum():
            x = x + step
            lam = max(lam / 3, 1e-7)
            if np.abs(step).max() < 1e-4:
                break
        else:
            lam *= 5
    try:
        cov = np.linalg.inv(J.T @ J)
    except np.linalg.LinAlgError:
        cov = np.full((len(x), len(x)), np.nan)
    pos = positions(x)
    out, sds = {}, {}
    for n, d in devices.items():
        if n in free:
            i = free.index(n)
            sds[n] = np.sqrt(np.clip(np.diag(cov)[1 + 3 * i: 4 + 3 * i], 0, None))
            out[n] = replace(d, position=pos[n].copy(), how="sound", sd_mm=float(sds[n].max()))
        else:
            out[n] = d
    res = {k: float((r.pairs[k]["delay_ms"] - x[0]) * c - np.linalg.norm(pos[k[0]] - pos[k[1]])) for k in pairs}
    moved = {n: float(np.linalg.norm(pos[n] - p0[n])) for n in free}
    return Fit(out, free, float(x[0]), res, moved, c, sds)


# --------------------------------------------------------------------------- levels


@dataclass
class Levels:
    """Each microphone's recording gain relative to the first microphone and each speaker's output relative to the
    first speaker (dB), from how strongly the sweeps arrived after the distance and the microphone's cardioid
    pickup. The speakers' own directivity is not modelled: it stays in the residuals."""

    mic_db: dict[str, float]
    speaker_db: dict[str, float]
    resid_db: dict[tuple[str, str], float]

    @property
    def rms_db(self) -> float:
        r = np.array(list(self.resid_db.values()))
        return float(np.sqrt(np.mean(r ** 2))) if len(r) else float("nan")

    def summary(self) -> str:
        mics = ", ".join(f"{n} {round(v, 1) + 0.0:+.1f}" for n, v in self.mic_db.items())  # no "-0.0"
        spk = ", ".join(f"{n} {round(v, 1) + 0.0:+.1f}" for n, v in self.speaker_db.items())
        return (f"recording gain {mics} dB; speaker output {spk} dB (the sweeps agree with these within "
                f"{self.rms_db:.1f} dB RMS)")

    def apply(self, devices: dict[str, Device]) -> dict[str, Device]:
        """The devices with these levels (others unchanged)."""
        lv = {**self.mic_db, **self.speaker_db}
        return {n: replace(d, level_db=lv[n]) if n in lv else d for n, d in devices.items()}


def levels(r: Ranging, devices: dict[str, Device]) -> Levels | None:
    """Fit every pair's sweep gain as speaker output + microphone gain - distance loss + cardioid pickup."""
    pairs = [k for k in r.ok_pairs() if k[0] in devices and k[1] in devices and "gain_db" in r.pairs[k]]
    order = list(devices)
    spk = sorted({s for s, _ in pairs}, key=order.index)
    mic = sorted({m for _, m in pairs}, key=order.index)
    if not pairs or len(pairs) < len(spk) + len(mic):
        return None
    A = np.zeros((len(pairs), len(spk) + len(mic) - 1))  # columns: every speaker, then every microphone but the first
    y = np.empty(len(pairs))
    for i, (s, m) in enumerate(pairs):
        S, M = devices[s], devices[m]
        d = float(np.linalg.norm(S.position - M.position))
        y[i] = r.pairs[(s, m)]["gain_db"] + 20 * np.log10(max(d, 1.0) / 1000.0) - cardioid_db(M, S.position)
        A[i, spk.index(s)] = 1.0
        if mic.index(m):
            A[i, len(spk) + mic.index(m) - 1] = 1.0
    x, *_ = np.linalg.lstsq(A, y, rcond=None)
    res = y - A @ x
    return Levels({m: (0.0 if k == 0 else float(x[len(spk) + k - 1])) for k, m in enumerate(mic)},
                  {s: float(x[k] - x[0]) for k, s in enumerate(spk)},
                  {p: float(v) for p, v in zip(pairs, res)})
