"""Hardware frame trigger: firmware lines, the pulse train, frames locked to it, cameras paired, camera setup."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from mavric.analysis.frame_trigger import lock_frames, trigger_runs
from mavric.analysis.sync import ClockFit
from mavric.rfid.protocol import KIND_TRIG, KIND_TRIGOFF, KIND_TRIGON, parse_line
from tests.test_flir import CFG, Camera, Node, blackfly_s_nodes, pyspin  # noqa: F401  (pyspin: fixture)


def raw_rows(rows):
    """RFID log rows (kind, arduino_us, reader) in arrival order, segment 0."""
    return pd.DataFrame([{"segment": 0, "kind": k, "arduino_us": us, "reader": n} for k, us, n in rows])


def test_trigger_lines_and_runs():
    assert (parse_line("630091,TRIG,60").kind, parse_line("630091,TRIG,60").reader) == (KIND_TRIG, 60)
    assert parse_line("5,TRIGON,33332000").reader == 33332000
    # only TRIG lines (the trigger started before the recording): two of them give the period exactly
    p = 33332.0
    rows = [(KIND_TRIG, 1000 + n * p + j, n) for n, j in ((30, 3), (60, -2), (90, 1), (120, 0))]
    run, = trigger_runs(raw_rows(rows))
    assert run.period_us == p and run.us0 == pytest.approx(1000, abs=3) and not run.started
    assert (run.first, run.last) == (30, 120) and run.resid_us <= 3
    # a start, a stop, and a new start at another rate
    rows = [(KIND_TRIGON, 500.0, 16668000), (KIND_TRIG, 500 + 60 * 16668.0, 60), (KIND_TRIGOFF, 2e6, 75),
            (KIND_TRIGON, 3e6, 33332000), (KIND_TRIG, 3e6 + 30 * p, 30)]
    a, b = trigger_runs(raw_rows(rows))
    assert a.started and a.stopped and a.fps == pytest.approx(59.995, abs=1e-3)
    assert b.started and not b.stopped and b.us0 == 3e6
    assert trigger_runs(raw_rows([(KIND_TRIG, 1e6, 30)])) == []  # one pulse: no period yet


def test_a_free_running_camera_does_not_lock():
    run, = trigger_runs(raw_rows([(KIND_TRIGON, 0.0, 33332000), (KIND_TRIG, 30 * 33332.0, 30)]))
    fit = ClockFit(0.0, 1.0, 10, 0.0, 0.0)
    t = np.arange(200) / 30.0012 + 0.004  # on the pulses, 4 ms later by the camera's clock
    _r, n, t_out, rep = lock_frames(t, [run], {0: fit})
    assert rep["locked"] and (n == np.arange(200)).all() and np.allclose(t_out, n * 0.033332)
    _r, _n, t_out, rep = lock_frames(np.arange(200) / 25.0, [run], {0: fit})  # 25 fps, ignoring the pulses
    assert not rep["locked"] and "do not follow the pulses" in rep["why"] and (t_out == np.arange(200) / 25.0).all()


def test_a_session_on_the_frame_trigger(tmp_path):
    from mavric.analysis.pipeline import prepare, run_analysis
    from mavric.sim import simulate_session

    sess = simulate_session(tmp_path, duration_s=60, seed=5, frame_trigger_fps=30.0, write_labels=False)
    assert sess.camera()["frame_trigger"] and sess.meta["frame_trigger"]["fps"] == 30.0
    P = prepare(sess)
    tr = P.sync["report"]["camera"]["trigger"]
    assert tr["locked"] and tr["duplicates"] == 0 and tr["spread_ms"] < 0.1
    assert tr["runs"][0]["period_us"] == 33332.0 and not tr["runs"][0]["started"]  # started before the recording
    t0, p = sess.meta["sim"]["trigger"]
    k = np.ceil(-t0 / p) + P.frames["hw_counter"].to_numpy()
    d = P.frame_t - (t0 + k * p)  # each frame at its pulse's true time (plus the clock fit's constant latency)
    assert np.std(d) < 50e-6 and np.ptp(d) < 0.3e-3
    assert tr["missed_pulses"] == int(P.frames["hw_counter"].diff().fillna(1).sub(1).sum())  # the camera's drops
    led = P.sync["report"]["led"]
    assert led["pass"] and led["offset_applied"] is False  # the frames are on the RFID clock already
    s = run_analysis(sess, plots=False)
    assert s["frame_trigger"]["cameras"]["cam0"]["locked"]
    sets = pd.read_csv(sess.analysis_dir / "frame_sets.csv")
    assert list(sets.columns) == ["run", "pulse", "t", "frame_cam0"] and len(sets) == len(P.frames)


def test_two_cameras_on_the_simulated_trigger(tmp_path):
    from mavric.acquire import simulation_controller, stall_text
    from mavric.analysis.pipeline import run_analysis
    from mavric.config import load_rig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    rig["cameras"] = [{"id": c, "backend": "synthetic", "fps": 25, "frame_trigger": True} for c in ("cam0", "cam1")]
    rig["sync"] = {**rig["sync"], "frame_trigger": {"source": "rfid", "fps": 30}}
    ctl = simulation_controller(rig, seed=1)
    try:
        for c in ctl.rig["cameras"]:
            ctl.connect_camera(c)
        time.sleep(2.2)
        st = ctl.status()
        assert all(c["frames"] == 0 for c in st["cameras"].values())  # no pulses yet
        assert "waiting for the frame trigger" in stall_text(st["cameras"]["cam0"])
        assert st["frame_trigger"]["running"] is None
        ctl.connect_rfid(ctl._sim_source)  # sends F30
        time.sleep(1.5)
        st = ctl.status()
        assert st["frame_trigger"]["running"] and st["frame_trigger"]["period_ns"] == 33332000
        assert all(c["frames"] > 20 for c in st["cameras"].values())
        sess = ctl.start_recording("trig")
        time.sleep(3.5)
        ctl.stop_recording()
    finally:
        ctl.shutdown()
    assert sess.meta["frame_trigger"]["cameras"] == ["cam0", "cam1"]
    assert all(c["frame_trigger"] and c["fps"] == 30 for c in sess.cameras)
    ft = run_analysis(sess, plots=False)["frame_trigger"]
    assert all(r["locked"] for r in ft["cameras"].values())
    assert ft["complete_sets"] >= 0.9 * ft["pulses"] and ft["pulses"] >= 90


def test_flir_cameras_expose_on_the_trigger_input(pyspin):
    from mavric.video.backends import make_backend

    nodes = blackfly_s_nodes()
    nodes.update({"TriggerSelector": Node("enum", "AcquisitionStart", ["FrameStart", "AcquisitionStart"]),
                  "TriggerSource": Node("enum", "Software", ["Line0", "Line1", "Line2", "Line3", "Software"]),
                  "TriggerActivation": Node("enum", "FallingEdge", ["RisingEdge", "FallingEdge"]),
                  "TriggerOverlap": Node("enum", "Off", ["Off", "ReadOut"])})
    cam = Camera(nodes=nodes, model="Blackfly S BFS-U3-16S2C")
    pyspin(cam)
    be = make_backend(dict(CFG, frame_trigger=True))
    be.open()
    n = cam.nodes
    assert (n["TriggerMode"].value, n["TriggerSelector"].value, n["TriggerSource"].value) == ("On", "FrameStart",
                                                                                             "Line0")
    assert n["TriggerActivation"].value == "RisingEdge" and n["TriggerOverlap"].value == "ReadOut"
    assert n["AcquisitionFrameRateEnable"].value is False  # the pulses set the rate
    info = be.info()
    assert info["frame_trigger"] and info["trigger_line"] == "Line0" and info["fps"] == 30.0
    assert not any("TriggerMode is still On" in w for w in info["warnings"])
    be.close()
    # a camera without the trigger source node runs free, and says so
    cam = Camera(nodes=blackfly_s_nodes(), model="Blackfly S")
    pyspin(cam)
    be = make_backend(dict(CFG, frame_trigger=True, trigger_line="Line3"))
    be.open()
    assert not be.frame_trigger and cam.nodes["TriggerMode"].value == "Off"
    assert any("frame trigger" in w and "runs free" in w for w in be.info()["warnings"])
    be.close()
