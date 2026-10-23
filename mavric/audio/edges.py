"""Sync pulse edges in an audio channel, located to a fraction of a sample.

The RFID Arduino's sync output (5 V, 80 ms, random 2-6 s intervals) reaches a
line input through a resistor divider. Line inputs are AC-coupled, so the
recorded pulse is a sharp step up, an exponential sag, and a sharp step down.
Only the steps are used: each is found where the sample-to-sample difference
is large, and its time is the crossing of half the step height, interpolated
between samples (precision of a few microseconds at 48 kHz). Works on any
polarity; `pulse_onsets` pairs the steps into pulses.
"""

from __future__ import annotations

import numpy as np

EDGE_COLUMNS = ["sample", "sign", "amp"]


class EdgeDetector:
    """Streaming step detector: feed consecutive blocks, get edges in absolute sample positions."""

    M = 24  # context (samples) needed on each side of an edge

    def __init__(self, sample_rate: float, min_step: float = 0.005, k_noise: float = 12.0, refractory_s: float = 0.001):
        self.sr = float(sample_rate)
        self.min_step = min_step
        self.k_noise = k_noise
        self.refractory = refractory_s * self.sr
        self._tail = np.zeros(0, np.float32)
        self._base = 0  # absolute sample index of _tail[0]
        self._noise: float | None = None
        self._last = -np.inf

    def reset(self) -> None:
        self._tail = np.zeros(0, np.float32)
        self._noise = None
        self._last = -np.inf

    def process(self, x: np.ndarray, k0: int) -> list[tuple[float, int, float]]:
        """Edges (position, sign, step amplitude) found in `x`, whose first sample is absolute index `k0`."""
        x = np.asarray(x, np.float32).ravel()
        if len(self._tail) and self._base + len(self._tail) != k0:
            self._tail = np.zeros(0, np.float32)  # gap in the stream: restart
        if not len(self._tail):
            self._base = k0
        buf = np.concatenate([self._tail, x]) if len(self._tail) else x
        base, M = self._base, self.M
        out: list[tuple[float, int, float]] = []
        if len(buf) > 2 * M + 1:
            d = np.diff(buf)
            med = np.median(d)
            sigma = 1.4826 * float(np.median(np.abs(d - med))) + 1e-9
            self._noise = sigma if self._noise is None else 0.95 * self._noise + 0.05 * sigma
            thr = max(self.min_step, self.k_noise * min(self._noise, sigma))
            lo, hi = M, len(buf) - M - 1
            cand = np.flatnonzero(np.abs(d[lo:hi]) > thr) + lo
            if len(cand):
                groups = np.split(cand, np.flatnonzero(np.diff(cand) > 8) + 1)
                for g in groups:
                    p = int(g[np.argmax(np.abs(d[g]))])
                    e = self._refine(buf, p, int(np.sign(d[p])), thr)
                    if e is None:
                        continue
                    pos = base + e[0]
                    if pos - self._last < self.refractory:
                        continue
                    self._last = pos
                    out.append((pos, e[1], e[2]))
            keep = 2 * M + 1
            self._tail = buf[-keep:].copy()
            self._base = base + len(buf) - keep
        else:
            self._tail = buf.copy()
        return out

    @staticmethod
    def _refine(buf: np.ndarray, p: int, sign: int, thr: float):
        """Half-height crossing of the step around d[p] (between buf[p] and buf[p+1])."""
        before = float(np.median(buf[p - 16: p - 3]))
        after = float(np.median(buf[p + 5: p + 13]))
        amp = after - before
        if np.sign(amp) != sign or abs(amp) < thr:
            return None  # a spike or ringing, not a step
        half = (before + after) / 2
        seg = buf[p - 4: p + 6] - half
        s = np.sign(seg) * sign  # -1 before the crossing, +1 after
        idx = np.flatnonzero((s[:-1] < 0) & (s[1:] >= 0))
        if not len(idx):
            return None
        j = int(idx[0])
        y0, y1 = float(seg[j]), float(seg[j + 1])
        frac = -y0 / (y1 - y0) if y1 != y0 else 0.0
        return (p - 4 + j + frac, sign, amp)


def detect_edges(x: np.ndarray, sample_rate: float, block: int = 1 << 16, **kw) -> np.ndarray:
    """Edges of a whole signal, as an (n, 3) array of (sample, sign, amp)."""
    det = EdgeDetector(sample_rate, **kw)
    out = []
    for k in range(0, len(x), block):
        out += det.process(x[k: k + block], k)
    return np.array(out, float).reshape(-1, 3)


def pulse_onsets(edges, sample_rate: float, pulse_s: float, tol: float = 0.4) -> tuple[np.ndarray, int]:
    """Leading edges of pulses: an edge followed by an opposite edge `pulse_s` later (+-tol).

    Returns (onset sample positions, polarity). The polarity is that of most
    leading edges, so an inverting input or cable is handled.
    """
    e = np.asarray(edges, float).reshape(-1, 3)
    if len(e) < 2:
        return np.array([]), 1
    pos, sign = e[:, 0], e[:, 1].astype(int)
    width = pulse_s * sample_rate
    gap = np.diff(pos)
    lead = (sign[:-1] == -sign[1:]) & (np.abs(gap - width) <= tol * width)
    if not lead.any():
        return np.array([]), 1
    polarity = 1 if (sign[:-1][lead] > 0).sum() >= (sign[:-1][lead] < 0).sum() else -1
    ok = lead & (sign[:-1] == polarity)
    return pos[:-1][ok], polarity
