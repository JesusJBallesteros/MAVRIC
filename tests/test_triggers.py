"""RFID-triggered clips: rule logic (deterministic) and a real-time simulated run."""

import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from mavric.config import TagMap, load_rig
from mavric.core import now
from mavric.rfid.serial_reader import Presence
from mavric.session import Session
from mavric.triggers import TriggerManager, TriggerRule, ring_seconds
from mavric.video.backends import Frame

TAGS = TagMap({"0A000000010B": "S01", "0A0000000208": "S02"}, {})


class FakeCam:
    """Stands in for CameraThread: hands every clip a few pre-trigger frames."""

    def __init__(self):
        self.backend = SimpleNamespace(fps=30.0, size=(64, 48), cfg={})
        self.open_clips = []

    def start_clip(self, rec, since_abs):
        for k in range(5):
            rec.submit(Frame(np.zeros((48, 64), np.uint8), since_abs + k / 30, k))
        self.open_clips.append(rec)
        return 5

    def stop_clip(self, rec):
        self.open_clips.remove(rec)


def _session(tmp_path):
    anchor = {"perf_s": now(), "utc_ns": time.time_ns()}
    from mavric.config import RoiSet

    return Session.create(tmp_path, "trig", load_rig(), TAGS, RoiSet(), anchor), anchor["perf_s"]


def _wait(cond, timeout=3.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_rule_parsing_and_matching():
    r = TriggerRule.from_cfg({"name": "b", "camera": "cam1", "antennas": [2], "match": ["S01"]})
    assert r.matches(2, "0A000000010B", TAGS)
    assert not r.matches(1, "0A000000010B", TAGS)
    assert not r.matches(2, "0A0000000208", TAGS)
    assert TriggerRule.from_cfg({"name": "a", "camera": "c", "antennas": "any"}).matches(4, "0A0000000208", TAGS)
    assert ring_seconds([r, TriggerRule("x", "cam1", pre_s=4)], "cam1") == pytest.approx(4.5)
    assert ring_seconds([r], "cam0") == 0.0


def test_clip_lifecycle_post_roll_and_extension(tmp_path):
    sess, t0 = _session(tmp_path)
    cam = FakeCam()
    rule = TriggerRule("p2", "cam1", antennas=[2], pre_s=1.0, post_s=0.4)
    tm = TriggerManager([rule], {"cam1": cam}, TAGS, sess, t0, {"codec": "mpeg4"})
    tm.start()
    try:
        tag = "0A000000010B"
        tm.on_presence(Presence(1, tag, "", now()))  # other antenna: ignored
        time.sleep(0.15)
        assert not sess.clips
        t_on = now()
        tm.on_presence(Presence(2, tag, "", t_on))
        assert _wait(lambda: len(sess.clips) == 1)
        c = sess.clips[0]
        assert c["trigger_t"] == pytest.approx(t_on - t0, abs=1e-6)
        assert c["start_t"] == pytest.approx(t_on - t0 - 1.0, abs=1e-6)
        assert c["pigeon"] == "S01" and c["antenna"] == 2
        # leaves, comes back during the post-roll: same clip continues
        tm.on_presence(Presence(2, "", tag, now()))
        time.sleep(0.2)
        tm.on_presence(Presence(2, tag, "", now()))
        time.sleep(0.6)
        assert len(sess.clips) == 1 and sess.clips[0]["stop_t"] is None
        t_off = now()
        tm.on_presence(Presence(2, "", tag, t_off))
        assert _wait(lambda: sess.clips[0]["stop_t"] is not None)
        c = sess.clips[0]
        assert c["end_reason"] == "post_roll_done"
        assert c["stop_t"] >= t_off - t0 + 0.4 - 0.02
        assert c["frames"] == 5
        assert (sess.path / c["file"]).exists() and (sess.path / c["frames_file"]).exists()
        assert not cam.open_clips
    finally:
        tm.stop()


def test_open_clip_closed_on_session_stop(tmp_path):
    sess, t0 = _session(tmp_path)
    cam = FakeCam()
    tm = TriggerManager([TriggerRule("any", "cam1", pre_s=0.5, post_s=5)], {"cam1": cam}, TAGS, sess, t0,
                        {"codec": "mpeg4"})
    tm.start()
    tm.seed({1: "0A0000000208", 2: ""})  # bird already present at start
    assert _wait(lambda: len(sess.clips) == 1)
    clips = tm.stop()
    assert clips[0]["end_reason"] == "session_stopped" and clips[0]["pigeon"] == "S02"
    assert not cam.open_clips


def test_missing_camera_reports_error(tmp_path):
    sess, t0 = _session(tmp_path)
    tm = TriggerManager([TriggerRule("x", "camX")], {}, TAGS, sess, t0, {})
    tm.start()
    tm.on_presence(Presence(1, "0A0000000208", "", now()))
    assert _wait(lambda: tm.status()[0]["error"] is not None)
    tm.stop()
    assert not sess.clips


def test_realtime_triggered_camera(tmp_path):
    """Simulated cage: cam0 continuous, cam1 records only RFID-triggered clips."""
    from mavric.acquire import simulation_controller
    from mavric.analysis.pipeline import run_analysis

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30},
                      {"id": "cam1", "backend": "synthetic", "fps": 30, "record": "triggered"}]
    pre, post = 1.0, 1.0
    rig["triggers"] = [{"name": "any", "camera": "cam1", "antennas": "any", "pre_s": pre, "post_s": post}]
    ctl = simulation_controller(rig, seed=4)
    try:
        ctl.connect_rfid(ctl._sim_source)
        for cam in ctl.rig["cameras"]:
            ctl.connect_camera(cam)
        time.sleep(1.5)  # let the ring buffer fill
        sess = ctl.start_recording("trig", "test")
        time.sleep(15)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()

    cams = {c["id"]: c for c in sess.meta["cameras"]}
    assert cams["cam1"]["record"] == "triggered" and not cams["cam1"].get("video_file")
    assert cams["cam0"]["record_format"] == "raw" and cams["cam0"]["video_files"][0]["file"] == "cam0_raw_000.mkv"
    assert sum(f["n_frames"] for f in cams["cam0"]["video_files"]) == len(pd.read_csv(sess.file("frames_cam0.csv")))
    assert [c["id"] for c in sess.cameras] == ["cam0"]
    clips = sess.clips
    assert len(clips) >= 1 and sess.meta["stats"]["clips"] == len(clips)
    raw = pd.read_csv(sess.file("rfid_raw.csv"), dtype={"tag": str})
    for c in clips:
        fr = pd.read_csv(sess.file(c["frames_file"]))
        assert len(fr) == c["frames"] > 0
        assert c["record_format"] == "raw" and sum(f["n_frames"] for f in c["files"]) == c["frames"]
        assert c["stop_t"] is not None and c["end_reason"] in ("post_roll_done", "session_stopped", "max_length")
        seeded = c["trigger_t"] < 0.5  # bird already on a perch when recording started
        if not seeded:
            # ring-buffer frames reach back pre_s before the trigger (plus the frames
            # between the trigger round and the moment the clip opened, ~1 round later)
            assert pre * 30 - 3 <= c["pre_frames"] <= pre * 30 + 12
            assert fr["host_t"].iloc[0] == pytest.approx(c["trigger_t"] - pre, abs=0.1)
            # the trigger is the round in which the tag first appeared at that antenna
            reads = raw[(raw["kind"] == "TAG") & (raw["reader"] == c["antenna"]) & (raw["tag"] == c["tag"])]
            assert (reads["host_t"] - c["trigger_t"]).abs().min() < 0.01
        if c["end_reason"] == "post_roll_done":
            assert fr["host_t"].iloc[-1] >= c["stop_t"] - 0.1
        assert fr["host_t"].is_monotonic_increasing

    s = run_analysis(sess, plots=False)
    assert s["clips"]["n"] == len(clips)
    ct = pd.read_csv(sess.analysis_dir / "clips.csv")
    assert len(ct) == len(clips)
    assert s["clips"]["linked_to_rfid_bout"] >= 1


def test_clip_rules_from_outputs_yaml_and_rig(tmp_path):
    from mavric.acquire import AcquisitionController
    from mavric.config import RoiSet
    from mavric.outputs.config import OutputsConfig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30},
                      {"id": "cam1", "backend": "synthetic", "fps": 30, "record": "triggered"}]
    rig["triggers"] = [{"name": "a", "camera": "cam1", "pre_s": 1}, {"name": "b", "camera": "cam0"}]
    clips = [{"name": "a", "camera": "cam1", "antennas": [2], "match": ["S01"], "pre_s": 4.0, "post_s": 2.0,
              "max_s": 60.0}]
    OutputsConfig({}, [], clips).save(rig["paths"]["outputs_file"])
    cfg = OutputsConfig.load(rig["paths"]["outputs_file"])
    assert cfg.clips == clips
    assert TriggerRule.from_cfg(clips[0]).to_cfg() == clips[0]

    ctl = AcquisitionController(rig, TagMap({"0A000000010B": "S01"}, {}), RoiSet())
    assert {r.name: r.pre_s for r in ctl.rules} == {"b": 3.0, "a": 4.0}, "the GUI's rule wins on equal names"
    assert ctl.roi_camera == "cam0"
    ctl.cams["cam1"] = SimpleNamespace(ring_s=0.0)  # a connected camera gets the ring buffer its rules need
    ctl.set_clip_rules(OutputsConfig())
    assert ctl.cams["cam1"].ring_s == pytest.approx(1.5)
    with pytest.raises(ValueError, match="duplicate"):
        OutputsConfig({}, [], clips * 2).validate()
    with pytest.raises(ValueError, match="camera"):
        OutputsConfig({}, [], [{"name": "x"}]).validate()


def test_rois_belong_to_first_rig_camera(tmp_path):
    """Connecting cam1 before cam0 must not move the ROIs and the sync LED box to cam1."""
    from mavric.acquire import simulation_controller

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "none.yaml")}
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30},
                      {"id": "cam1", "backend": "synthetic", "fps": 30}]
    ctl = simulation_controller(rig, seed=1)
    try:
        ctl.connect_rfid(ctl._sim_source)
        ctl.connect_camera(rig["cameras"][1])
        ctl.connect_camera(rig["cameras"][0])
        sess = ctl.start_recording("order")
        time.sleep(1.0)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    cams = {c["id"]: c for c in sess.meta["cameras"]}
    assert cams["cam0"]["rois_camera"] and cams["cam0"]["led_bbox"] is not None
    assert not cams["cam1"]["rois_camera"] and cams["cam1"]["led_bbox"] is None
    assert Session.open(sess.path).camera()["id"] == "cam0"
