"""Camera positions: every camera in one coordinate frame, from the ChArUco board seen by several cameras at once.

Each camera needs its lens calibration first (mavric.video.calibration). A
*snapshot* is the board held still where two or more cameras see it; the
cameras run freely, so the board must be still, not the frames simultaneous.

    1. Each camera's view of each snapshot gives the board's pose in that camera (PnP).
    2. Cameras are placed one by one from the first camera (the ROI camera): each
       new camera joins through the placed camera it shares the most snapshots
       with, and the pair is fitted on all their common corners
       (cv2.stereoCalibrate, lenses fixed).
    3. The world frame: with an origin view (the board lying flat on the floor,
       seen by any placed camera) the origin is the board's top-left outer
       corner, x along its columns, y towards its top edge, z up, in mm.
       Without one, the world is the first camera's own frame.

Checks: each camera's corners against the board placed by the other cameras
(px), and corners triangulated from two cameras against the board's true shape
(mm).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import yaml

from .calibration import HEADER, BoardSpec, Detection, LensCalibration, SimulatedBoardFeed, SimulatedLens, \
    view_descriptor

FLIP = np.diag([1.0, -1.0, -1.0, 1.0])  # board <-> world for a board lying face up: y and z turned over
MIN_CORNERS = 8


def to_T(rvec, tvec) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = cv2.Rodrigues(np.asarray(rvec, float).reshape(3, 1))[0]
    T[:3, 3] = np.ravel(tvec)
    return T


def inv_T(T: np.ndarray) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = T[:3, :3].T
    out[:3, 3] = -T[:3, :3].T @ T[:3, 3]
    return out


def angle_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    """Angle of the rotation between two rotation matrices."""
    c = (np.trace(Ra.T @ Rb) - 1) / 2
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def board_pose(spec: BoardSpec, det: Detection, lens: LensCalibration) -> tuple[np.ndarray, float] | None:
    """Board -> camera transform (4x4, mm) and its reprojection error (px), or None."""
    if det.n < MIN_CORNERS:
        return None
    obj, img = spec.board.matchImagePoints(det.corners.reshape(-1, 1, 2), det.ids.reshape(-1, 1))
    obj, img = np.asarray(obj, np.float64).reshape(-1, 3), np.asarray(img, np.float64).reshape(-1, 2)
    n, rvecs, tvecs, errs = cv2.solvePnPGeneric(obj, img, lens.K, lens.dist, flags=cv2.SOLVEPNP_IPPE)
    if not n:
        return None
    k = int(np.argmin(np.ravel(errs)))
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, lens.K, lens.dist, rvecs[k], tvecs[k])
    p, _ = cv2.projectPoints(obj, rvec, tvec, lens.K, lens.dist)
    err = float(np.sqrt(np.mean(np.sum((p.reshape(-1, 2) - img) ** 2, axis=1))))
    return to_T(rvec, tvec), err


# --------------------------------------------------------------------------- snapshots


@dataclass
class Snapshot:
    dets: dict[str, Detection]  # camera -> what it saw, the board held still

    @property
    def cameras(self) -> list[str]:
        return [c for c, d in self.dets.items() if d.n >= MIN_CORNERS and not d.misplaced]


class SnapshotCollector:
    """Picks snapshots: the board still in every camera that sees it, two cameras or more, in a new position."""

    def __init__(self, spec: BoardSpec, still_px: float = 0.8, still_frames: int = 5, new_dist: float = 0.12):
        self.spec = spec
        self.still_px, self.still_frames, self.new_dist = still_px, still_frames, new_dist
        self.snapshots: list[Snapshot] = []
        self._prev: dict[str, Detection] = {}
        self._still: dict[str, int] = defaultdict(int)
        self._descs: dict[str, list[np.ndarray]] = defaultdict(list)

    def clear(self) -> None:
        self.snapshots, self._prev = [], {}
        self._still.clear()
        self._descs.clear()

    def _motion(self, cam: str, det: Detection) -> float:
        p = self._prev.get(cam)
        if p is None:
            return np.inf
        _, a, b = np.intersect1d(p.ids, det.ids, return_indices=True)
        return float(np.median(np.linalg.norm(p.corners[a] - det.corners[b], axis=1))) if len(a) >= 4 else np.inf

    def offer(self, dets: dict[str, Detection], auto: bool = True, force: bool = False) -> str:
        """The latest detection of each camera. Returns: captured | moving | seen | few | misplaced."""
        for cam, d in dets.items():
            self._still[cam] = self._still[cam] + 1 if self._motion(cam, d) <= self.still_px else 0
            self._prev[cam] = d
        if any(d.misplaced for d in dets.values()):
            return "misplaced"
        seeing = [c for c, d in dets.items() if d.n >= MIN_CORNERS]
        if len(seeing) < 2:
            return "few"
        descs = {c: view_descriptor(self.spec, dets[c]) for c in seeing}
        if not force:
            if not auto or any(self._still[c] < self.still_frames for c in seeing):
                return "moving" if auto else "seen"
            nov = 0.0
            for c, dsc in descs.items():
                old = self._descs[c]
                nov = max(nov, np.inf if not old else float(np.abs(np.array(old) - dsc)[:, :5].sum(axis=1).min()))
            if nov < self.new_dist:
                return "seen"
        self.snapshots.append(Snapshot({c: dets[c] for c in seeing}))
        for c, dsc in descs.items():
            self._descs[c].append(dsc)
        return "captured"

    def pair_counts(self) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = defaultdict(int)
        for s in self.snapshots:
            cams = sorted(s.cameras)
            for i, a in enumerate(cams):
                for b in cams[i + 1:]:
                    out[(a, b)] += 1
        return dict(out)


# --------------------------------------------------------------------------- solving


@dataclass
class CameraPose:
    camera: str
    T: np.ndarray  # world -> camera, 4x4 (mm)
    via: str = ""  # the camera it was placed from ("" for the first camera)
    n_views: int = 0  # snapshots shared with `via`
    pair_rms_px: float = 0.0  # stereo fit with `via`
    rms_px: float = float("nan")  # its corners against the board placed by the other cameras

    @property
    def centre(self) -> np.ndarray:
        """Camera position in the world (mm)."""
        return -self.T[:3, :3].T @ self.T[:3, 3]

    @property
    def direction(self) -> np.ndarray:
        """Viewing direction (unit vector) in the world."""
        return self.T[2, :3].copy()

    def as_dict(self) -> dict:
        return {"T_world_to_camera": np.round(self.T, 6).tolist(), "centre_mm": np.round(self.centre, 2).tolist(),
                "via": self.via, "n_views": int(self.n_views), "pair_rms_px": round(float(self.pair_rms_px), 4),
                "rms_px": None if not np.isfinite(self.rms_px) else round(float(self.rms_px), 4)}

    @classmethod
    def from_dict(cls, camera: str, d: dict) -> CameraPose:
        rms = d.get("rms_px")
        return cls(camera, np.array(d["T_world_to_camera"], float), str(d.get("via", "")), int(d.get("n_views", 0)),
                   float(d.get("pair_rms_px", 0.0)), float("nan") if rms is None else float(rms))


@dataclass
class RigSolution:
    poses: dict[str, CameraPose]
    world: str  # "floor" (origin view) or the first camera's id
    err_3d_mm: float = float("nan")  # median distance of triangulated corners from the board's shape
    err_3d_p95_mm: float = float("nan")
    n_snapshots: int = 0
    unplaced: list[str] = field(default_factory=list)  # cameras that share no snapshot with the placed ones
    date: str = ""
    dropped: int = 0  # snapshots left out as outliers

    def verdict(self) -> tuple[bool, list[str]]:
        why = []
        if self.unplaced:
            why.append(f"not placed: {', '.join(self.unplaced)} (show the board to it together with a placed camera)")
        for p in self.poses.values():
            if p.via and p.n_views < 5:
                why.append(f"{p.camera}: only {p.n_views} snapshots with {p.via} (want ≥ 5)")
            if np.isfinite(p.rms_px) and p.rms_px > 1.5:
                why.append(f"{p.camera}: {p.rms_px:.2f} px against the other cameras (want ≤ 1.5): did the camera "
                           "or its lens move since its lens calibration?")
        if np.isfinite(self.err_3d_mm) and self.err_3d_mm > 2.0:
            why.append(f"3D error {self.err_3d_mm:.1f} mm (want ≤ 2 mm)")
        return not why, why

    def summary(self) -> str:
        ok, _ = self.verdict()
        lines = [f"{'PASS' if ok else 'FAIL'}  {len(self.poses)} camera(s) placed from {self.n_snapshots} snapshots"
                 + (f" ({self.dropped} left out)" if self.dropped else "") + "; "
                 f"world: {'the board on the floor' if self.world == 'floor' else self.world + ' (no origin view)'}"
                 + (f"; 3D error {self.err_3d_mm:.2f} mm (p95 {self.err_3d_p95_mm:.2f})"
                    if np.isfinite(self.err_3d_mm) else "")]
        for p in self.poses.values():
            c = p.centre
            lines.append(f"  {p.camera}: at ({c[0]:.0f}, {c[1]:.0f}, {c[2]:.0f}) mm"
                         + (f", via {p.via} ({p.n_views} snapshots, {p.pair_rms_px:.2f} px)" if p.via else "")
                         + (f", {p.rms_px:.2f} px vs the others" if np.isfinite(p.rms_px) else ""))
        return "\n".join(lines)


def _pair_fit(spec: BoardSpec, snaps: list[Snapshot], a: str, b: str, la: LensCalibration, lb: LensCalibration):
    """a -> b transform (4x4) and RMS from all corners both cameras saw in the same snapshots."""
    obj, ia, ib = [], [], []
    for s in snaps:
        da, db = s.dets[a], s.dets[b]
        common, ka, kb = np.intersect1d(da.ids, db.ids, return_indices=True)
        if len(common) < 6:
            continue
        o = np.column_stack([spec.corner_xy[common], np.zeros(len(common))]).astype(np.float32)
        obj.append(o.reshape(-1, 1, 3))
        ia.append(da.corners[ka].reshape(-1, 1, 2).astype(np.float32))
        ib.append(db.corners[kb].reshape(-1, 1, 2).astype(np.float32))
    if len(obj) < 1:
        return None
    rms, *_rest, R, T, _E, _F = cv2.stereoCalibrate(
        obj, ia, ib, la.K, la.dist, lb.K, lb.dist, tuple(la.image_size), flags=cv2.CALIB_FIX_INTRINSIC,
        criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 200, 1e-9))
    M = np.eye(4)
    M[:3, :3], M[:3, 3] = R, np.ravel(T)
    return M, float(rms), len(obj)


def triangulate(P: dict[str, CameraPose], lenses: dict[str, LensCalibration], a: str, b: str,
                pa: np.ndarray, pb: np.ndarray) -> np.ndarray:
    """World points (n, 3) from matching image points of cameras a and b."""
    na = cv2.undistortPoints(pa.reshape(-1, 1, 2).astype(np.float64), lenses[a].K, lenses[a].dist).reshape(-1, 2)
    nb = cv2.undistortPoints(pb.reshape(-1, 1, 2).astype(np.float64), lenses[b].K, lenses[b].dist).reshape(-1, 2)
    X = cv2.triangulatePoints(P[a].T[:3], P[b].T[:3], na.T, nb.T)
    return (X[:3] / X[3]).T


def _rigid_fit_residuals(model: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Distances after the best rigid fit of `model` onto `pts` (Kabsch)."""
    cm, cp = model.mean(axis=0), pts.mean(axis=0)
    U, _, Vt = np.linalg.svd((model - cm).T @ (pts - cp))
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    return np.linalg.norm((model - cm) @ R.T + cp - pts, axis=1)


def solve_rig(spec: BoardSpec, snapshots: list[Snapshot], lenses: dict[str, LensCalibration], reference: str,
              origin: Snapshot | None = None, drop_outliers: bool = True) -> RigSolution:
    """Place every camera that can be reached from `reference` through shared snapshots.

    Snapshots whose cameras disagree by more than 3 x the median (and over 1 px)
    are dropped once and the rig solved again.
    """
    if reference not in lenses:
        raise ValueError(f"{reference} has no lens calibration")
    cams = sorted({c for s in snapshots for c in s.cameras if c in lenses} | {reference})
    shared: dict[tuple[str, str], list[Snapshot]] = defaultdict(list)
    for s in snapshots:
        cs = [c for c in s.cameras if c in lenses]
        for i, a in enumerate(cs):
            for b in cs[i + 1:]:
                shared[(a, b)].append(s)
                shared[(b, a)].append(s)
    M = {reference: np.eye(4)}  # reference frame -> camera
    poses = {reference: CameraPose(reference, np.eye(4))}
    while True:  # grow from the placed cameras, best-connected first
        best = max(((len(shared[(a, b)]), a, b) for a in M for b in cams if b not in M), default=None)
        if best is None or best[0] == 0:
            break
        n, a, b = best
        fit = _pair_fit(spec, shared[(a, b)], a, b, lenses[a], lenses[b])
        if fit is None:
            break
        Mab, rms, n_used = fit
        M[b] = Mab @ M[a]
        poses[b] = CameraPose(b, M[b], a, n_used, rms)
    world = reference
    if origin is not None:
        seen = [(origin.dets[c].n, c) for c in origin.cameras if c in M]
        if seen:
            c = max(seen)[1]
            got = board_pose(spec, origin.dets[c], lenses[c])
            if got is not None:
                T_c_world = got[0] @ FLIP  # world -> board -> camera
                T_ref_world = inv_T(M[c]) @ T_c_world
                for p in poses.values():
                    p.T = M[p.camera] @ T_ref_world
                world = "floor"
    sol = RigSolution(poses, world, n_snapshots=len(snapshots), unplaced=[c for c in cams if c not in M],
                      date=dt.datetime.now().isoformat(timespec="seconds"))
    per_snap = _check(spec, sol, snapshots, lenses)
    if drop_outliers and len(per_snap) >= 6:
        e = np.array([v for v in per_snap.values()])
        bad = [k for k, v in per_snap.items() if v > max(3 * float(np.median(e)), 1.0)]
        if bad:  # the board moved in one camera while another took its frame: solve again without those
            keep = [s for k, s in enumerate(snapshots) if k not in bad]
            sol = solve_rig(spec, keep, lenses, reference, origin, drop_outliers=False)
            sol.dropped = len(bad)
            sol.n_snapshots = len(snapshots)
    return sol


def _check(spec: BoardSpec, sol: RigSolution, snapshots: list[Snapshot],
           lenses: dict[str, LensCalibration]) -> dict[int, float]:
    """Fill each camera's reprojection error against the others, and the 3D error of triangulated corners.

    Returns each snapshot's RMS disagreement between its cameras (px), by index.
    """
    P = sol.poses
    errs: dict[str, list[np.ndarray]] = defaultdict(list)
    per_snap: dict[int, float] = {}
    d3 = []
    for k, s in enumerate(snapshots):
        cs = [c for c in s.cameras if c in P]
        if len(cs) < 2:
            continue
        for c in cs:
            others = sorted(((s.dets[o].n, o) for o in cs if o != c), reverse=True)
            o = others[0][1]
            got = board_pose(spec, s.dets[o], lenses[o])
            if got is None:
                continue
            T_world_board = inv_T(P[o].T) @ got[0]
            T_c_board = P[c].T @ T_world_board
            d = s.dets[c]
            obj = np.column_stack([spec.corner_xy[d.ids], np.zeros(d.n)])
            rvec = cv2.Rodrigues(T_c_board[:3, :3])[0]
            p, _ = cv2.projectPoints(obj, rvec, T_c_board[:3, 3], lenses[c].K, lenses[c].dist)
            e = np.linalg.norm(p.reshape(-1, 2) - d.corners, axis=1)
            errs[c].append(e)
            per_snap[k] = max(per_snap.get(k, 0.0), float(np.sqrt(np.mean(e ** 2))))
        a, b = [x for _, x in sorted(((s.dets[c].n, c) for c in cs), reverse=True)[:2]]
        common, ka, kb = np.intersect1d(s.dets[a].ids, s.dets[b].ids, return_indices=True)
        if len(common) >= 6:
            X = triangulate(P, lenses, a, b, s.dets[a].corners[ka], s.dets[b].corners[kb])
            model = np.column_stack([spec.corner_xy[common], np.zeros(len(common))])
            d3.append(_rigid_fit_residuals(model, X))
    for c, e in errs.items():
        P[c].rms_px = float(np.sqrt(np.mean(np.concatenate(e) ** 2)))
    if d3:
        d3 = np.concatenate(d3)
        sol.err_3d_mm, sol.err_3d_p95_mm = float(np.median(d3)), float(np.percentile(d3, 95))
    return per_snap


# --------------------------------------------------------------------------- picture


def plot_rig(sol: RigSolution, spec: BoardSpec, path: str | Path | None = None, width_px: int = 760) -> np.ndarray:
    """Top and side views of the cameras (and the origin board): an RGB image, also saved to `path` if given."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..analysis.plots import AXIS, INK, INK2, MUTED, S_MANUAL, STYLE

    poses = list(sol.poses.values())
    C = np.array([p.centre for p in poses])
    D = np.array([p.direction for p in poses])
    span = max(200.0, float(np.ptp(C, axis=0).max()) if len(C) > 1 else 500.0)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(width_px / 100, width_px / 100 * 0.48), dpi=100)
        for ax, (i, j), title in zip(axes, [(0, 1), (0, 2)], ["From above (x, y)", "From the side (x, z)"]):
            if sol.world == "floor":
                bw, bh = spec.size_mm
                xs, ys = ([0, bw, bw, 0, 0], [0, 0, -bh, -bh, 0]) if j == 1 else ([0, bw], [0, 0])
                ax.plot(xs, ys, color=INK2, lw=1.2)
                ax.plot([0], [0], "o", color=INK2, ms=3)
            for p, c, d in zip(poses, C, D):
                ax.plot(c[i], c[j], "o", color=S_MANUAL, ms=6, zorder=3)
                ax.annotate("", xy=(c[i] + d[i] * 0.25 * span, c[j] + d[j] * 0.25 * span), xytext=(c[i], c[j]),
                            arrowprops={"arrowstyle": "->", "color": S_MANUAL, "lw": 1.2})
                ax.text(c[i], c[j], f"  {p.camera}", color=INK, fontsize=8, va="bottom")
            ax.set_aspect("equal", adjustable="datalim")
            ax.set_title(title, fontsize=9)
            ax.set_xlabel("x (mm)")
            ax.set_ylabel(("y" if j == 1 else "z") + " (mm)")
            ax.grid(True, color=AXIS, lw=0.4, alpha=0.6)
        fig.text(0.01, 0.01, "Arrows: viewing directions." + (" Grey: the origin board on the floor."
                                                             if sol.world == "floor" else
                                                             f" World = {sol.world}'s frame (no origin view)."),
                 fontsize=7, color=MUTED)
        fig.tight_layout(rect=(0, 0.04, 1, 1))
        fig.canvas.draw()
        img = np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()
        if path is not None:
            fig.savefig(path, dpi=150)
        plt.close(fig)
    return img


# --------------------------------------------------------------------------- files


def save_poses(path: str | Path, sol: RigSolution) -> Path:
    """Add the camera positions to calibration.yaml (lens calibrations are kept; cameras not placed now lose theirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    d.setdefault("cameras", {})
    for c in d["cameras"].values():
        if isinstance(c, dict):
            c.pop("pose", None)  # positions belong together: an old one would not match the new world
    for cid, pose in sol.poses.items():
        d["cameras"].setdefault(cid, {})
        d["cameras"][cid]["pose"] = pose.as_dict()
    d["world"] = {"frame": sol.world, "date": sol.date, "n_snapshots": int(sol.n_snapshots),
                  "err_3d_mm": None if not np.isfinite(sol.err_3d_mm) else round(sol.err_3d_mm, 3),
                  "note": ("origin: the top-left outer corner of the board lying on the floor; x along its columns, "
                           "y towards its top edge, z up; mm" if sol.world == "floor" else
                           f"{sol.world}'s own frame (no origin view): x right, y down, z along its view; mm")}
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(d, f, sort_keys=False, default_flow_style=None, allow_unicode=True, width=120)
    tmp.replace(p)
    return p


def load_poses(path: str | Path) -> tuple[dict[str, CameraPose], dict]:
    p = Path(path)
    if not p.exists():
        return {}, {}
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    poses = {str(cid): CameraPose.from_dict(str(cid), c["pose"]) for cid, c in (d.get("cameras") or {}).items()
             if isinstance(c, dict) and c.get("pose")}
    return poses, dict(d.get("world") or {})


# --------------------------------------------------------------------------- simulated rig


def look_at(position, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """World -> camera transform of a camera at `position` looking at `target` (image x right, y down)."""
    pos, tgt = np.asarray(position, float), np.asarray(target, float)
    z = tgt - pos
    z /= np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    T = np.eye(4)
    T[:3, :3] = np.stack([x, y, z])
    T[:3, 3] = -T[:3, :3] @ pos
    return T


class SimulatedRig:
    """Simulated cameras around a cage, each with its own lens, all seeing one board moved by hand.

    The world is the frame of the board lying on the floor (FLIP), so camera
    positions found from an origin view can be compared with the truth here.
    """

    LAYOUT = [  # position, target (mm)
        ((156.0, -1050.0, 820.0), (156.0, -195.0, 150.0)),  # front, from above
        ((980.0, -120.0, 760.0), (156.0, -195.0, 150.0)),  # right side
        ((-560.0, 420.0, 600.0), (60.0, -120.0, 180.0)),  # back left, nearer
    ]

    def __init__(self, spec: BoardSpec, sizes: dict[str, tuple[int, int]], seed: int = 0, near: float | None = None):
        self.spec = spec
        self.rng = np.random.default_rng(seed + 77)
        self.lenses: dict[str, SimulatedLens] = {}
        self.T: dict[str, np.ndarray] = {}
        if near is None:  # small images: come closer
            near = float(np.clip(min(s[0] for s in sizes.values()) / 1024, 0.6, 1.0))
        for k, (cid, size) in enumerate(sizes.items()):
            w = size[0]
            self.lenses[cid] = SimulatedLens(spec, size, f_px=w * (1.05 + 0.08 * k),
                                             dist=(-0.10 - 0.03 * k, 0.04, 0.0004, -0.0003, 0.0), seed=seed + k)
            pos, tgt = (np.array(v) for v in self.LAYOUT[k % len(self.LAYOUT)])
            self.T[cid] = look_at(tgt + near * (pos - tgt), tgt)
        self.feeds = {cid: SimulatedBoardFeed(lens, seed=seed + 10 + k) for k, (cid, lens) in
                      enumerate(self.lenses.items())}
        self.poses = [self.random_board(), self.random_board()]

    def lens_truth(self, cid: str) -> LensCalibration:
        """The simulated lens as a calibration (for tests that skip the lens step)."""
        lens = self.lenses[cid]
        return LensCalibration(cid, lens.size, lens.K.copy(), lens.dist.copy(), 0.0, 0, [], 1.0, 0,
                               {"fx": 0.0, "fy": 0.0, "cx": 0.0, "cy": 0.0})

    def visible(self, cid: str, T_world_board: np.ndarray, min_share: float = 0.6) -> bool:
        T = self.T[cid] @ T_world_board
        if T[2, 3] <= 0 or T[:3, 2] @ (-T[:3, 3]) >= 0:  # behind the camera, or seen from the back
            return False
        lens = self.lenses[cid]
        p = lens.project(cv2.Rodrigues(T[:3, :3])[0], T[:3, 3])
        w, h = lens.size
        return ((p[:, 0] > 5) & (p[:, 0] < w - 5) & (p[:, 1] > 5) & (p[:, 1] < h - 5)).mean() >= min_share

    def random_board(self, min_cams: int = 2) -> np.ndarray:
        """A board pose (board -> world) held up in the cage, facing the cameras, seen by `min_cams` of them."""
        bw, bh = self.spec.size_mm
        centres = np.array([inv_T(T)[:3, 3] for T in self.T.values()])
        for _ in range(500):
            c = np.array([self.rng.uniform(-150, 450), self.rng.uniform(-450, 100), self.rng.uniform(120, 420)])
            face = centres[self.rng.integers(len(centres))] * 0.5 + centres.mean(axis=0) * 0.5 - c
            face /= np.linalg.norm(face)
            face = face + self.rng.normal(0, 0.25, 3)
            face /= np.linalg.norm(face)
            bz = -face  # the board's z points away from the side with the markers
            ref = np.array([0.0, 0.0, 1.0]) if abs(bz[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
            bx = np.cross(ref, bz)
            bx /= np.linalg.norm(bx)
            by = np.cross(bz, bx)
            R = np.column_stack([bx, by, bz])
            roll = cv2.Rodrigues(bz * np.radians(self.rng.uniform(-30, 30)))[0]
            R = roll @ R
            T = np.eye(4)
            T[:3, :3] = R
            T[:3, 3] = c - R @ np.array([bw / 2, bh / 2, 0.0])
            if sum(self.visible(cid, T) for cid in self.T) >= min_cams:
                return T
        return T

    def render(self, cid: str, T_world_board: np.ndarray, background=None) -> np.ndarray:
        T = self.T[cid] @ T_world_board
        return self.lenses[cid].render(cv2.Rodrigues(T[:3, :3])[0], T[:3, 3], background)

    def floor_view(self, cid: str, background=None) -> np.ndarray:
        """The board lying on the floor at the world origin."""
        return self.render(cid, FLIP, background)

    def frame(self, cid: str, t: float, hold_s: float = 1.8, move_s: float = 0.7, background=None,
              floor: bool = False) -> np.ndarray:
        """The board moved by hand between poses seen by two cameras or more (or lying on the floor)."""
        if floor:
            return self.floor_view(cid, background)
        period = hold_s + move_s
        k = int(t // period)
        while len(self.poses) < k + 2:
            self.poses.append(self.random_board())
        u = (t - k * period - hold_s) / move_s
        A, B = self.poses[k], self.poses[k + 1]
        if u <= 0:
            return self.render(cid, A, background)
        u = min(1.0, u)
        ra, rb = cv2.Rodrigues(A[:3, :3])[0], cv2.Rodrigues(B[:3, :3])[0]
        T = np.eye(4)
        T[:3, :3] = cv2.Rodrigues(ra + u * (rb - ra))[0]
        T[:3, 3] = A[:3, 3] + u * (B[:3, 3] - A[:3, 3])
        return self.render(cid, T, background)
