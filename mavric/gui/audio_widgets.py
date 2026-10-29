"""Live audio displays: per-channel level meters and scrolling spectrograms."""

from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ..audio.source import SPEC_STOPS  # one-hue ramp (blue), surface -> dark: quiet recedes into the background
from .common import RED

METER_FILL = "#2a78d6"
FLOOR_DB = -60.0


class LevelMeters(QtWidgets.QWidget):
    """Peak meter per channel (dBFS) with a falling peak hold and a clip marker."""

    def __init__(self, names: list[str], channels: list[int], parent=None):
        super().__init__(parent)
        g = QtWidgets.QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setVerticalSpacing(3)
        self.bars, self.vals, self.clips = [], [], []
        for i, (name, ch) in enumerate(zip(names, channels)):
            g.addWidget(QtWidgets.QLabel(f"{ch} {name}"), i, 0)
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, int(-FLOOR_DB * 10))
            bar.setTextVisible(False)
            bar.setFixedHeight(12)
            bar.setStyleSheet(f"QProgressBar {{border: 1px solid #c3c2bd; border-radius: 3px; background: #f0efec}}"
                              f"QProgressBar::chunk {{background: {METER_FILL}; border-radius: 2px}}")
            g.addWidget(bar, i, 1)
            val = QtWidgets.QLabel("")
            val.setMinimumWidth(64)
            val.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            g.addWidget(val, i, 2)
            clip = QtWidgets.QLabel("")
            clip.setMinimumWidth(34)
            g.addWidget(clip, i, 3)
            self.bars.append(bar)
            self.vals.append(val)
            self.clips.append(clip)
        g.setColumnStretch(1, 1)
        n = len(names)
        self._hold = np.full(n, FLOOR_DB)
        self._t = time.monotonic()
        self._clipped = np.zeros(n, int)
        self._clip_t = np.full(n, -1e9)

    def update_levels(self, peak_db: np.ndarray, clipped: list[int]) -> None:
        t = time.monotonic()
        dt, self._t = t - self._t, t
        self._hold = np.maximum(np.maximum(self._hold - 20 * dt, FLOOR_DB), peak_db)  # hold falls 20 dB/s
        clipped = np.asarray(clipped)
        self._clip_t[clipped > self._clipped] = t
        self._clipped = clipped
        for i, bar in enumerate(self.bars):
            bar.setValue(int((max(peak_db[i], FLOOR_DB) - FLOOR_DB) * 10))
            self.vals[i].setText(f"{self._hold[i]:.0f} dBFS" if self._hold[i] > FLOOR_DB else "< -60")
            on = t - self._clip_t[i] < 2.0
            self.clips[i].setText("CLIP" if on else "")
            self.clips[i].setStyleSheet(f"color:white; background:{RED}; font-weight:bold; padding:0 3px" if on else "")


class SpectrogramView(pg.GraphicsLayoutWidget):
    """Scrolling spectrogram (last `seconds`) of each microphone channel."""

    def __init__(self, names: list[str], sample_rate: float, fmax_hz: float = 10000, seconds: float = 10.0,
                 nfft: int = 1024, hop: int = 512, db_range=(-90.0, -20.0), parent=None):
        super().__init__(parent)
        self.sr, self.nfft, self.hop, self.seconds = sample_rate, nfft, hop, seconds
        self.db_range = db_range
        fmax_hz = min(fmax_hz, sample_rate / 2)
        self.n_freq = int(fmax_hz / (sample_rate / nfft)) + 1
        self.n_cols = int(seconds * sample_rate / hop)
        self.data = np.full((len(names), self.n_freq, self.n_cols), db_range[0], np.float32)
        self.win = np.hanning(nfft).astype(np.float32)
        self._norm = 2.0 / self.win.sum()
        self._pending = np.zeros((0, len(names)), np.float32)
        cmap = pg.ColorMap([p for p, _ in SPEC_STOPS], [c for _, c in SPEC_STOPS])
        self.imgs = []
        for i, name in enumerate(names):
            p = self.addPlot(row=i, col=0)
            p.setMouseEnabled(False, False)
            p.hideButtons()
            p.setLabel("left", f"{name}  kHz")
            if i == len(names) - 1:
                p.setLabel("bottom", "seconds ago")
            else:
                p.getAxis("bottom").setStyle(showValues=False)
            img = pg.ImageItem()
            img.setLookupTable(cmap.getLookupTable(nPts=256))
            img.setImage(self.data[i], levels=db_range, autoLevels=False)  # setRect needs the image size
            img.setRect(QtCore.QRectF(-seconds, 0, seconds, self.n_freq * sample_rate / nfft / 1000))
            p.addItem(img)
            p.setXRange(-seconds, 0, padding=0)
            p.setYRange(0, fmax_hz / 1000, padding=0)
            self.imgs.append(img)
        self.setMinimumHeight(110 * len(names))

    def feed(self, x: np.ndarray) -> None:
        """Append new samples (n, channels)."""
        if not len(x):
            return
        buf = np.concatenate([self._pending, x.astype(np.float32)])
        n_new = max(0, (len(buf) - self.nfft) // self.hop + 1)
        if n_new == 0:
            self._pending = buf
            return
        n_new = min(n_new, self.n_cols)
        start = len(buf) - self.nfft - (n_new - 1) * self.hop
        idx = start + np.arange(n_new)[:, None] * self.hop + np.arange(self.nfft)[None, :]
        frames = buf[idx] * self.win[None, :, None]  # (n_new, nfft, ch)
        spec = np.abs(np.fft.rfft(frames, axis=1))[:, : self.n_freq, :] * self._norm
        spec = 20 * np.log10(np.maximum(spec, 1e-10))  # (n_new, freq, ch)
        self.data = np.roll(self.data, -n_new, axis=2)
        self.data[:, :, -n_new:] = spec.transpose(2, 1, 0)
        self._pending = buf[start + n_new * self.hop:]

    def refresh(self) -> None:
        for i, img in enumerate(self.imgs):
            img.setImage(self.data[i], levels=self.db_range, autoLevels=False)
