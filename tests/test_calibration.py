"""Camera calibration (C1): the ChArUco board, sticker sheet, detection, view collection, lens calibration, files."""

import os
import re
import time

import cv2
import numpy as np
import pytest

from mavric.video.calibration import (
    BoardDetector,
    BoardSpec,
    SimulatedBoardFeed,
    SimulatedLens,
    ViewCollector,
    calibrate_lens,
    load_calibrations,
    load_views,
    save_lens,
)
from mavric.video.sheet import cut_size_mm, make_sheet, square_name, sticker_layout

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SPEC = BoardSpec()


def test_board_from_rig_and_layout():
    from mavric.config import load_rig

    spec = BoardSpec.from_rig(load_rig())
    assert spec == SPEC and spec.problems() == []
    assert (spec.n_markers, spec.n_corners) == (40, 63)
    assert spec.marker_square[0] == (1, 0) and square_name(*spec.marker_square[0]) == "B1"
    assert all((c + r) % 2 == 1 for c, r in spec.marker_square.values())  # markers only in white squares
    assert sorted(spec.corner_markers(0)) == [0, 4]  # the corner between B1, A2 and their black neighbours
    assert BoardSpec(marker_mm=40).problems() and BoardSpec(squares_x=12, squares_y=12).problems()
    assert cut_size_mm(SPEC) == 37.0


def test_stickers_rebuild_the_opencv_board():
    """Stickers placed as the sheet says (upright, centred in their square) give exactly OpenCV's board."""
    ppm = 8
    s, m = int(SPEC.square_mm * ppm), int(SPEC.marker_mm * ppm)
    img = np.full((SPEC.squares_y * s, SPEC.squares_x * s), 255, np.uint8)
    for r in range(SPEC.squares_y):
        for c in range(SPEC.squares_x):
            if (c + r) % 2 == 0:
                img[r * s:(r + 1) * s, c * s:(c + 1) * s] = 0  # the printed checkerboard: black top left
    for i, c, r in sticker_layout(SPEC):
        cells = SPEC.marker_image(i)
        sticker = np.where(cv2.resize(cells, (m, m), interpolation=cv2.INTER_NEAREST) > 0, 0, 255).astype(np.uint8)
        y, x = r * s + (s - m) // 2, c * s + (s - m) // 2
        img[y:y + m, x:x + m] = sticker
    ref = SPEC.board.generateImage((img.shape[1], img.shape[0]), marginSize=0, borderBits=1)
    assert (img == ref).mean() > 0.995
    det = BoardDetector(SPEC).detect(cv2.copyMakeBorder(img, 60, 60, 60, 60, cv2.BORDER_CONSTANT, value=255))
    assert len(det.marker_ids) == 40 and det.n == 63 and det.misplaced == []


def test_sticker_sheet_pdf(tmp_path):
    p = make_sheet(SPEC, tmp_path / "s.pdf")
    data = p.read_bytes()
    assert data.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page[^s]", data)) == 3
    with pytest.raises(ValueError):
        make_sheet(BoardSpec(marker_mm=50), tmp_path / "bad.pdf")


def _views(lens, n, seed=5):
    det = BoardDetector(SPEC)
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n):
        rv, tv = lens.random_pose(rng)
        d = det.detect(lens.render(rv, tv))
        out.append((d, lens.project(rv, tv)))
    return out


def test_lens_calibration_recovers_a_simulated_lens():
    lens = SimulatedLens(SPEC, (1024, 768), seed=1)
    views = _views(lens, 20)
    errs = [np.linalg.norm(d.corners - truth[d.ids], axis=1) for d, truth in views]
    assert max(np.median(e) for e in errs) < 0.5 and np.median(np.concatenate(errs)) < 0.3  # a fraction of a pixel
    cal = calibrate_lens([d for d, _ in views], SPEC, "cam0")
    ok, why = cal.verdict()
    assert ok, why
    assert cal.rms_px < 0.4
    assert abs(cal.K[0, 0] / lens.K[0, 0] - 1) < 0.005 and abs(cal.K[1, 1] / lens.K[1, 1] - 1) < 0.005
    assert np.allclose(cal.K[:2, 2], lens.K[:2, 2], atol=3.0)
    # the fitted model reprojects the true corners: straightening works
    pts = np.column_stack([SPEC.corner_xy, np.zeros(SPEC.n_corners)])
    d, truth = views[0]
    o, i = SPEC.board.matchImagePoints(d.corners.reshape(-1, 1, 2), d.ids.reshape(-1, 1))
    _, rv, tv = cv2.solvePnP(o, i, cal.K, cal.dist)
    p, _ = cv2.projectPoints(pts, rv, tv, cal.K, cal.dist)
    assert np.median(np.linalg.norm(p.reshape(-1, 2) - truth, axis=1)) < 0.5


def test_misplaced_and_turned_stickers_are_found():
    lens = SimulatedLens(SPEC, (1280, 1024), seed=2)
    tex = lens.texture.copy()

    def box(i):
        xy = SPEC.marker_xy[i]
        x0, y0 = ((xy.min(axis=0) + lens.margin_mm) * lens.ppm).round().astype(int)
        x1, y1 = ((xy.max(axis=0) + lens.margin_mm) * lens.ppm).round().astype(int)
        return slice(y0, y1), slice(x0, x1)

    a, b, c = box(5), box(12), box(20)
    pa = tex[a].copy()
    tex[a] = tex[b]
    tex[b] = pa
    tex[c] = np.rot90(tex[c]).copy()
    lens.texture = tex
    rv, tv = lens.random_pose(np.random.default_rng(1), min_visible=0.95)
    d = BoardDetector(SPEC).detect(lens.render(rv, tv))
    assert d.misplaced == [5, 12, 20]
    assert d.n >= 50  # the rest of the board is still used ...
    assert np.abs(d.corners - lens.project(rv, tv)[d.ids]).max() < 1.0  # ... and right
    assert ViewCollector(SPEC).offer(d, force=True) == "misplaced"  # never captured


def test_view_collector_waits_for_a_still_board_in_a_new_place():
    lens = SimulatedLens(SPEC, (640, 480), seed=3)
    feed = SimulatedBoardFeed(lens, hold_s=1.6, move_s=0.6, seed=4)
    det = BoardDetector(SPEC)
    col = ViewCollector(SPEC)
    results = []
    for k in range(int(3 * 2.2 / 0.066)):  # three holds, ~15 frames a second
        t = k * 0.066
        res = col.offer(det.detect(feed.frame(t)))
        results.append((t, res))
    phase = [(t % 2.2) for t, _ in results]
    captured = [t for (t, r) in results if r == "captured"]
    assert 2 <= len(captured) <= 3
    assert all(p < 1.6 for p, (_, r) in zip(phase, results) if r == "captured")  # only while held still
    assert any(r == "seen" for _, r in results)  # the same pose again is not captured twice
    assert any(r == "moving" for _, r in results)


def test_save_and_load(tmp_path):
    lens = SimulatedLens(SPEC, (640, 480), seed=6)
    views = [d for d, _ in _views(lens, 8, seed=2)]
    cal = calibrate_lens(views, SPEC, "cam0", serial="123")
    path = tmp_path / "config" / "calibration.yaml"
    save_lens(path, cal, views)
    cal1 = calibrate_lens(views[:6], SPEC, "cam1")
    save_lens(path, cal1)
    got = load_calibrations(path)
    assert set(got) == {"cam0", "cam1"}  # saving one camera keeps the other
    assert np.allclose(got["cam0"].K, cal.K, atol=1e-5) and got["cam0"].serial == "123"
    assert np.allclose(got["cam0"].dist, cal.dist, atol=1e-7)
    back = load_views(path, "cam0")
    assert len(back) == len(views) and np.allclose(back[0].corners, views[0].corners)
    again = calibrate_lens(back, SPEC, "cam0")
    assert np.allclose(again.K, cal.K, rtol=1e-6)
    assert "calibration" in path.read_text(encoding="utf-8").splitlines()[0].lower()


def test_calibration_dialog_on_the_simulated_camera(tmp_path):
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.gui.tips import missing_tips

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    ctl.connect_camera(ctl.rig["cameras"][0])
    try:
        dlg = CalibrationDialog(win)
        assert missing_tips(dlg) == []
        assert dlg.cb_sim.isVisibleTo(dlg)  # the simulated camera gets a simulated board
        t_end = time.monotonic() + 12
        while time.monotonic() < t_end and len(dlg.collector.views) < 4:
            app.processEvents()
            time.sleep(0.01)
        assert len(dlg.collector.views) >= 4
        cal = calibrate_lens(dlg.collector.views, dlg.spec, dlg.cid)
        dlg._calibrated(cal)
        assert "px RMS" in dlg.result_label.text() and dlg.b_save.isEnabled()
        dlg._save()
        assert load_calibrations(ctl.rig["paths"]["calibration_file"])["cam0"].n_views == cal.n_views
        assert "cam0 " in win.cal_label.text()
        dlg.close()
    finally:
        ctl.shutdown()
        ctl.disconnect_camera("cam0")
        win.close()


# --------------------------------------------------------------------------- camera positions (C2)


@pytest.fixture(scope="module")
def sim_rig():
    from mavric.video.extrinsics import SimulatedRig, Snapshot

    rig = SimulatedRig(SPEC, {c: (640, 480) for c in ("cam0", "cam1", "cam2")}, seed=1)
    det = BoardDetector(SPEC)
    snaps = []
    for _ in range(18):
        Tb = rig.random_board()
        snaps.append(Snapshot({c: det.detect(rig.render(c, Tb)) for c in rig.T}))
    origin = Snapshot({c: det.detect(rig.floor_view(c)) for c in rig.T})
    lenses = {c: rig.lens_truth(c) for c in rig.T}
    return rig, snaps, origin, lenses


def test_camera_positions_of_a_simulated_rig(sim_rig):
    from mavric.video.extrinsics import angle_deg, inv_T, solve_rig

    rig, snaps, origin, lenses = sim_rig
    sol = solve_rig(SPEC, snaps, lenses, "cam0", origin)
    assert sol.world == "floor" and set(sol.poses) == {"cam0", "cam1", "cam2"} and not sol.unplaced
    for c, p in sol.poses.items():
        assert np.linalg.norm(p.centre - inv_T(rig.T[c])[:3, 3]) < 2.0  # mm
        assert angle_deg(p.T[:3, :3], rig.T[c][:3, :3]) < 0.2  # degrees
        assert p.rms_px < 1.5
    assert sol.err_3d_mm < 1.0
    assert sol.poses["cam0"].via == "" and sol.poses["cam2"].via in ("cam0", "cam1")
    # without an origin view the world is the first camera's frame
    own = solve_rig(SPEC, snaps, lenses, "cam0")
    assert own.world == "cam0" and np.allclose(own.poses["cam0"].T, np.eye(4))
    d = np.linalg.norm(own.poses["cam1"].centre)  # the distance between the cameras does not depend on the frame
    assert abs(d - np.linalg.norm(sol.poses["cam1"].centre - sol.poses["cam0"].centre)) < 1.0


def test_triangulation_puts_a_point_where_it_is(sim_rig):
    from mavric.video.extrinsics import solve_rig, triangulate

    rig, snaps, origin, lenses = sim_rig
    sol = solve_rig(SPEC, snaps, lenses, "cam0", origin)
    X = np.array([[100.0, -200.0, 0.0], [250.0, -50.0, 300.0]])  # world points (mm)
    pts = {}
    for c in ("cam0", "cam1"):
        T = rig.T[c]
        p, _ = cv2.projectPoints(X, cv2.Rodrigues(T[:3, :3])[0], T[:3, 3], rig.lenses[c].K, rig.lenses[c].dist)
        pts[c] = p.reshape(-1, 2)
    got = triangulate(sol.poses, lenses, "cam0", "cam1", pts["cam0"], pts["cam1"])
    assert np.abs(got - X).max() < 2.0


def test_positions_saved_next_to_the_lenses(sim_rig, tmp_path):
    from mavric.video.extrinsics import load_poses, save_poses, solve_rig

    rig, snaps, origin, lenses = sim_rig
    path = tmp_path / "calibration.yaml"
    for lens in lenses.values():
        save_lens(path, lens)
    sol = solve_rig(SPEC, snaps, lenses, "cam0", origin)
    save_poses(path, sol)
    poses, world = load_poses(path)
    assert set(poses) == {"cam0", "cam1", "cam2"} and world["frame"] == "floor"
    assert np.allclose(poses["cam1"].T, sol.poses["cam1"].T, atol=1e-5)
    assert set(load_calibrations(path)) == {"cam0", "cam1", "cam2"}  # lenses kept
    two = solve_rig(SPEC, snaps, {c: lenses[c] for c in ("cam0", "cam1")}, "cam0", origin)
    save_poses(path, two)
    poses, _ = load_poses(path)
    assert set(poses) == {"cam0", "cam1"}  # an old position would not match the new world: removed


def test_positions_tab_on_simulated_cameras(tmp_path):
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.video.extrinsics import load_poses, solve_rig

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    rig["cameras"] = [{"id": c, "backend": "synthetic", "fps": 30} for c in ("cam0", "cam1")]
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    for c in rig["cameras"]:
        ctl.connect_camera(c)
    try:
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end and any(th.latest is None for th in ctl.cams.values()):
            app.processEvents()
        dlg = CalibrationDialog(win)
        for cid in ctl.cams:  # skip tab 1: the simulated lenses as they are
            save_lens(ctl.rig["paths"]["calibration_file"], dlg._rig().lens_truth(cid))
        dlg.tabs.setCurrentIndex(1)
        dlg._lens_status()
        assert dlg.lens_status.text().count("✓") == 2
        t_end = time.monotonic() + 20
        while time.monotonic() < t_end and len(dlg.snaps.snapshots) < 4:
            app.processEvents()
            time.sleep(0.01)
        assert len(dlg.snaps.snapshots) >= 4
        dlg._set_origin()
        assert dlg.origin is not None
        dlg._solved(solve_rig(dlg.spec, dlg.snaps.snapshots, dlg._lenses(), "cam0", dlg.origin))
        assert "PASS" in dlg.pos_result.text() or "FAIL" in dlg.pos_result.text()
        assert "of its true position" in dlg.pos_result.text() and dlg.pos_plot.pixmap() is not None
        dlg._save_positions()
        assert set(load_poses(ctl.rig["paths"]["calibration_file"])[0]) == {"cam0", "cam1"}
        assert "⌖" in win.cal_label.text()
        dlg.close()
    finally:
        for c in list(ctl.cams):
            ctl.disconnect_camera(c)
        ctl.shutdown()
        win.close()
