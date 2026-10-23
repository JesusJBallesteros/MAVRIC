"""Bench check of one camera: frame rate, drops, hardware clock, sync-LED compatibility."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..core import now
from .backends import make_backend

DEFAULT_LED_PULSE_S = 0.080  # firmware SYNC_PULSE_US


def led_pulse_warning(fps: float, pulse_s: float = DEFAULT_LED_PULSE_S) -> str | None:
    """The LED must stay on for >= 2 frame intervals so at least one full exposure sees it."""
    if not fps or fps <= 0:
        return None
    need = 2.0 / fps
    if pulse_s + 1e-9 < need:
        return (f"sync LED pulse {pulse_s * 1e3:.0f} ms covers < 2 frames at {fps:.1f} fps: set "
                f"SYNC_PULSE_US >= {int(np.ceil(need * 1e6 / 1000) * 1000)} in the firmware and "
                f"sync.led_pulse_s in rig.yaml")
    return None


def camera_check(cfg: dict, seconds: float = 5.0, world=None, save_png: str | Path | None = None,
                 led_pulse_s: float = DEFAULT_LED_PULSE_S) -> dict:
    """Open the camera, grab for `seconds`, report what the recording pipeline relies on."""
    from ..analysis.sync import fit_lower_envelope

    be = make_backend(cfg, world=world)
    be.open()
    host, ticks, counters, shapes = [], [], [], set()
    last = None
    try:
        info = be.info()
        t_end = now() + seconds
        while now() < t_end:
            fr = be.grab(1.0)
            if fr is None:
                continue
            host.append(fr.host_abs)
            ticks.append(fr.hw_ticks)
            counters.append(fr.hw_counter)
            shapes.add(fr.image.shape)
            last = fr.image
        failed = be.n_failed
    finally:
        be.close()

    rep: dict = {"info": info, "frames": len(host), "failed_frames": failed, "image_shapes": sorted(shapes),
                 "warnings": list(info.get("warnings") or [])}
    if len(host) >= 2:
        h = np.asarray(host)
        rep["measured_fps"] = float((len(h) - 1) / (h[-1] - h[0]))
        rep["host_interval_ms"] = {"median": float(np.median(np.diff(h)) * 1e3),
                                   "p99": float(np.percentile(np.diff(h), 99) * 1e3)}
    if all(c is not None for c in counters) and len(counters) >= 2:
        c = np.asarray(counters, dtype=np.int64)
        rep["camera_dropped"] = int(np.clip(np.diff(c) - 1, 0, None).sum())
    if all(t is not None for t in ticks) and len(ticks) >= 10:
        dev = np.asarray(ticks, float) / float(info.get("hw_tick_hz") or 1e9)
        fit = fit_lower_envelope(dev, np.asarray(host), window_s=max(0.5, seconds / 10))
        rep["hw_clock"] = {"drift_ppm": fit.drift_ppm, "latency_med_ms": fit.latency_med_s * 1e3,
                           "latency_p95_ms": fit.latency_p95_s * 1e3,
                           "hw_fps": float((len(dev) - 1) / (dev[-1] - dev[0])) if dev[-1] > dev[0] else None}
    else:
        rep["warnings"].append("no hardware timestamps: frame times will be host arrival times")
    w = led_pulse_warning(rep.get("measured_fps") or info.get("fps") or 0, led_pulse_s)
    if w:
        rep["warnings"].append(w)
    if save_png is not None and last is not None:
        Path(save_png).parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(save_png), last)
        rep["snapshot"] = str(save_png)
    return rep
