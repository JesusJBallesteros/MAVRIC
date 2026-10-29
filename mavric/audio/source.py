"""Random access to a session's audio on the session timeline, for review and analysis.

`AudioSource` reads any time span across the hourly files, keeps a loudness
overview (min/max per ~21 ms bin, cached in analysis/audio_envelope.npz) so
multi-hour sessions draw instantly, and computes spectrograms of short spans
on demand.
"""

from __future__ import annotations

import json

import numpy as np

from ..session import Session

ENVELOPE_FILE = "audio_envelope.npz"
# spectrogram colours (low -> high level): review, the annotated video and the README pictures
SPEC_STOPS = [(0.0, "#fcfcfb"), (0.15, "#cde2fb"), (0.4, "#86b6ef"), (0.6, "#3987e5"), (0.8, "#1c5cab"),
              (1.0, "#0d366b")]


def spectrogram_lut(n: int = 256) -> np.ndarray:
    """SPEC_STOPS as an (n, 3) uint8 RGB lookup table."""
    pos = np.array([p for p, _ in SPEC_STOPS])
    rgb = np.array([[int(c[i:i + 2], 16) for i in (1, 3, 5)] for _, c in SPEC_STOPS], float)
    x = np.linspace(0, 1, n)
    return np.stack([np.interp(x, pos, rgb[:, k]) for k in range(3)], axis=1).round().astype(np.uint8)


class AudioSource:
    def __init__(self, sess: Session, tmap):
        a = sess.audio
        if not a:
            raise ValueError(f"Session {sess.path.name} has no audio")
        self.sess = sess
        self.tmap = tmap
        self.sr = float(a["sample_rate"])
        self.channels: list[str] = list(a["channels"])
        self.sync_channel = a.get("sync_channel")
        self.files = a["files"]
        self.n_samples = int(sum(f["n_samples"] for f in self.files))
        self.mic_idx = [i for i, n in enumerate(self.channels) if n != self.sync_channel]
        self.mics = [self.channels[i] for i in self.mic_idx]
        self.mic_near = {str(k): [int(x) for x in v] for k, v in (a.get("mic_near") or {}).items()}
        self._handles: dict[int, object] = {}
        self.t_start = float(tmap(np.array([0.0]))[0])
        self.t_stop = float(tmap(np.array([float(self.n_samples)]))[0])

    # ------------------------------------------------------------------ time <-> samples
    def sample_at(self, t) -> np.ndarray:
        t = np.atleast_1d(np.asarray(t, float))
        s = self.tmap.sample_at(t)
        s = np.where(np.isnan(s) & (t <= self.t_start), 0.0, s)
        s = np.where(np.isnan(s) & (t >= self.t_stop), float(self.n_samples), s)
        return s

    def time_of(self, samples) -> np.ndarray:
        return self.tmap(np.asarray(samples, float))

    # ------------------------------------------------------------------ reading
    def _handle(self, i: int):
        import soundfile as sf

        if i not in self._handles:
            self._handles[i] = sf.SoundFile(str(self.sess.file(self.files[i]["file"])))
        return self._handles[i]

    def close(self) -> None:
        for h in self._handles.values():
            h.close()
        self._handles = {}

    def read_samples(self, s0: int, s1: int, cols: list[int] | None = None) -> np.ndarray:
        """Samples [s0, s1) of the recording (all files joined), float32 (n, len(cols))."""
        s0, s1 = max(0, int(s0)), min(self.n_samples, int(s1))
        cols = list(range(len(self.channels))) if cols is None else list(cols)
        out = np.zeros((max(0, s1 - s0), len(cols)), np.float32)
        for i, f in enumerate(self.files):
            a, b = int(f["first_sample"]), int(f["first_sample"]) + int(f["n_samples"])
            lo, hi = max(s0, a), min(s1, b)
            if hi <= lo:
                continue
            h = self._handle(i)
            h.seek(lo - a)
            x = h.read(hi - lo, dtype="float32", always_2d=True)
            out[lo - s0: lo - s0 + len(x)] = x[:, cols]
        return out

    def read(self, t0: float, t1: float, cols: list[int] | None = None) -> tuple[np.ndarray, int]:
        """Audio between session times t0 and t1, and the recording sample of its first row."""
        s0, s1 = (int(round(x)) for x in self.sample_at([t0, t1]))
        return self.read_samples(s0, s1, cols), s0

    # ------------------------------------------------------------------ overview
    def _stamp(self) -> str:
        st = []
        for f in self.files:
            p = self.sess.file(f["file"])
            s = p.stat()
            st.append([f["file"], s.st_size, int(s.st_mtime)])
        return json.dumps(st)

    def envelope(self, bin_samples: int = 1024, progress=None) -> dict:
        """min/max per bin for every mic: {"t": bin centres (session s), "lo": (n, mics), "hi": (n, mics)}."""
        cache = self.sess.analysis_dir / ENVELOPE_FILE
        stamp = self._stamp()
        if cache.exists():
            d = np.load(cache, allow_pickle=False)
            if str(d["stamp"]) == stamp and int(d["bin"]) == bin_samples:
                return self._env_dict(d["lo"], d["hi"], bin_samples)
        n_bins = int(np.ceil(self.n_samples / bin_samples))
        lo = np.zeros((n_bins, len(self.mic_idx)), np.float32)
        hi = np.zeros_like(lo)
        chunk = bin_samples * 2048
        for s0 in range(0, self.n_samples, chunk):
            x = self.read_samples(s0, s0 + chunk, self.mic_idx)
            n = len(x) // bin_samples * bin_samples
            k = s0 // bin_samples
            if n:
                xb = x[:n].reshape(-1, bin_samples, x.shape[1])
                lo[k: k + len(xb)] = xb.min(axis=1)
                hi[k: k + len(xb)] = xb.max(axis=1)
            if len(x) > n:  # last partial bin
                lo[k + n // bin_samples] = x[n:].min(axis=0)
                hi[k + n // bin_samples] = x[n:].max(axis=0)
            if progress:
                progress(min(1.0, (s0 + chunk) / max(self.n_samples, 1)))
        np.savez_compressed(cache, lo=lo, hi=hi, bin=bin_samples, stamp=stamp)
        return self._env_dict(lo, hi, bin_samples)

    def _env_dict(self, lo, hi, bin_samples) -> dict:
        centres = (np.arange(len(lo)) + 0.5) * bin_samples
        return {"t": self.time_of(centres), "lo": lo, "hi": hi, "bin": bin_samples}

    # ------------------------------------------------------------------ spectrogram
    def spectrogram(self, t0: float, t1: float, col: int, n_cols: int = 1200, nfft: int = 1024,
                    fmax: float = 10000.0) -> tuple[np.ndarray, float, float, float]:
        """dB spectrogram of one channel between t0 and t1: (S (n_freq, n_cols), t0, t1, f_top)."""
        x, s0 = self.read(t0, t1, [col])
        x = x[:, 0]
        if len(x) < nfft:
            return np.full((2, 2), -120.0, np.float32), t0, t1, fmax
        starts = np.unique(np.linspace(0, len(x) - nfft, max(2, n_cols)).astype(int))
        win = np.hanning(nfft).astype(np.float32)
        frames = x[starts[:, None] + np.arange(nfft)[None, :]] * win
        n_freq = int(min(fmax, self.sr / 2) / (self.sr / nfft)) + 1
        spec = np.abs(np.fft.rfft(frames, axis=1))[:, :n_freq] * (2.0 / win.sum())
        S = 20 * np.log10(np.maximum(spec, 1e-10)).T.astype(np.float32)
        ta, tb = (float(v) for v in self.time_of(s0 + np.array([starts[0] + nfft / 2, starts[-1] + nfft / 2])))
        return S, ta, tb, (n_freq - 1) * self.sr / nfft
