"""Calibrate cameras, tab 3: each place's height (and box) in 3D, checked live in every camera."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PySide6 import QtCore, QtWidgets

from ..config import RoiSet
from ..video.places import MAX_RAY_GAP_MM, centre_3d, epipolar_curve, place_3d, problem, rois_in_camera, \
    triangulate
from .common import GRAY, VideoView

HOW_TO_PLACES = (
    "<b>How</b>: each place is the surface drawn in the ROI camera (draw the outline of what the subjects stand on), "
    "lifted to its height above the floor origin. Type a measured height, or <i>Mark in two cameras</i>: click a spot "
    "of the surface in the ROI camera, then the same spot in another camera. Check that the outlines sit on the places "
    "in every camera. A box height makes the place the space above its surface, so it also covers a subject standing "
    "on it. Then <i>Save places</i>.")

SELECTED = "#eda100"
NONE_Z = -1.0  # the height spin box's "not set"


class PlacesTab(QtWidgets.QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg, self.win, self.ctl = dlg, dlg.win, dlg.ctl
        self.rs = RoiSet()
        self.cams: dict = {}
        self.world: dict = {}
        self.mark: dict | None = None  # two-camera marking in progress: {"name", "p0", "cam0"}
        self._dirty = True
        self._rows: list[str] = []
        self._last: dict[str, int] = {}
        lay = QtWidgets.QHBoxLayout(self)
        grid = QtWidgets.QWidget()
        g = QtWidgets.QGridLayout(grid)
        self.views: dict[str, VideoView] = {}
        self.captions: dict[str, QtWidgets.QLabel] = {}
        for k, cid in enumerate(self.ctl.cams):
            box = QtWidgets.QVBoxLayout()
            cap = QtWidgets.QLabel(cid)
            box.addWidget(cap)
            vv = VideoView()
            vv.clicked.connect(lambda x, y, c=cid: self._clicked(c, x, y))
            box.addWidget(vv, 1)
            g.addLayout(box, k // 2, k % 2)
            self.views[cid], self.captions[cid] = vv, cap
        lay.addWidget(grid, 1)
        side = QtWidgets.QWidget()
        side.setFixedWidth(390)
        v = QtWidgets.QVBoxLayout(side)
        lay.addWidget(side)

        b = QtWidgets.QGroupBox("Calibration")
        f = QtWidgets.QVBoxLayout(b)
        self.ready_label = QtWidgets.QLabel("")
        self.ready_label.setWordWrap(True)
        f.addWidget(self.ready_label)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Places")
        f = QtWidgets.QVBoxLayout(b)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Place", "Height (mm)", "Box (mm)", "Seen by"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setToolTip("The places drawn in the ROI camera: height above the floor origin and box height (mm), "
                              "and the other cameras that see them")
        self.table.itemSelectionChanged.connect(self._changed)
        f.addWidget(self.table)
        self.b_mark = QtWidgets.QPushButton("Mark in two cameras")
        self.b_mark.clicked.connect(self._toggle_mark)
        f.addWidget(self.b_mark)
        self.b_truth = QtWidgets.QPushButton("Use the true heights (simulation)")
        self.b_truth.clicked.connect(self._use_truth)
        self.b_truth.setVisible(self._scene() is not None)
        f.addWidget(self.b_truth)
        self.places_label = QtWidgets.QLabel("")
        self.places_label.setWordWrap(True)
        self.places_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.places_label)
        self.b_save = QtWidgets.QPushButton("Save places")
        self.b_save.clicked.connect(self._save)
        f.addWidget(self.b_save)
        v.addWidget(b)

        how = QtWidgets.QLabel(HOW_TO_PLACES)
        how.setWordWrap(True)
        how.setStyleSheet("color: #52514e")
        v.addWidget(how)
        v.addStretch(1)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600")
        v.addWidget(self.status)

    # ------------------------------------------------------------------ state
    def _scene(self):
        return getattr(self.ctl.world, "scene", None)

    def activate(self) -> None:
        """On showing the tab: the ROIs as drawn now in the acquire window, and the saved calibration."""
        self._end_mark()
        self.rs = self.win.current_rois() if hasattr(self.win, "current_rois") else self.ctl.rois
        self.cams, self.world = self.ctl.camera_models()
        why = problem(self.rs, self.cams, self.world, self.ctl.roi_camera)
        missing = [c for c in self.ctl.cams if c not in self.cams]
        if why:
            self.ready_label.setText(f"<b style='color:#c0392b'>Not ready</b>: {why}.")
        else:
            self.ready_label.setText(f"✓ {self.ctl.roi_camera} (ROIs) and {len(self.cams) - 1} other camera(s) placed, "
                                     f"world origin on the floor ({str(self.world.get('date', ''))[:10]})"
                                     + (f"<br>Not placed (no outlines there): {', '.join(missing)}" if missing else ""))
        for w in (self.table, self.b_mark, self.b_save, self.b_truth):
            w.setEnabled(why is None)
        self._fill_table()
        self._dirty = True

    def _fill_table(self) -> None:
        rois = sorted(self.rs.perches, key=lambda r: (r.antenna is None, r.antenna or 0)) + (
            [self.rs.sync] if self.rs.sync is not None else [])
        self._rows = [r.name for r in rois]
        self.table.setRowCount(len(rois))
        for i, r in enumerate(rois):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(r.name + (" (LED)" if r.kind == "sync" else "")))
            z = QtWidgets.QDoubleSpinBox()
            z.setRange(NONE_Z, 10000.0)
            z.setDecimals(0)
            z.setSpecialValueText("—")
            z.setValue(NONE_Z if r.plane_z_mm is None else float(r.plane_z_mm))
            z.setToolTip("Height above the floor origin (mm): of the place's surface, or of the sync LED. — = not "
                         "placed in 3D")
            z.valueChanged.connect(lambda _v, n=r.name: self._edited(n))
            self.table.setCellWidget(i, 1, z)
            bx = QtWidgets.QDoubleSpinBox()
            bx.setRange(0.0, 3000.0)
            bx.setDecimals(0)
            bx.setSpecialValueText("—")
            bx.setValue(float(r.box_mm or 0.0))
            bx.setEnabled(r.kind != "sync")
            bx.setToolTip("Box height (mm): the place is the space this tall above its surface (— = the surface only)")
            bx.valueChanged.connect(lambda _v, n=r.name: self._edited(n))
            self.table.setCellWidget(i, 2, bx)
            self.table.setItem(i, 3, QtWidgets.QTableWidgetItem(""))
        self.table.resizeColumnsToContents()
        if rois and not self.table.selectedItems():
            self.table.selectRow(0)

    def _edited(self, name: str) -> None:
        i = self._rows.index(name)
        z = self.table.cellWidget(i, 1).value()
        bx = self.table.cellWidget(i, 2).value()
        self.rs = RoiSet([replace(r, plane_z_mm=None if z <= NONE_Z else float(z), box_mm=float(bx), points3d=None)
                          if r.name == name else r for r in self.rs.rois], self.rs.image_size)
        self._dirty = True

    def _changed(self) -> None:
        self._dirty = True

    def selected(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._rows[rows[0].row()] if rows else None

    # ------------------------------------------------------------------ live
    def tick(self) -> None:
        for cid, vv in self.views.items():
            th = self.ctl.cams.get(cid)
            if th is None or th.latest is None:
                self.captions[cid].setText(f"{cid}: not connected")
                continue
            if th.latest.grab_idx != self._last.get(cid):
                self._last[cid] = th.latest.grab_idx
                vv.set_image(th.latest.image)
        if self._dirty:
            self._dirty = False
            self._overlays()

    def _overlays(self) -> None:
        cam0 = self.cams.get(self.ctl.roi_camera)
        placed = RoiSet([place_3d(r, cam0) for r in self.rs.rois], self.rs.image_size) if cam0 else self.rs
        sel = self.selected()
        seen: dict[str, list[str]] = {r.name: [] for r in self.rs.rois}
        for cid, vv in self.views.items():
            sub = rois_in_camera(placed, cid, self.cams, self.ctl.roi_camera) if cam0 else (
                self.rs if cid == self.ctl.roi_camera else RoiSet())
            vv.load_rois(sub, editable=False)
            for r in sub.rois:
                seen[r.name].append(cid)
                vv.set_roi_state(r.name, SELECTED if r.name == sel else GRAY, 3 if r.name == sel else 2, r.name)
            n = len(sub.rois)
            self.captions[cid].setText(f"{cid}" + (" (ROIs drawn here)" if cid == self.ctl.roi_camera else
                                                   f": {n} place(s) in view" if cid in self.cams else
                                                   ": not placed (Calibrate cameras, tab 2)"))
        lines = []
        truth = self._scene().truth() if self._scene() is not None else None
        for i, name in enumerate(self._rows):
            self.table.item(i, 3).setText(" ".join(c for c in seen.get(name, []) if c != self.ctl.roi_camera))
            r = next(x for x in placed.rois if x.name == name)
            c = centre_3d(r)
            if c is None:
                lines.append(f"{name}: {'no height yet' if r.plane_z_mm is None else 'its height is out of view'}")
                continue
            line = f"{name}: centre x {c[0]:.0f}, y {c[1]:.0f}, z {c[2]:.0f} mm"
            if r.kind != "sync":
                P = np.asarray(r.points3d)
                span = P[:, :2].max(axis=0) - P[:, :2].min(axis=0)
                line += f", {span[0]:.0f} × {span[1]:.0f} mm"
            if truth is not None:
                tz = truth["SYNC"] if r.kind == "sync" else truth.get(r.antenna)
                if tz is not None:
                    line += f" <i>(simulated: {tz:.0f})</i>"
            lines.append(line)
        self.places_label.setText("<br>".join(lines))

    # ------------------------------------------------------------------ marking
    def _toggle_mark(self) -> None:
        if self.mark is not None:
            self._end_mark("Marking cancelled.")
            return
        name = self.selected()
        r = next((x for x in self.rs.rois if x.name == name), None)
        if r is None:
            return
        cam0 = self.ctl.roi_camera
        self.mark = {"name": name}
        self.b_mark.setText("Cancel marking")
        if r.kind == "sync":
            p = np.asarray(r.points, float)
            self._first_click(cam0, *((p.min(axis=0) + p.max(axis=0)) / 2))
            return
        self.views[cam0].click_mode = True
        self.status.setText(f"Click a spot of {name}'s surface in {cam0}: one you can find in another camera too "
                            "(a corner, the end of a bar, a mark).")

    def _first_click(self, cid: str, x: float, y: float) -> None:
        cam0 = self.cams[cid]
        self.mark["p0"] = (x, y)
        self.views[cid].click_mode = False
        self.views[cid].set_guides(points=[(x, y)])
        others = []
        for c, vv in self.views.items():
            if c == cid or c not in self.cams:
                continue
            vv.set_guides(curves=[epipolar_curve(cam0, (x, y), self.cams[c])])
            vv.click_mode = True
            others.append(c)
        if not others:
            self._end_mark("No other placed camera to mark the spot in.")
            return
        self.status.setText(f"Now click the same spot of {self.mark['name']} in {' or '.join(others)}: it is on the "
                            "dashed line.")

    def _clicked(self, cid: str, x: float, y: float) -> None:
        if self.mark is None:
            return
        if "p0" not in self.mark:
            if cid == self.ctl.roi_camera:
                self._first_click(cid, x, y)
            return
        if cid == self.ctl.roi_camera:
            return
        X, gap = triangulate(self.cams[self.ctl.roi_camera], self.mark["p0"], self.cams[cid], (x, y))
        name = self.mark["name"]
        if not np.isfinite(gap) or gap > MAX_RAY_GAP_MM:
            self.status.setText(f"The two clicks do not meet in 3D ({gap:.0f} mm apart, at most {MAX_RAY_GAP_MM:.0f}): "
                                "not the same spot? Click it again on the dashed line.")
            return
        i = self._rows.index(name)
        self.table.cellWidget(i, 1).setValue(round(float(X[2])))
        self._end_mark(f"{name}: height {X[2]:.0f} mm from {self.ctl.roi_camera} and {cid} (the rays pass {gap:.1f} mm "
                       "apart). Check its outline in every camera.")

    def _end_mark(self, text: str = "") -> None:
        self.mark = None
        self.b_mark.setText("Mark in two cameras")
        for vv in self.views.values():
            vv.click_mode = False
            vv.set_guides()
        if text:
            self.status.setText(text)

    def _use_truth(self) -> None:
        truth = self._scene().truth()
        for i, name in enumerate(self._rows):
            r = next(x for x in self.rs.rois if x.name == name)
            z = truth["SYNC"] if r.kind == "sync" else truth.get(r.antenna)
            if z is not None:
                self.table.cellWidget(i, 1).setValue(z)
                if r.kind != "sync" and not r.box_mm:
                    self.table.cellWidget(i, 2).setValue(130.0)

    # ------------------------------------------------------------------ save
    def placed_rois(self) -> RoiSet:
        cam0 = self.cams.get(self.ctl.roi_camera)
        return RoiSet([place_3d(r, cam0) if cam0 else r for r in self.rs.rois], self.rs.image_size)

    def _save(self) -> None:
        rs = self.placed_rois()
        if hasattr(self.win, "set_rois"):
            self.win.set_rois(rs, save=True)
        else:
            rs.save(self.ctl.rig["paths"]["rois_file"])
        n = sum(1 for r in rs.rois if r.points3d)
        self.status.setText(f"Saved: {n} of {len(rs.rois)} ROIs placed in 3D. Every camera's live view shows them.")
