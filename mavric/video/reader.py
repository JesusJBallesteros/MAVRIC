"""Frame-accurate reading of a camera's recording, whether raw chunks, a compressed file or a single video."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def video_parts(sess, cam: dict, prefer: str = "raw") -> list[tuple[Path, int, int | None]]:
    """[(path, first_frame, n_frames)] of a session camera's recording.

    prefer="raw" uses the lossless chunks when present (bit-exact, intra-only:
    fast exact seeking); prefer="compressed" uses <cam>_compressed.mp4 first.
    Raw chunks missing from session.json (e.g. after a crash) are found on disk.
    """
    raw = [(sess.file(f["file"]), int(f["first_frame"]), int(f["n_frames"])) for f in cam.get("video_files") or []]
    if not raw and cam.get("record_format") == "raw":
        found = sorted([*sess.path.glob(f"{cam['id']}_raw_*.mkv"), *sess.path.glob(f"{cam['id']}_raw_*.avi")])
        first = 0
        for p in found:
            n = _count(p)
            raw.append((p, first, n))
            first += n
    comp = cam.get("compressed_file")
    comp_ok = comp and sess.file(comp).exists() and not (cam.get("compressed") or {}).get("geometry_changed")
    if prefer == "compressed" and comp_ok:
        return [(sess.file(comp), 0, None)]
    if raw:
        return raw
    if comp_ok:
        return [(sess.file(comp), 0, None)]
    if cam.get("video_file"):
        return [(sess.file(cam["video_file"]), 0, None)]
    raise FileNotFoundError(f"camera {cam.get('id')} has no recording")


def _count(path: Path) -> int:
    """Exact frame count by decoding packets (Matroska stores no frame count)."""
    import av

    with av.open(str(path)) as c:
        return sum(1 for p in c.demux(video=0) if p.size)


class VideoReader:
    """Random access by global frame index across one or more files (sequential reads stay fast)."""

    def __init__(self, parts: list[tuple[Path, int, int | None]]):
        self.parts = []
        first = 0
        for p, _f, n in parts:
            if n is None:
                cap = cv2.VideoCapture(str(p))
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                cap.release()
            self.parts.append((Path(p), first, int(n)))
            first += int(n)
        self.n = first
        self._cap = None
        self._part = -1
        self.pos = -1
        self.last = None

    def _open(self, k: int) -> None:
        if self._cap is not None:
            self._cap.release()
        self._cap = cv2.VideoCapture(str(self.parts[k][0]))
        if not self._cap.isOpened():
            raise RuntimeError(f"Cannot open {self.parts[k][0]}")
        self._part = k
        self.pos = self.parts[k][1] - 1

    def get(self, idx: int) -> np.ndarray | None:
        if self.n == 0:
            return None
        idx = int(np.clip(idx, 0, self.n - 1))
        if idx == self.pos and self.last is not None:
            return self.last
        k = max(i for i, (_p, f, _n) in enumerate(self.parts) if f <= idx)
        if k != self._part:
            self._open(k)
        local = idx - self.parts[k][1]
        if self.pos < idx <= self.pos + 15:
            for _ in range(idx - self.pos - 1):
                self._cap.grab()
        elif idx != self.pos + 1:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, local)
        ok, img = self._cap.read()
        if ok:
            self.pos, self.last = idx, img
        return self.last

    def frames(self, start: int = 0, stop: int | None = None):
        """Sequential iteration (global index, BGR image)."""
        stop = self.n if stop is None else min(stop, self.n)
        for i in range(start, stop):
            img = self.get(i)
            if img is None:
                return
            yield i, img

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def open_session_video(sess, cam_id: str | None = None, prefer: str = "raw") -> VideoReader:
    return VideoReader(video_parts(sess, sess.camera(cam_id), prefer))
