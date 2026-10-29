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
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml"),
                    "calibration_file": str(tmp_path / "calibration.yaml")}
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
        assert load_calibrations(tmp_path / "calibration.yaml")["cam0"].n_views == cal.n_views
        assert "cam0 " in win.cal_label.text()
        dlg.close()
    finally:
        ctl.shutdown()
        ctl.disconnect_camera("cam0")
        win.close()
