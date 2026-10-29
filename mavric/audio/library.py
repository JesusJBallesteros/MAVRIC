"""Stimulus library: the sounds MAVRIC can play, loaded into memory once.

Every WAV / FLAC / OGG file under `audio.sounds_dir` (default `sounds/`) is a
sound, named by its path below that folder without the extension
(`calls/coo_A.wav` -> `calls/coo_A`). Files are mixed down to mono and
resampled to the stream's rate at load, so starting a sound costs no file I/O.
Stimuli are short (under 2 s in the plan), so the whole library sits in
memory; longer files load too, with a note.

`test_tone` (1 kHz, 200 ms, -20 dBFS) is always there, for checking a speaker
or headphones without any files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

EXTENSIONS = (".wav", ".flac", ".ogg")
TEST_TONE = "test_tone"
LONG_S = 10.0  # longer sounds load, with a note (the plan expects stimuli under 2 s)


@dataclass
class Sound:
    name: str
    samples: np.ndarray  # mono float32 at the library rate
    sample_rate: int
    file: str = ""
    source_rate: int = 0  # rate of the file before resampling
    source_channels: int = 1
    sha1: str = ""  # of the file, so a session records exactly which stimulus was played

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate

    @property
    def peak_dbfs(self) -> float:
        return float(20 * np.log10(max(float(np.abs(self.samples).max(initial=0.0)), 1e-10)))


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Band-limited resampling through the FFT (fine for short stimuli; best is to save them at `dst`)."""
    if src == dst or not len(x):
        return x.astype(np.float32)
    pad = int(0.05 * src)  # zeros at the end keep the circular FFT from wrapping the tail onto the start
    xp = np.concatenate([x.astype(np.float64), np.zeros(pad)])
    n2 = int(round(len(xp) * dst / src))
    spec = np.fft.rfft(xp)
    out = np.zeros(n2 // 2 + 1, complex)
    m = min(len(spec), len(out))
    out[:m] = spec[:m]
    y = np.fft.irfft(out, n2) * (n2 / len(xp))
    return y[: int(round(len(x) * dst / src))].astype(np.float32)


def list_sound_names(folder: str | Path) -> list[str]:
    """Sound names from the file names alone (no decoding), test_tone first: for pickers."""
    folder = Path(folder)
    names = sorted({p.relative_to(folder).with_suffix("").as_posix() for p in folder.rglob("*")
                    if p.suffix.lower() in EXTENSIONS}, key=str.lower) if folder.is_dir() else []
    return [TEST_TONE] + [n for n in names if n != TEST_TONE]


def test_tone(sample_rate: int) -> Sound:
    from .check import tone

    return Sound(TEST_TONE, tone(1000.0, 0.2, sample_rate, -20.0), sample_rate, "(built in)", sample_rate)


class SoundLibrary:
    def __init__(self, folder: str | Path, sample_rate: int = 48000):
        self.folder = Path(folder)
        self.sample_rate = int(sample_rate)
        self.sounds: dict[str, Sound] = {}
        self.notes: list[str] = []  # files that could not be read, resampled or very long sounds
        self.reload()

    def reload(self) -> None:
        import soundfile as sf

        sounds = {TEST_TONE: test_tone(self.sample_rate)}
        notes = []
        files = sorted(p for p in self.folder.rglob("*") if p.suffix.lower() in EXTENSIONS) \
            if self.folder.is_dir() else []
        for p in files:
            name = p.relative_to(self.folder).with_suffix("").as_posix()
            if name in sounds:
                notes.append(f"{p.name}: another file is already called {name!r}; skipped")
                continue
            try:
                x, sr = sf.read(str(p), dtype="float32", always_2d=True)
            except Exception as e:
                notes.append(f"{p.name}: cannot be read ({type(e).__name__}: {e})")
                continue
            mono = x.mean(axis=1)
            if sr != self.sample_rate:
                notes.append(f"{name}: {sr} Hz, resampled to {self.sample_rate} Hz")
                mono = resample(mono, sr, self.sample_rate)
            s = Sound(name, mono.astype(np.float32), self.sample_rate, str(p), int(sr), x.shape[1],
                      hashlib.sha1(p.read_bytes()).hexdigest()[:12])
            if s.duration_s > LONG_S:
                notes.append(f"{name}: {s.duration_s:.0f} s long (kept in memory)")
            sounds[name] = s
        self.sounds, self.notes = sounds, notes

    def manifest(self) -> dict:
        """What each sound is, for session.json (audio.sounds)."""
        out = {}
        for name, s in self.sounds.items():
            try:
                rel = Path(s.file).relative_to(self.folder).as_posix()
            except ValueError:
                rel = s.file  # the built-in tone
            out[name] = {"file": rel,
                         "duration_s": round(s.duration_s, 6), "source_rate": s.source_rate,
                         "channels": s.source_channels, "peak_dbfs": round(s.peak_dbfs, 2), "sha1": s.sha1}
        return out

    @property
    def names(self) -> list[str]:
        return sorted(self.sounds, key=lambda n: (n != TEST_TONE, n.lower()))

    def get(self, name: str) -> Sound:
        key = str(name).strip()
        if key not in self.sounds:  # allow the file name with its extension
            key = Path(key).with_suffix("").as_posix() if Path(key).suffix.lower() in EXTENSIONS else key
        if key not in self.sounds:
            raise KeyError(f"no sound {name!r} in {self.folder} (have: {', '.join(self.names[:12])}"
                           f"{' ...' if len(self.sounds) > 12 else ''})")
        return self.sounds[key]
