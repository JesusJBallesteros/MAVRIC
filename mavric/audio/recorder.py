"""Writes a recording: audio files plus the clock and sync-edge sidecars.

    audio_000.flac ...   all configured inputs, one file per `split_s` (no gaps between files)
    audio_clock.csv      host time of block deliveries (for the audio clock fit)
    audio_edges.csv      sync pulse edges, in samples from the start of the recording
    audio_playback.csv   sounds played on the speakers (only when something was played)

The clock sidecar keeps, per `clock_every_s`, the block delivered with the
least delay (the lower envelope is what the clock fit uses), plus every block
that came with a driver warning such as an input overflow.

Playback rows give the recording sample at which the sound reached the
speaker output: the rendered sample plus the driver's round trip
(`latency_samples`). One row per sound, written when it ends, with the
samples actually played (a stopped or looped sound has its true length).
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

import numpy as np

from ..core import CsvAppender
from .config import AudioConfig
from .edges import EDGE_COLUMNS

CLOCK_FILE = "audio_clock.csv"
EDGES_FILE = "audio_edges.csv"
PLAYBACK_FILE = "audio_playback.csv"
CLOCK_COLUMNS = ["sample", "n", "host_t", "adc_t", "flags"]
PLAYBACK_COLUMNS = ["sample", "n_samples", "speaker", "sound", "gain_db", "loop", "stopped", "source",
                    "latency_samples"]
FORMATS = {"FLAC": ("FLAC", ".flac"), "WAV": ("RF64", ".wav")}  # RF64: WAV without the 4 GB limit


class AudioRecorder:
    def __init__(self, folder: str | Path, cfg: AudioConfig, t0: float, clock_every_s: float = 0.1,
                 stream_info: dict | None = None):
        self.folder = Path(folder)
        self.cfg = cfg
        self.t0 = t0
        self.sr = cfg.sample_rate
        self.clock_every = clock_every_s
        if cfg.format not in FORMATS:
            raise ValueError(f"audio format {cfg.format!r}: use one of {sorted(FORMATS)}")
        self.stream_info = stream_info or {}
        self.q: queue.Queue = queue.Queue()
        self.n = 0  # samples written
        self.base: int | None = None  # stream sample of recording sample 0
        self.files: list[dict] = []
        self.n_edges = 0
        self.flag_counts: dict[str, int] = {}
        self.error: str | None = None
        self._sf = None
        self._best = None  # lowest-delay block in the current clock window
        self._win_end = 0.0
        self._since_flush = 0
        self._clock = CsvAppender(self.folder / CLOCK_FILE, CLOCK_COLUMNS)
        self._edges = CsvAppender(self.folder / EDGES_FILE, EDGE_COLUMNS)
        self._play: CsvAppender | None = None  # opened with the first played sound
        self._voices: dict[int, dict] = {}  # started, not ended yet
        self.n_played = 0
        self._th = threading.Thread(target=self._run, daemon=True, name="audio-writer")

    def start(self) -> None:
        self._th.start()

    def submit(self, k0, x, host_abs, adc_t, flags, edges) -> None:
        self.q.put((k0, x, host_abs, adc_t, flags, edges))

    def playback(self, entry: dict, offset: int) -> None:
        """A mixer log entry (start or end of a sound), in stream samples; `offset`: render -> speaker."""
        self.q.put(("play", entry, offset))

    # ------------------------------------------------------------------ writer thread
    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                if item[0] == "play":
                    self._playback(item[1], item[2])
                else:
                    self._write(*item)
            except Exception as e:
                if self.error is None:
                    self.error = f"{type(e).__name__}: {e}"

    def _playback(self, e: dict, offset: int) -> None:
        if e["kind"] == "start":
            if self.base is not None and e["sample"] >= self.base:  # sounds started before the recording are left out
                self._voices[e["voice"]] = {**e, "offset": offset}
            return
        s = self._voices.pop(e["voice"], None)
        if s is not None:
            self._write_play(s, e["n"], e.get("stopped", False))

    def _write_play(self, s: dict, n: int, stopped: bool) -> None:
        if self._play is None:
            self._play = CsvAppender(self.folder / PLAYBACK_FILE, PLAYBACK_COLUMNS)
        gain_db = 20 * np.log10(max(s["gain"], 1e-10))
        self._play.write([s["sample"] + s["offset"] - self.base, int(n), s["speaker"], s["name"], f"{gain_db:.2f}",
                          int(s["loop"]), int(stopped), s["source"], s["offset"]])
        self.n_played += 1

    def _open_file(self) -> None:
        import soundfile as sf

        fmt, ext = FORMATS[self.cfg.format]
        name = f"audio_{len(self.files):03d}{ext}"
        self._sf = sf.SoundFile(self.folder / name, "w", samplerate=self.sr, channels=len(self.cfg.names),
                                format=fmt, subtype=self.cfg.subtype)
        self.files.append({"file": name, "first_sample": self.n, "n_samples": 0})

    def _write(self, k0, x, host_abs, adc_t, flags, edges) -> None:
        if self.base is None:
            self.base = k0
        split = int(self.cfg.split_s * self.sr)
        pos = 0
        while pos < len(x):
            if self._sf is None:
                self._open_file()
            f = self.files[-1]
            k = min(len(x) - pos, split - f["n_samples"])
            self._sf.write(x[pos: pos + k])
            f["n_samples"] += k
            pos += k
            self.n += k
            if f["n_samples"] >= split:
                self._sf.close()
                self._sf = None
        self._since_flush += len(x)
        if self._sf is not None and self._since_flush >= 2 * self.sr:
            self._sf.flush()
            self._since_flush = 0

        # clock: keep the least-delayed block per window, and every flagged block
        end = self.n
        host_t = host_abs - self.t0
        row = (end, len(x), host_t, adc_t, flags)
        if flags:
            for fl in flags.split("|"):
                self.flag_counts[fl] = self.flag_counts.get(fl, 0) + 1
            self._flush_clock()
            self._write_clock(row)
            self._win_end = end / self.sr + self.clock_every
        else:
            delay = host_t - end / self.sr
            if self._best is None or delay < self._best[0]:
                self._best = (delay, row)
            if end / self.sr >= self._win_end:
                self._flush_clock()
                self._win_end = end / self.sr + self.clock_every

        for p, s, a in edges:
            if p >= self.base:
                self._edges.write([f"{p - self.base:.3f}", int(s), f"{a:.5f}"])
                self.n_edges += 1

    def _write_clock(self, row) -> None:
        end, n, host_t, adc_t, flags = row
        adc = "" if adc_t is None or not np.isfinite(adc_t) or adc_t == 0 else f"{adc_t:.6f}"
        self._clock.write([end, n, f"{host_t:.6f}", adc, flags])

    def _flush_clock(self) -> None:
        if self._best is not None:
            self._write_clock(self._best[1])
            self._best = None

    # ------------------------------------------------------------------ finish
    def finish(self) -> dict:
        self.q.put(None)
        self._th.join(timeout=30)
        self._flush_clock()
        if self._sf is not None:
            self._sf.close()
            self._sf = None
        for s in list(self._voices.values()):  # still playing when the recording stopped: up to its end
            self._write_play(s, max(0, self.base + self.n - s["sample"]), True)
        self._voices.clear()
        self._clock.close()
        self._edges.close()
        if self._play is not None:
            self._play.close()
        return self.stats()

    def stats(self) -> dict:
        return {"samples": self.n, "duration_s": self.n / self.sr, "files": len(self.files),
                "sync_edges": self.n_edges, "flags": dict(self.flag_counts), "queue": self.q.qsize(),
                "played": self.n_played, "error": self.error}

    def info(self) -> dict:
        """Session metadata (session.json "audio")."""
        c = self.cfg
        return {
            "device": self.stream_info.get("device") or c.device,
            "host_api": self.stream_info.get("host_api") or c.host_api,
            "sample_rate": self.sr,
            "channels": c.names,
            "input_channels": c.in_channels,
            "sync_channel": c.sync_channel if c.sync_index is not None else None,
            "speakers": {str(k): v for k, v in c.speakers.items()},
            "format": c.format,
            "subtype": c.subtype,
            "block": c.block,
            "latency_s": self.stream_info.get("latency_s"),
            "pulse_s": c.pulse_s,
            "mic_near": {k: list(v) for k, v in c.mic_near.items()},
            "files": self.files,
            "clock_file": CLOCK_FILE,
            "edges_file": EDGES_FILE,
        }
