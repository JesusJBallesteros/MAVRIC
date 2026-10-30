"""Has a camera moved? Its view now against a reference view of the cage.

Each camera's reference view (calibration/reference_<cam>.png next to
calibration.yaml) is the view the places and the camera position were made
for. It is taken at the first recording after they were saved (saving ROIs or
camera positions clears it), or by hand (acquire window: New reference).
Sessions copy it next to their calibration.yaml.

Features of the still scene (ORB corners) are matched between the two pictures
and a rotation + shift + scale is fitted with RANSAC, so birds and other moving
things do not count. The largest displacement anywhere in the picture says how
far it moved, measured against the picture's diagonal:

- under WARN_FRAC (0.5 %, 4 px at 640×480, 9 px at 1440×1080): in place;
- under MOVED_FRAC (2 %): shifted a little; the places are drawn slightly off;
- beyond: moved; put the camera back (the alignment overlay) or draw the ROIs /
  calibrate its position again.

It only warns: it never stops a recording.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

WARN_FRAC = 0.005
MOVED_FRAC = 0.02
MAX_SIDE = 960  # pictures are compared at most this large (speed)
ECC_SIDE = 480  # the whole-picture alignment works on pictures at most this large
MIN_CC = 0.3  # its correlation: below, the pictures are too different to align
MIN_INLIERS = 12
MIN_CELLS = 4  # matched details must come from at least this many parts (of GRID × GRID) of the picture
GRID = 4
DETAIL_TOL_PX = 8.0  # a detail is looked for this close to where the whole-picture alignment puts it
SEARCH_FRAC = 0.1  # without that alignment: this far (of the diagonal) from where it was
MAX_HAMMING = 64  # ORB descriptors (256 bits) further apart than this are different details
CHANGE_SD = 0.6  # aligned and normalised pictures differing more than this (in sd) changed there: details left out
OUTLINE_BGR = (255, 0, 255)  # the reference's outlines in the alignment overlay
LEVEL_TEXT = {"ok": "in place", "shifted": "shifted a little", "moved": "moved", "unknown": "cannot compare"}


def reference_path(cal_path: str | Path, cam_id: str) -> Path:
    return Path(cal_path).with_suffix("") / f"reference_{cam_id}.png"  # config/calibration/ for config/calibration.yaml


def save_reference(cal_path: str | Path, cam_id: str, img: np.ndarray) -> Path:
    p = reference_path(cal_path, cam_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(p), _gray(img))
    return p


def clear_references(cal_path: str | Path, cam_ids) -> list[str]:
    """Remove the reference views of these cameras (their places or position changed); the ones removed."""
    out = []
    for cid in cam_ids:
        p = reference_path(cal_path, cid)
        if p.exists():
            p.unlink()
            out.append(cid)
    return out


def copy_references(cal_path: str | Path, folder: str | Path) -> None:
    """Copy every reference view next to the calibration.yaml in `folder` (a session)."""
    import shutil

    src = Path(cal_path).with_suffix("")
    if not src.is_dir():
        return
    dst = Path(folder) / "calibration"
    for p in src.glob("reference_*.png"):
        dst.mkdir(exist_ok=True)
        shutil.copy2(p, dst / p.name)


def load_reference(cal_path: str | Path, cam_id: str) -> Reference | None:
    p = reference_path(cal_path, cam_id)
    if not p.exists():
        return None
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    return Reference(img, p.stat().st_mtime) if img is not None else None


def _gray(img: np.ndarray) -> np.ndarray:
    g = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if g.dtype != np.uint8:
        g = cv2.convertScaleAbs(g, alpha=255.0 / max(1.0, float(g.max())))
    return g


def _small(g: np.ndarray, side: int = MAX_SIDE) -> tuple[np.ndarray, float]:
    k = min(1.0, side / max(g.shape))
    return (cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else g), k


def _features(g: np.ndarray):
    """ORB corners and descriptors (a detector per call: checks run in more than one thread)."""
    return cv2.ORB_create(nfeatures=2000, fastThreshold=10).detectAndCompute(g, None)


class Reference:
    """A camera's reference view, with its features (and outlines, for the overlay) worked out once."""

    def __init__(self, img: np.ndarray, stamp: float = 0.0):
        self.gray = _gray(img)
        self.stamp = stamp
        self.size = (self.gray.shape[1], self.gray.shape[0])
        self.small, self.k = _small(self.gray)
        self.tiny, _ = _small(self.gray, ECC_SIDE)
        self.kp, self.des = _features(self.small)
        self._edges = None

    def edges(self) -> np.ndarray:
        if self._edges is None:
            e = cv2.Canny(cv2.GaussianBlur(self.gray, (5, 5), 0), 40, 120)
            self._edges = e > 0
        return self._edges


@dataclass
class Shift:
    """How the picture moved against the reference: where its middle went (px, right and down), how far it turned
    (degrees, clockwise on screen) and how far anything in it moved at most (px)."""

    dx: float = 0.0
    dy: float = 0.0
    rot_deg: float = 0.0
    scale: float = 1.0
    max_px: float = 0.0
    inliers: int = 0
    level: str = "unknown"  # ok | shifted | moved | unknown
    why: str = ""  # for unknown

    def text(self) -> str:
        if self.level == "unknown":
            return f"cannot compare with its reference ({self.why})"
        if self.level == "ok":
            return f"in place ({self.max_px:.1f} px)"
        parts = [f"{abs(self.dx):.0f} px {'right' if self.dx > 0 else 'left'}",
                 f"{abs(self.dy):.0f} px {'down' if self.dy > 0 else 'up'}"]
        if abs(self.rot_deg) >= 0.2:
            parts.append(f"turned {abs(self.rot_deg):.1f}° {'clockwise' if self.rot_deg > 0 else 'anticlockwise'}")
        return f"{LEVEL_TEXT[self.level]} {self.max_px:.0f} px: the picture sits " + ", ".join(parts)

    def as_dict(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def level_of(max_px: float, size: tuple[int, int]) -> str:
    diag = math.hypot(*size)
    return "ok" if max_px < WARN_FRAC * diag else "shifted" if max_px < MOVED_FRAC * diag else "moved"


def _ecc(a: np.ndarray, b: np.ndarray, levels: int = 4) -> tuple[np.ndarray | None, float]:
    """Rotation + shift (2×3, a's pixels to b's) aligning the whole pictures, coarse to fine; (None, 0) if it fails."""
    W = np.eye(2, 3, dtype=np.float32)
    cc = 0.0
    for lev in range(levels - 1, -1, -1):
        s = 0.5 ** lev
        x, y = (cv2.GaussianBlur(cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA).astype(np.float32),
                                 (0, 0), 1.0) for im in (a, b))
        w = W.copy()
        w[:, 2] *= s
        try:
            cc, w = cv2.findTransformECC(x, y, w, cv2.MOTION_EUCLIDEAN,
                                         (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 60, 1e-6), None, 3)
        except cv2.error:  # did not converge
            return None, 0.0
        w[:, 2] /= s
        W = w
    return W.astype(float), float(cc)


def _changed(ref_tiny: np.ndarray, tiny: np.ndarray, A: np.ndarray) -> np.ndarray:
    """Where the pictures differ once aligned (birds that moved), in the picture's pixels; each picture is
    normalised first, so a change of light does not count."""
    h, w = tiny.shape
    warped = cv2.warpAffine(ref_tiny, A, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    a, b = ((x - x.mean()) / (x.std() + 1e-6) for x in
            (cv2.GaussianBlur(im.astype(np.float32), (0, 0), 1.5) for im in (warped, tiny)))
    m = (np.abs(a - b) > CHANGE_SD).astype(np.uint8)
    return cv2.dilate(m, np.ones((9, 9), np.uint8)) > 0


def _matches(ref: Reference, kp, des, A0: np.ndarray, tol: float, shape,
             changed: np.ndarray | None = None, kc: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Each reference detail matched to the most alike one within `tol` of where A0 puts it (identical perches or a
    wire mesh repeat further away; a ratio test would throw their details out), but none where the pictures
    changed (`changed`, at `kc` times these pixels), and at most a few per part of the picture, so a bird's many
    details cannot outvote the cage."""
    cand = []
    for cands in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ref.des, des, k=4):
        for m in cands:  # most alike first
            a, b = np.array(ref.kp[m.queryIdx].pt), np.array(kp[m.trainIdx].pt)
            if m.distance < MAX_HAMMING and np.hypot(*(A0[:, :2] @ a + A0[:, 2] - b)) < tol:
                if changed is not None:
                    y, x = (min(int(v * kc), n - 1) for v, n in zip((b[1], b[0]), changed.shape))
                    if changed[y, x]:
                        break
                cand.append((m.distance, _cell(a, shape), a, b))
                break
    cap = max(8, len(cand) // 8)
    used: dict[int, int] = {}
    src, dst = [], []
    for _d, c, a, b in sorted(cand, key=lambda r: r[0]):
        if used.get(c, 0) < cap:
            used[c] = used.get(c, 0) + 1
            src.append(a)
            dst.append(b)
    return np.float32(src).reshape(-1, 2), np.float32(dst).reshape(-1, 2)


def _cell(p, shape, n: int = GRID) -> int:
    return int(min(n - 1, p[1] * n // shape[0]) * n + min(n - 1, p[0] * n // shape[1]))


def _fit(src: np.ndarray, dst: np.ndarray, shape) -> tuple[np.ndarray | None, int]:
    """Rotation + shift + scale from matched details (RANSAC); None when too few agree, or all in one part of the
    picture (a bird)."""
    if len(src) < MIN_INLIERS:
        return None, 0
    A, inl = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.0, maxIters=3000,
                                         confidence=0.995)
    if A is None or inl is None:
        return None, 0
    ok = inl.ravel().astype(bool)
    n = int(ok.sum())
    if n < MIN_INLIERS or len({_cell(p, shape) for p in src[ok]}) < MIN_CELLS:
        return None, n
    return A.astype(float), n


def _disagreement(ref_tiny: np.ndarray, tiny: np.ndarray, A: np.ndarray, edges: np.ndarray) -> float:
    """How much the pictures differ along the picture's edges once aligned with A (median, normalised): the still
    cage's edges outnumber a bird's, so the right alignment differs least."""
    h, w = tiny.shape
    warped = cv2.warpAffine(ref_tiny, A, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    a, b = ((x - x.mean()) / (x.std() + 1e-6) for x in
            (cv2.GaussianBlur(im.astype(np.float32), (0, 0), 1.0) for im in (warped, tiny)))
    return float(np.median(np.abs(a - b)[edges]))


def measure(ref: Reference, img: np.ndarray) -> Shift:
    """The picture `img` against the reference view. The whole pictures are aligned first (ECC: birds hardly count,
    a change of light not at all), then refined with matched details spread over the picture, with and without the
    parts that changed (birds that moved); of these, the alignment under which the edges agree best counts."""
    g = _gray(img)
    if (g.shape[1], g.shape[0]) != ref.size:
        return Shift(why=f"the picture is {g.shape[1]}×{g.shape[0]}, its reference {ref.size[0]}×{ref.size[1]}")
    small, k = _small(g)
    tiny, kt = _small(g, ECC_SIDE)
    At, cc = _ecc(ref.tiny, tiny)
    whole = At is not None and cc >= MIN_CC
    if whole:
        A0 = At.copy()
        A0[:, 2] *= k / kt  # to the pixels of the details' picture
        tol = DETAIL_TOL_PX
    else:
        A0, tol = np.eye(2, 3), SEARCH_FRAC * math.hypot(*small.shape)
    cands = [(A0, 0)] if whole else []  # (transform in the details' pixels, details agreeing)
    kp, des = _features(small)
    if des is not None and ref.des is not None and len(kp) >= MIN_INLIERS and len(ref.kp) >= MIN_INLIERS:
        masks = [None, _changed(ref.tiny, tiny, At)] if whole else [None]
        for changed in masks:
            A, n = _fit(*_matches(ref, kp, des, A0, tol, small.shape, changed, kt / k), small.shape)
            if A is not None:
                cands.append((A, n))
    if not cands:
        return Shift(why="too few details match: the light changed a lot, or the camera turned far")
    if len(cands) > 1:
        edges = cv2.Canny(tiny, 40, 120) > 0
        if edges.sum() > 50:
            def score(c):
                t = c[0].copy()
                t[:, 2] *= kt / k
                return _disagreement(ref.tiny, tiny, t, edges)

            cands.sort(key=score)
    A, n = cands[0]
    A = np.asarray(A, float).copy()
    A[:, 2] /= k  # the same scale on both: back to full-size pixels
    w, h = ref.size
    pts = np.array([[0, 0], [w, 0], [w, h], [0, h], [w / 2, h / 2]], float)
    disp = pts @ A[:, :2].T + A[:, 2] - pts
    dist = np.hypot(disp[:, 0], disp[:, 1])
    mx = float(dist.max())
    return Shift(float(disp[4, 0]), float(disp[4, 1]), float(math.degrees(math.atan2(A[1, 0], A[0, 0]))),
                 float(math.hypot(A[0, 0], A[1, 0])), mx, n, level_of(mx, ref.size))


def overlay(img: np.ndarray, ref: Reference, shift: Shift | None = None) -> np.ndarray:
    """The live picture with the reference view's outlines on it (magenta) and, when it moved, an arrow from where
    the middle of the picture is to where it belongs: turn the camera until the picture meets the outlines."""
    out = img.copy() if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if out.dtype != np.uint8:
        out = cv2.cvtColor(_gray(img), cv2.COLOR_GRAY2BGR)
    e = ref.edges()
    if e.shape == out.shape[:2]:
        out[e] = (0.3 * out[e] + 0.7 * np.array(OUTLINE_BGR)).astype(np.uint8)
    if shift is not None and shift.level in ("shifted", "moved"):
        w, h = ref.size
        c = np.array([w / 2 + shift.dx, h / 2 + shift.dy])
        cv2.arrowedLine(out, tuple(int(v) for v in c), (int(w / 2), int(h / 2)), OUTLINE_BGR,
                        max(2, w // 400), tipLength=0.2)
    return out


def steady(shifts: list[Shift]) -> Shift | None:
    """The middle one of a few measurements (by how far it moved): one picture with a bird close to the lens does not
    raise a warning on its own."""
    ok = sorted((s for s in shifts if s.level != "unknown"), key=lambda s: s.max_px)
    return ok[(len(ok) - 1) // 2] if ok else (shifts[-1] if shifts else None)


# --------------------------------------------------------------------------- sessions


def led_flashes(frames) -> bool | None:
    """Whether the sync LED flashes in its box (the frames' led_mean), as the sync check needs it; None: no box."""
    if "led_mean" not in frames or frames["led_mean"].notna().sum() <= 10:
        return None
    v = frames["led_mean"].to_numpy(float)
    return bool(np.nanpercentile(v, 99.5) - np.nanpercentile(v, 5) > 20)


def session_check(sess, n: int = 7) -> dict[str, dict]:
    """Every camera of a session against its reference view (the session's copy): n pictures spread over the
    recording. A camera counts as moved from the first of the pictures from which every later one says so (the
    middle of the last three decides), so a single odd picture does not. Also: whether the sync LED flashes in its
    box, and what was measured while recording."""
    import pandas as pd

    from ..session import CALIBRATION_FILE
    from .reader import VideoReader, video_parts

    out = {}
    for cam in sess.cameras:
        cid = cam["id"]
        entry: dict = {"reference": False, "samples": [], "level": "unknown", "since_s": None,
                       "while_recording": cam.get("position_check")}
        ref = load_reference(sess.file(CALIBRATION_FILE), cid)
        if ref is not None:
            entry["reference"] = True
            try:
                rd = VideoReader(video_parts(sess, cam))
            except (FileNotFoundError, RuntimeError):
                rd = None
            fps = float(cam.get("fps") or 30.0)
            if rd is not None and rd.n:
                for q in np.linspace(0.05, 0.95, n):
                    idx = int(q * (rd.n - 1))
                    img = rd.get(idx)
                    if img is not None:
                        s = measure(ref, img)
                        entry["samples"].append({"frame": idx, "t_s": round(idx / fps, 1), **s.as_dict()})
            known = [s for s in entry["samples"] if s["level"] != "unknown"]
            if known:
                last = steady([Shift(**{k: v for k, v in s.items() if k not in ("frame", "t_s")}) for s in known[-3:]])
                entry["level"], entry["max_px"] = last.level, last.max_px
                entry["text"] = last.text()
                if last.level != "ok":  # from the first picture after the last one in place before it moved
                    bad = max(i for i, s in enumerate(known) if s["level"] != "ok")
                    good = [i for i, s in enumerate(known[:bad]) if s["level"] == "ok"]
                    entry["since_s"] = known[good[-1] + 1]["t_s"] if good else 0.0
                    entry["in_place_until_s"] = known[good[-1]]["t_s"] if good else None
        if cam.get("frames_file") and sess.file(cam["frames_file"]).exists():
            entry["led_flashes"] = led_flashes(pd.read_csv(sess.file(cam["frames_file"])))
        out[cid] = entry
    return out


def recorded_warnings(sess) -> list[str]:
    """What acquisition measured while recording (at the start, and each change during it) that was not in place."""
    out = []
    for cam in sess.cameras:
        pc = cam.get("position_check") or {}
        if pc.get("level") in ("shifted", "moved"):
            out.append(f"{cam['id']} at the start: {Shift(**pc).text()}")
    for row in sess.meta.get("position_checks") or []:
        if row.get("level") in ("shifted", "moved"):
            s = Shift(**{k: v for k, v in row.items() if k not in ("t_s", "camera")})
            out.append(f"{row['camera']} at {_mmss(row['t_s'])}: {s.text()}")
    return out


def check_lines(check: dict[str, dict]) -> list[str]:
    """One line per camera, for the analysis summary and the command line."""
    lines = []
    for cid, e in check.items():
        if not e["reference"]:
            s = f"{cid}: no reference view to compare with"
        elif e["level"] == "unknown":
            s = f"{cid}: cannot compare with its reference view"
        elif e["level"] == "ok":
            s = f"{cid}: in place ({e['max_px']:.1f} px)"
        else:
            a, b = e.get("in_place_until_s"), e["since_s"]
            s = (f"{cid}: {e['text']}" + (f", since between {_mmss(a)} and {_mmss(b)}" if a is not None else
                                          ", all session")
                 + ". The places drawn on it are off by that much: put it back (acquire: Alignment overlay) or draw "
                   "the ROIs / calibrate its position again")
        if e.get("led_flashes") is False:
            s += "; the sync LED does not flash in its box (moved, or the LED hidden)"
        lines.append(s)
    return lines


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"
