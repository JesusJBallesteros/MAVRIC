"""Video recording with a per-frame timing sidecar.

Two recording formats (rig.yaml / Video settings: video.record_format):

- raw (default): every frame bit-exact, in size-limited Matroska chunks
  <cam>_raw_000.mkv, _001, ... (FFV1 lossless, or uncompressed rawvideo).
  `mavric compress` makes the shareable / DeepLabCut file afterwards.
- h264: compressed while recording into <cam>.mp4 (small, lossy).

Containers get constant-rate PTS (frame_idx / nominal fps) so every player
and labeling tool can open them; the *true* timing of each frame lives in
frames_<cam>.csv (frame_idx runs on across chunks) and is what the analysis uses.
"""

from __future__ import annotations

import queue
import threading
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ..core import CsvAppender
from .backends import Frame

FRAME_COLUMNS = [
    "frame_idx",  # index in the video file
    "grab_idx",  # host-side delivery count (gaps = recorder queue drops)
    "host_t",  # s since session start, host clock at hand-over
    "hw_ticks",  # camera clock
    "hw_counter",  # camera frame counter (gaps = camera-side drops)
    "line_status",  # camera GPIO bitmask, if logged
    "led_mean",  # mean intensity inside the sync-LED ROI
]


def _even(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    return img[: h - (h % 2), : w - (w % 2)]


class VideoWriter:
    """Thin PyAV wrapper with codec fallback (libx264 -> mpeg4)."""

    def __init__(self, path: str | Path, fps: float, size: tuple[int, int], cfg: dict | None = None):
        cfg = cfg or {}
        self.path = Path(path)
        self.fps = fps
        w, h = size
        self.size = (w - w % 2, h - h % 2)
        self.container = av.open(str(self.path), mode="w")
        rate = Fraction(fps).limit_denominator(1001)
        gop = max(1, int(round(fps * float(cfg.get("gop_s", 1.0)))))
        self.codec = None
        last_err = None
        for codec in [cfg.get("codec", "libx264"), "libx264", "mpeg4"]:
            try:
                st = self.container.add_stream(codec, rate=rate)
                st.width, st.height = self.size
                st.pix_fmt = "yuv420p"
                st.codec_context.gop_size = gop
                if codec == "libx264":
                    st.options = {"crf": str(cfg.get("crf", 20)), "preset": str(cfg.get("preset", "veryfast"))}
                else:
                    st.bit_rate = int(cfg.get("bit_rate", 8_000_000))
                st.codec_context.open()
                self.stream = st
                self.codec = codec
                break
            except Exception as e:  # codec unavailable -> next
                last_err = e
                continue
        if self.codec is None:
            raise RuntimeError(f"No usable video encoder: {last_err}")
        self.n = 0

    def write(self, img: np.ndarray) -> None:
        img = _even(img)
        if img.shape[1] != self.size[0] or img.shape[0] != self.size[1]:
            img = img[: self.size[1], : self.size[0]]
        fmt = "gray" if img.ndim == 2 else "bgr24"
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format=fmt).reformat(format="yuv420p")
        vf.pts = self.n
        for pkt in self.stream.encode(vf):
            self.container.mux(pkt)
        self.n += 1

    def close(self) -> None:
        for pkt in self.stream.encode():
            self.container.mux(pkt)
        self.container.close()


class LosslessChunkWriter:
    """Bit-exact frames in Matroska chunks of about `chunk_mb` each: <stem>_raw_000.mkv, ..."""

    def __init__(self, stem: str | Path, fps: float, size: tuple[int, int], codec: str = "ffv1",
                 chunk_mb: float = 2000, on_chunk=None):
        self.stem = Path(stem)
        self.fps = fps
        self.size = tuple(size)
        if codec not in ("ffv1", "rawvideo"):
            raise ValueError(f"raw codec {codec!r}: use ffv1 or rawvideo")
        self.codec = codec
        self.chunk_bytes = int(float(chunk_mb) * 1e6)
        self.on_chunk = on_chunk  # called with the chunk list whenever a chunk is closed
        self.files: list[dict] = []
        self.n = 0
        self._c = None
        self._bytes = 0
        self._pix = None

    @property
    def path(self) -> Path:
        ext = ".mkv" if self.codec == "ffv1" else ".avi"
        return self.stem.parent / (self.files[0]["file"] if self.files else f"{self.stem.name}_raw_000{ext}")

    def _open(self, color: bool) -> None:
        # FFV1 in Matroska; uncompressed frames in AVI (Matroska does not take raw BGR)
        ext = ".mkv" if self.codec == "ffv1" else ".avi"
        name = f"{self.stem.name}_raw_{len(self.files):03d}{ext}"
        path = self.stem.parent / name
        c = av.open(str(path), mode="w")
        try:
            st = c.add_stream(self.codec, rate=Fraction(self.fps).limit_denominator(1001))
            st.width, st.height = self.size
            if self.codec == "ffv1":
                self._pix = "bgr0" if color else "gray"
                st.options = {"level": "3", "slices": "16", "slicecrc": "1", "g": "1"}  # intra-only: exact seeking
                st.codec_context.thread_count = 0
            else:
                self._pix = "bgr24" if color else "gray"
            st.pix_fmt = self._pix
            st.codec_context.open()  # fail here, cleanly, rather than in the first encode
        except Exception:
            c.close()
            path.unlink(missing_ok=True)
            raise
        self._c, self._st = c, st
        self._bytes = 0
        self.files.append({"file": name, "first_frame": self.n, "n_frames": 0})

    def _close_chunk(self) -> None:
        if self._c is None:
            return
        for pkt in self._st.encode():
            self._c.mux(pkt)
        self._c.close()
        self._c = None
        if self.on_chunk:
            self.on_chunk(list(self.files))

    def write(self, img: np.ndarray) -> None:
        h, w = img.shape[:2]
        if (w, h) != self.size:  # the frames decide the size; a change starts a new chunk
            self._close_chunk()
            self.size = (w, h)
        if self._c is None:
            self._open(img.ndim == 3)
        fmt = "gray" if img.ndim == 2 else "bgr24"
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(img), format=fmt)
        if self._pix != fmt:
            vf = vf.reformat(format=self._pix)
        vf.pts = self.files[-1]["n_frames"]
        for pkt in self._st.encode(vf):
            self._c.mux(pkt)
            self._bytes += pkt.size
        self.files[-1]["n_frames"] += 1
        self.n += 1
        if self._bytes >= self.chunk_bytes:
            self._close_chunk()

    def close(self) -> None:
        self._close_chunk()


def make_writer(video_path: str | Path, fps: float, size: tuple[int, int], video_cfg: dict | None, on_chunk=None):
    """H.264 VideoWriter or LosslessChunkWriter, from video.record_format."""
    cfg = video_cfg or {}
    if cfg.get("record_format", "h264") == "raw":
        return LosslessChunkWriter(Path(video_path).with_suffix(""), fps, size, cfg.get("raw_codec", "ffv1"),
                                   cfg.get("raw_chunk_mb", 2000), on_chunk)
    return VideoWriter(video_path, fps, size, cfg)


class VideoRecorder(threading.Thread):
    """Queue-fed encoder thread; never blocks the grab loop (drops and counts instead)."""

    def __init__(
        self,
        video_path: str | Path,
        frames_path: str | Path,
        fps: float,
        size: tuple[int, int],
        session_t0_abs: float,
        video_cfg: dict | None = None,
        led_bbox: tuple[int, int, int, int] | None = None,
        queue_s: float = 5.0,
        on_chunk=None,
    ):
        super().__init__(daemon=True, name=f"recorder-{Path(video_path).stem}")
        self.writer = make_writer(video_path, fps, size, video_cfg, on_chunk)
        self.raw = isinstance(self.writer, LosslessChunkWriter)
        self.sidecar = CsvAppender(frames_path, FRAME_COLUMNS)
        self.t0 = session_t0_abs
        self.led_bbox = led_bbox
        self.q: queue.Queue = queue.Queue(maxsize=max(10, int(fps * queue_s)))
        self.n_dropped = 0
        self.error: str | None = None

    def submit(self, fr: Frame) -> bool:
        try:
            self.q.put_nowait(fr)
            return True
        except queue.Full:
            self.n_dropped += 1
            return False

    def led_mean(self, img: np.ndarray) -> float | str:
        if self.led_bbox is None:
            return ""
        x0, y0, x1, y1 = self.led_bbox
        patch = img[max(0, y0) : y1, max(0, x0) : x1]
        return f"{float(patch.mean()):.2f}" if patch.size else ""

    def run(self) -> None:
        while True:
            fr = self.q.get()
            if fr is None:
                break
            if self.error:
                continue  # keep draining so submit()/finish() never block
            try:
                idx = self.writer.n
                self.writer.write(fr.image)
                self.sidecar.write(
                    [
                        idx,
                        fr.grab_idx,
                        f"{fr.host_abs - self.t0:.6f}",
                        "" if fr.hw_ticks is None else fr.hw_ticks,
                        "" if fr.hw_counter is None else fr.hw_counter,
                        "" if fr.line_status is None else fr.line_status,
                        self.led_mean(fr.image),
                    ]
                )
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
        try:
            self.writer.close()
        finally:
            self.sidecar.close()

    def finish(self) -> dict:
        self.q.put(None)
        self.join()
        out = {"frames": self.writer.n, "recorder_dropped": self.n_dropped, "codec": self.writer.codec,
               "error": self.error}
        if self.raw:
            out["files"] = list(self.writer.files)
        return out
