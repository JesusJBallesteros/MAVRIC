"""3D places: each place ROI as a horizontal surface (or a box standing on it) in the world frame of the camera
calibration, and its outline in every calibrated camera.

A place is drawn in the ROI camera as before. Its 3D outline is the drawn polygon
carried along that camera's rays onto the horizontal plane at the surface's
height (z, mm above the floor origin of the camera positions). The height is
typed (measured), or found by marking one spot of the surface in the ROI camera
and in another camera (triangulated). With a box height the place is the volume
above its surface, so its outline in another camera also covers a subject
standing on it. The sync LED is a point: its box in the ROI camera, carried to
the LED's height.

Nothing here changes the ROIs drawn in the ROI camera: they stay exactly as drawn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from ..config import Roi, RoiSet
from .calibration import LensCalibration, load_calibrations
from .extrinsics import CameraPose, load_poses

UNDISTORT_CRITERIA = (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 40, 1e-10)
MIN_LED_BOX_PX = 8  # the sync LED box in another camera is at least this big
MAX_RAY_GAP_MM = 25.0  # two clicks further apart than this (closest approach of the rays) are not the same spot


@dataclass
class CameraModel:
    """A calibrated camera: lens and position (world -> camera, mm)."""

    camera: str
    K: np.ndarray
    dist: np.ndarray
    T: np.ndarray
    size: tuple[int, int]

    @classmethod
    def from_calibration(cls, lens: LensCalibration, pose: CameraPose) -> CameraModel:
        return cls(lens.camera, np.asarray(lens.K, float), np.asarray(lens.dist, float).ravel(),
                   np.asarray(pose.T, float), (int(lens.image_size[0]), int(lens.image_size[1])))

    @property
    def R(self) -> np.ndarray:
        return self.T[:3, :3]

    @property
    def centre(self) -> np.ndarray:
        return -self.R.T @ self.T[:3, 3]

    def _r_max(self) -> float:
        """Largest normalised radius inside the image (with a margin): beyond it, the distortion model folds back."""
        if not hasattr(self, "_rmax"):
            w, h = self.size
            c = np.array([[0, 0], [w, 0], [w, h], [0, h], [w / 2, 0], [w, h / 2], [w / 2, h], [0, h / 2]], float)
            n = self.normalised(c)
            self._rmax = float(np.linalg.norm(n, axis=1).max() * 1.15)
        return self._rmax

    def normalised(self, px) -> np.ndarray:
        px = np.asarray(px, np.float64).reshape(-1, 1, 2)
        return cv2.undistortPoints(px, self.K, self.dist, R=None, P=None, criteria=UNDISTORT_CRITERIA).reshape(-1, 2)

    def to_camera(self, X) -> np.ndarray:
        X = np.asarray(X, float).reshape(-1, 3)
        return X @ self.R.T + self.T[:3, 3]

    def project(self, X) -> tuple[np.ndarray, np.ndarray]:
        """Image points (n, 2) of world points, and which of them the camera can see (in front, inside its view)."""
        Xc = self.to_camera(X)
        ok = Xc[:, 2] > 1.0
        z = np.where(ok, Xc[:, 2], 1.0)
        n = Xc[:, :2] / z[:, None]
        ok &= np.linalg.norm(n, axis=1) <= self._r_max()
        p, _ = cv2.projectPoints(Xc.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), self.K, self.dist)
        return p.reshape(-1, 2), ok

    def rays(self, px) -> np.ndarray:
        """Unit viewing directions (n, 3) in the world through image points."""
        n = self.normalised(px)
        d = np.column_stack([n, np.ones(len(n))]) @ self.R  # R.T @ d for every row
        return d / np.linalg.norm(d, axis=1, keepdims=True)

    def on_plane(self, px, z: float) -> np.ndarray:
        """World points where the rays through image points meet the horizontal plane at height z (nan: never)."""
        d = self.rays(px)
        C = self.centre
        with np.errstate(divide="ignore", invalid="ignore"):
            s = (z - C[2]) / d[:, 2]
        s = np.where(np.isfinite(s) & (s > 0), s, np.nan)
        return C + s[:, None] * d

    def at_depth(self, px, depth: float) -> np.ndarray:
        """World points of image points at a distance `depth` (mm) along the camera's axis."""
        n = self.normalised(px)
        Xc = np.column_stack([n * depth, np.full(len(n), depth)])
        return (Xc - self.T[:3, 3]) @ self.R  # R.T @ (Xc - t)


def load_cameras(path: str | Path) -> tuple[dict[str, CameraModel], dict]:
    """Every camera with both a lens calibration and a position in calibration.yaml, and the `world:` section."""
    lenses = load_calibrations(path)
    poses, world = load_poses(path)
    return {cid: CameraModel.from_calibration(lenses[cid], p) for cid, p in poses.items() if cid in lenses}, world


def problem(rs: RoiSet, cams: dict[str, CameraModel], world: dict, roi_camera: str | None) -> str | None:
    """Why the places cannot be placed in 3D or shown in other cameras (None: they can)."""
    if not roi_camera:
        return "no camera in rig.yaml"
    if roi_camera not in cams:
        return (f"{roi_camera} (the ROI camera) has no lens calibration and position yet: Calibrate cameras…, tabs 1 "
                "and 2")
    if world.get("frame") != "floor":
        return ("the camera positions have no floor origin, so heights have no meaning: set it in Calibrate cameras…, "
                "tab 2 (Board on the floor) and compute the positions again")
    if rs.image_size and tuple(rs.image_size) != tuple(cams[roi_camera].size):
        return (f"the ROIs were drawn on a {rs.image_size[0]}×{rs.image_size[1]} image, {roi_camera} is calibrated "
                f"at {cams[roi_camera].size[0]}×{cams[roi_camera].size[1]}")
    return None


def place_3d(roi: Roi, cam0: CameraModel) -> Roi:
    """The ROI with its 3D outline (points3d) worked out from its height; unchanged without one."""
    if roi.plane_z_mm is None:
        return roi
    z = float(roi.plane_z_mm)
    pts = np.asarray(roi.points, float)
    if roi.kind == "sync":
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        c = cam0.on_plane([((x0 + x1) / 2, (y0 + y1) / 2)], z)[0]
        if not np.all(np.isfinite(c)):
            return replace(roi, points3d=None)
        depth = float(cam0.to_camera(c)[0, 2])
        X = cam0.at_depth([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], depth)
    else:
        X = cam0.on_plane(pts, z)
        if not np.all(np.isfinite(X)):
            return replace(roi, points3d=None)
    return replace(roi, points3d=[tuple(round(float(v), 2) for v in p) for p in X])


def centre_3d(roi: Roi) -> np.ndarray | None:
    if not roi.points3d:
        return None
    return np.asarray(roi.points3d, float).mean(axis=0)


def _densify(P: np.ndarray, closed: bool = True, step: int = 12) -> np.ndarray:
    """Points along each edge, so straight 3D edges bend with the lens distortion when projected."""
    out = []
    n = len(P)
    for i in range(n if closed else n - 1):
        a, b = P[i], P[(i + 1) % n]
        for u in np.linspace(0, 1, step, endpoint=False):
            out.append(a + u * (b - a))
    if not closed:
        out.append(P[-1])
    return np.array(out)


def outline_in(roi: Roi, cam: CameraModel) -> list[tuple[float, float]] | None:
    """The place's outline in a camera (image points), or None if that camera does not see it."""
    if not roi.points3d:
        return None
    P = np.asarray(roi.points3d, float)
    w, h = cam.size
    if roi.kind == "sync":
        p, ok = cam.project(P)
        if not ok.all():
            return None
        (x0, y0), (x1, y1) = p.min(axis=0), p.max(axis=0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hw, hh = max((x1 - x0) / 2, MIN_LED_BOX_PX / 2), max((y1 - y0) / 2, MIN_LED_BOX_PX / 2)
        if not (0 <= cx < w and 0 <= cy < h):
            return None
        return [(cx - hw, cy - hh), (cx + hw, cy - hh), (cx + hw, cy + hh), (cx - hw, cy + hh)]
    dense = _densify(P)
    if roi.box_mm and roi.box_mm > 0:
        top = dense + np.array([0.0, 0.0, float(roi.box_mm)])
        p, ok = cam.project(np.vstack([dense, top]))
        if ok.mean() < 0.5:
            return None
        hull = cv2.convexHull(p[ok].astype(np.float32)).reshape(-1, 2)
        pts = hull.astype(float)
    else:
        p, ok = cam.project(dense)
        if ok.mean() < 0.5:
            return None
        pts = p[ok]
    inside = (pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)
    if not inside.any():
        return None
    return [(round(float(x), 1), round(float(y), 1)) for x, y in pts]


def rois_in_camera(rs: RoiSet, cam_id: str, cams: dict[str, CameraModel], roi_camera: str | None) -> RoiSet:
    """The ROIs as seen by another camera: each placed ROI's outline there (ROIs it does not see are left out).

    The ROI camera gets the ROIs exactly as drawn."""
    if cam_id == roi_camera:
        return rs
    cam, cam0 = cams.get(cam_id), cams.get(roi_camera or "")
    if cam is None or cam0 is None:
        return RoiSet([], None)
    out = []
    for r in rs.rois:
        pts = outline_in(place_3d(r, cam0), cam)
        if pts:
            out.append(Roi(r.name, pts, r.antenna, r.kind))
    return RoiSet(out, cam.size)


def refresh_3d(rs: RoiSet, cam0: CameraModel | None) -> RoiSet:
    """Every ROI's 3D outline worked out again from its height (after its polygon was edited)."""
    if cam0 is None:
        return rs
    return RoiSet([place_3d(r, cam0) for r in rs.rois], rs.image_size)


# --------------------------------------------------------------------------- marking a spot in two cameras


def triangulate(ca: CameraModel, pa, cb: CameraModel, pb) -> tuple[np.ndarray, float]:
    """The world point seen at image point pa in camera a and pb in camera b, and how far apart the two rays pass
    (mm): a few mm when both clicks are on the same spot."""
    Ca, Cb = ca.centre, cb.centre
    da, db = ca.rays([pa])[0], cb.rays([pb])[0]
    w0 = Ca - Cb
    a, b, c = da @ da, da @ db, db @ db
    d, e = da @ w0, db @ w0
    den = a * c - b * b
    if abs(den) < 1e-12:  # parallel rays
        return np.full(3, np.nan), float("inf")
    s = (b * e - c * d) / den
    t = (a * e - b * d) / den
    Pa, Pb = Ca + s * da, Cb + t * db
    if s <= 0 or t <= 0:
        return np.full(3, np.nan), float("inf")
    return (Pa + Pb) / 2, float(np.linalg.norm(Pa - Pb))


def epipolar_curve(ca: CameraModel, pa, cb: CameraModel, near_mm: float = 100.0, far_mm: float = 8000.0,
                   n: int = 120) -> np.ndarray:
    """Where the spot clicked at pa in camera a can be in camera b: its ray, projected (a curve with distortion)."""
    d = ca.rays([pa])[0]
    s = np.geomspace(near_mm, far_mm, n)
    p, ok = cb.project(ca.centre + s[:, None] * d)
    return p[ok]
