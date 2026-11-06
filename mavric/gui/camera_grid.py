"""All connected cameras side by side, for monitoring, with the ROIs each camera sees."""

from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6 import QtGui

from .common import RED, _RoiGraphics, qcolor, to_rgb


def grid_columns(n: int, width: float, height: float, aspect: float = 1.25) -> int:
    """The number of columns that shows `n` images of this aspect (width / height) largest in a width x height area:
    three cameras sit two above one in a tall area, side by side in a wide one."""
    best, size = 1, -1.0
    for cols in range(1, max(1, n) + 1):
        rows = math.ceil(n / cols)
        s = min(width / cols, height * aspect / rows)  # the width of one tile
        if s > size * 1.001:
            best, size = cols, s
    return best


class CameraGrid(pg.GraphicsLayoutWidget):
    """One tile per camera, with a status line; frames are decimated for display only."""

    MAX_TILE_W = 800  # wider frames are shown at 1/2, 1/3, ... (recording is unaffected)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground(self.palette().color(QtGui.QPalette.ColorRole.Window))
        self.tiles: dict[str, dict] = {}
        self._ids: list[str] = []
        self._cols = 0
        self._aspect = 1.25  # of the images shown (1280 x 1024 until a frame says otherwise)
        self._roisets: dict = {}  # camera -> the ROIs on its tile, kept when the tiles are laid out again
        self._labels: dict[str, tuple[str, bool]] = {}

    def _columns(self) -> int:
        return grid_columns(len(self._ids), max(1, self.width()), max(1, self.height()), self._aspect)

    def set_cameras(self, ids: list[str]) -> None:
        if list(ids) == self._ids and self._cols == self._columns():
            return
        self._ids = list(ids)
        self._layout()

    def _layout(self) -> None:
        self.clear()
        self.tiles = {}
        ids = self._ids
        cols = self._cols = self._columns()
        last_alone = len(ids) > 1 and len(ids) % cols == 1  # a single tile on the last row spans it, centred
        for i, cid in enumerate(ids):
            span = cols if last_alone and i == len(ids) - 1 else 1
            vb = self.addViewBox(row=i // cols, col=i % cols, colspan=span, lockAspect=True, invertY=True,
                                 enableMenu=False)
            vb.setMouseEnabled(False, False)
            img = pg.ImageItem()
            vb.addItem(img)
            text, alert = self._labels.get(cid, (cid, False))
            label = pg.TextItem(text, anchor=(0, 0), color="#ffffff",
                                fill=qcolor(RED if alert else "#0b0b0b", 190 if alert else 170))
            label.setZValue(10)
            vb.addItem(label)
            self.tiles[cid] = {"vb": vb, "img": img, "label": label, "last": -1, "shape": None, "alert": alert,
                               "rois": {}}
            if cid in self._roisets:
                self.set_rois(cid, self._roisets[cid])

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if getattr(self, "_ids", None) and self._columns() != self._cols:  # (also called while being built)
            self._layout()

    def set_rois(self, cid: str, roiset) -> None:
        """The ROIs drawn on a tile, in the camera's full-size image coordinates."""
        self._roisets[cid] = roiset
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
        self._labels[cid] = (text, alert)
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
                aspect = frame.image.shape[1] / max(1, frame.image.shape[0])
                if abs(aspect - self._aspect) > 0.05:
                    self._aspect = aspect
        t["label"].setText(text)
        if alert != t["alert"]:
            t["alert"] = alert
            t["label"].fill = pg.mkBrush(qcolor(RED if alert else "#0b0b0b", 190 if alert else 170))
            t["label"].update()
