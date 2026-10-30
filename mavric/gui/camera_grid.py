"""All connected cameras side by side, for monitoring, with the ROIs each camera sees."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6 import QtGui

from .common import RED, _RoiGraphics, qcolor, to_rgb


class CameraGrid(pg.GraphicsLayoutWidget):
    """One tile per camera, with a status line; frames are decimated for display only."""

    MAX_TILE_W = 800  # wider frames are shown at 1/2, 1/3, ... (recording is unaffected)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground(self.palette().color(QtGui.QPalette.ColorRole.Window))
        self.tiles: dict[str, dict] = {}
        self._ids: list[str] = []

    def set_cameras(self, ids: list[str]) -> None:
        if list(ids) == self._ids:
            return
        self.clear()
        self.tiles = {}
        self._ids = list(ids)
        cols = max(1, math.ceil(math.sqrt(len(ids))))
        for i, cid in enumerate(ids):
            vb = self.addViewBox(row=i // cols, col=i % cols, lockAspect=True, invertY=True, enableMenu=False)
            vb.setMouseEnabled(False, False)
            img = pg.ImageItem()
            vb.addItem(img)
            label = pg.TextItem(cid, anchor=(0, 0), color="#ffffff", fill=qcolor("#0b0b0b", 170))
            label.setZValue(10)
            vb.addItem(label)
            self.tiles[cid] = {"vb": vb, "img": img, "label": label, "last": -1, "shape": None, "alert": False,
                               "rois": {}}

    def set_rois(self, cid: str, roiset) -> None:
        """The ROIs drawn on a tile, in the camera's full-size image coordinates."""
        t = self.tiles.get(cid)
        if t is None:
            return
        for g in t["rois"].values():
            g.remove(t["vb"])
        t["rois"] = {r.name: _RoiGraphics(t["vb"], r, False) for r in (roiset.rois if roiset is not None else [])}

    def set_roi_state(self, name: str, color: str, width: float, text: str) -> None:
        for t in self.tiles.values():
            g = t["rois"].get(name)
            if g is not None:
                g.set_state(color, width, text)

    def update_tile(self, cid: str, frame, text: str, alert: bool = False) -> None:
        t = self.tiles.get(cid)
        if t is None:
            return
        if frame is not None and frame.grab_idx != t["last"]:
            t["last"] = frame.grab_idx
            im = frame.image
            k = max(1, math.ceil(im.shape[1] / self.MAX_TILE_W))
            if k > 1:
                im = im[::k, ::k]
            t["img"].setImage(to_rgb(im), autoLevels=False, levels=(0, 255))
            if t["shape"] != im.shape[:2]:
                t["shape"] = im.shape[:2]
                t["img"].setTransform(QtGui.QTransform.fromScale(k, k))  # shown smaller, in full-size coordinates
                t["vb"].autoRange(padding=0.01)
        t["label"].setText(text)
        if alert != t["alert"]:
            t["alert"] = alert
            t["label"].fill = pg.mkBrush(qcolor(RED if alert else "#0b0b0b", 190 if alert else 170))
            t["label"].update()
