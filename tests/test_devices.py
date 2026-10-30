"""Microphone and speaker positions: geometry, the sound check and fit, and the Calibrate cameras tab 4."""

from __future__ import annotations

import os
import time
from dataclasses import replace

import numpy as np
import pytest
import yaml

from mavric.audio.geometry import Device, aim_at, cardioid_db, confusable, direction, fingerprints, load_devices, \
    place_sources, save_devices, suggested_layout
from mavric.audio.ranging import Ranging, analyse, arrival, fit, sweep

SR = 48000.0
SPEAKERS = ["spk1", "spk2", "spk3", "spk4"]


@pytest.fixture(scope="module")
def cage():
    from mavric.config import RoiSet
    from mavric.sim import SimWorld
    from mavric.video.calibration import BoardSpec
    from mavric.video.extrinsics import SimulatedRig
    from mavric.video.places import place_3d

    world = SimWorld.random(duration_s=120, seed=2)
    rig = SimulatedRig(BoardSpec(), {c: world.image_size for c in ("cam0", "cam1")}, near=1.0)
    scene = world.attach_rig(rig, "cam0")
    tr = scene.truth()
    rs = world.roi_set({1: "P1", 2: "P2", 3: "P3", 4: "P4"})
    rs = RoiSet([place_3d(replace(r, plane_z_mm=tr["SYNC"] if r.kind == "sync" else tr[r.antenna]),
                          scene.cams["cam0"]) for r in rs.rois], rs.image_size)
    return world, scene, rs


def test_directions_and_the_cardioid():
    assert np.allclose(direction(*aim_at((0, 0, 0), (100, 100, 0))), np.array([1, 1, 0]) / np.sqrt(2))
    assert aim_at((0, 0, 100), (0, 0, 0)) == (0.0, -90.0)
    mic = Device("m", "mic", np.zeros(3), (0.0, 0.0))  # facing +x
    assert cardioid_db(mic, (1000, 0, 0)) == pytest.approx(0.0)
    assert cardioid_db(mic, (0, 1000, 0)) == pytest.approx(-6.02, abs=0.01)  # side: half
    assert cardioid_db(mic, (-1000, 0, 0)) == -25.0  # rear: a real cardioid's rejection


def test_places_heard_by_two_microphones(cage):
    _, scene, rs = cage
    devs = scene.devices()
    src = place_sources(rs)
    assert set(src) == {"P1", "P2", "P3", "P4"}
    fp = fingerprints([devs["micA"], devs["micB"]], src)
    # P1 and P3 are on micA's half (x below the middle): later at micB; P2 and P4 earlier
    assert fp["P1"][0] > 0.3 and fp["P3"][0] > 0.3 and fp["P2"][0] < -0.3 and fp["P4"][0] < -0.3
    assert confusable(fp) == []
    same = {"X": src["P1"], "Y": src["P1"] + 5.0}
    assert confusable(fingerprints([devs["micA"], devs["micB"]], same)) == [("X", "Y")]


def test_suggested_layout_surrounds_the_places(cage):
    _, scene, rs = cage
    src = place_sources(rs)
    lay = suggested_layout(src, ["micA", "micB"], SPEAKERS)
    P = np.array(list(src.values()))
    for n in SPEAKERS:  # outside the places, aimed inwards
        p = lay[n].position
        assert not (P[:, 0].min() < p[0] < P[:, 0].max() and P[:, 1].min() < p[1] < P[:, 1].max())
        assert lay[n].axis @ (P.mean(axis=0) - p) > 0
    a, b = lay["micA"], lay["micB"]
    assert a.position[2] > P[:, 2].max() and b.position[2] > P[:, 2].max()
    assert a.axis @ b.axis < 0  # facing opposite sides
    truth = scene.devices()
    assert max(np.linalg.norm(lay[n].position - truth[n].position) for n in lay) < 150  # a fair start here


def test_devices_saved_next_to_the_cameras(tmp_path, cage):
    _, scene, _ = cage
    path = tmp_path / "calibration.yaml"
    path.write_text(yaml.safe_dump({"cameras": {"cam0": {"lens": {"x": 1}}}, "world": {"date": "2026-10-29"}}),
                    encoding="utf-8")
    devs = scene.devices()
    devs["micA"] = replace(devs["micA"], how="sound", sd_mm=12.0)
    save_devices(path, devs, {"sound_fit": {"rms_mm": 3.0}})
    back, meta = load_devices(path)
    assert set(back) == set(devs) and back["micA"].how == "sound" and back["micA"].sd_mm == 12.0
    assert all(np.allclose(back[n].position, devs[n].position, atol=0.06) for n in devs)
    assert meta["world_date"] == "2026-10-29" and meta["sound_fit"]["rms_mm"] == 3.0
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert d["cameras"]["cam0"]["lens"] == {"x": 1}  # the rest of the file is kept


def test_a_sweep_arrives_where_it_was_delayed():
    ref = sweep(SR)
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.002, 12000)
    t = np.arange(len(ref))
    for delay, gain in ((401.37, 0.4), (455.9, 0.3)):  # the direct sound, then a reflection almost as strong
        x += gain * np.interp(np.arange(len(x)) - delay, t, ref, left=0, right=0)
    a = arrival(x, 0, ref, SR)
    assert a["delay"] == pytest.approx(401.37, abs=0.1) and a["snr_db"] > 30


def synthetic_ranging(devs, latency_ms=0.7, c=343.0, noise=0.002, seed=1):
    """A recording of every speaker's sweep at every microphone, as analyse() sees it."""
    ref = sweep(SR)
    rng = np.random.default_rng(seed)
    mics = [n for n, d in devs.items() if d.kind == "mic"]
    spks = [n for n, d in devs.items() if d.kind == "speaker"]
    x = rng.normal(0, noise, (int(SR * 0.25 * len(spks)) + 4000, len(mics)))
    t = np.arange(len(ref))
    starts = {}
    for k, s in enumerate(spks):
        s0 = int(1000 + k * SR * 0.25)
        starts[s] = [s0]
        for j, m in enumerate(mics):
            d = np.linalg.norm(devs[s].position - devs[m].position)
            delay = s0 + (d / c + latency_ms) * SR / 1e3
            x[:, j] += 0.3 * 1000 / d * np.interp(np.arange(len(x)) - delay, t, ref, left=0, right=0)
    return Ranging(SR, 0.0, analyse(x, starts, {m: j for j, m in enumerate(mics)}, ref, SR, 0))


def test_the_fit_finds_the_microphones_and_the_latency(cage):
    _, scene, rs = cage
    truth = scene.devices()
    r = synthetic_ranging(truth)
    assert len(r.ok_pairs()) == 8
    check = fit(r, truth)  # the true layout: only the latency to find
    assert check.latency_ms == pytest.approx(0.7, abs=0.01) and check.rms_mm < 3 and check.verdict()[0]
    lay = suggested_layout(place_sources(rs), ["micA", "micB"], SPEAKERS)
    start = {**{n: truth[n] for n in SPEAKERS}, "micA": lay["micA"], "micB": lay["micB"]}
    f = fit(r, start, ["micA", "micB"])
    for n in ("micA", "micB"):
        err = f.devices[n].position - truth[n].position
        assert np.linalg.norm(err[:2]) < 10 and abs(err[2]) < 60  # across: from the sound; height: mostly typed
        assert f.devices[n].how == "sound"
    assert f.verdict()[0] and "moved" in f.summary()
    wrong = dict(truth)
    wrong["spk1"] = replace(truth["spk1"], position=truth["spk1"].position + [250.0, 0, 0])
    ok, why = fit(r, wrong).verdict()  # a speaker typed 25 cm off: the check says which one
    assert not ok and any("spk1" in w for w in why)


def test_measured_on_the_live_simulated_interface(cage):
    from mavric.audio.config import AudioConfig
    from mavric.audio.ranging import measure
    from mavric.audio.sim import HIDDEN_LATENCY
    from mavric.audio.stream import AudioStream
    from mavric.config import load_rig

    world, scene, _ = cage
    cfg = AudioConfig.from_rig(load_rig())
    cfg.host_api = "sim"
    cfg.inputs = {1: "micA", 2: "micB", 3: "sync"}
    cfg.speakers = {1: "spk1", 2: "spk2", 3: "spk3", 4: "spk4"}
    cfg.sync_channel = "sync"
    st = AudioStream(cfg, world=world)
    st.open()
    try:
        r = measure(st, repeats=2, gap_s=0.25)
    finally:
        st.close()
    assert len(r.ok_pairs()) == 8 and all(v["spread_ms"] < 0.01 for v in r.pairs.values())
    f = fit(r, scene.devices())
    assert f.latency_ms == pytest.approx(HIDDEN_LATENCY / SR * 1e3, abs=0.01) and f.rms_mm < 3


@pytest.mark.skipif(os.environ.get("MAVRIC_SKIP_GUI") == "1", reason="GUI tests disabled")
def test_devices_tab_on_the_simulated_cage(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.audio.ranging import measure
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.gui.tips import missing_tips
    from tests.test_places import placed, save_truth

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    rig["cameras"] = [{"id": c, "backend": "synthetic", "fps": 30} for c in ("cam0", "cam1")]
    rig["audio"] = {**rig["audio"], "inputs": {1: "micA", 2: "micB", 3: "sync"}, "sync_channel": "sync",
                    "speakers": {1: "spk1", 2: "spk2", 3: "spk3", 4: "spk4"}}
    ctl = simulation_controller(rig, seed=1)
    scene = ctl.world.scene
    save_truth(ctl.rig["paths"]["calibration_file"], scene)
    ctl.rois = placed(ctl.world, scene)
    win = AcquireWindow(ctl, sim=True)
    for c in rig["cameras"]:
        ctl.connect_camera(c)
    ctl.connect_audio()
    try:
        dlg = CalibrationDialog(win)
        dlg.tabs.setCurrentIndex(3)
        tab = dlg.devices
        assert "✓" in tab.ready_label.text() and tab.table.rowCount() == 6
        assert tab.b_measure.isEnabled() and missing_tips(dlg) == []
        assert all(d.how == "suggested" for d in tab.devices.values())
        assert "every place sounds different" in tab.fp_label.text()
        tab._moved("micA", np.array([10.0, -200.0, 640.0]))  # dragged in the space view
        assert tab.table.cellWidget(0, 1).value() == 10.0 and tab.devices["micA"].how == "typed"
        for n in SPEAKERS:  # the speakers' true places typed in
            tab.devices[n] = scene.devices()[n]
        tab._measured(measure(ctl.audio, repeats=2, gap_s=0.25))
        assert "8 of 8" in tab.sound_label.text() and tab.b_fit.isEnabled()
        tab._fit()
        assert "simulated: every fitted device within" in tab.sound_label.text()
        tab._apply_fit()
        truth = scene.devices()
        assert all(np.linalg.norm(tab.devices[n].position[:2] - truth[n].position[:2]) < 15 for n in ("micA", "micB"))
        tab._save()
        saved, meta = load_devices(ctl.rig["paths"]["calibration_file"])
        assert set(saved) == {"micA", "micB", *SPEAKERS} and saved["micA"].how == "sound" and "sound_check" in meta
        assert "6 sound devices" in win.cal_label.text()
        dlg.close()
    finally:
        win.close()
