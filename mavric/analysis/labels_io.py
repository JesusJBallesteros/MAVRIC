"""Manual labels: our CSV format, state-change conversion, and BORIS import.

manual_labels.csv     annotator, roi, pigeon, start_s, stop_s, flag
label_coverage.csv    annotator, start_s, stop_s   (spans actually reviewed)

Times are seconds on the session/video timeline (the same `t` the analysis
assigns to frames), so labels from any tool become comparable to RFID.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..config import EMPTY
from ..session import COVERAGE_FILE, LABELS_FILE, Session

LABEL_COLUMNS = ["annotator", "roi", "pigeon", "start_s", "stop_s", "flag"]
COVERAGE_COLUMNS = ["annotator", "start_s", "stop_s"]


def load_labels(sess: Session) -> pd.DataFrame | None:
    p = sess.file(LABELS_FILE)
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"roi": str, "pigeon": str, "annotator": str, "flag": str}, keep_default_na=False)
    for c in LABEL_COLUMNS:
        if c not in df:
            df[c] = ""
    df["start_s"] = df["start_s"].astype(float)
    df["stop_s"] = df["stop_s"].astype(float)
    return df[LABEL_COLUMNS]


def save_labels(sess: Session, df: pd.DataFrame) -> Path:
    p = sess.file(LABELS_FILE)
    tmp = p.with_suffix(".tmp")
    df[LABEL_COLUMNS].sort_values(["start_s", "roi"]).to_csv(tmp, index=False)
    tmp.replace(p)
    return p


def load_coverage(sess: Session) -> list[tuple[float, float]]:
    p = sess.file(COVERAGE_FILE)
    if not p.exists():
        return []
    df = pd.read_csv(p)
    return [(float(a), float(b)) for a, b in zip(df["start_s"], df["stop_s"]) if b > a]


def save_coverage(sess: Session, spans: list[tuple[float, float]], annotator: str = "") -> Path:
    p = sess.file(COVERAGE_FILE)
    pd.DataFrame([{"annotator": annotator, "start_s": a, "stop_s": b} for a, b in merge_spans(spans)],
                 columns=COVERAGE_COLUMNS).to_csv(p, index=False)
    return p


def merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted((min(x), max(x)) for x in spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out if b > a]


# --------------------------------------------------------------------------- state changes <-> intervals


def changes_to_intervals(changes: dict[str, list[tuple[float, str]]], t_end: float, annotator: str = "",
                         flags: dict[str, list[tuple[float, str]]] | None = None) -> pd.DataFrame:
    """Per-ROI state-change points [(t, pigeon or EMPTY)] -> occupancy intervals.

    The state set at time t lasts until the next change point of the same ROI
    (or t_end). EMPTY states produce no rows.
    """
    rows = []
    for roi, pts in changes.items():
        pts = sorted(pts)
        for i, (t, who) in enumerate(pts):
            t1 = pts[i + 1][0] if i + 1 < len(pts) else t_end
            if who and who != EMPTY and t1 > t:
                rows.append({"annotator": annotator, "roi": roi, "pigeon": who, "start_s": t, "stop_s": t1, "flag": ""})
    df = pd.DataFrame(rows, columns=LABEL_COLUMNS)
    if flags:
        for roi, pts in flags.items():
            for t, flag in pts:
                m = (df["roi"] == roi) & (df["start_s"] <= t) & (df["stop_s"] > t)
                df.loc[m, "flag"] = flag
    return df


def intervals_to_changes(df: pd.DataFrame, rois: list[str]) -> dict[str, list[tuple[float, str]]]:
    """Inverse of changes_to_intervals (used to load labels back into the review GUI)."""
    ch: dict[str, list[tuple[float, str]]] = {r: [] for r in rois}
    for roi, g in df.groupby("roi"):
        g = g.sort_values("start_s")
        pts: list[tuple[float, str]] = []
        prev_stop = None
        for _, r in g.iterrows():
            if prev_stop is not None and r["start_s"] > prev_stop + 1e-6:
                pts.append((prev_stop, EMPTY))
            pts.append((float(r["start_s"]), r["pigeon"]))
            prev_stop = float(r["stop_s"])
        if prev_stop is not None:
            pts.append((prev_stop, EMPTY))
        ch.setdefault(roi, [])
        ch[roi] = pts
    return ch


# --------------------------------------------------------------------------- BORIS


def _col(df: pd.DataFrame, *names: str) -> str | None:
    low = {c.lower().strip(): c for c in df.columns}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def import_boris(
    sess: Session,
    path: str | Path,
    frame_t: np.ndarray | None = None,
    fps: float | None = None,
    behavior_map: dict[str, str] | None = None,
    subject_map: dict[str, str] | None = None,
    annotator: str = "boris",
    coverage: str = "media",
) -> tuple[pd.DataFrame, list[tuple[float, float]]]:
    """Read a BORIS 'aggregated events' export (CSV/TSV/XLSX).

    BORIS codes on the video file's own clock (PTS = frame_idx / nominal fps);
    times are converted to the session timeline through the frame table.
    Subjects are pigeons, STATE behaviours are perches (ROI names).
    coverage="media" treats the whole media file as reviewed (unlabeled time =
    empty perches); "events" limits it to first..last coded event.
    """
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path, sep=None, engine="python")
    c_sub = _col(df, "Subject")
    c_beh = _col(df, "Behavior", "Behaviour")
    c_type = _col(df, "Behavior type", "Behaviour type")
    c_start = _col(df, "Start (s)", "Start")
    c_stop = _col(df, "Stop (s)", "Stop")
    c_dur = _col(df, "Media duration (s)", "Media duration")
    if not (c_sub and c_beh and c_start and c_stop):
        raise ValueError(f"{path.name}: not a BORIS aggregated-events table (columns: {list(df.columns)})")

    cam = sess.camera()
    fps = fps or float(cam.get("fps", 30))
    rois = {r.name.lower(): r.name for r in sess.rois.perches}
    pigeons = {p.lower(): p for p in sess.tags.pigeons}
    behavior_map = {k.lower(): v for k, v in (behavior_map or {}).items()}
    subject_map = {k.lower(): v for k, v in (subject_map or {}).items()}

    def to_t(media_s):
        media_s = np.asarray(media_s, float)
        if frame_t is None or len(frame_t) == 0:
            return media_s
        return np.interp(media_s * fps, np.arange(len(frame_t)), frame_t)

    if c_type:
        df = df[df[c_type].astype(str).str.upper().str.startswith("STATE")]
    rows, skipped = [], set()
    for _, r in df.iterrows():
        beh = str(r[c_beh]).strip()
        sub = str(r[c_sub]).strip()
        roi = behavior_map.get(beh.lower()) or rois.get(beh.lower())
        pig = subject_map.get(sub.lower()) or pigeons.get(sub.lower()) or sub
        if roi is None:
            skipped.add(beh)
            continue
        rows.append({"annotator": annotator, "roi": roi, "pigeon": pig,
                     "start_s": float(r[c_start]), "stop_s": float(r[c_stop]), "flag": ""})
    lab = pd.DataFrame(rows, columns=LABEL_COLUMNS)
    if len(lab):
        lab["start_s"] = to_t(lab["start_s"])
        lab["stop_s"] = to_t(lab["stop_s"])
    if coverage == "media" and c_dur and df[c_dur].notna().any():
        media_end = float(pd.to_numeric(df[c_dur], errors="coerce").max())
        cov = [(float(to_t([0.0])[0]), float(to_t([media_end])[0]))]
    elif len(lab):
        cov = [(float(lab["start_s"].min()), float(lab["stop_s"].max()))]
    else:
        cov = []
    if skipped:
        print(f"[import_boris] behaviours not matching any ROI were skipped: {sorted(skipped)}")
    return lab, cov
