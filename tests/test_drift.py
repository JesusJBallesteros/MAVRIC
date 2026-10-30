"""Has a camera moved? Reference views, the measurement, sessions, acquisition and the alignment overlay."""

from __future__ import annotations

import os
import time

import cv2
import numpy as np
import pytest

from mavric.video.drift import (
    Reference,
    Shift,
    check_lines,
    clear_references,
    copy_references,
    load_reference,
    measure,
    overlay,
    recorded_warnings,
    reference_path,
    save_reference,
    session_check,
    steady,
)


def bump(img, dx, dy, rot):
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), -rot, 1.0)
    M[:, 2] += (dx, dy)
    return cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)


@pytest.fixture(scope="module")
def cage():
    from mavric.sim import SimWorld
    from mavric.video.calibration import BoardSpec
    from mavric.video.extrinsics import SimulatedRig

    world = SimWorld.random(duration_s=300, seed=3)
    world.attach_rig(SimulatedRig(BoardSpec(), {c: world.image_size for c in ("cam0", "cam1")}, near=1.0), "cam0")
    return world


def test_a_bump_is_measured_and_graded(cage):
    ref = Reference(cage.render(10.0, cam="cam1"))
    still = measure(ref, cage.render(120.0, cam="cam1"))  # the birds moved, the camera did not
    assert still.level == "ok" and still.max_px < 1.0 and "in place" in still.text()
    s = measure(ref, bump(cage.render(120.0, cam="cam1"), 12, 5, 0.5))
    assert (s.dx, s.dy, s.rot_deg) == pytest.approx((12, 5, 0.5), abs=0.5)
    assert s.level == "moved" and "12 px right, 5 px down, turned 0.5° clockwise" in s.text()
    small = measure(ref, bump(cage.render(120.0, cam="cam1"), 3, 0, 0))  # under 0.5 % of the diagonal: lenient
    assert small.level == "ok"
    s = measure(ref, bump(cage.render(120.0, cam="cam1"), -6, 0, 0))
    assert s.level == "shifted" and "6 px left" in s.text()
    dim = cv2.convertScaleAbs(bump(cage.render(120.0, cam="cam1"), -30, 10, 2.0), alpha=0.6, beta=10)
    s = measure(ref, dim)  # less light, and turned 2°
    assert (s.dx, s.dy, s.rot_deg) == pytest.approx((-30, 10, 2.0), abs=1.0) and s.level == "moved"
    assert measure(ref, np.full((480, 640), 90, np.uint8)).level == "unknown"  # nothing to compare
    assert "640×480" in measure(ref, cv2.resize(cage.render(120.0, cam="cam1"), (320, 240))).why
    # several measurements: the middle one counts, so one odd picture does not warn
    assert steady([Shift(max_px=1.0, level="ok"), Shift(max_px=30.0, level="moved"), Shift(max_px=1.2, level="ok")
                   ]).level == "ok"
    # the overlay: the reference's outlines in magenta, an arrow when it moved
    img = bump(cage.render(120.0, cam="cam1"), 12, 5, 0.5)
    ov = overlay(img, ref, measure(ref, img))
    magenta = (ov[..., 0] > 150) & (ov[..., 1] < 110) & (ov[..., 2] > 150)
    assert ov.shape == img.shape and magenta.sum() > 200


def test_reference_views_saved_cleared_and_copied(tmp_path, cage):
    cal = tmp_path / "config" / "calibration.yaml"
    p = save_reference(cal, "cam1", cage.render(10.0, cam="cam1"))
    assert p == tmp_path / "config" / "calibration" / "reference_cam1.png" == reference_path(cal, "cam1")
    assert load_reference(cal, "cam1").size == (640, 480) and load_reference(cal, "cam0") is None
    sess = tmp_path / "session"
    sess.mkdir()
    copy_references(cal, sess)
    assert (sess / "calibration" / "reference_cam1.png").exists()
    assert clear_references(cal, ["cam0", "cam1"]) == ["cam1"] and load_reference(cal, "cam1") is None


def test_a_session_where_the_camera_was_bumped(tmp_path):
    from mavric.analysis.pipeline import run_analysis
    from mavric.sim import simulate_session

    still = simulate_session(tmp_path, duration_s=60, seed=4, name="still")
    chk = session_check(still)
    assert chk["cam0"]["reference"] and chk["cam0"]["level"] == "ok" and len(chk["cam0"]["samples"]) == 7
    assert chk["cam0"]["led_flashes"] is True and check_lines(chk)[0].startswith("cam0: in place")
    sess = simulate_session(tmp_path, duration_s=60, seed=4, name="bumped", camera_moved=(20.0, 8.0, 1.0, 35.0))
    e = session_check(sess)["cam0"]
    assert e["level"] == "moved" and e["in_place_until_s"] < 35.0 < e["since_s"]
    s = run_analysis(sess, plots=False)
    line = s["camera_positions"][0]
    assert "moved" in line and "20 px right" in line and "since between 0:" in line
    assert (sess.analysis_dir / "camera_positions.json").exists()


def test_acquisition_notes_a_moved_camera(tmp_path):
    from mavric.acquire import simulation_controller
    from mavric.config import load_rig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    ctl = simulation_controller(rig, seed=1)
    th = ctl.connect_camera(ctl.rig["cameras"][0])

    def fresh():  # wait for a new picture
        k = th.latest.grab_idx if th.latest is not None else -1
        t_end = time.monotonic() + 5
        while (th.latest is None or th.latest.grab_idx == k) and time.monotonic() < t_end:
            time.sleep(0.02)

    try:
        fresh()
        assert ctl.check_positions() == {}  # no reference view yet
        sess = ctl.start_recording("first")  # takes it
        ctl.stop_recording()
        assert sess.meta["reference_views_taken"] == ["cam0"]
        assert (sess.path / "calibration" / "reference_cam0.png").exists()
        for _ in range(3):
            fresh()
            assert ctl.check_positions()["cam0"].level == "ok"
        ctl.world.moved["cam0"] = (25.0, 10.0, 1.0, 0.0)  # bumped
        for _ in range(3):
            fresh()
            s = ctl.check_positions()["cam0"]
        assert s.level == "moved"
        sess = ctl.start_recording("second")
        assert sess.camera("cam0")["position_check"]["level"] == "moved"
        assert any("cam0 moved" in w and "Alignment overlay" in w for w in sess.meta["warnings"])
        ctl.world.moved.clear()  # put back while recording
        for _ in range(3):
            fresh()
            ctl.check_positions()
        ctl.stop_recording()
        assert [r["level"] for r in sess.meta["position_checks"]] == ["ok"]
        assert len(recorded_warnings(sess)) == 1 and "at the start" in recorded_warnings(sess)[0]
    finally:
        ctl.shutdown()


@pytest.mark.skipif(os.environ.get("MAVRIC_SKIP_GUI") == "1", reason="GUI tests disabled")
def test_alignment_overlay_in_the_acquire_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.tips import missing_tips

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    try:
        ctl.connect_camera(ctl.rig["cameras"][0])
        win._refresh_views()
        t_end = time.monotonic() + 5
        while ctl.cams["cam0"].latest is None and time.monotonic() < t_end:
            app.processEvents()
        win._update_positions(background=False)
        assert "cam0 — no reference yet" in win.pos_label.text() and missing_tips(win) == []
        monkeypatch.setattr(QtWidgets.QMessageBox, "question",
                            lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
        win._new_reference()
        assert ctl.reference("cam0") is not None and "cam0 ✓ in place" in win.pos_label.text()
        ctl.world.moved["cam0"] = (20.0, 0.0, 0.0, 0.0)
        win.b_align.setChecked(True)
        img = None
        t_end = time.monotonic() + 3
        while time.monotonic() < t_end:
            app.processEvents()
            img = win.video.img.image
        assert win._align_shift is not None and win._align_shift.level == "moved"
        assert img is not None and ((img[..., 0] > 150) & (img[..., 1] < 110) & (img[..., 2] > 150)).sum() > 100
        assert "magenta outlines" in win.statusBar().currentMessage()
        for _ in range(3):
            win._update_positions(background=False)
        assert "cam0 ⚠ moved" in win.pos_label.text()
        win._save_rois()  # new ROIs: the view they were drawn on becomes the reference at the next recording
        assert ctl.reference("cam0") is None
    finally:
        win.close()
        ctl.shutdown()
