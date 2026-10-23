"""Post-hoc annotated video: ROIs, RFID identity, manual label, disagreements."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import pandas as pd

from ..config import TagMap
from ..video.recorder import VideoWriter
from ..video.reader import open_session_video


def _bgr(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


class IntervalLookup:
    """Who is in each ROI at time t, from an interval table (roi, pigeon, start_s, stop_s)."""

    def __init__(self, df: pd.DataFrame | None, rois: list[str]):
        self.by_roi = {}
        for r in rois:
            g = df[df["roi"] == r].sort_values("start_s") if df is not None and len(df) else None
            if g is None or not len(g):
                self.by_roi[r] = None
                continue
            self.by_roi[r] = (g["start_s"].to_numpy(), g["stop_s"].to_numpy(), g["pigeon"].to_numpy(str))

    def at(self, roi: str, t: float) -> list[str]:
        d = self.by_roi.get(roi)
        if d is None:
            return []
        a, b, p = d
        i = np.searchsorted(a, t, side="right")
        lo = max(0, i - 8)  # intervals in one ROI rarely overlap; look back a few
        return [p[k] for k in range(lo, i) if b[k] > t]


def draw_overlay(img: np.ndarray, rois, t: float, rfid: IntervalLookup | None, manual: IntervalLookup | None,
                 tags: TagMap, frame_idx: int | None = None, show_rfid: bool = True) -> np.ndarray:
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    out = img.copy()
    for roi in rois:
        pts = np.array(roi.points, np.int32)
        rf = rfid.at(roi.name, t) if (rfid and show_rfid) else []
        mn = manual.at(roi.name, t) if manual else []
        color = _bgr(tags.color(rf[0])) if rf else (_bgr(tags.color(mn[0])) if mn else (170, 170, 170))
        disagree = manual is not None and show_rfid and sorted(rf) != sorted(mn)
        cv2.polylines(out, [pts], True, (40, 40, 230) if disagree else color, 3 if (rf or mn or disagree) else 1)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        lines = [roi.name]
        if show_rfid:
            lines.append("RFID: " + ("+".join(rf) if rf else "-"))
        if manual is not None:
            lines.append("Label: " + ("+".join(mn) if mn else "-"))
        for k, txt in enumerate(lines):
            y = max(14, y0 - 6 - 16 * (len(lines) - 1 - k))
            cv2.putText(out, txt, (int(x0), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(out, txt, (int(x0), int(y)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    stamp = f"t={t:8.2f}s" + (f"  frame {frame_idx}" if frame_idx is not None else "")
    cv2.putText(out, stamp, (8, out.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(out, stamp, (8, out.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return out


def render_annotated(P, labels: pd.DataFrame | None = None, out_path: str | Path | None = None,
                     t_range: tuple[float, float] | None = None,
                     progress: Callable[[float], None] | None = None) -> Path:
    sess = P.session
    rois = sess.rois.perches
    names = [r.name for r in rois]
    rfid = IntervalLookup(P.bouts, names)
    manual = IntervalLookup(labels, names) if labels is not None and len(labels) else None
    tags = sess.tags
    cam = sess.camera()
    src = open_session_video(sess)  # raw chunks or a single video file
    n = src.n
    frame_t = P.frame_t if P.frame_t is not None else np.arange(n) / float(cam.get("fps", 30))
    i0, i1 = 0, min(n, len(frame_t))
    if t_range:
        i0 = int(np.searchsorted(frame_t, t_range[0]))
        i1 = int(np.searchsorted(frame_t, t_range[1]))
    out_path = Path(out_path or sess.analysis_dir / f"annotated_{cam['id']}.mp4")
    w = h = None
    writer = None
    for i, img in src.frames(i0, i1):
        ann = draw_overlay(img, rois, float(frame_t[i]), rfid, manual, tags, i)
        if writer is None:
            h, w = ann.shape[:2]
            writer = VideoWriter(out_path, float(cam.get("fps", 30)), (w, h), {"crf": 23, "preset": "veryfast"})
        writer.write(ann)
        if progress and (i - i0) % 100 == 0:
            progress((i - i0) / max(1, i1 - i0))
    src.close()
    if writer is not None:
        writer.close()
    if progress:
        progress(1.0)
    return out_path
