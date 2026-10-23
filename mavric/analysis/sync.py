"""Clock alignment: Arduino micros -> host, camera ticks -> host, LED/SYNC validation.

Model: a line or frame is stamped on the host *after* it happened,
host = a + b * device + latency, with latency >= 0. The line under the cloud
of points (lower envelope) therefore recovers offset `a` and drift `b`
without being biased by USB/driver jitter.

The sync LED gives an independent check: the firmware logs each LED rising
edge (SYNC line, Arduino clock) and the camera sees it (frame brightness or a
camera GPIO line). After both clocks are mapped to host time, the two event
trains must coincide up to a constant pipeline offset, which is then applied
so that RFID times sit on the video timeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..rfid.protocol import KIND_SYNC


@dataclass
class ClockFit:
    intercept: float
    slope: float
    n: int
    latency_med_s: float
    latency_p95_s: float
    method: str = "lower_envelope"

    @property
    def drift_ppm(self) -> float:
        """Device clock rate error vs host, ppm (positive = device clock runs fast)."""
        return (1.0 / self.slope - 1.0) * 1e6

    def __call__(self, device_s):
        return self.intercept + self.slope * np.asarray(device_s, dtype=float)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["drift_ppm"] = self.drift_ppm
        return d


def fit_lower_envelope(device_s, host_s, window_s: float = 10.0) -> ClockFit:
    dev = np.asarray(device_s, float)
    host = np.asarray(host_s, float)
    ok = np.isfinite(dev) & np.isfinite(host)
    dev, host = dev[ok], host[ok]
    if len(dev) < 3:
        raise ValueError("Not enough points for a clock fit")
    span = dev.max() - dev.min()
    if span < 2 * window_s:
        window_s = max(span / 5, 1e-3)
    d = host - dev
    bins = np.floor((dev - dev.min()) / window_s).astype(int)
    order = np.lexsort((d, bins))
    first = np.r_[True, bins[order][1:] != bins[order][:-1]]
    idx = order[first]  # min latency point per window
    x, y = dev[idx], host[idx]
    if len(x) < 2:
        slope, intercept = 1.0, float(np.min(d))
    else:
        slope, intercept = np.polyfit(x, y, 1)
        for _ in range(2):  # drop windows whose minimum is still an outlier (bursts)
            r = y - (intercept + slope * x)
            mad = np.median(np.abs(r - np.median(r))) + 1e-6
            keep = r < np.median(r) + 4 * mad
            if keep.sum() < 2 or keep.all():
                break
            x, y = x[keep], y[keep]
            slope, intercept = np.polyfit(x, y, 1)
        # shift so the envelope sits under (almost) all points
        intercept += float(np.percentile(host - (intercept + slope * dev), 0.5))
    lat = host - (intercept + slope * dev)
    return ClockFit(float(intercept), float(slope), int(len(dev)), float(np.median(lat)), float(np.percentile(lat, 95)))


# --------------------------------------------------------------------------- RFID


def fit_rfid_clock(raw: pd.DataFrame) -> dict[int, ClockFit]:
    """One fit per Arduino segment (a reset restarts micros)."""
    fits = {}
    rows = raw[raw["arduino_us"].notna()]
    for seg, g in rows.groupby("segment"):
        if len(g) >= 3:
            fits[int(seg)] = fit_lower_envelope(g["arduino_us"].to_numpy() / 1e6, g["host_t"].to_numpy())
    return fits


def rfid_times(raw: pd.DataFrame, fits: dict[int, ClockFit], offset_s: float = 0.0) -> np.ndarray:
    """Event time of every raw row on the (video) timeline; host stamp if no fit applies."""
    t = raw["host_t"].to_numpy(dtype=float).copy()
    us = raw["arduino_us"].to_numpy(dtype=float)
    seg = raw["segment"].to_numpy()
    for s, f in fits.items():
        m = (seg == s) & np.isfinite(us)
        t[m] = f(us[m] / 1e6)
    return t + offset_s


# --------------------------------------------------------------------------- camera


def camera_tick_hz(cam_info: dict | None) -> float:
    """Ticks per second of the per-frame hardware timestamps of a recorded camera.

    Spinnaker (backend "flir") always delivers nanoseconds; sessions recorded
    before this was handled stored the Chameleon3's 125 us register increment
    instead, so the backend overrides whatever was saved.
    """
    cam_info = cam_info or {}
    if cam_info.get("backend") == "flir":
        return 1e9
    return float(cam_info.get("hw_tick_hz") or 1e9)


def fit_camera_clock(frames: pd.DataFrame, tick_hz: float | None) -> ClockFit | None:
    if "hw_ticks" not in frames or frames["hw_ticks"].isna().all():
        return None
    tick_hz = tick_hz or 1e9
    return fit_lower_envelope(frames["hw_ticks"].to_numpy(float) / tick_hz, frames["host_t"].to_numpy(float))


def frame_times(frames: pd.DataFrame, fit: ClockFit | None, tick_hz: float | None) -> np.ndarray:
    if fit is None:
        return frames["host_t"].to_numpy(float)
    return fit(frames["hw_ticks"].to_numpy(float) / (tick_hz or 1e9))


def camera_drops(frames: pd.DataFrame) -> dict:
    out = {"recorder_dropped": 0, "camera_dropped": 0}
    if "grab_idx" in frames:
        g = frames["grab_idx"].to_numpy()
        out["recorder_dropped"] = int(np.sum(np.clip(np.diff(g) - 1, 0, None)))
    if "hw_counter" in frames and frames["hw_counter"].notna().all():
        c = frames["hw_counter"].to_numpy(dtype=np.int64)
        out["camera_dropped"] = int(np.sum(np.clip(np.diff(c) - 1, 0, None)))
    return out


# --------------------------------------------------------------------------- LED validation


def led_onsets(frames: pd.DataFrame, t_frames: np.ndarray, fps: float) -> tuple[np.ndarray, str]:
    """Rising edges of the sync LED as seen by the camera (onset midway between frames)."""
    state = None
    method = "none"
    if "line_status" in frames and frames["line_status"].notna().all() and frames["line_status"].nunique() > 1:
        ls = frames["line_status"].to_numpy(dtype=np.int64)
        base = pd.Series(ls).mode().iloc[0]
        state = ls != base
        method = "camera_line"
    elif "led_mean" in frames and frames["led_mean"].notna().sum() > 10:
        v = frames["led_mean"].to_numpy(float)
        lo, hi = np.nanpercentile(v, 5), np.nanpercentile(v, 99.5)
        if hi - lo > 20:
            state = v > (lo + hi) / 2
            method = "led_roi"
    if state is None:
        return np.array([]), method
    idx = np.flatnonzero(state[1:] & ~state[:-1]) + 1
    # the edge happened between the previous frame and this one: use the midpoint
    return t_frames[idx] - 0.5 / fps, method


def match_pulses(led_t: np.ndarray, sync_t: np.ndarray, fps: float, max_offset_s: float = 2.0) -> dict:
    """Align LED onsets with SYNC lines: find the common offset, then match one-to-one."""
    res = {"n_led": int(len(led_t)), "n_sync": int(len(sync_t)), "n_matched": 0, "offset_s": None,
           "resid_med_abs_s": None, "resid_p95_abs_s": None, "pairs": []}
    if len(led_t) == 0 or len(sync_t) == 0:
        return res
    tol = max(1.5 / fps, 0.02)
    d = (led_t[:, None] - sync_t[None, :]).ravel()
    d = d[np.abs(d) <= max_offset_s]
    if len(d) == 0:
        return res
    edges = np.arange(-max_offset_s, max_offset_s + tol, tol)
    h, _ = np.histogram(d, edges)
    k = int(np.argmax(h))
    near = d[(d >= edges[k] - tol) & (d < edges[k + 1] + tol)]
    off0 = float(np.median(near))
    pairs = []
    for i, lt in enumerate(led_t):
        j = int(np.argmin(np.abs(sync_t - (lt - off0))))
        if abs(lt - sync_t[j] - off0) <= tol:
            pairs.append((i, j))
    if not pairs:
        return res
    li = np.array([p[0] for p in pairs])
    sj = np.array([p[1] for p in pairs])
    diff = led_t[li] - sync_t[sj]
    off = float(np.median(diff))
    r = np.abs(diff - off)
    res.update(
        n_matched=int(len(pairs)),
        offset_s=off,
        resid_med_abs_s=float(np.median(r)),
        resid_p95_abs_s=float(np.percentile(r, 95)),
        pairs=[(float(led_t[a]), float(sync_t[b])) for a, b in pairs],
    )
    return res


def synchronise(raw: pd.DataFrame, frames: pd.DataFrame | None, cam_info: dict | None) -> dict:
    """Full sync: returns fits, per-row RFID times, per-frame times and a report."""
    rfid_fits = fit_rfid_clock(raw)
    out = {"rfid_fits": rfid_fits, "cam_fit": None, "offset_s": 0.0, "frame_t": None}
    report = {"rfid_segments": {str(k): v.as_dict() for k, v in rfid_fits.items()}}
    if frames is not None and len(frames):
        fps = float(cam_info.get("fps") or 30.0)
        tick_hz = camera_tick_hz(cam_info)
        cam_fit = fit_camera_clock(frames, tick_hz)
        t_frames = frame_times(frames, cam_fit, tick_hz)
        out["cam_fit"], out["frame_t"] = cam_fit, t_frames
        report["camera"] = {"fit": cam_fit.as_dict() if cam_fit else None, "n_frames": int(len(frames)),
                            **camera_drops(frames)}
        if len(t_frames) > 1:
            report["camera"]["measured_fps"] = float((len(t_frames) - 1) / (t_frames[-1] - t_frames[0]))
        onsets, method = led_onsets(frames, t_frames, fps)
        sync_rows = raw[raw["kind"] == KIND_SYNC]
        sync_t = rfid_times(sync_rows, rfid_fits)
        m = match_pulses(onsets, sync_t, fps)
        m["method"] = method
        frame_s = 1.0 / fps
        validated = m["n_matched"] >= 5
        m["validated"] = bool(validated)
        if validated:
            m["pass"] = bool(m["resid_med_abs_s"] < frame_s and m["resid_p95_abs_s"] < 0.1)
            out["offset_s"] = m["offset_s"]
        report["led"] = {k: v for k, v in m.items() if k != "pairs"}
        out["led_pairs"] = m["pairs"]
    out["rfid_t"] = rfid_times(raw, rfid_fits, out["offset_s"])
    report["applied_offset_s"] = out["offset_s"]
    out["report"] = report
    return out
