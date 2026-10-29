"""Plays library sounds on the speakers of the live audio stream.

Used by the acquire window's soundboard, the outputs engine's `audio` device
and `mavric audio play`. A sound is queued in the stream's mixer and starts
with the next audio block (latency: about one block plus the device's output
latency). The recorder logs every played sound in `audio_playback.csv`.

Levels: `gain_db` scales the file as it is. The stream's limiter clips the mix
at `audio.max_level_dbfs`; play() says so when a sound's peak plus its gain
would go over it.
"""

from __future__ import annotations

import re

from .library import SoundLibrary


class Player:
    def __init__(self, stream, library: SoundLibrary):
        self.stream = stream
        self.library = library

    @property
    def speakers(self) -> list[str]:
        return [self.stream.cfg.speakers[c] for c in sorted(self.stream.cfg.speakers)]

    def resolve_speakers(self, spec) -> list[str]:
        """'all' or '' -> every speaker; otherwise names or 1-based outputs, separated by commas or spaces."""
        if spec is None or str(spec).strip().lower() in ("", "all", "*"):
            return self.speakers
        items = spec if isinstance(spec, (list, tuple)) else re.split(r"[,\s]+", str(spec).strip())
        out = []
        for it in items:
            it = str(it).strip()
            if not it:
                continue
            ch = self.stream.cfg.speaker_index(it) + 1  # KeyError for unknown speakers
            out.append(self.stream.cfg.speakers[ch])
        return out

    def play(self, sound: str, speakers="all", gain_db: float = 0.0, loop: bool = False,
             source: str = "") -> tuple[list[int], str]:
        """Start `sound` on each speaker. Returns (voice ids, a short description for logs)."""
        if not self.stream.cfg.n_out_open:
            raise RuntimeError("no speakers configured (rig.yaml audio.speakers)")
        snd = self.library.get(sound)
        spk = self.resolve_speakers(speakers)
        ids = [self.stream.play(snd.samples, s, gain_db, name=snd.name, loop=loop, source=source) for s in spk]
        text = f"{snd.name} ({snd.duration_s * 1e3:.0f} ms{', loop' if loop else ''}) on {', '.join(spk)} " \
               f"at {gain_db:+.1f} dB"
        peak = snd.peak_dbfs + gain_db
        if peak > self.stream.cfg.max_level_dbfs + 0.05:
            text += f"; LIMITED: peak {peak:.1f} dBFS > limit {self.stream.cfg.max_level_dbfs:g} dBFS"
        return ids, text

    def stop(self, speakers=None, voices: list[int] | None = None) -> str:
        """Fade out the given voices, else the given speakers, else everything."""
        if voices:
            for v in voices:
                self.stream.stop(voice=v)
            return f"stopped {len(voices)} sound(s)"
        if speakers is None or str(speakers).strip().lower() in ("", "all", "*"):
            self.stream.stop()
            return "stopped all sounds"
        spk = self.resolve_speakers(speakers)
        for s in spk:
            self.stream.stop(speaker=s)
        return f"stopped {', '.join(spk)}"

    def stop_all(self) -> None:
        self.stream.stop()

    def playing(self) -> list[dict]:
        return [{**v, "speaker": self.stream.cfg.speakers.get(v["channel"] + 1, str(v["channel"] + 1))}
                for v in self.stream.mixer.active()]
