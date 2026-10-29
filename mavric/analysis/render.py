"""Post-hoc annotated video: ROIs, RFID identity, manual label, disagreements; with audio, the sound and a
scrolling spectrogram strip with the sound events.

The video plays at a constant frame rate, while the frames' true times
(frames_<cam>.csv) drift and skip dropped frames. The audio track is therefore
resampled onto the video's own timeline, so sound and picture stay together
for any length of video.
"""

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


# --------------------------------------------------------------------------- audio


def video_timeline(frame_t: np.ndarray, i0: int, i1: int, fps: float) -> tuple[np.ndarray, np.ndarray]:
    """Knots (video seconds, session seconds) for frames i0..i1-1 written at a constant `fps`, plus the end of the
    last frame."""
    t = np.asarray(frame_t[i0:i1], float)
    tau = np.arange(len(t)) / fps
    step = t[-1] - t[-2] if len(t) > 1 else 1 / fps
    return np.r_[tau, tau[-1] + 1 / fps], np.r_[t, t[-1] + step]


def audio_on_video_time(src, tau_k: np.ndarray, t_k: np.ndarray, cols: list[int], chunk_s: float = 0.5):
    """The session's audio resampled onto the video's timeline: chunks of (n, len(cols)) float32.

    Output sample k plays k / rate seconds into the video, where the picture
    shows the session time interpolated between the frames' times.
    """
    rate = src.sr
    total = int(round(tau_k[-1] * rate))
    n = max(1, int(chunk_s * rate))
    for k0 in range(0, total, n):
        tau = (k0 + np.arange(min(n, total - k0))) / rate
        t = np.interp(tau, tau_k, t_k)
        s = src.sample_at(t)
        lo = int(np.floor(s.min()))
        x = src.read_samples(lo, int(np.ceil(s.max())) + 2, cols)
        if not len(x):
            yield np.zeros((len(t), len(cols)), np.float32)
            continue
        u = s - lo
        i = np.clip(np.floor(u).astype(int), 0, len(x) - 1)
        f = (u - i)[:, None]
        y = x[i] * (1 - f) + x[np.minimum(i + 1, len(x) - 1)] * f
        y[(t < src.t_start) | (t > src.t_stop)] = 0
        yield y.astype(np.float32)


def audio_columns(src, choice: str) -> tuple[list[int], str]:
    """Channel columns for the video's audio track: 'auto' | 'stereo' | 'mix' | a microphone name.

    Returns (columns, mode): mode 'stereo' (first two mics, left/right), 'mono' or 'mix' (mean of the mics).
    """
    mics = src.mic_idx
    if choice in ("auto", "stereo"):
        return (mics[:2], "stereo") if len(mics) >= 2 else (mics[:1], "mono")
    if choice == "mix":
        return mics, "mix"
    if choice in src.channels:
        return [src.channels.index(choice)], "mono"
    raise ValueError(f"--audio {choice!r}: use auto, stereo, mix, none or one of {src.mics}")


def track_gain(src, cols: list[int], t0: float, t1: float, target_dbfs: float = -3.0, max_db: float = 30.0) -> float:
    """Gain that brings the loudest moment of [t0, t1] to `target_dbfs` (at most +max_db), from the overview."""
    env = src.envelope()
    m = (env["t"] >= t0) & (env["t"] <= t1)
    k = [src.mic_idx.index(c) for c in cols if c in src.mic_idx]
    if not m.any() or not k:
        return 1.0
    peak = float(max(np.abs(env["lo"][m][:, k]).max(), np.abs(env["hi"][m][:, k]).max()))
    return float(min(10 ** (max_db / 20), 10 ** (target_dbfs / 20) / max(peak, 1e-9)))


class SpectrogramStrip:
    """A scrolling spectrogram under the picture: `span_s` around the frame's time, the playhead in the middle,
    one band per microphone and a lane with the sound events above them."""

    LANE = 18

    def __init__(self, src, width: int, events: pd.DataFrame | None, tags: TagMap, band_h: int = 72,
                 span_s: float = 6.0, fmax: float = 8000.0, nfft: int = 512, chunk_s: float = 20.0):
        from ..audio.source import spectrogram_lut

        self.src, self.width, self.band_h, self.span = src, int(width), int(band_h), float(span_s)
        self.cps = self.width / self.span  # columns per second
        self.nfft = nfft
        self.n_chunk = max(1, int(round(chunk_s * self.cps)))
        self.fmax = min(fmax, src.sr / 2)
        self.lut = spectrogram_lut()[:, ::-1]  # BGR
        self.tags = tags
        self.events = events if events is not None else pd.DataFrame(columns=["start_s", "stop_s"])
        self.cache: dict[tuple[int, int], np.ndarray] = {}
        self.levels: dict[int, tuple[float, float]] = {}
        self.height = self.LANE + len(src.mic_idx) * (self.band_h + 1)

    def _chunk(self, col: int, k: int) -> np.ndarray:
        key = (col, k)
        if key in self.cache:
            return self.cache[key]
        if len(self.cache) > 64:
            self.cache.clear()
        src, n = self.src, self.nfft
        c = np.arange(k * self.n_chunk, (k + 1) * self.n_chunk)
        t = c / self.cps
        ok = (t >= src.t_start) & (t <= src.t_stop)
        img = np.empty((self.band_h, len(c), 3), np.uint8)
        img[:] = self.lut[0]
        if ok.any():
            s = np.round(src.sample_at(t[ok])).astype(int)
            lo = int(s.min()) - n // 2
            x = src.read_samples(lo, int(s.max()) + n // 2 + 1, [col])[:, 0]
            pad = max(0, -lo)  # read_samples starts at sample 0 at the earliest
            x = np.pad(x, (pad, n))
            idx = (s - lo - n // 2)[:, None] + np.arange(n)[None, :]
            win = np.hanning(n).astype(np.float32)
            spec = np.abs(np.fft.rfft(x[idx] * win, axis=1)) * (2.0 / win.sum())
            f = np.fft.rfftfreq(n, 1 / src.sr)
            S = 20 * np.log10(np.maximum(spec[:, f <= self.fmax], 1e-10))
            rows = np.linspace(0, S.shape[1] - 1, self.band_h)[::-1]  # top row = fmax
            S = np.stack([np.interp(rows, np.arange(S.shape[1]), r) for r in S], axis=1)
            if col not in self.levels:
                lo_db = float(np.percentile(S, 20))
                self.levels[col] = (lo_db, max(lo_db + 40.0, float(S.max())))
            a, b = self.levels[col]
            img[:, ok] = self.lut[np.clip((S - a) / (b - a) * 255, 0, 255).astype(np.uint8)]
        self.cache[key] = img
        return img

    def draw(self, t: float) -> np.ndarray:
        c0 = int(round((t - self.span / 2) * self.cps))
        out = np.empty((self.height, self.width, 3), np.uint8)
        out[:] = (251, 252, 252)
        y = self.LANE
        for col in self.src.mic_idx:
            parts, c = [], c0
            while c < c0 + self.width:
                k = c // self.n_chunk
                a = c - k * self.n_chunk
                b = min(self.n_chunk, a + c0 + self.width - c)
                parts.append(self._chunk(col, k)[:, a:b])
                c += b - a
            out[y:y + self.band_h] = np.concatenate(parts, axis=1)
            _text(out, self.src.channels[col], (4, y + 13), 0.38)
            y += self.band_h + 1
        t0 = c0 / self.cps
        e = self.events
        e = e[(e["stop_s"] > t0) & (e["start_s"] < t0 + self.span)] if len(e) else e
        for r in e.itertuples():
            x0 = int(round((r.start_s - t0) * self.cps))
            x1 = max(x0 + 2, int(round((r.stop_s - t0) * self.cps)))
            em = str(r.emitter)
            color = _bgr(self.tags.color(em)) if em in self.tags.pigeons else (129, 135, 137)
            cv2.rectangle(out, (x0, 3), (x1, self.LANE - 4), color, -1)
            cv2.rectangle(out, (x0, self.LANE), (x1, self.height - 2), color, 1)
            what = "played" if r.type == "playback" else ("" if r.type == "other" else r.type)
            _text(out, em.split(":", 1)[-1] + (f" {what}" if what else ""), (max(2, x0 + 2), self.LANE - 6), 0.36)
        cv2.line(out, (self.width // 2, 0), (self.width // 2, self.height - 1), (11, 11, 11), 1)
        return out


def _text(img, txt: str, org, scale: float) -> None:
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 3, cv2.LINE_AA)
    cv2.putText(img, txt, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (11, 11, 11), 1, cv2.LINE_AA)


# --------------------------------------------------------------------------- render


def render_annotated(P, labels: pd.DataFrame | None = None, out_path: str | Path | None = None,
                     t_range: tuple[float, float] | None = None,
                     progress: Callable[[float], None] | None = None, audio: str = "auto",
                     spectrogram: bool = True, span_s: float = 6.0, fmax: float | None = None) -> Path:
    """Annotated copy of the first camera's video; with audio, also the sound (`audio`: auto, stereo, mix, none or a
    microphone name) and the spectrogram strip, up to `fmax` Hz (default: rig.yaml audio.display_fmax_hz, as in
    review)."""
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
    fps = float(cam.get("fps", 30))
    asrc = chunks = strip = events = None
    mode = "mono"
    if P.audio is not None and sess.audio and (audio != "none" or spectrogram) and i1 > i0:
        from ..audio.source import AudioSource
        from .sounds import sound_table

        asrc = AudioSource(sess, P.audio)
        if audio != "none":
            cols, mode = audio_columns(asrc, audio)
            tau_k, t_k = video_timeline(frame_t, i0, i1, fps)
            gain = track_gain(asrc, cols, float(t_k[0]), float(t_k[-1]))
            chunks = ((y.mean(axis=1, keepdims=True) if mode == "mix" else y) * gain
                      for y in audio_on_video_time(asrc, tau_k, t_k, cols))
        if spectrogram:
            events = sound_table(sess, P.audio)
    writer = None
    a_written = 0.0  # seconds of audio in the file
    for i, img in src.frames(i0, i1):
        t = float(frame_t[i])
        ann = draw_overlay(img, rois, t, rfid, manual, tags, i)
        if spectrogram and asrc is not None:
            if strip is None:
                if fmax is None:
                    from ..config import load_rig

                    fmax = float((load_rig().get("audio") or {}).get("display_fmax_hz", 10000))
                strip = SpectrogramStrip(asrc, ann.shape[1], events, tags, span_s=span_s, fmax=fmax)
            ann = np.vstack([ann, strip.draw(t)])
        if writer is None:
            h, w = ann.shape[:2]
            writer = VideoWriter(out_path, fps, (w, h), {"crf": 23, "preset": "veryfast"})
            if chunks is not None:
                writer.add_audio(int(asrc.sr), 2 if mode == "stereo" else 1)
        writer.write(ann)
        while chunks is not None and a_written < (i - i0 + 1) / fps + 0.5:  # keep the audio just ahead
            y = next(chunks, None)
            if y is None:
                chunks = None
                break
            writer.write_audio(y)
            a_written += len(y) / asrc.sr
        if progress and (i - i0) % 100 == 0:
            progress((i - i0) / max(1, i1 - i0))
    src.close()
    if writer is not None:
        for y in chunks or []:
            writer.write_audio(y)
        writer.close()
    if asrc is not None:
        asrc.close()
    if progress:
        progress(1.0)
    return out_path
