"""The sync check in plain words: how well the clocks, the frame trigger and the sync LED agree, per camera.

Used by `mavric analyze` (printed, and written to analysis/sync_summary.txt). The figure with the same content is
plots.sync_diagnostics.
"""

from __future__ import annotations

from .io import load_frames
from .sync import synchronise


def camera_syncs(P) -> dict[str, dict]:
    """synchronise() for every continuously recorded camera (the prepared camera's own result is reused)."""
    sess = P.session
    if not sess.cameras:
        return {}
    own = sess.camera(P.cam_id)["id"]
    return {c["id"]: P.sync if c["id"] == own else synchronise(P.raw, load_frames(sess, c["id"]), c)
            for c in sess.cameras}


def _dur(s: float) -> str:
    """A short time: µs below 1 ms, else ms."""
    s = abs(s)
    if s < 1e-3:
        return f"{s * 1e6:.1f} µs" if s < 1e-5 else f"{s * 1e6:.0f} µs"
    return f"{s * 1e3:.1f} ms"


def _pace(cam: dict) -> str:
    n, d, rd = cam["n_frames"], cam.get("camera_dropped") or 0, cam.get("recorder_dropped") or 0
    fps = cam.get("measured_fps")
    share = f" ({d / (n + d):.2%})" if n + d else ""
    return (f"pace: {n} frames" + (f" at {fps:.2f} per second" if fps else "")
            + f"; the camera dropped {d}{share}, the recorder {rd}")


def _led(led: dict) -> list[str]:
    if not led or led.get("method") == "none":
        return ["sync LED: not measured in this camera (no SYNC box over the LED, or the box never changed)"]
    if not led.get("validated"):
        return [f"sync LED: not checked: {led['n_led']} flashes found in the picture, {led['n_sync']} sent, "
                f"fewer than 5 matched (is the SYNC box over the LED?)"]
    how = "the camera's line status" if led["method"] == "camera_line" else "the SYNC box brightness"
    T = led["frame_s"]
    n_view = led.get("n_sync_in_view", led["n_sync"])
    extra = led.get("n_extra", 0)
    out = [f"sync LED (from {how}): {led['n_matched']} of {n_view} flashes sent while it recorded were seen"
           + (f", {extra} extra flash(es) not sent" if extra else ", none extra"),
           f"  one flash: seen within {_dur(led['resid_p95_abs_s'])} of when it was sent (95%; median "
           f"{_dur(led['resid_med_abs_s'])})",
           f"    a frame lasts {_dur(T)}: a flash can start up to {_dur(T / 2)} before or after the frame that "
           "shows it (the frame rate's limit)"]
    off = led["offset_s"]
    se = led.get("offset_se_s")
    applied = ("applied to the RFID times" if led.get("offset_applied", True) else
               "not applied: the frames have the pulses' times")
    out.append(f"  all flashes: video and RFID times agree to {off * 1e3:+.1f}"
               + (f" ± {se * 1e3:.1f} ms" if se is not None else " ms") + f" ({applied})")
    tr, tr_se = led.get("trend_ms_per_h"), led.get("trend_se_ms_per_h")
    if tr is not None:
        moving = abs(tr) > 2 * tr_se and abs(tr) > 1.0
        out.append(f"    over the session: {f'it changes by {tr:+.1f} ms per hour' if moving else 'steady'}")
    out.append(f"  {'PASS' if led.get('pass') else 'FAIL'} (needed: half the flashes within one frame, 95% within "
               "0.1 s)")
    return out


def sync_lines(report: dict, cams: dict[str, dict], trigger: dict | None = None) -> list[str]:
    """The sync check as short sentences: the RFID board's clock, then each camera's pace, trigger and LED."""
    lines = []
    for seg, f in (report.get("rfid_segments") or {}).items():
        ppm = f["drift_ppm"]
        lines.append(f"RFID board clock" + (f" (segment {seg})" if len(report["rfid_segments"]) > 1 else "")
                     + f": {abs(ppm):.0f} ppm {'fast' if ppm > 0 else 'slow'} against the PC "
                     f"({abs(ppm) * 3.6:.0f} ms per hour), corrected; serial delay median "
                     f"{f['latency_med_s'] * 1e3:.1f} ms, 95% under {f['latency_p95_s'] * 1e3:.1f} ms")
    for cid, sy in cams.items():
        rep = sy["report"]
        cam = rep.get("camera")
        if not cam:
            continue
        tr = cam.get("trigger")
        sub = []
        if tr and tr.get("locked"):
            lines.append(f"{cid}: on the frame trigger, {tr['fps']:.4f} pulses per second")
            sub.append(f"pace: {tr['pulses']} pulses while it recorded; a frame on {tr['assigned']} "
                       f"({tr['assigned'] / max(tr['pulses'], 1):.2%}), {tr['missed_pulses']} without one")
            sub.append(f"trigger: each frame's own timestamp is within {_dur(tr['spread_ms'] / 1e3)} (SD) of its "
                       "pulse, so it was taken on it and takes its time")
        else:
            lines.append(f"{cid}: free-running, set to {cam['fps_set']:g} fps")
            sub.append(_pace(cam))
            if tr:
                sub.append(f"frame trigger: set but NOT locked ({tr.get('why', '')}): its own clock is used")
        sub += _led(rep.get("led") or {})
        lines += ["  " + s for s in sub]
    if trigger and len(trigger.get("cameras") or {}) > 1:
        lines.append(f"3D: {trigger['complete_sets']} of {trigger['pulses']} pulses have a frame from every "
                     "frame-triggered camera (frame_sets.csv)")
    return lines
