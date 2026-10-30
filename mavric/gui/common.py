"""Shared Qt widgets: video view with ROI overlays, workers, small helpers."""

from __future__ import annotations

import threading
import traceback
from dataclasses import replace

import cv2
import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ..config import Roi, RoiSet

pg.setConfigOptions(imageAxisOrder="row-major", antialias=True, background="#fcfcfb", foreground="#52514e")

GRAY = "#aaaaaa"
RED = "#e34948"


def to_rgb(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def qcolor(hex_color: str, alpha: int = 255) -> QtGui.QColor:
    c = QtGui.QColor(hex_color)
    c.setAlpha(alpha)
    return c


class Worker(QtCore.QObject):
    """Run a callable in a Python thread; deliver result or error on the Qt thread."""

    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(float)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")


class _RoiGraphics:
    def __init__(self, view: pg.ViewBox, roi: Roi, editable: bool):
        self.roi = roi
        self.kind = roi.kind
        pen = pg.mkPen(GRAY, width=2)
        if roi.kind == "sync":
            xs = [p[0] for p in roi.points]
            ys = [p[1] for p in roi.points]
            self.item = pg.RectROI([min(xs), min(ys)], [max(xs) - min(xs), max(ys) - min(ys)], pen=pg.mkPen("#eda100", width=2),
                                   movable=editable, resizable=editable, rotatable=False)
        else:
            self.item = pg.PolyLineROI(roi.points, closed=True, pen=pen, movable=editable)
        self.item.setZValue(10)
        view.addItem(self.item)
        self.text = pg.TextItem("", anchor=(0, 1), color="#ffffff", fill=qcolor("#0b0b0b", 160))
        self.text.setZValue(11)
        view.addItem(self.text)
        self.set_editable(editable)
        self.update_text_pos()
        self.item.sigRegionChanged.connect(self.update_text_pos)

    def points(self) -> list[tuple[float, float]]:
        if self.kind == "sync":
            x, y = self.item.pos()
            w, h = self.item.size()
            return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        return [(float(p.x()), float(p.y())) for p in
                (self.item.mapToParent(pos) for _, pos in self.item.getLocalHandlePositions())]

    def update_text_pos(self, *_):
        pts = self.points()
        self.text.setPos(min(p[0] for p in pts), min(p[1] for p in pts))

    def set_editable(self, editable: bool) -> None:
        self.item.translatable = editable
        for h in self.item.getHandles():
            h.setVisible(editable)
        self.item.setAcceptedMouseButtons(QtCore.Qt.MouseButton.LeftButton if editable else QtCore.Qt.MouseButton.NoButton)

    def set_state(self, color: str, width: float, text: str) -> None:
        self.item.setPen(pg.mkPen(color, width=width))
        self.text.setText(text)

    def remove(self, view) -> None:
        view.removeItem(self.item)
        view.removeItem(self.text)


class VideoView(pg.GraphicsLayoutWidget):
    """Camera image with perch ROIs (polygons) and a sync-LED ROI (rectangle)."""

    clicked = QtCore.Signal(float, float)  # image point of a left click, while click_mode is on

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground(self.palette().color(QtGui.QPalette.ColorRole.Window))
        self.vb = self.addViewBox(lockAspect=True, invertY=True, enableMenu=False)
        self.img = pg.ImageItem()
        self.vb.addItem(self.img)
        self.rois: dict[str, _RoiGraphics] = {}
        self.editable = False
        self.click_mode = False
        self._guides: list = []
        self._has_image = False
        self._size = None
        self.scene().sigMouseClicked.connect(self._on_click)

    def _on_click(self, ev) -> None:
        if not self.click_mode or ev.button() != QtCore.Qt.MouseButton.LeftButton or self.img.image is None:
            return
        p = self.vb.mapSceneToView(ev.scenePos())
        h, w = self.img.image.shape[:2]
        if 0 <= p.x() < w and 0 <= p.y() < h:
            ev.accept()
            self.clicked.emit(float(p.x()), float(p.y()))

    def set_guides(self, curves=(), points=(), color: str = "#eda100") -> None:
        """Dashed curves (n, 2) and cross marks over the image (e.g. where a spot clicked in another camera can be)."""
        for it in self._guides:
            self.vb.removeItem(it)
        self._guides = []
        for c in curves:
            if len(c) >= 2:
                it = pg.PlotCurveItem(c[:, 0], c[:, 1], pen=pg.mkPen(color, width=2, style=QtCore.Qt.PenStyle.DashLine))
                it.setZValue(12)
                self.vb.addItem(it)
                self._guides.append(it)
        if len(points):
            pts = np.asarray(points, float).reshape(-1, 2)
            it = pg.ScatterPlotItem(pts[:, 0], pts[:, 1], symbol="+", size=18, pen=pg.mkPen(color, width=2),
                                    brush=None)
            it.setZValue(12)
            self.vb.addItem(it)
            self._guides.append(it)

    def set_image(self, img: np.ndarray) -> None:
        size = img.shape[:2]
        self.img.setImage(to_rgb(img), autoLevels=False, levels=(0, 255))
        if not self._has_image or size != self._size:
            self._has_image = True
            self._size = size
            self.vb.autoRange(padding=0.01)

    def set_rois_visible(self, on: bool) -> None:
        for g in self.rois.values():
            g.item.setVisible(on)
            g.text.setVisible(on)

    def image_size(self) -> tuple[int, int] | None:
        if self.img.image is None:
            return None
        h, w = self.img.image.shape[:2]
        return w, h

    def load_rois(self, roiset: RoiSet, editable: bool = False) -> None:
        for g in self.rois.values():
            g.remove(self.vb)
        self.rois = {}
        self.editable = editable
        for r in roiset.rois:
            self.rois[r.name] = _RoiGraphics(self.vb, r, editable)

    def add_roi(self, roi: Roi) -> None:
        if roi.name in self.rois:
            self.rois[roi.name].remove(self.vb)
        self.rois[roi.name] = _RoiGraphics(self.vb, roi, self.editable)

    def set_editable(self, editable: bool) -> None:
        self.editable = editable
        for g in self.rois.values():
            g.set_editable(editable)

    def roiset(self, image_size: tuple[int, int] | None = None) -> RoiSet:
        rois = [replace(g.roi, points=g.points()) for g in self.rois.values()]  # keeps the 3D settings
        return RoiSet(rois, image_size or self.image_size())

    def set_roi_state(self, name: str, color: str, width: float, text: str) -> None:
        if name in self.rois:
            self.rois[name].set_state(color, width, text)


def swatch(color: str, size: int = 12) -> QtGui.QIcon:
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtGui.QColor(color))
    return QtGui.QIcon(pm)


def message(parent, title: str, text: str, error: bool = False) -> None:
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Icon.Critical if error else QtWidgets.QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(text.split("\n\n")[0])
    if "\n\n" in text:
        box.setDetailedText(text)
    box.exec()
