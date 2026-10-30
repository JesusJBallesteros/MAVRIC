"""Frames locked to the hardware frame trigger.

With `sync: frame_trigger:` in rig.yaml, the RFID Arduino sends one pulse per
frame to every frame-triggered camera (Timer5, pin D46) and logs `TRIG` lines:
pulse n at its own time in the Arduino's microseconds, about once a second
(`TRIGON` for pulse 0, with the period). The pulses come every P microseconds
exactly, a whole number of 4 us timer ticks from the same crystal as micros(),
so two TRIG lines of a run give P and the time of every pulse. The RFID clock
fit puts them on the session timeline.

Each frame is the pulse it was exposed on. Its time from the camera's own clock
is rounded to the nearest pulse, after the phase common to all frames (the two
clocks' different latencies) is taken out. The frame then takes that pulse's
time: exact, and the same for every camera, so pulse numbers pair the cameras'
frames (frame_sets.csv) for 3D.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..rfid.protocol import KIND_TRIG, KIND_TRIGOFF, KIND_TRIGON

TICK_US = 4.0  # the firmware's timer tick (16 MHz / 64)
LOCK_TOL = 0.25  # a frame further than this (in periods) from its pulse is not locked
LOCK_SHARE = 0.95  # frames that must be locked for the pulse times to replace the camera clock


@dataclass
class TriggerRun:
    """One stretch of pulses, from a start (F command) to a stop or a board reset."""

    segment: int  # Arduino clock segment (a reset starts a new one)
    us0: float  # Arduino microseconds of pulse 0 (unwrapped, in its segment)
    period_us: float
    first: int  # first and last pulse numbers logged
    last: int
    resid_us: float  # largest gap between a logged pulse and the regular train
    started: bool  # its start (TRIGON) is in the log
    stopped: bool  # its stop (TRIGOFF) is in the log

    @property
    def fps(self) -> float:
        return 1e6 / self.period_us

    def us(self, n) -> np.ndarray:
        return self.us0 + np.asarray(n, float) * self.period_us

    def as_dict(self) -> dict:
        return {**asdict(self), "fps": self.fps}


def trigger_runs(raw: pd.DataFrame) -> list[TriggerRun]:
    """The frame trigger's runs in an RFID log (rows in arrival order)."""
    rows = raw[raw["kind"].isin([KIND_TRIGON, KIND_TRIG, KIND_TRIGOFF]) & raw["arduino_us"].notna()]
    runs: list[TriggerRun] = []
    cur: dict | None = None

    def close():
        if cur is None:
            return
        pts = np.array(cur["pts"], float).reshape(-1, 2)
        period = cur["period"]
        if period is None:
            if len(pts) < 2 or np.ptp(pts[:, 0]) == 0:
                return  # one pulse: no period yet
            period = round(np.polyfit(pts[:, 0], pts[:, 1], 1)[0] / TICK_US) * TICK_US
        us0 = float(np.median(pts[:, 1] - pts[:, 0] * period))
        resid = pts[:, 1] - (us0 + pts[:, 0] * period)
        runs.append(TriggerRun(cur["seg"], us0, float(period), int(pts[:, 0].min()), int(pts[:, 0].max()),
                               float(np.abs(resid).max()), cur["started"], cur["stopped"]))

    for r in rows.itertuples():
        seg, n, us = int(r.segment), int(r.reader), float(r.arduino_us)
        if r.kind == KIND_TRIGON:
            close()
            cur = {"seg": seg, "pts": [(0, us)], "period": n / 1000.0, "started": True, "stopped": False}
        elif r.kind == KIND_TRIG:
            if cur is None or seg != cur["seg"] or (cur["pts"] and n <= cur["pts"][-1][0]):
                close()
                cur = {"seg": seg, "pts": [], "period": None, "started": False, "stopped": False}
            cur["pts"].append((n, us))
        elif cur is not None:  # TRIGOFF
            cur["stopped"] = True
            close()
            cur = None
    close()
    return runs


def lock_frames(t_est: np.ndarray, runs: list[TriggerRun], fits: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray,
                                                                                    dict]:
    """(run index, pulse number, time) of every frame, and a report. Frames no run covers keep their estimated time
    (run -1); without a lock (report["locked"] false) the times are the estimates."""
    t_est = np.asarray(t_est, float)
    run_of = np.full(len(t_est), -1)
    pulse = np.full(len(t_est), -1, np.int64)
    t_out = t_est.copy()
    rep: dict = {"frames": int(len(t_est)), "runs": [r.as_dict() for r in runs], "locked": False}
    resid, phases, missed, dup = [], [], 0, 0
    for i, run in enumerate(runs):
        f = fits.get(run.segment)
        if f is None:
            continue
        t0 = float(f(run.us0 / 1e6))
        ps = f.slope * run.period_us / 1e6  # session seconds per pulse
        lo = t0 - 0.5 * ps if run.started else -np.inf
        hi = float(f(run.us(run.last) / 1e6)) + 0.5 * ps if run.stopped else np.inf
        m = (t_est >= lo) & (t_est < hi) & (run_of < 0)
        if not m.any():
            continue
        x = (t_est[m] - t0) / ps
        delta = float(np.angle(np.mean(np.exp(2j * np.pi * x))) / (2 * np.pi))  # the common phase
        k = np.round(x - delta).astype(np.int64)
        resid.append(x - delta - k)
        phases.append(delta * ps)
        d = np.diff(k)
        missed += int(np.clip(d - 1, 0, None).sum())
        dup += int((d <= 0).sum())
        run_of[m], pulse[m] = i, k
        t_out[m] = f(run.us(k) / 1e6)
    if not resid:
        rep["why"] = "no trigger run covers the frames"
        return run_of, pulse, t_est, rep
    r = np.concatenate(resid)
    ps_all = np.mean([f.slope for f in fits.values()]) * runs[0].period_us / 1e6
    locked_share = float(np.mean(np.abs(r) < LOCK_TOL))
    rep.update(fps=runs[0].fps, assigned=int((run_of >= 0).sum()), locked_share=locked_share,
               spread_ms=float(np.std(r) * ps_all * 1e3), phase_ms=float(np.mean(phases) * 1e3),
               missed_pulses=missed, duplicates=dup)
    rep["locked"] = bool(locked_share >= LOCK_SHARE and dup == 0)
    if not rep["locked"]:
        rep["why"] = ("the frames do not follow the pulses (is the camera in trigger mode, on the trigger wire?)"
                      if locked_share < LOCK_SHARE else f"{dup} frame(s) on the same pulse as the one before")
        return run_of, pulse, t_est, rep
    return run_of, pulse, t_out, rep


def frame_sets(sess, raw: pd.DataFrame, fits: dict) -> tuple[pd.DataFrame, dict]:
    """Every pulse of the frame trigger with each frame-triggered camera's frame on it: (run, pulse, t,
    frame_<cam> ...), and a report per camera. A pulse with a frame from every camera is a 3D set."""
    from .io import load_frames
    from .sync import camera_tick_hz, fit_camera_clock, frame_times

    runs = trigger_runs(raw)
    parts, reps = [], {}
    for cam in sess.cameras:
        if not cam.get("frame_trigger") or not cam.get("frames_file"):
            continue
        cid = cam["id"]
        frames = load_frames(sess, cid)
        if not len(frames):
            continue
        tick = camera_tick_hz(cam)
        t_est = frame_times(frames, fit_camera_clock(frames, tick), tick)
        run, n, t, rep = lock_frames(t_est, runs, fits)
        reps[cid] = rep
        if rep["locked"]:
            ok = run >= 0
            parts.append(pd.DataFrame({"run": run[ok], "pulse": n[ok], "t": t[ok], "camera": cid,
                                       "frame_idx": frames["frame_idx"].to_numpy()[ok]}))
    if not parts:
        return pd.DataFrame(columns=["run", "pulse", "t"]), reps
    long = pd.concat(parts)
    sets = long.pivot_table(index=["run", "pulse"], columns="camera", values="frame_idx", aggfunc="first")
    sets.columns = [f"frame_{c}" for c in sets.columns]
    sets = sets.astype("Int64").reset_index()
    t = long.groupby(["run", "pulse"])["t"].first().reset_index()
    sets = t.merge(sets, on=["run", "pulse"]).sort_values(["run", "pulse"]).reset_index(drop=True)
    return sets, reps
