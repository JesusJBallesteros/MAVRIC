"""Audio samples -> session timeline.

Two layers, as for the camera:

1. Host clock fit. Each recorded block was stamped with the host clock on
   delivery; the lower envelope of (sample, host time) gives the audio
   clock's offset and drift (a few ms accuracy).
2. Sync pulses. The RFID Arduino's sync output is recorded on an audio
   channel. Its edges are located to a fraction of a sample and matched to
   the SYNC lines (Arduino clock). The audio map is then the piecewise-linear
   interpolation through the matched pulses: exact at every pulse whatever
   the drift of either clock, and within microseconds in between.

Blocks lost to an input overflow break the sample count, so the recording is
split there into segments that are mapped independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..audio.edges import EDGE_COLUMNS, pulse_onsets
from ..rfid.protocol import KIND_SYNC
from ..session import Session
from .sync import fit_lower_envelope, match_pulses, rfid_times


@dataclass
class AudioTimeMap:
    """sample -> session time, one piecewise-linear map per segment."""

    sample_rate: float
    segments: list[tuple[int, int, np.ndarray, np.ndarray]] = field(default_factory=list)  # (s0, s1, knots, t)

    def __call__(self, samples) -> np.ndarray:
        s = np.asarray(samples, float)
        out = np.full(s.shape, np.nan)
        for s0, s1, ks, kt in self.segments:
            m = (s >= s0) & (s <= s1)
            out[m] = np.interp(s[m], ks, kt)
        return out

    def sample_at(self, t) -> np.ndarray:
        """Inverse map: session time -> (fractional) sample; NaN outside the recording."""
        t = np.asarray(t, float)
        out = np.full(t.shape, np.nan)
        for _s0, _s1, ks, kt in self.segments:
            m = (t >= kt[0]) & (t <= kt[-1])
            out[m] = np.interp(t[m], kt, ks)
        return out

    def knots(self) -> pd.DataFrame:
        rows = [{"segment": i, "sample": float(k), "t": float(v)}
                for i, (_a, _b, ks, kt) in enumerate(self.segments) for k, v in zip(ks, kt)]
        return pd.DataFrame(rows, columns=["segment", "sample", "t"])


def load_audio_clock(sess: Session) -> pd.DataFrame:
    a = sess.audio
    df = pd.read_csv(sess.file(a.get("clock_file", "audio_clock.csv")), dtype={"flags": str}, keep_default_na=False,
                     na_values={"adc_t": [""]})
    return df


def load_audio_edges(sess: Session, redetect: bool = False) -> pd.DataFrame:
    """Sync edges logged during recording; detected from the audio files if missing."""
    a = sess.audio
    p = sess.file(a.get("edges_file", "audio_edges.csv"))
    if p.exists() and not redetect:
        return pd.read_csv(p)
    cache = sess.analysis_dir / "audio_edges_offline.csv"
    if cache.exists() and not redetect:
        return pd.read_csv(cache)
    ch = a["channels"].index(a["sync_channel"]) if a.get("sync_channel") in a.get("channels", []) else None
    if ch is None:
        return pd.DataFrame(columns=EDGE_COLUMNS)
    e = edges_from_files(sess, ch)
    e.to_csv(cache, index=False)
    return e


def edges_from_files(sess: Session, channel: int, chunk_s: float = 20.0) -> pd.DataFrame:
    import soundfile as sf

    from ..audio.edges import EdgeDetector

    a = sess.audio
    sr = float(a["sample_rate"])
    det = EdgeDetector(sr)
    rows = []
    for f in a["files"]:
        k = int(f["first_sample"])
        for blk in sf.blocks(str(sess.file(f["file"])), blocksize=int(chunk_s * sr), dtype="float32", always_2d=True):
            rows += det.process(blk[:, channel], k)
            k += len(blk)
    return pd.DataFrame(rows, columns=EDGE_COLUMNS)


def _segments(clock: pd.DataFrame, n_samples: int) -> list[tuple[int, int]]:
    """Sample ranges between input overflows (data lost before the flagged block)."""
    cuts = sorted({int(r.sample - r.n) for r in clock.itertuples() if "in_over" in str(r.flags)} - {0})
    bounds = [0] + [c for c in cuts if 0 < c < n_samples] + [n_samples]
    return [(a, b) for a, b in zip(bounds[:-1], bounds[1:]) if b > a]


def synchronise_audio(sess: Session, raw: pd.DataFrame, rfid_fits: dict, offset_s: float = 0.0) -> tuple:
    """Returns (AudioTimeMap, report). `offset_s` is the video offset already applied to RFID times."""
    a = sess.audio
    sr = float(a["sample_rate"])
    n_samples = int(sum(f["n_samples"] for f in a["files"]))
    clock = load_audio_clock(sess)
    clock = clock[clock["sample"] > 0]
    flags: dict[str, int] = {}
    for fl in clock["flags"]:
        for f in filter(None, str(fl).split("|")):
            flags[f] = flags.get(f, 0) + 1
    report: dict = {"sample_rate": sr, "channels": a["channels"], "n_samples": n_samples,
                    "duration_s": n_samples / sr, "files": len(a["files"]), "flags": flags}

    sync_rows = raw[raw["kind"] == KIND_SYNC]
    sync_t = rfid_times(sync_rows, rfid_fits, offset_s)
    edges = load_audio_edges(sess) if a.get("sync_channel") else pd.DataFrame(columns=EDGE_COLUMNS)
    onsets, polarity = pulse_onsets(edges[EDGE_COLUMNS].to_numpy(float), sr, float(a.get("pulse_s", 0.08)))
    report["pulses"] = {"n_edges": int(len(edges)), "n_onsets": int(len(onsets)), "polarity": polarity,
                        "n_sync": int(len(sync_t))}

    segs = _segments(clock, n_samples)
    report["segments"] = len(segs)
    tmap = AudioTimeMap(sr)
    seg_reports = []
    matched_total, resid_all = 0, []
    for s0, s1 in segs:
        sr_ = {"first_sample": s0, "last_sample": s1}
        c = clock[(clock["sample"] > s0) & (clock["sample"] <= s1)]
        host_fit = None
        if len(c) >= 3:
            host_fit = fit_lower_envelope(c["sample"].to_numpy(float) / sr, c["host_t"].to_numpy(float))
            sr_["host_fit"] = host_fit.as_dict()
        on = onsets[(onsets >= s0) & (onsets < s1)]
        knots_s, knots_t = None, None
        if host_fit is not None and len(on) and len(sync_t):
            m = match_pulses(host_fit(on / sr) + offset_s, np.asarray(sync_t), fps=50.0, max_offset_s=1.0)
            pairs = m["pairs"]
            sr_["pulses"] = {k: m[k] for k in ("n_matched", "offset_s", "resid_med_abs_s", "resid_p95_abs_s")}
            if len(pairs) >= 2:
                coarse = host_fit(on / sr) + offset_s
                ks = np.array([on[int(np.argmin(np.abs(coarse - p[0])))] for p in pairs])
                kt = np.array([p[1] for p in pairs])
                order = np.argsort(ks)
                ks, kt = ks[order], kt[order]
                keep = np.r_[True, np.diff(ks) > 0]
                ks, kt = ks[keep], kt[keep]
                b, a0 = np.polyfit(ks / sr, kt, 1)
                r = kt - (a0 + b * ks / sr)
                ok = np.abs(r - np.median(r)) < max(5 * np.median(np.abs(r - np.median(r))), 2e-4)
                if ok.sum() >= 2 and not ok.all():
                    ks, kt = ks[ok], kt[ok]
                    b, a0 = np.polyfit(ks / sr, kt, 1)
                    r = kt - (a0 + b * ks / sr)
                sr_["pulse_fit"] = {"n": int(len(ks)), "drift_ppm": float((1 / b - 1) * 1e6),
                                    "lin_resid_med_ms": float(np.median(np.abs(r)) * 1e3),
                                    "lin_resid_p95_ms": float(np.percentile(np.abs(r), 95) * 1e3)}
                matched_total += len(ks)
                resid_all += list(np.abs(r))
                # knots: matched pulses, extended linearly to the segment ends
                ends = np.array([s0, s1], float)
                knots_s = np.r_[ends[0], ks, ends[1]]
                knots_t = np.r_[a0 + b * ends[0] / sr, kt, a0 + b * ends[1] / sr]
                if knots_s[1] == knots_s[0]:
                    knots_s, knots_t = knots_s[1:], knots_t[1:]
                if knots_s[-1] == knots_s[-2]:
                    knots_s, knots_t = knots_s[:-1], knots_t[:-1]
        if knots_s is None and host_fit is not None:
            ends = np.array([s0, s1], float)
            knots_s, knots_t = ends, host_fit(ends / sr) + offset_s
            sr_["method"] = "host_clock"
        elif knots_s is not None:
            sr_["method"] = "sync_pulses"
        if knots_s is not None:
            tmap.segments.append((s0, s1, knots_s, knots_t))
        seg_reports.append(sr_)
    report["segment_reports"] = seg_reports
    methods = {s.get("method") for s in seg_reports}
    report["method"] = "sync_pulses" if methods == {"sync_pulses"} else (
        "mixed" if "sync_pulses" in methods else ("host_clock" if "host_clock" in methods else "none"))
    p = report["pulses"]
    p["n_matched"] = matched_total
    if resid_all:
        p["lin_resid_med_ms"] = float(np.median(resid_all) * 1e3)
        p["lin_resid_p95_ms"] = float(np.percentile(resid_all, 95) * 1e3)
        fits = [s["pulse_fit"]["drift_ppm"] for s in seg_reports if "pulse_fit" in s]
        p["drift_ppm"] = float(np.median(fits))
    if seg_reports and "host_fit" in seg_reports[0]:
        report["host_drift_ppm"] = seg_reports[0]["host_fit"]["drift_ppm"]
        report["host_latency_med_ms"] = seg_reports[0]["host_fit"]["latency_med_s"] * 1e3
    validated = matched_total >= 5
    report["validated"] = bool(validated)
    if validated:
        # The map interpolates through the pulses, so it is exact there. The residual from a straight line
        # measures how unevenly the Arduino and audio clocks run; seconds would mean pulses paired wrongly.
        report["pass"] = bool(p["n_matched"] >= 0.8 * min(len(onsets), max(p["n_sync"], 1))
                              and p["lin_resid_p95_ms"] < 5.0)
    return tmap, report
