"""Camera calibration with a ChArUco board: the board, detection, collecting views, lens calibration, files.

A ChArUco board is a checkerboard with an ArUco marker in every white square.
The markers give every corner an identity, so the board can be partly hidden
or partly out of view, and two cameras always agree on which corner is which.
A plain checkerboard becomes one with marker stickers (mavric.video.sheet).
Corner positions still come from the checkerboard squares (to a fraction of a
pixel); the markers only identify them.

Board coordinates are millimetres, x to the right and y down from the outer
corner of the top-left square, which is black. Marker k sits in the k-th white
square, row by row from the top (OpenCV's layout).

    config/calibration.yaml            board and per-camera lens calibration
    config/calibration/<cam>_lens.npz  the corners of every view used, to redo the fit later

A lens calibration (intrinsics: focal length, image centre, distortion) holds
while the camera's lens, focus, zoom and image size stay the same.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import cv2
import numpy as np
import yaml

DICTIONARIES = {name: getattr(cv2.aruco, name) for name in (
    "DICT_4X4_50", "DICT_4X4_100", "DICT_4X4_250", "DICT_5X5_50", "DICT_5X5_100", "DICT_5X5_250", "DICT_6X6_50",
    "DICT_6X6_100", "DICT_6X6_250")}

# acceptance of a lens calibration
MAX_RMS_PX = 0.5
MIN_VIEWS = 12
MIN_COVERAGE = 0.6
MIN_TILTED = 4
GRID = (8, 6)  # image cells for the coverage map


# --------------------------------------------------------------------------- the board


@dataclass(frozen=True)
class BoardSpec:
    squares_x: int = 8  # squares across
    squares_y: int = 10  # squares down
    square_mm: float = 39.0
    marker_mm: float = 29.0
    dictionary: str = "DICT_4X4_50"

    @classmethod
    def from_rig(cls, rig: dict) -> BoardSpec:
        b = ((rig.get("calibration") or {}).get("board") or {})
        sq = b.get("squares", [cls.squares_x, cls.squares_y])
        return cls(int(sq[0]), int(sq[1]), float(b.get("square_mm", cls.square_mm)),
                   float(b.get("marker_mm", cls.marker_mm)), str(b.get("dictionary", cls.dictionary)))

    def as_dict(self) -> dict:
        return {"squares": [self.squares_x, self.squares_y], "square_mm": self.square_mm,
                "marker_mm": self.marker_mm, "dictionary": self.dictionary}

    def describe(self) -> str:
        return (f"{self.squares_x} × {self.squares_y} squares of {self.square_mm:g} mm, {self.marker_mm:g} mm markers "
                f"({self.dictionary})")

    def problems(self) -> list[str]:
        out = []
        if self.dictionary not in DICTIONARIES:
            out.append(f"unknown dictionary {self.dictionary!r} (use one of {', '.join(DICTIONARIES)})")
        elif self.n_markers > int(self.dictionary.rsplit("_", 1)[1]):
            out.append(f"{self.n_markers} markers needed but {self.dictionary} has only "
                       f"{self.dictionary.rsplit('_', 1)[1]}")
        if not 0 < self.marker_mm < self.square_mm:
            out.append("the marker must be smaller than the square")
        if self.squares_x < 3 or self.squares_y < 3:
            out.append("at least 3 x 3 squares")
        return out

    @property
    def n_corners(self) -> int:
        return (self.squares_x - 1) * (self.squares_y - 1)

    @property
    def n_markers(self) -> int:
        return self.squares_x * self.squares_y // 2

    @property
    def size_mm(self) -> tuple[float, float]:
        return self.squares_x * self.square_mm, self.squares_y * self.square_mm

    @cached_property
    def dictionary_obj(self):
        return cv2.aruco.getPredefinedDictionary(DICTIONARIES[self.dictionary])

    @cached_property
    def board(self):
        b = cv2.aruco.CharucoBoard((self.squares_x, self.squares_y), self.square_mm, self.marker_mm,
                                   self.dictionary_obj)
        b.setLegacyPattern(False)  # top-left square black, whatever the number of rows
        return b

    @cached_property
    def corner_xy(self) -> np.ndarray:
        """(n_corners, 2) board position of every inner corner, by corner id."""
        return np.asarray(self.board.getChessboardCorners(), np.float64)[:, :2]

    @cached_property
    def marker_xy(self) -> dict[int, np.ndarray]:
        """Marker id -> its four corners (4, 2) on the board, in the marker's own order (top-left first)."""
        ids = np.ravel(self.board.getIds())
        return {int(i): np.asarray(p, np.float64)[:, :2] for i, p in zip(ids, self.board.getObjPoints())}

    @cached_property
    def marker_square(self) -> dict[int, tuple[int, int]]:
        """Marker id -> (column, row) of its square."""
        return {i: tuple(int(v) for v in (p.mean(axis=0) // self.square_mm)) for i, p in self.marker_xy.items()}

    def corner_markers(self, corner_id: int) -> list[int]:
        """The markers in the (white) squares that touch an inner corner."""
        nx = self.squares_x - 1
        c, r = corner_id % nx + 1, corner_id // nx + 1  # the corner is the top-left corner of square (c, r)
        near = {(c - 1, r - 1), (c, r - 1), (c - 1, r), (c, r)}
        return [i for i, sq in self.marker_square.items() if sq in near]

    def marker_image(self, marker_id: int) -> np.ndarray:
        """The marker's cells (1 = black), border included: (n + 2, n + 2) for an n x n dictionary."""
        n = self.dictionary_obj.markerSize + 2
        return (self.dictionary_obj.generateImageMarker(int(marker_id), n) < 128).astype(np.uint8)

    def image(self, px_per_mm: float = 4.0, margin_mm: float = 10.0) -> np.ndarray:
        """The board as a grey image with a white margin: board (x, y) mm -> pixel (margin + (x, y)) * px_per_mm."""
        w, h = self.size_mm
        m = int(round(margin_mm * px_per_mm))
        return self.board.generateImage((int(round(w * px_per_mm)) + 2 * m, int(round(h * px_per_mm)) + 2 * m),
                                        marginSize=m, borderBits=1)


# --------------------------------------------------------------------------- detection


@dataclass
class Detection:
    corners: np.ndarray  # (n, 2) image position of each inner corner found
    ids: np.ndarray  # (n,) corner ids
    marker_ids: np.ndarray  # (m,)
    marker_corners: np.ndarray  # (m, 4, 2)
    misplaced: list[int] = field(default_factory=list)  # markers whose square or rotation does not fit the board
    image_size: tuple[int, int] = (0, 0)  # (width, height)

    @property
    def n(self) -> int:
        return len(self.ids)


class BoardDetector:
    def __init__(self, spec: BoardSpec):
        self.spec = spec
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        cp = cv2.aruco.CharucoParameters()
        cp.minMarkers = 2  # a corner needs both neighbouring markers: robust against a misplaced sticker
        self.det = cv2.aruco.CharucoDetector(spec.board, cp, params)

    def detect(self, img: np.ndarray) -> Detection:
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        cc, ci, mc, mi = self.det.detectBoard(gray)
        corners = np.zeros((0, 2), np.float32) if cc is None else np.asarray(cc, np.float32).reshape(-1, 2)
        ids = np.zeros(0, int) if ci is None else np.asarray(ci).reshape(-1).astype(int)
        m_ids = np.zeros(0, int) if mi is None else np.asarray(mi).reshape(-1).astype(int)
        m_c = np.zeros((0, 4, 2), np.float32) if mc is None or not len(mc) else \
            np.asarray(mc, np.float32).reshape(-1, 4, 2)
        keep = m_ids < self.spec.n_markers  # a marker from the dictionary that is not on this board
        m_ids, m_c = m_ids[keep], m_c[keep]
        bad = misplaced_markers(self.spec, m_ids, m_c)
        if bad:
            # OpenCV drops the whole board when a marker does not fit: find the corners again without the bad ones
            ok = ~np.isin(m_ids, bad)
            cc, ci, _, _ = self.det.detectBoard(gray, markerCorners=tuple(m_c[ok].reshape(-1, 1, 4, 2)),
                                                markerIds=m_ids[ok].reshape(-1, 1).astype(np.int32))
            corners = np.zeros((0, 2), np.float32) if cc is None else np.asarray(cc, np.float32).reshape(-1, 2)
            ids = np.zeros(0, int) if ci is None else np.asarray(ci).reshape(-1).astype(int)
            if len(ids):  # and no corner that touches a bad marker
                good = np.array([not (set(self.spec.corner_markers(int(c))) & set(bad)) for c in ids])
                corners, ids = corners[good], ids[good]
        return Detection(corners, ids, m_ids, m_c, bad, (w, h))


def misplaced_markers(spec: BoardSpec, ids: np.ndarray, corners: np.ndarray) -> list[int]:
    """Markers that sit in the wrong square or are turned, judged against their neighbours.

    Each marker is compared with a homography fitted to the markers within two
    squares of it (local, so lens distortion barely matters). A sticker in
    another square or turned by 90 degrees misses by about a marker side.
    """
    if len(ids) < 4:
        return []
    side = np.linalg.norm(corners[:, 1] - corners[:, 0], axis=1).mean()
    where = {int(i): k for k, i in enumerate(ids)}
    centres = {i: spec.marker_xy[i].mean(axis=0) for i in where}
    bad = []
    for i, k in where.items():
        near = [j for j in where if j != i and np.linalg.norm(centres[j] - centres[i]) <= 2.3 * spec.square_mm]
        if len(near) < 3:
            continue
        src = np.concatenate([spec.marker_xy[j] for j in near])
        dst = np.concatenate([corners[where[j]] for j in near])
        H, _ = cv2.findHomography(src, dst, cv2.RANSAC, max(2.0, 0.25 * side))
        if H is None:
            continue
        p = cv2.perspectiveTransform(spec.marker_xy[i].reshape(-1, 1, 2), H).reshape(-1, 2)
        if np.linalg.norm(p - corners[k], axis=1).mean() > 0.5 * side:
            bad.append(i)
    return sorted(bad)


def draw_detection(img: np.ndarray, det: Detection, spec: BoardSpec) -> np.ndarray:
    """The image (BGR) with the corners found (green) and misplaced markers (red, with their id)."""
    out = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img.copy()
    r = max(2, int(round(max(out.shape[:2]) / 400)))
    for p in det.corners:
        cv2.circle(out, (int(round(p[0])), int(round(p[1]))), r, (60, 200, 60), -1, cv2.LINE_AA)
    for i, c in zip(det.marker_ids, det.marker_corners):
        if int(i) in det.misplaced:
            cv2.polylines(out, [c.astype(np.int32)], True, (40, 40, 230), 2 * r, cv2.LINE_AA)
            org = (int(c[:, 0].max()) + 2 * r, int(c[:, 1].min()) + 4 * r)  # beside the marker, not over it
            for color, th in (((255, 255, 255), 3 * max(1, r // 2)), ((40, 40, 230), max(1, r // 2))):
                cv2.putText(out, str(int(i)), org, cv2.FONT_HERSHEY_SIMPLEX, 0.4 * r, color, th, cv2.LINE_AA)
    return out


# --------------------------------------------------------------------------- collecting views


def board_quad(spec: BoardSpec, det: Detection) -> np.ndarray | None:
    """The board's four outer corners in the image (from a homography of the corners found), or None."""
    if det.n < 6:
        return None
    H, _ = cv2.findHomography(spec.corner_xy[det.ids], det.corners, 0)
    if H is None:
        return None
    w, h = spec.size_mm
    return cv2.perspectiveTransform(np.array([[[0, 0]], [[w, 0]], [[w, h]], [[0, h]]], np.float64), H).reshape(4, 2)


def view_descriptor(spec: BoardSpec, det: Detection) -> np.ndarray | None:
    """Where the board is, how big and how tilted: (x, y, size, tilt left/right, tilt up/down, roll)."""
    q = board_quad(spec, det)
    if q is None:
        return None
    w, h = det.image_size
    c = q.mean(axis=0) / (w, h)
    d1, d2 = q[2] - q[0], q[3] - q[1]
    area = 0.5 * abs(d1[0] * d2[1] - d1[1] * d2[0])
    size = np.sqrt(area / (w * h))
    top, right, bottom, left = (np.linalg.norm(q[(k + 1) % 4] - q[k]) for k in range(4))
    tilt_lr = np.log(max(right, 1e-6) / max(left, 1e-6))
    tilt_ud = np.log(max(bottom, 1e-6) / max(top, 1e-6))
    roll = np.arctan2(*(q[1] - q[0])[::-1])
    return np.array([c[0], c[1], size, tilt_lr, tilt_ud, roll])


def is_tilted(desc: np.ndarray, min_log_ratio: float = 0.12) -> bool:
    """Seen at an angle (one edge at least ~13 % longer than the opposite one): such views pin the focal length."""
    return max(abs(desc[3]), abs(desc[4])) >= min_log_ratio


class ViewCollector:
    """Picks views for a lens calibration: the board held still, in a position not captured yet."""

    def __init__(self, spec: BoardSpec, min_corners: int = 12, still_px: float = 0.8, still_frames: int = 5,
                 new_dist: float = 0.12):
        self.spec = spec
        self.min_corners = max(6, min_corners)
        self.still_px = still_px
        self.still_frames = still_frames
        self.new_dist = new_dist
        self.views: list[Detection] = []
        self.descs: list[np.ndarray] = []
        self._prev: Detection | None = None
        self._still = 0

    def clear(self) -> None:
        self.views, self.descs, self._prev, self._still = [], [], None, 0

    def _motion(self, det: Detection) -> float:
        p = self._prev
        if p is None or not len(p.ids):
            return np.inf
        common, a, b = np.intersect1d(p.ids, det.ids, return_indices=True)
        if len(common) < 4:
            return np.inf
        return float(np.median(np.linalg.norm(p.corners[a] - det.corners[b], axis=1)))

    def novelty(self, desc: np.ndarray) -> float:
        if not self.descs:
            return np.inf
        d = np.array(self.descs)
        diff = np.abs(d - desc)
        diff[:, 5] = np.abs((diff[:, 5] + np.pi) % (2 * np.pi) - np.pi)
        w = np.array([1.0, 1.0, 1.5, 0.8, 0.8, 0.3])
        return float((diff * w).sum(axis=1).min())

    def offer(self, det: Detection, auto: bool = True, force: bool = False) -> str:
        """Feed every frame's detection. Returns what happened: captured | moving | seen | few | misplaced."""
        moving = self._motion(det) > self.still_px
        self._prev = det
        self._still = 0 if moving else self._still + 1
        if det.n < self.min_corners:
            return "few"
        if det.misplaced:
            return "misplaced"
        desc = view_descriptor(self.spec, det)
        if desc is None:
            return "few"
        if not force:
            if not auto:
                return "moving" if moving else "seen"
            if self._still < self.still_frames:
                return "moving"
            if self.novelty(desc) < self.new_dist:
                return "seen"
        self.views.append(det)
        self.descs.append(desc)
        return "captured"

    def coverage(self, image_size: tuple[int, int] | None = None) -> tuple[float, np.ndarray]:
        """Share of the image cells (GRID) holding a captured corner, and the (rows, cols) hit map."""
        hits = np.zeros(GRID[::-1], bool)
        if not self.views:
            return 0.0, hits
        w, h = image_size or self.views[0].image_size
        for v in self.views:
            cx = np.clip((v.corners[:, 0] / w * GRID[0]).astype(int), 0, GRID[0] - 1)
            cy = np.clip((v.corners[:, 1] / h * GRID[1]).astype(int), 0, GRID[1] - 1)
            hits[cy, cx] = True
        return float(hits.mean()), hits

    @property
    def n_tilted(self) -> int:
        return sum(is_tilted(d) for d in self.descs)


def draw_coverage(img: np.ndarray, hits: np.ndarray) -> np.ndarray:
    """Tint the image cells that already hold captured corners."""
    out = img.copy()
    h, w = out.shape[:2]
    gy, gx = hits.shape
    tint = np.zeros_like(out)
    tint[:] = (60, 170, 60)
    for r in range(gy):
        for c in range(gx):
            if hits[r, c]:
                y0, y1, x0, x1 = r * h // gy, (r + 1) * h // gy, c * w // gx, (c + 1) * w // gx
                out[y0:y1, x0:x1] = cv2.addWeighted(out[y0:y1, x0:x1], 0.86, tint[y0:y1, x0:x1], 0.14, 0)
    for r in range(1, gy):
        cv2.line(out, (0, r * h // gy), (w, r * h // gy), (200, 200, 200), 1)
    for c in range(1, gx):
        cv2.line(out, (c * w // gx, 0), (c * w // gx, h), (200, 200, 200), 1)
    return out


# --------------------------------------------------------------------------- lens calibration


@dataclass
class LensCalibration:
    camera: str
    image_size: tuple[int, int]
    K: np.ndarray
    dist: np.ndarray
    rms_px: float
    n_views: int
    per_view_px: list[float]
    coverage: float
    n_tilted: int
    std: dict  # standard deviation of fx, fy, cx, cy (px)
    model: str = "standard"  # standard: k1 k2 p1 p2 k3 | rational: + k4 k5 k6 (wide lenses)
    rejected: list[int] = field(default_factory=list)  # views left out as outliers
    board: dict = field(default_factory=dict)
    serial: str = ""
    date: str = ""

    def verdict(self) -> tuple[bool, list[str]]:
        why = []
        if self.rms_px > MAX_RMS_PX:
            why.append(f"reprojection error {self.rms_px:.2f} px (want ≤ {MAX_RMS_PX} px): hold the board still, "
                       "check focus and that the board is flat")
        if self.n_views < MIN_VIEWS:
            why.append(f"{self.n_views} views (want ≥ {MIN_VIEWS})")
        if self.coverage < MIN_COVERAGE:
            why.append(f"corners cover {self.coverage:.0%} of the image (want ≥ {MIN_COVERAGE:.0%}): bring the board "
                       "into the corners and edges")
        if self.n_tilted < MIN_TILTED:
            why.append(f"{self.n_tilted} tilted views (want ≥ {MIN_TILTED}): turn the board 20-40° left/right "
                       "and up/down")
        return not why, why

    def summary(self) -> str:
        fx, fy, cx, cy = self.K[0, 0], self.K[1, 1], self.K[0, 2], self.K[1, 2]
        ok, _ = self.verdict()
        return (f"{self.camera}: {'PASS' if ok else 'FAIL'}  {self.rms_px:.2f} px RMS, {self.n_views} views, "
                f"coverage {self.coverage:.0%}, {self.n_tilted} tilted; f = {fx:.1f} / {fy:.1f} px "
                f"(± {self.std.get('fx', 0):.1f}), centre {cx:.1f}, {cy:.1f}")

    def as_dict(self) -> dict:
        return {"serial": str(self.serial), "date": self.date, "image_size": [int(v) for v in self.image_size],
                "model": self.model, "K": np.round(self.K, 6).tolist(), "dist": np.round(self.dist.ravel(), 8).tolist(),
                "rms_px": round(float(self.rms_px), 4), "n_views": int(self.n_views),
                "coverage": round(float(self.coverage), 3), "n_tilted": int(self.n_tilted),
                "std_px": {k: round(float(v), 3) for k, v in self.std.items()},
                "per_view_px": [round(float(v), 3) for v in self.per_view_px],
                "rejected_views": [int(v) for v in self.rejected], "board": self.board}

    @classmethod
    def from_dict(cls, camera: str, d: dict) -> LensCalibration:
        return cls(camera, tuple(d["image_size"]), np.array(d["K"], float), np.array(d["dist"], float),
                   float(d["rms_px"]), int(d["n_views"]), list(d.get("per_view_px", [])), float(d.get("coverage", 0)),
                   int(d.get("n_tilted", 0)), dict(d.get("std_px", {})), d.get("model", "standard"),
                   list(d.get("rejected_views", [])), dict(d.get("board", {})), str(d.get("serial", "")),
                   str(d.get("date", "")))

    def undistort_maps(self, alpha: float = 0.0):
        """Maps for cv2.remap that straighten the image (alpha 0: only valid pixels, 1: keep every pixel)."""
        newK, _ = cv2.getOptimalNewCameraMatrix(self.K, self.dist, self.image_size, alpha)
        return cv2.initUndistortRectifyMap(self.K, self.dist, None, newK, self.image_size, cv2.CV_16SC2)


def calibrate_lens(views: list[Detection], spec: BoardSpec, camera: str = "", model: str = "standard",
                   serial: str = "", reject_factor: float = 3.0) -> LensCalibration:
    """Fit focal length, image centre and distortion to the captured views.

    Views whose own error is more than `reject_factor` times the median (and
    over 1 px) are dropped once and the fit repeated: a blurred or moved view
    should not spoil the rest.
    """
    if not views:
        raise ValueError("no views")
    size = views[0].image_size
    obj, img, used = [], [], []
    for k, v in enumerate(views):
        if v.image_size != size:
            raise ValueError(f"view {k} is {v.image_size}, the first was {size}: the image size changed")
        o, i = spec.board.matchImagePoints(v.corners.reshape(-1, 1, 2), v.ids.reshape(-1, 1))
        if o is None or len(o) < 6:
            continue
        obj.append(np.asarray(o, np.float32).reshape(-1, 1, 3))
        img.append(np.asarray(i, np.float32).reshape(-1, 1, 2))
        used.append(k)
    if len(obj) < 3:
        raise ValueError(f"only {len(obj)} usable views; capture more")
    flags = cv2.CALIB_RATIONAL_MODEL if model == "rational" else 0

    def fit(o, i):
        return cv2.calibrateCameraExtended(o, i, size, None, None, flags=flags)

    rms, K, dist, _r, _t, std_in, _std_ex, per_view = fit(obj, img)
    per_view = np.ravel(per_view)
    rejected = []
    med = float(np.median(per_view))
    bad = [k for k, e in enumerate(per_view) if e > max(reject_factor * med, 1.0)]
    if bad and len(obj) - len(bad) >= 3:
        rejected = [used[k] for k in bad]
        keep = [k for k in range(len(obj)) if k not in bad]
        obj, img, used = [obj[k] for k in keep], [img[k] for k in keep], [used[k] for k in keep]
        rms, K, dist, _r, _t, std_in, _std_ex, per_view = fit(obj, img)
        per_view = np.ravel(per_view)
    std_in = np.ravel(std_in)
    col = ViewCollector(spec)
    col.views = [views[k] for k in used]
    col.descs = [d for d in (view_descriptor(spec, views[k]) for k in used) if d is not None]
    cov, _ = col.coverage(size)
    return LensCalibration(camera, tuple(size), K, np.ravel(dist), float(rms), len(obj), per_view.tolist(), cov,
                           col.n_tilted, {"fx": float(std_in[0]), "fy": float(std_in[1]), "cx": float(std_in[2]),
                                          "cy": float(std_in[3])}, model, rejected, spec.as_dict(), serial,
                           dt.datetime.now().isoformat(timespec="seconds"))


# --------------------------------------------------------------------------- files


HEADER = ("# Camera calibration, written by mavric acquire -> Calibrate cameras... (or mavric calibrate).\n"
          "# A lens calibration holds while the camera's lens, focus, zoom and image size stay the same.\n")


def load_calibrations(path: str | Path) -> dict[str, LensCalibration]:
    p = Path(path)
    if not p.exists():
        return {}
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(cid): LensCalibration.from_dict(str(cid), c["lens"]) for cid, c in (d.get("cameras") or {}).items()
            if c and c.get("lens")}


def save_lens(path: str | Path, cal: LensCalibration, views: list[Detection] | None = None) -> Path:
    """Add or replace one camera's lens calibration (other cameras are kept); store its views next to it."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    d.setdefault("cameras", {})
    d["cameras"].setdefault(cal.camera, {})
    d["cameras"][cal.camera]["lens"] = cal.as_dict()
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(d, f, sort_keys=False, default_flow_style=None, allow_unicode=True, width=120)
    tmp.replace(p)
    if views:
        vdir = p.parent / p.stem
        vdir.mkdir(exist_ok=True)
        np.savez_compressed(vdir / f"{cal.camera}_lens.npz",
                            corners=np.concatenate([v.corners for v in views]),
                            ids=np.concatenate([v.ids for v in views]),
                            view=np.concatenate([np.full(v.n, k) for k, v in enumerate(views)]),
                            image_size=np.array(cal.image_size))
    return p


def load_views(path: str | Path, camera: str) -> list[Detection]:
    """The views saved with a camera's lens calibration."""
    p = Path(path)
    f = p.parent / p.stem / f"{camera}_lens.npz"
    if not f.exists():
        return []
    z = np.load(f)
    size = tuple(int(v) for v in z["image_size"])
    out = []
    for k in np.unique(z["view"]):
        m = z["view"] == k
        out.append(Detection(z["corners"][m], z["ids"][m], np.zeros(0, int), np.zeros((0, 4, 2), np.float32), [],
                             size))
    return out


# --------------------------------------------------------------------------- simulated views


class SimulatedLens:
    """Renders the board seen through a known lens at any pose, for tests and the simulated rig."""

    def __init__(self, spec: BoardSpec, image_size=(640, 480), f_px: float | None = None,
                 dist=(-0.12, 0.05, 0.0005, -0.0003, 0.0), px_per_mm: float = 4.0, seed: int = 0):
        self.spec = spec
        self.size = tuple(image_size)
        w, h = self.size
        f = f_px or 1.1 * w
        self.K = np.array([[f, 0, w / 2 + 3.5], [0, f * 1.002, h / 2 - 2.5], [0, 0, 1]], float)
        self.dist = np.array(dist, float)
        self.ppm = px_per_mm
        self.margin_mm = 12.0
        self.texture = spec.image(px_per_mm, self.margin_mm)
        u, v = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
        pts = np.stack([u.ravel(), v.ravel()], axis=1).reshape(-1, 1, 2)
        n = cv2.undistortPoints(pts, self.K, self.dist, R=None, P=None, criteria=
                                    (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 1e-9))
        self.rays = np.concatenate([n.reshape(-1, 2), np.ones((w * h, 1))], axis=1)  # (w*h, 3) normalized rays
        self.rng = np.random.default_rng(seed)

    def render(self, rvec, tvec, background: np.ndarray | None = None, noise: float = 2.0) -> np.ndarray:
        """Grey image of the board at pose (rvec, tvec): board mm -> camera mm."""
        w, h = self.size
        R, _ = cv2.Rodrigues(np.asarray(rvec, float))
        Hn = np.column_stack([R[:, 0], R[:, 1], np.ravel(tvec)])  # board (x, y, 1) -> ray * depth
        b = self.rays @ np.linalg.inv(Hn).T
        z = b[:, 2:3]
        xy = b[:, :2] / np.where(np.abs(z) < 1e-12, 1e-12, z)
        mx = ((xy[:, 0] + self.margin_mm) * self.ppm - 0.5).reshape(h, w).astype(np.float32)
        my = ((xy[:, 1] + self.margin_mm) * self.ppm - 0.5).reshape(h, w).astype(np.float32)
        behind = (z.reshape(h, w) <= 0)
        mx[behind] = -1e4
        img = cv2.remap(self.texture, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        inside = cv2.remap(np.full(self.texture.shape, 255, np.uint8), mx, my, cv2.INTER_NEAREST,
                           borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
        bg = background if background is not None else np.full((h, w), 70, np.uint8)
        if bg.ndim == 3:
            bg = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY)
        out = np.where(inside, (img.astype(np.float32) * 0.82 + 25), bg.astype(np.float32))
        out = cv2.GaussianBlur(out, (0, 0), 0.6)
        out += self.rng.normal(0, noise, out.shape)
        return np.clip(out, 0, 255).astype(np.uint8)

    def project(self, rvec, tvec) -> np.ndarray:
        """Image position of every inner corner (n_corners, 2)."""
        pts = np.column_stack([self.spec.corner_xy, np.zeros(self.spec.n_corners)])
        p, _ = cv2.projectPoints(pts, np.asarray(rvec, float), np.asarray(tvec, float), self.K, self.dist)
        return p.reshape(-1, 2)

    def random_pose(self, rng=None, min_visible: float = 0.7) -> tuple[np.ndarray, np.ndarray]:
        """A pose with the board mostly in view: 0.5-1.1 m away, tilted up to ±40°, anywhere in the image."""
        rng = rng or self.rng
        w, h = self.size
        bw, bh = self.spec.size_mm
        for _ in range(200):
            z = self.K[1, 1] * bh / (rng.uniform(0.3, 0.85) * h)  # the board fills 30-85 % of the image height
            tilt = np.radians([rng.uniform(-40, 40), rng.uniform(-40, 40), rng.uniform(-25, 25)])
            R = cv2.Rodrigues(np.array([tilt[0], 0, 0]))[0] @ cv2.Rodrigues(np.array([0, tilt[1], 0]))[0] @ \
                cv2.Rodrigues(np.array([0, 0, tilt[2]]))[0]
            u, v = rng.uniform(0.15, 0.85) * w, rng.uniform(0.15, 0.85) * h
            ray = np.linalg.inv(self.K) @ np.array([u, v, 1.0])
            centre = ray / ray[2] * z
            t = centre - R @ np.array([bw / 2, bh / 2, 0.0])
            rvec = cv2.Rodrigues(R)[0].ravel()
            p = self.project(rvec, t)
            vis = ((p[:, 0] > 5) & (p[:, 0] < w - 5) & (p[:, 1] > 5) & (p[:, 1] < h - 5)).mean()
            if vis >= min_visible:
                return rvec, t
        return rvec, t


class SimulatedBoardFeed:
    """A board moved by hand in front of a simulated camera: held still for `hold_s`, then moved to a new pose."""

    def __init__(self, lens: SimulatedLens, hold_s: float = 1.6, move_s: float = 0.6, seed: int = 3):
        self.lens = lens
        self.hold_s, self.move_s = hold_s, move_s
        self.rng = np.random.default_rng(seed)
        self.poses = [lens.random_pose(self.rng) for _ in range(2)]

    def frame(self, t: float, background: np.ndarray | None = None) -> np.ndarray:
        period = self.hold_s + self.move_s
        k = int(t // period)
        while len(self.poses) < k + 2:
            self.poses.append(self.lens.random_pose(self.rng))
        u = (t - k * period - self.hold_s) / self.move_s
        (r0, t0), (r1, t1) = self.poses[k], self.poses[k + 1]
        if u <= 0:
            return self.lens.render(r0, t0, background)
        u = min(1.0, u)
        return self.lens.render(r0 + u * (r1 - r0), t0 + u * (t1 - t0), background)
