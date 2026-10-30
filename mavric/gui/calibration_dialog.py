"""Calibrate cameras: each camera's lens, then all cameras' positions, from live views of the ChArUco board; then
the places in 3D."""

from __future__ import annotations

import time

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..config import resolve_path
from ..video.calibration import (
    MIN_COVERAGE,
    MIN_TILTED,
    MIN_VIEWS,
    BoardDetector,
    BoardSpec,
    ViewCollector,
    calibrate_lens,
    draw_coverage,
    draw_detection,
    load_calibrations,
    save_lens,
)
from ..video.extrinsics import MIN_CORNERS, SimulatedRig, Snapshot, SnapshotCollector, plot_rig, save_poses, \
    solve_rig
from .common import VideoView, Worker, message

HOW_TO = (
    "<b>How</b>: hold the board still in front of the camera, a different place and angle each time. A view is "
    "captured when the board has been still for a moment in a new position. Fill the image: centre, edges and "
    f"corners (green cells), near and far, and turn the board 20–40° left/right and up/down. {MIN_VIEWS}–25 views "
    "are enough. Then <i>Calibrate</i>, check the result, and <i>Save</i>.")

HOW_TO_POSITIONS = (
    "<b>How</b>: calibrate every camera's lens first (tab 1). Then hold the board still where two or more cameras "
    "see it, in many places and angles; a snapshot is captured when it has been still in every camera that sees it. "
    "Make every camera share snapshots with another one (the counts below). Then lay the board flat on the floor where "
    "a camera sees it and press <i>Board on the floor (origin)</i>: its top-left outer corner becomes the origin, "
    "z up. Then <i>Compute positions</i> and <i>Save positions</i>.")

RESULT_LINES = {
    "captured": "Captured",
    "moving": "Hold the board still",
    "seen": "Move the board to a new position or angle",
    "few": "Show more of the board",
    "misplaced": "A sticker is in the wrong square or turned (red): fix it first",
}


class CalibrationDialog(QtWidgets.QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ctl = win.ctl
        self.spec = BoardSpec.from_rig(self.ctl.rig)
        self.path = resolve_path(self.ctl.rig["paths"].get("calibration_file", "config/calibration.yaml"))
        self.detector = BoardDetector(self.spec)
        self.collector = ViewCollector(self.spec)
        self.cal = None
        self.maps = None
        self.sim_rig: SimulatedRig | None = None
        self.sim_camera = False
        self.snaps = SnapshotCollector(self.spec)
        self.origin: Snapshot | None = None
        self.solution = None
        self._capture_snap = False
        self._last_grabs: dict[str, int] = {}
        self._capture = False
        self._last_grab = -1
        self._last_t = 0.0
        self._t0 = time.monotonic()
        self._workers: list[Worker] = []
        self._size: tuple[int, int] | None = None
        self.setWindowTitle("Calibrate cameras")
        self.resize(1280, 780)

        outer = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs)
        lens_tab = QtWidgets.QWidget()
        self.tabs.addTab(lens_tab, "1 · Lens (each camera)")
        lay = QtWidgets.QHBoxLayout(lens_tab)
        self.view = VideoView()
        lay.addWidget(self.view, 1)
        side = QtWidgets.QWidget()
        side.setFixedWidth(390)
        v = QtWidgets.QVBoxLayout(side)
        lay.addWidget(side)

        g = QtWidgets.QGroupBox("Camera")
        f = QtWidgets.QVBoxLayout(g)
        self.cam_combo = QtWidgets.QComboBox()
        for cid in self.ctl.cams:
            self.cam_combo.addItem(cid, cid)
        self.cam_combo.currentIndexChanged.connect(self._camera_changed)
        f.addWidget(self.cam_combo)
        self.saved_label = QtWidgets.QLabel("")
        self.saved_label.setWordWrap(True)
        f.addWidget(self.saved_label)
        self.cb_sim = QtWidgets.QCheckBox("Simulated board")
        self.cb_sim.setChecked(True)
        f.addWidget(self.cb_sim)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Board")
        f = QtWidgets.QVBoxLayout(g)
        probs = self.spec.problems()
        lab = QtWidgets.QLabel(self.spec.describe() + (f"<br><b>Problem:</b> {'; '.join(probs)}" if probs else "")
                               + "<br><small>calibration: board: in config/rig.yaml</small>")
        lab.setWordWrap(True)
        f.addWidget(lab)
        b = QtWidgets.QPushButton("Sticker sheet…")
        b.clicked.connect(self._sheet)
        f.addWidget(b)
        self.board_label = QtWidgets.QLabel("")
        self.board_label.setWordWrap(True)
        f.addWidget(self.board_label)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Views")
        f = QtWidgets.QGridLayout(g)
        self.cb_auto = QtWidgets.QCheckBox("Capture automatically")
        self.cb_auto.setChecked(True)
        f.addWidget(self.cb_auto, 0, 0, 1, 2)
        b = QtWidgets.QPushButton("Capture now (Space)")
        b.clicked.connect(self._capture_now)
        f.addWidget(b, 1, 0)
        b = QtWidgets.QPushButton("Clear views")
        b.clicked.connect(self._clear)
        f.addWidget(b, 1, 1)
        self.views_label = QtWidgets.QLabel("")
        self.views_label.setWordWrap(True)
        f.addWidget(self.views_label, 2, 0, 1, 2)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Lens")
        f = QtWidgets.QGridLayout(g)
        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.addItem("standard (k1 k2 p1 p2 k3)", "standard")
        self.model_combo.addItem("rational (wide-angle lenses)", "rational")
        f.addWidget(self.model_combo, 0, 0, 1, 2)
        self.b_cal = QtWidgets.QPushButton("Calibrate")
        self.b_cal.clicked.connect(self._calibrate)
        f.addWidget(self.b_cal, 1, 0)
        self.b_save = QtWidgets.QPushButton("Save")
        self.b_save.clicked.connect(self._save)
        self.b_save.setEnabled(False)
        f.addWidget(self.b_save, 1, 1)
        self.cb_undist = QtWidgets.QCheckBox("Show straightened image")
        self.cb_undist.setEnabled(False)
        f.addWidget(self.cb_undist, 2, 0, 1, 2)
        self.result_label = QtWidgets.QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.result_label, 3, 0, 1, 2)
        v.addWidget(g)

        how = QtWidgets.QLabel(HOW_TO)
        how.setWordWrap(True)
        how.setStyleSheet("color: #52514e")
        v.addWidget(how)
        v.addStretch(1)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600")
        v.addWidget(self.status)

        self._build_positions_tab()
        from .places_tab import PlacesTab

        self.places = PlacesTab(self)
        self.tabs.addTab(self.places, "3 · Places in 3D")
        self.tabs.currentChanged.connect(lambda i: self.places.activate() if i == 2 else None)
        QtGui.QShortcut(QtGui.QKeySequence("Space"), self, activated=self._space)
        from .tips import CALIBRATE, CALIBRATE_ATTR, apply_tips

        apply_tips(self, CALIBRATE, CALIBRATE_ATTR, self)
        self._camera_changed()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # ------------------------------------------------------------------ camera
    @property
    def cid(self) -> str | None:
        return self.cam_combo.currentData()

    def _backend(self):
        th = self.ctl.cams.get(self.cid) if self.cid else None
        return th.backend if th is not None else None

    def _camera_changed(self, *_) -> None:
        self._clear()
        self._size = None
        be = self._backend()
        self.sim_camera = be is not None and getattr(be, "kind", "") == "synthetic"
        self.cb_sim.setVisible(self.sim_camera)
        self._show_saved()

    def _show_saved(self) -> None:
        if not self.cid:
            self.saved_label.setText("Connect a camera in the acquire window first.")
            return
        c = load_calibrations(self.path).get(self.cid)
        if c is None:
            self.saved_label.setText(f"{self.cid}: no lens calibration saved yet.")
            return
        ok, _ = c.verdict()
        txt = (f"Saved: {c.rms_px:.2f} px, {c.n_views} views, {c.date[:10]} ({'PASS' if ok else 'FAIL'})")
        if self._size and tuple(c.image_size) != self._size:
            txt += (f"<br><b>The image is now {self._size[0]}×{self._size[1]}, the calibration was made at "
                    f"{c.image_size[0]}×{c.image_size[1]}: calibrate again.</b>")
        self.saved_label.setText(txt)

    def _frame(self) -> np.ndarray | None:
        th = self.ctl.cams.get(self.cid) if self.cid else None
        if th is None or th.latest is None or th.latest.grab_idx == self._last_grab:
            return None
        self._last_grab = th.latest.grab_idx
        img = th.latest.image
        if self.sim_camera and self.cb_sim.isChecked():
            img = self._rig().feeds[self.cid].frame(time.monotonic() - self._t0, background=img)
        return img

    def _rig(self) -> SimulatedRig:
        """One simulated rig for all synthetic cameras: own lenses, own positions, one board."""
        scene = getattr(self.ctl.world, "scene", None)
        if self.sim_rig is None and scene is not None:  # the simulated cage's own cameras
            self.sim_rig = scene.rig
        if self.sim_rig is None:
            sizes = {}
            for cid, th in self.ctl.cams.items():
                img = th.latest.image if th.latest is not None else None
                sizes[cid] = (img.shape[1], img.shape[0]) if img is not None else (640, 480)
            self.sim_rig = SimulatedRig(self.spec, sizes)
        return self.sim_rig

    # ------------------------------------------------------------------ live
    def _tick(self) -> None:
        if self.tabs.currentIndex() == 1:
            self._tick_positions()
            return
        if self.tabs.currentIndex() == 2:
            self.places.tick()
            return
        if time.monotonic() - self._last_t < 0.066:  # detection at most ~15 times a second
            return
        img = self._frame()
        if img is None:
            if not self.cid or self.cid not in self.ctl.cams:
                self.status.setText("Camera not connected.")
            return
        self._last_t = time.monotonic()
        h, w = img.shape[:2]
        if self._size != (w, h):
            if self._size is not None and self.collector.views:
                self._clear()
                self.status.setText("The image size changed: the views were cleared.")
            self._size = (w, h)
            self._show_saved()
        if self.cb_undist.isChecked() and self.maps is not None:
            show = cv2.remap(img, *self.maps, cv2.INTER_LINEAR)
            self.view.set_image(show)
            self.status.setText("Straightened image: straight edges in the scene should look straight.")
            return
        det = self.detector.detect(img)
        res = self.collector.offer(det, auto=self.cb_auto.isChecked(), force=self._capture)
        if self._capture and res != "captured":
            self.status.setText("Not captured: " + RESULT_LINES.get(res, res).lower())
        self._capture = False
        _, hits = self.collector.coverage((w, h))
        show = draw_coverage(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img, hits)
        show = draw_detection(show, det, self.spec)
        if res == "captured":
            show = cv2.addWeighted(show, 0.6, np.full_like(show, 255), 0.4, 0)  # flash
        self.view.set_image(show)
        self.board_label.setText(
            f"Board check: {len(det.marker_ids)}/{self.spec.n_markers} markers, {det.n}/{self.spec.n_corners} corners"
            + (f"; <b style='color:#c0392b'>misplaced or turned: {', '.join(map(str, det.misplaced))}</b>"
               if det.misplaced else ""))
        if res != "few" or det.n:
            self.status.setText(RESULT_LINES.get(res, res) + (f" (view {len(self.collector.views)})"
                                                              if res == "captured" else ""))
        self._update_views()

    def _update_views(self) -> None:
        cov, _ = self.collector.coverage(self._size)
        n, nt = len(self.collector.views), self.collector.n_tilted

        def mark(ok):
            return "✓" if ok else "·"

        self.views_label.setText(
            f"{mark(n >= MIN_VIEWS)} {n} views (≥ {MIN_VIEWS})   {mark(cov >= MIN_COVERAGE)} coverage {cov:.0%} "
            f"(≥ {MIN_COVERAGE:.0%})   {mark(nt >= MIN_TILTED)} {nt} tilted (≥ {MIN_TILTED})")
        self.b_cal.setEnabled(n >= 4)

    def _capture_now(self) -> None:
        self._capture = True

    def _clear(self) -> None:
        self.collector.clear()
        self.cal = None
        self.maps = None
        self.cb_undist.setChecked(False)
        self.cb_undist.setEnabled(False)
        self.b_save.setEnabled(False)
        self.result_label.setText("")
        self._update_views()

    # ------------------------------------------------------------------ calibrate / save
    def _calibrate(self) -> None:
        views = list(self.collector.views)
        be = self._backend()
        serial = str((be.info() if be is not None else {}).get("serial", "") or "")
        w = Worker(calibrate_lens, views, self.spec, self.cid or "", self.model_combo.currentData(), serial)
        w.done.connect(self._calibrated)
        w.failed.connect(lambda e: message(self, "Calibration failed", e, error=True))
        self._workers.append(w)
        self.b_cal.setEnabled(False)
        self.result_label.setText("Calibrating …")
        w.start()

    def _calibrated(self, cal) -> None:
        self.cal = cal
        self.b_cal.setEnabled(True)
        self.b_save.setEnabled(True)
        self.maps = cal.undistort_maps()
        self.cb_undist.setEnabled(True)
        ok, why = cal.verdict()
        K = cal.K
        txt = (f"<b style='color:{'#1e7d32' if ok else '#c0392b'}'>{'PASS' if ok else 'FAIL'}</b>  "
               f"{cal.rms_px:.2f} px RMS from {cal.n_views} views"
               + (f" ({len(cal.rejected)} left out as outliers)" if cal.rejected else "")
               + f"<br>focal length {K[0, 0]:.1f} / {K[1, 1]:.1f} px (± {cal.std['fx']:.1f}), "
                 f"centre {K[0, 2]:.1f}, {K[1, 2]:.1f}"
               + "<br>distortion " + ", ".join(f"{d:+.3f}" for d in cal.dist[:5]))
        if self.sim_rig is not None and self.sim_camera and self.cb_sim.isChecked():
            T = self.sim_rig.lenses[cal.camera].K
            txt += (f"<br><i>simulated lens: focal {T[0, 0]:.1f} / {T[1, 1]:.1f}, centre {T[0, 2]:.1f}, "
                    f"{T[1, 2]:.1f}</i>")
        if why:
            txt += "<br>" + "<br>".join(f"• {x}" for x in why)
        self.result_label.setText(txt)

    def _save(self) -> None:
        if self.cal is None:
            return
        save_lens(self.path, self.cal, self.collector.views)
        self._show_saved()
        self.win.statusBar().showMessage(f"Lens calibration of {self.cal.camera} saved to {self.path}", 8000)
        if hasattr(self.win, "update_calibration_label"):
            self.win.update_calibration_label()

    def _sheet(self) -> None:
        from ..video.sheet import default_sheet_path, make_sheet

        default = default_sheet_path(self.spec, self.path.parent / "calibration")
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save the sticker sheet", str(default), "PDF (*.pdf)")
        if not p:
            return
        try:
            out = make_sheet(self.spec, p)
        except Exception as e:  # board settings that make no board
            message(self, "Sticker sheet", f"Could not make the sheet: {e}", error=True)
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(out)))

    # ------------------------------------------------------------------ tab 2: camera positions
    def _build_positions_tab(self) -> None:
        tab = QtWidgets.QWidget()
        self.tabs.addTab(tab, "2 · Camera positions (all cameras)")
        lay = QtWidgets.QHBoxLayout(tab)
        grid = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(grid)
        self.pos_views: dict[str, VideoView] = {}
        self.pos_captions: dict[str, QtWidgets.QLabel] = {}
        for k, cid in enumerate(self.ctl.cams):
            box = QtWidgets.QVBoxLayout()
            cap = QtWidgets.QLabel(cid)
            box.addWidget(cap)
            vv = VideoView()
            box.addWidget(vv, 1)
            g.addLayout(box, k // 2, k % 2)
            self.pos_views[cid], self.pos_captions[cid] = vv, cap
        n = len(self.ctl.cams)
        self.pos_plot = QtWidgets.QLabel("")  # the cameras seen from above and from the side, after computing
        self.pos_plot.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.pos_plot.setMinimumSize(200, 150)
        g.addWidget(self.pos_plot, n // 2, n % 2)
        lay.addWidget(grid, 1)
        side = QtWidgets.QWidget()
        side.setFixedWidth(390)
        v = QtWidgets.QVBoxLayout(side)
        lay.addWidget(side)

        b = QtWidgets.QGroupBox("Lenses")
        f = QtWidgets.QVBoxLayout(b)
        self.lens_status = QtWidgets.QLabel("")
        self.lens_status.setWordWrap(True)
        f.addWidget(self.lens_status)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Snapshots")
        f = QtWidgets.QGridLayout(b)
        self.cb_auto_snap = QtWidgets.QCheckBox("Capture snapshots automatically")
        self.cb_auto_snap.setChecked(True)
        f.addWidget(self.cb_auto_snap, 0, 0, 1, 2)
        bt = QtWidgets.QPushButton("Capture snapshot (Space)")
        bt.clicked.connect(self._capture_snapshot)
        f.addWidget(bt, 1, 0)
        bt = QtWidgets.QPushButton("Clear snapshots")
        bt.clicked.connect(self._clear_snapshots)
        f.addWidget(bt, 1, 1)
        self.snap_label = QtWidgets.QLabel("")
        self.snap_label.setWordWrap(True)
        f.addWidget(self.snap_label, 2, 0, 1, 2)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("World origin")
        f = QtWidgets.QVBoxLayout(b)
        bt = QtWidgets.QPushButton("Board on the floor (origin)")
        bt.clicked.connect(self._set_origin)
        f.addWidget(bt)
        self.origin_label = QtWidgets.QLabel("No origin yet: positions will be in the first camera's frame.")
        self.origin_label.setWordWrap(True)
        f.addWidget(self.origin_label)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Positions")
        f = QtWidgets.QGridLayout(b)
        self.b_solve = QtWidgets.QPushButton("Compute positions")
        self.b_solve.clicked.connect(self._solve)
        f.addWidget(self.b_solve, 0, 0)
        self.b_save_pos = QtWidgets.QPushButton("Save positions")
        self.b_save_pos.clicked.connect(self._save_positions)
        self.b_save_pos.setEnabled(False)
        f.addWidget(self.b_save_pos, 0, 1)
        self.pos_result = QtWidgets.QLabel("")
        self.pos_result.setWordWrap(True)
        self.pos_result.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.pos_result, 1, 0, 1, 2)
        v.addWidget(b)

        how = QtWidgets.QLabel(HOW_TO_POSITIONS)
        how.setWordWrap(True)
        how.setStyleSheet("color: #52514e")
        v.addWidget(how)
        v.addStretch(1)
        self.pos_status = QtWidgets.QLabel("")
        self.pos_status.setWordWrap(True)
        self.pos_status.setStyleSheet("font-weight: 600")
        v.addWidget(self.pos_status)
        self.tabs.currentChanged.connect(lambda i: self._lens_status() if i == 1 else None)
        self._lens_status()

    def _lenses(self) -> dict:
        cals = load_calibrations(self.path)
        out = {}
        for cid in self.ctl.cams:
            c = cals.get(cid)
            th = self.ctl.cams[cid]
            size = (th.latest.image.shape[1], th.latest.image.shape[0]) if th.latest is not None else None
            if c is not None and (size is None or tuple(c.image_size) == size):
                out[cid] = c
        return out

    def _lens_status(self) -> None:
        cals = load_calibrations(self.path)
        lenses = self._lenses()
        parts = []
        for cid in self.ctl.cams:
            if cid in lenses:
                parts.append(f"✓ {cid}: {lenses[cid].rms_px:.2f} px")
            elif cid in cals:
                parts.append(f"✗ {cid}: calibrated at another image size; calibrate its lens again (tab 1)")
            else:
                parts.append(f"✗ {cid}: no lens calibration yet (tab 1)")
        self.lens_status.setText("<br>".join(parts) or "Connect the cameras in the acquire window first.")

    def _snap_frame(self, cid: str):
        th = self.ctl.cams.get(cid)
        if th is None or th.latest is None:
            return None
        img = th.latest.image
        if self._all_synthetic() and self.cb_sim.isChecked():
            img = self._rig().frame(cid, time.monotonic() - self._t0, background=img)
        return img

    def _all_synthetic(self) -> bool:
        return bool(self.ctl.cams) and all(getattr(th.backend, "kind", "") == "synthetic"
                                           for th in self.ctl.cams.values())

    def _tick_positions(self) -> None:
        if time.monotonic() - self._last_t < 0.1:
            return
        self._last_t = time.monotonic()
        dets = {}
        for cid, vv in self.pos_views.items():
            img = self._snap_frame(cid)
            if img is None:
                self.pos_captions[cid].setText(f"{cid}: not connected")
                continue
            d = self.detector.detect(img)
            dets[cid] = d
            vv.set_image(draw_detection(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img, d,
                                        self.spec))
            self.pos_captions[cid].setText(f"{cid}: {d.n} corners" + (" ✓" if d.n >= MIN_CORNERS else "")
                                           + (f", misplaced {d.misplaced}" if d.misplaced else ""))
        lenses = self._lenses()
        usable = {c: d for c, d in dets.items() if c in lenses}
        res = self.snaps.offer(usable, auto=self.cb_auto_snap.isChecked(), force=self._capture_snap)
        if self._capture_snap and res != "captured":
            self.pos_status.setText("Not captured: " + {"few": "fewer than two calibrated cameras see the board"}.get(
                res, RESULT_LINES.get(res, res).lower()))
        elif res == "captured":
            self.pos_status.setText(f"Captured snapshot {len(self.snaps.snapshots)}")
        elif res == "few":
            self.pos_status.setText("Show the board to two calibrated cameras or more")
        else:
            self.pos_status.setText(RESULT_LINES.get(res, res))
        self._capture_snap = False
        pairs = self.snaps.pair_counts()
        self.snap_label.setText(f"{len(self.snaps.snapshots)} snapshots" + ("".join(
            f"<br>{'✓' if n >= 5 else '·'} {a} + {b}: {n}" for (a, b), n in sorted(pairs.items()))))

    def _capture_snapshot(self) -> None:
        self._capture_snap = True

    def _clear_snapshots(self) -> None:
        self.snaps.clear()
        self.solution = None
        self.b_save_pos.setEnabled(False)
        self.pos_result.setText("")
        self.pos_plot.clear()

    def _set_origin(self) -> None:
        lenses = self._lenses()
        dets = {}
        for cid in self.ctl.cams:
            if cid not in lenses:
                continue
            th = self.ctl.cams[cid]
            if th.latest is None:
                continue
            img = th.latest.image
            if self._all_synthetic() and self.cb_sim.isChecked():
                img = self._rig().floor_view(cid, background=img)  # the simulated board, lying on the floor
            dets[cid] = self.detector.detect(img)
        snap = Snapshot(dets)
        if not snap.cameras:
            self.origin_label.setText("<b>No calibrated camera sees the whole board well enough</b> "
                                      f"(≥ {MIN_CORNERS} corners, no misplaced sticker). Nothing changed.")
            return
        self.origin = snap
        self.origin_label.setText(f"Origin set, seen by {', '.join(snap.cameras)}. Keep the cameras where they are.")

    def _solve(self) -> None:
        lenses = self._lenses()
        ref = next(iter(self.ctl.cams), None)
        if ref is None or ref not in lenses:
            message(self, "Camera positions", f"Calibrate the lens of {ref or 'the first camera'} first (tab 1).")
            return
        if not self.snaps.snapshots:
            message(self, "Camera positions", "Capture snapshots first: the board seen by two cameras or more.")
            return
        w = Worker(solve_rig, self.spec, list(self.snaps.snapshots), lenses, ref, self.origin)
        w.done.connect(self._solved)
        w.failed.connect(lambda e: message(self, "Camera positions failed", e, error=True))
        self._workers.append(w)
        self.b_solve.setEnabled(False)
        self.pos_result.setText("Computing …")
        w.start()

    def _solved(self, sol) -> None:
        self.solution = sol
        self.b_solve.setEnabled(True)
        self.b_save_pos.setEnabled(bool(sol.poses))
        ok, why = sol.verdict()
        txt = sol.summary().replace("\n", "<br>").replace("PASS", "<b style='color:#1e7d32'>PASS</b>").replace(
            "FAIL", "<b style='color:#c0392b'>FAIL</b>")
        if self._all_synthetic() and self.cb_sim.isChecked() and self.sim_rig is not None and sol.world == "floor":
            from ..video.extrinsics import inv_T

            err = max(float(np.linalg.norm(p.centre - inv_T(self.sim_rig.T[c])[:3, 3])) for c, p in sol.poses.items())
            txt += f"<br><i>simulated rig: every camera within {err:.1f} mm of its true position</i>"
        if why:
            txt += "<br>" + "<br>".join(f"• {x}" for x in why)
        self.pos_result.setText(txt)
        img = plot_rig(sol, self.spec, width_px=max(420, min(900, self.pos_plot.width())))
        qi = QtGui.QImage(img.data, img.shape[1], img.shape[0], img.strides[0], QtGui.QImage.Format.Format_RGB888)
        self.pos_plot.setPixmap(QtGui.QPixmap.fromImage(qi.copy()))

    def _save_positions(self) -> None:
        if self.solution is None:
            return
        save_poses(self.path, self.solution)
        self.win.statusBar().showMessage(f"Camera positions saved to {self.path}", 8000)
        if hasattr(self.win, "update_calibration_label"):
            self.win.update_calibration_label()

    def _space(self) -> None:
        if self.tabs.currentIndex() == 1:
            self._capture_snapshot()
        elif self.tabs.currentIndex() == 0:
            self._capture_now()

    def closeEvent(self, ev) -> None:
        self.timer.stop()
        super().closeEvent(ev)
