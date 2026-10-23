"""Place RFID-triggered clips on the session timeline and link them to their RFID bout."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..session import Session
from .sync import ClockFit, camera_tick_hz, fit_lower_envelope

CLIP_COLUMNS = [
    "clip", "camera", "rule", "pigeon", "antenna", "trigger_t", "first_frame_t", "last_frame_t",
    "n_frames", "pre_frames", "end_reason", "bout_start_s", "bout_stop_s", "covers_bout",
]


def load_clip_frames(sess: Session, clip: dict) -> pd.DataFrame | None:
    p = sess.file(clip["frames_file"])
    if not p.exists():
        return None
    df = pd.read_csv(p)
    for c in ("hw_ticks", "hw_counter", "line_status", "led_mean"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def clip_frame_times(sess: Session, cam_fits: dict[str, ClockFit] | None = None) -> dict[int, np.ndarray]:
    """Frame times of every clip on the session timeline.

    The camera clock fit comes from the continuous recording of that camera
    when there is one (cam_fits); for triggered-only cameras it is fitted on
    all of its clip frames together (the camera free-runs between clips, so
    its hardware clock is continuous). Without hardware timestamps the host
    stamps are used.
    """
    cam_fits = dict(cam_fits or {})
    frames = {i: load_clip_frames(sess, c) for i, c in enumerate(sess.clips)}
    tick_hz = {c["id"]: camera_tick_hz(c) for c in sess.meta.get("cameras", [])}
    by_cam: dict[str, list[pd.DataFrame]] = {}
    for i, c in enumerate(sess.clips):
        f = frames[i]
        if f is not None and len(f) and c["camera"] not in cam_fits:
            by_cam.setdefault(c["camera"], []).append(f)
    for cam, parts in by_cam.items():
        allf = pd.concat(parts, ignore_index=True)
        if allf["hw_ticks"].notna().sum() >= 3:
            cam_fits[cam] = fit_lower_envelope(allf["hw_ticks"].to_numpy(float) / tick_hz.get(cam, 1e9),
                                               allf["host_t"].to_numpy(float))
    out = {}
    for i, c in enumerate(sess.clips):
        f = frames[i]
        if f is None or not len(f):
            continue
        fit = cam_fits.get(c["camera"])
        if fit is not None and f["hw_ticks"].notna().all():
            out[i] = fit(f["hw_ticks"].to_numpy(float) / tick_hz.get(c["camera"], 1e9))
        else:
            out[i] = f["host_t"].to_numpy(float)
    return out


def clip_table(sess: Session, bouts: pd.DataFrame, cam_fits: dict[str, ClockFit] | None = None,
               link_tol_s: float = 1.0) -> pd.DataFrame:
    """One row per clip: where it sits on the timeline and which RFID bout triggered it."""
    times = clip_frame_times(sess, cam_fits)
    rows = []
    for i, c in enumerate(sess.clips):
        t = times.get(i)
        b0 = b1 = np.nan
        if len(bouts):
            m = bouts[(bouts["antenna"] == c.get("antenna")) & (bouts["tag"] == c.get("tag"))]
            m = m[(m["start_s"] - c["trigger_t"]).abs() <= link_tol_s]
            if len(m):
                b = m.iloc[(m["start_s"] - c["trigger_t"]).abs().argmin()]
                b0, b1 = float(b["start_s"]), float(b["stop_s"])
        first = float(t[0]) if t is not None else np.nan
        last = float(t[-1]) if t is not None else np.nan
        rows.append({
            "clip": c["file"], "camera": c["camera"], "rule": c["rule"], "pigeon": c.get("pigeon"),
            "antenna": c.get("antenna"), "trigger_t": c["trigger_t"], "first_frame_t": first, "last_frame_t": last,
            "n_frames": 0 if t is None else len(t), "pre_frames": c.get("pre_frames"),
            "end_reason": c.get("end_reason"), "bout_start_s": b0, "bout_stop_s": b1,
            "covers_bout": bool(np.isfinite(b0) and first <= b0 + 0.1 and last >= b1 - 0.1),
        })
    return pd.DataFrame(rows, columns=CLIP_COLUMNS)
