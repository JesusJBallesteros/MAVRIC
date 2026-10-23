"""Live audio: one PortAudio stream (inputs, plus outputs for the speakers when configured).

Recording and playback share one duplex stream on one device, so they run on
one sample clock and each played sound has an exact position in the recording.

The PortAudio callback only stamps the host clock, renders the playback
mixer and queues the input block. A worker thread then updates the live ring
buffer and level meters, detects sync pulse edges and hands the block to the
recorder, if one is attached.
"""

from __future__ import annotations

import queue
import threading
from collections import deque

import numpy as np

from ..core import EventBus, now
from .config import AudioConfig
from .devices import find_device, import_sounddevice
from .edges import EdgeDetector


def db(x) -> float:
    return float(20 * np.log10(max(float(x), 1e-10)))


class AudioRing:
    """The last `seconds` of all channels, for live displays."""

    def __init__(self, n_ch: int, capacity: int):
        self.buf = np.zeros((capacity, n_ch), np.float32)
        self.count = 0  # samples written so far
        self._lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        with self._lock:
            n, cap = len(x), len(self.buf)
            m = min(n, cap)
            self.buf[np.arange(self.count + n - m, self.count + n) % cap] = x[n - m:]
            self.count += n

    def since(self, count: int) -> tuple[np.ndarray, int]:
        """Samples written after sample number `count` (at most the ring size), and the new count."""
        with self._lock:
            cap = len(self.buf)
            n = int(min(self.count - count, cap, self.count))
            if n <= 0:
                return np.zeros((0, self.buf.shape[1]), np.float32), self.count
            idx = np.arange(self.count - n, self.count) % cap
            return self.buf[idx].copy(), self.count


class Mixer:
    """Plays sample arrays on output channels; rendered inside the audio callback."""

    def __init__(self, n_out: int, max_level_dbfs: float = -6.0):
        self.n_out = n_out
        self.max_amp = float(10 ** (max_level_dbfs / 20))
        self._pending: deque = deque()
        self._voices: list[list] = []  # [samples, channel, position, gain, name]
        self._stop: deque = deque()
        self.played: deque = deque(maxlen=10000)  # (stream sample at start, channel, name)

    def play(self, samples: np.ndarray, channel: int, gain: float = 1.0, name: str = "") -> None:
        if not 0 <= channel < self.n_out:
            raise ValueError(f"output channel {channel + 1} not open (have {self.n_out})")
        self._pending.append([np.asarray(samples, np.float32).ravel(), channel, 0, float(gain), name])

    def stop(self, channel: int | None = None) -> None:
        self._stop.append(channel)

    @property
    def busy(self) -> bool:
        return bool(self._voices or self._pending)

    def render(self, out: np.ndarray, k0: int) -> None:
        out.fill(0)
        while self._stop:
            ch = self._stop.popleft()
            if ch is None:
                self._pending.clear()
            self._voices = [v for v in self._voices if ch is not None and v[1] != ch]
        while self._pending:
            v = self._pending.popleft()
            self._voices.append(v)
            self.played.append((k0, v[1], v[4]))
        if not self._voices:
            return
        n = len(out)
        alive = []
        for v in self._voices:
            buf, ch, pos, gain, _ = v
            k = min(n, len(buf) - pos)
            out[:k, ch] += buf[pos: pos + k] * gain
            v[2] = pos + k
            if v[2] < len(buf):
                alive.append(v)
        self._voices = alive
        np.clip(out, -self.max_amp, self.max_amp, out=out)


class SoundDeviceBackend:
    """A PortAudio device stream through python-sounddevice."""

    def __init__(self, cfg: AudioConfig):
        self.cfg = cfg
        self.stream = None
        self.device: dict | None = None

    def open(self, handler) -> None:
        sd = import_sounddevice()
        c = self.cfg
        dev = find_device(c.device, c.host_api, c.n_in_open, c.n_out_open)
        self.device = dev
        cols = [ch - 1 for ch in c.in_channels]

        if c.n_out_open:
            def cb(indata, outdata, frames, time_info, status):
                handler(indata[:, cols], outdata, frames, now(), time_info.inputBufferAdcTime, _flags(status))

            self.stream = sd.Stream(device=(dev["index"], dev["index"]), channels=(c.n_in_open, c.n_out_open),
                                    samplerate=c.sample_rate, blocksize=c.block, dtype="float32",
                                    latency=c.latency, callback=cb)
        else:
            def cb(indata, frames, time_info, status):
                handler(indata[:, cols], None, frames, now(), time_info.inputBufferAdcTime, _flags(status))

            self.stream = sd.InputStream(device=dev["index"], channels=c.n_in_open, samplerate=c.sample_rate,
                                         blocksize=c.block, dtype="float32", latency=c.latency, callback=cb)
        self.stream.start()

    def info(self) -> dict:
        d = self.device or {}
        lat = self.stream.latency if self.stream is not None else None
        lat = list(lat) if isinstance(lat, (tuple, list)) else [lat, None]
        return {"device": d.get("name"), "host_api": d.get("host_api"), "latency_s": lat,
                "sample_rate": float(self.stream.samplerate) if self.stream is not None else None}

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
            finally:
                self.stream.close()
                self.stream = None


def _flags(status) -> str:
    if not status:
        return ""
    names = (("input_overflow", "in_over"), ("input_underflow", "in_under"),
             ("output_underflow", "out_under"), ("output_overflow", "out_over"))
    return "|".join(short for attr, short in names if getattr(status, attr, False))


class AudioStream:
    """Live audio device: ring buffer, level meters, sync edges, recorder hand-off, playback mixer."""

    def __init__(self, cfg: AudioConfig, bus: EventBus | None = None, world=None, ring_s: float = 12.0, backend=None):
        self.cfg = cfg
        self.bus = bus
        if backend is None:
            if cfg.is_sim:
                from .sim import SimAudioBackend

                backend = SimAudioBackend(cfg, world)
            else:
                backend = SoundDeviceBackend(cfg)
        self.backend = backend
        n_ch = len(cfg.names)
        self.ring = AudioRing(n_ch, int(ring_s * cfg.sample_rate))
        self.mixer = Mixer(cfg.n_out_open, cfg.max_level_dbfs)
        self.edges = EdgeDetector(cfg.sample_rate) if cfg.sync_index is not None else None
        self.n_samples = 0  # stream samples so far (input and output advance together)
        self.n_blocks = 0
        self.flag_counts: dict[str, int] = {}
        self.n_edges = 0
        self.last_edge_abs: float | None = None
        self.error: str | None = None
        self._peak = np.zeros(n_ch)
        self._sq = np.zeros(n_ch)
        self._n_lvl = 0
        self.clipped = np.zeros(n_ch, int)
        self._sink = None
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._work, daemon=True, name="audio-worker")
        self._k_cb = 0  # callback-side sample counter

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        self._worker.start()
        try:
            self.backend.open(self._callback)
        except Exception:
            self._q.put(None)
            raise

    def close(self) -> None:
        try:
            self.backend.close()
        finally:
            self._q.put(None)
            if self._worker.is_alive():
                self._worker.join(timeout=3)

    def info(self) -> dict:
        return {**self.backend.info(), "configured_sample_rate": self.cfg.sample_rate, "block": self.cfg.block}

    # ------------------------------------------------------------------ recording
    def attach(self, sink) -> None:
        """Send every following block to `sink.submit(k0, block, host_abs, adc_t, flags, edges)`."""
        with self._lock:
            self._sink = sink

    def detach(self):
        with self._lock:
            sink, self._sink = self._sink, None
            return sink

    # ------------------------------------------------------------------ playback
    def play(self, samples: np.ndarray, speaker: str | int, gain_db: float = 0.0, name: str = "") -> None:
        self.mixer.play(samples, self.cfg.speaker_index(speaker), 10 ** (gain_db / 20), name)

    # ------------------------------------------------------------------ data path
    def _callback(self, indata, outdata, frames, host_abs, adc_t, flags) -> None:
        k0 = self._k_cb
        self._k_cb += frames
        if outdata is not None:
            self.mixer.render(outdata, k0)
        self._q.put((k0, indata.copy(), host_abs, adc_t, flags))

    def _work(self) -> None:
        si = self.cfg.sync_index
        while True:
            item = self._q.get()
            if item is None:
                return
            k0, x, host_abs, adc_t, flags = item
            try:
                self.ring.write(x)
                pk = np.abs(x).max(axis=0)
                with self._lock:
                    self._peak = np.maximum(self._peak, pk)
                    self._sq += (x.astype(np.float64) ** 2).sum(axis=0)
                    self._n_lvl += len(x)
                self.clipped += (np.abs(x) >= 0.999).sum(axis=0)
                if flags:
                    for f in flags.split("|"):
                        self.flag_counts[f] = self.flag_counts.get(f, 0) + 1
                edges = []
                if self.edges is not None:
                    edges = self.edges.process(x[:, si], k0)
                    if edges:
                        self.n_edges += len(edges)
                        self.last_edge_abs = host_abs
                self.n_samples = k0 + len(x)
                self.n_blocks += 1
                with self._lock:
                    sink = self._sink
                if sink is not None:
                    sink.submit(k0, x, host_abs, adc_t, flags, edges)
            except Exception as e:  # keep the stream alive; report once
                if self.error is None:
                    self.error = f"{type(e).__name__}: {e}"
                    if self.bus:
                        self.bus.publish("status", {"source": "audio", "error": self.error})

    # ------------------------------------------------------------------ live status
    def take_levels(self) -> tuple[np.ndarray, np.ndarray]:
        """Peak and RMS (dBFS) per channel since the last call."""
        with self._lock:
            peak, sq, n = self._peak, self._sq, self._n_lvl
            self._peak = np.zeros_like(peak)
            self._sq = np.zeros_like(sq)
            self._n_lvl = 0
        rms = np.sqrt(sq / n) if n else np.zeros_like(sq)
        return np.array([db(p) for p in peak]), np.array([db(r) for r in rms])

    @property
    def overflows(self) -> int:
        return self.flag_counts.get("in_over", 0)

    def status(self) -> dict:
        return {"samples": self.n_samples, "seconds": self.n_samples / self.cfg.sample_rate,
                "queue": self._q.qsize(), "flags": dict(self.flag_counts), "sync_edges": self.n_edges,
                "last_sync_age_s": (now() - self.last_edge_abs) if self.last_edge_abs else None,
                "clipped": self.clipped.tolist(), "error": self.error}


class MemorySink:
    """Collects blocks in memory (audio test)."""

    def __init__(self):
        self.blocks: list[np.ndarray] = []
        self.clock: list[tuple[int, float, str]] = []  # (end sample, host_abs, flags)
        self.edges: list[tuple[float, int, float]] = []
        self.k_first: int | None = None

    def submit(self, k0, x, host_abs, adc_t, flags, edges) -> None:
        if self.k_first is None:
            self.k_first = k0
        self.blocks.append(x)
        self.clock.append((k0 + len(x) - self.k_first, host_abs, flags))
        self.edges += [(p - self.k_first, s, a) for p, s, a in edges if p >= self.k_first]

    def data(self) -> np.ndarray:
        return np.concatenate(self.blocks) if self.blocks else np.zeros((0, 1), np.float32)
