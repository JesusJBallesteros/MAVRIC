"""3D places: ROIs lifted to their height, marked in two cameras, and shown in every camera (simulated cage)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace

import cv2
import numpy as np
import pytest

from mavric.config import Roi, RoiSet
from mavric.video.places import MAX_RAY_GAP_MM, CameraModel, outline_in, place_3d, problem, rois_in_camera, \
    triangulate

ANTENNAS = {1: "P1", 2: "P2", 3: "P3", 4: "P4"}


@pytest.fixture(scope="module")
def cage():
    from mavric.sim import SimWorld
    from mavric.video.calibration import BoardSpec
    from mavric.video.extrinsics import SimulatedRig

    world = SimWorld.random(duration_s=120, seed=2)
    rig = SimulatedRig(BoardSpec(), {c: world.image_size for c in ("cam0", "cam1", "cam2")}, near=1.0)
    scene = world.attach_rig(rig, "cam0")
    return world, scene


def placed(world, scene, box=130.0) -> RoiSet:
    truth = scene.truth()
    rs = world.roi_set(ANTENNAS)
    return RoiSet([replace(r, plane_z_mm=truth["SYNC"] if r.kind == "sync" else truth[r.antenna],
                           box_mm=0.0 if r.kind == "sync" else box) for r in rs.rois], rs.image_size)


def save_truth(path, scene) -> None:
    """The simulated cameras' true lenses and positions, as if calibrated (floor origin)."""
    from mavric.video.calibration import save_lens
    from mavric.video.extrinsics import CameraPose, RigSolution, save_poses

    for cid in scene.cams:
        save_lens(path, scene.rig.lens_truth(cid))
    save_poses(path, RigSolution({c: CameraPose(c, scene.rig.T[c]) for c in scene.cams}, "floor", 0.3, 0.5, 20,
                                 date="2026-10-29T12:00:00"))


def test_places_at_their_height_sit_on_the_perches(cage):
    world, scene = cage
    rs = placed(world, scene)
    cam0 = scene.cams["cam0"]
    for r in rs.perches:
        X = np.asarray(place_3d(r, cam0).points3d)
        assert np.abs(X - scene.surface(r.antenna)).max() < 0.5  # mm: the drawn outline, lifted to the true height
    led = np.asarray(place_3d(rs.sync, cam0).points3d).mean(axis=0)
    assert np.linalg.norm(led - scene.led) < 1.0
    for cid in ("cam1", "cam2"):  # every place the camera sees: its outline holds the bar's middle
        sub = rois_in_camera(rs, cid, scene.cams, "cam0")
        assert sub.image_size == scene.cams[cid].size
        for r in sub.perches:
            p, ok = scene.cams[cid].project([scene.perches[r.antenna].mean(axis=0)])
            assert ok[0] and cv2.pointPolygonTest(np.float32(r.points), tuple(map(float, p[0])), False) >= 0
        if sub.sync is not None:
            p, _ = scene.cams[cid].project([scene.led])
            x0, y0, x1, y1 = sub.sync.bbox()
            assert x0 <= p[0, 0] <= x1 and y0 <= p[0, 1] <= y1
    assert rois_in_camera(rs, "cam0", scene.cams, "cam0") is rs  # the ROI camera: as drawn


def test_a_box_covers_a_bird_standing_on_its_place(cage):
    world, scene = cage
    flat, box = placed(world, scene, box=0.0), placed(world, scene, box=130.0)
    cam1 = scene.cams["cam1"]
    r_flat = next(r for r in rois_in_camera(flat, "cam1", scene.cams, "cam0").perches if r.antenna == 4)
    r_box = next(r for r in rois_in_camera(box, "cam1", scene.cams, "cam0").perches if r.antenna == 4)
    bird = scene.perches[4].mean(axis=0) + (0, 0, 45.0)  # a pigeon's body on bar 4
    p, _ = cam1.project([bird])
    assert cv2.pointPolygonTest(np.float32(r_box.points), tuple(map(float, p[0])), False) >= 0
    assert cv2.contourArea(np.float32(r_box.points)) > 1.5 * cv2.contourArea(np.float32(r_flat.points))


def test_marking_one_spot_in_two_cameras_gives_its_height(cage):
    _, scene = cage
    rng = np.random.default_rng(0)
    c0, c1 = scene.cams["cam0"], scene.cams["cam1"]
    for k, top in scene.perches.items():
        X = top[0]  # a corner of the bar
        pa, _ = c0.project([X])
        pb, ok = c1.project([X])
        assert ok[0]
        Xt, gap = triangulate(c0, pa[0] + rng.normal(0, 0.5, 2), c1, pb[0] + rng.normal(0, 0.5, 2))
        assert abs(Xt[2] - X[2]) < 5 and gap < 3
        other = scene.perches[k % 4 + 1][2]  # a different spot in the second camera: the rays do not meet
        pc, _ = c1.project([other])
        assert triangulate(c0, pa[0], c1, pc[0])[1] > MAX_RAY_GAP_MM


def test_rois_json_keeps_the_3d_places_and_reads_old_files(tmp_path, cage):
    world, scene = cage
    rs = RoiSet([place_3d(r, scene.cams["cam0"]) for r in placed(world, scene).rois], world.image_size)
    rs.save(tmp_path / "rois.json")
    back = RoiSet.load(tmp_path / "rois.json")
    for a, b in zip(rs.rois, back.rois):
        assert (a.plane_z_mm, a.box_mm) == (b.plane_z_mm, b.box_mm)
        assert np.allclose(a.points3d, b.points3d)
    old = {"image_size": [640, 480], "rois": [{"name": "P1", "points": [[1, 2], [30, 2], [30, 9]], "antenna": 1,
                                                "kind": "perch"}]}
    (tmp_path / "old.json").write_text(json.dumps(old), encoding="utf-8")
    r = RoiSet.load(tmp_path / "old.json").rois[0]
    assert (r.plane_z_mm, r.box_mm, r.points3d) == (None, 0.0, None)
    assert place_3d(r, scene.cams["cam0"]) is r and outline_in(r, scene.cams["cam1"]) is None


def test_what_3d_places_need(cage):
    world, scene = cage
    rs = world.roi_set(ANTENNAS)
    floor = {"frame": "floor"}
    assert problem(rs, scene.cams, floor, "cam0") is None
    assert "no lens calibration" in problem(rs, {}, floor, "cam0")
    assert "floor origin" in problem(rs, scene.cams, {"frame": "cam0"}, "cam0")
    assert "drawn on a 320×240" in problem(RoiSet(rs.rois, (320, 240)), scene.cams, floor, "cam0")
    far = CameraModel("x", scene.cams["cam0"].K, scene.cams["cam0"].dist, scene.cams["cam0"].T, (640, 480))
    r = Roi("high", [(100, 100), (120, 100), (120, 120)], 1, plane_z_mm=5000.0)  # above the camera: never reached
    assert place_3d(r, far).points3d is None


def test_session_keeps_the_calibration_and_shows_places_in_other_cameras(tmp_path, cage):
    from mavric.config import TagMap, load_rig
    from mavric.session import Session

    world, scene = cage
    rig = load_rig()
    cal = tmp_path / "calibration.yaml"
    save_truth(cal, scene)
    rig["paths"] = {**rig["paths"], "calibration_file": str(cal)}
    rs = placed(world, scene)
    sess = Session.create(tmp_path / "sessions", "t", rig, TagMap(), rs, {"utc_ns": time.time_ns(), "perf_s": 0.0})
    for c in ("cam0", "cam1"):
        sess.add_camera({"id": c, "rois_camera": c == "cam0", "video_file": f"{c}.mp4"})
    assert (sess.path / "calibration.yaml").exists()
    assert sess.roi_camera == "cam0" and sess.rois_in("cam0").rois == sess.rois.rois
    sub = sess.rois_in("cam1")
    assert sub.image_size == scene.cams["cam1"].size and {r.name for r in sub.perches} <= set(ANTENNAS.values())
    assert len(sub.perches) >= 2


@pytest.mark.skipif(os.environ.get("MAVRIC_SKIP_GUI") == "1", reason="GUI tests disabled")
def test_places_tab_and_rois_in_every_camera(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import GRID, AcquireWindow
    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.gui.tips import missing_tips

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    rig["cameras"] = [{"id": c, "backend": "synthetic", "fps": 30} for c in ("cam0", "cam1")]
    ctl = simulation_controller(rig, seed=1)
    scene = ctl.world.scene
    assert scene is not None and ctl.rig["paths"]["rois_file"].endswith("_sim3d_rois.json")
    save_truth(ctl.rig["paths"]["calibration_file"], scene)
    win = AcquireWindow(ctl, sim=True)
    for c in rig["cameras"]:
        ctl.connect_camera(c)
    try:
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end and any(th.latest is None for th in ctl.cams.values()):
            app.processEvents()
        dlg = CalibrationDialog(win)
        dlg.tabs.setCurrentIndex(2)
        tab = dlg.places
        assert "✓" in tab.ready_label.text() and tab.table.rowCount() == 5
        assert missing_tips(dlg) == []
        # mark P1 in two cameras: a corner of its bar, clicked in cam0 then in cam1
        tab.table.selectRow(0)
        tab._toggle_mark()
        X = scene.perches[1][0]
        p0, _ = scene.cams["cam0"].project([X])
        p1, _ = scene.cams["cam1"].project([X])
        tab._clicked("cam0", *p0[0])
        assert tab.views["cam1"].click_mode and tab.views["cam1"]._guides
        tab._clicked("cam1", *(p1[0] + 40))  # not the same spot
        assert tab.mark is not None and "do not meet" in tab.status.text()
        tab._clicked("cam1", *p1[0])
        assert tab.mark is None and abs(tab.table.cellWidget(0, 1).value() - X[2]) <= 2
        tab._use_truth()
        for _ in range(5):
            tab.tick()
            app.processEvents()
        assert "place(s) in view" in tab.captions["cam1"].text() and "simulated" in tab.places_label.text()
        tab._save()
        saved = RoiSet.load(ctl.rig["paths"]["rois_file"])
        assert all(r.points3d for r in saved.rois)
        dlg.close()
        # the acquire window: cam1's live view and the grid show the places
        win._refresh_views()
        win.view_combo.setCurrentIndex(win.view_combo.findData("cam1"))
        assert set(win.video.rois) and not win.cb_edit.isEnabled()
        win.view_combo.setCurrentIndex(win.view_combo.findData(GRID))
        assert all(win.grid.tiles[c]["rois"] for c in ("cam0", "cam1"))
        win.view_combo.setCurrentIndex(win.view_combo.findData("cam0"))
        assert win.current_rois().rois[0].plane_z_mm is not None
        # a recording keeps the calibration and finds the LED in cam1 too
        ctl.connect_rfid(ctl._sim_source)
        sess = ctl.start_recording("places")
        time.sleep(2.0)
        ctl.stop_recording()
        assert (sess.path / "calibration.yaml").exists()
        assert all(c["led_bbox"] for c in sess.meta["cameras"])
        assert sess.rois_in("cam1").perches
    finally:
        win.close()
    # review and render the other camera, with the places as it sees them
    from mavric.analysis.pipeline import prepare
    from mavric.analysis.render import render_annotated
    from mavric.gui.review_window import ReviewWindow, StartDialog
    from mavric.session import Session

    sess = Session.open(sess.path)
    dlg = StartDialog(sess, "T")
    assert dlg.cam_combo.count() == 2 and dlg.camera() == "cam0"
    dlg.close()
    rw = ReviewWindow(sess, "T", camera="cam1")
    assert rw.camera == "cam1" and set(rw.video.rois) == {r.name for r in sess.rois_in("cam1").rois}
    rw.dirty = False
    rw.close()
    out = render_annotated(prepare(sess, cam_id="cam1"), audio="none", spectrogram=False)
    assert out.name == "annotated_cam1.mp4" and out.stat().st_size > 1000
