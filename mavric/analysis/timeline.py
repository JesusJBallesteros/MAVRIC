"""One timeline for other tools: the video, its sound and every label track, on the video's own clock.

    analysis/timeline/
      audio_<cam>.wav               the microphones, resampled so that sample 0 is the video's first frame
      sound_events.txt              Audacity label tracks, in seconds of the video:
      played_sounds.txt               sounds, played sounds,
      <subject>_places_rfid.txt       each subject's places by RFID
      <subject>_places_labels.txt     and by the manual labels
      timeline.csv                  every track in one table, in session and video seconds
      README.txt                    how to open it in Audacity, Ethograph or BORIS

The video is any of the session's videos with a constant frame rate, where
frame k plays at k / fps: `<cam>_compressed.mp4` from `mavric compress`, or
`<cam>.mp4`. Sessions without video use the audio's own clock instead
(seconds from the first sample), and the WAV holds the recording as it is.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

TIMELINE_COLUMNS = ["track", "subject", "place", "label", "start_s", "stop_s", "start_clock_s", "stop_clock_s"]

README = """MAVRIC timeline export: session {session}

Clock: {clock}. Every file here counts seconds from the same zero, so they line up in any tool.
  {zero}

Files
  {wav}  the microphones ({channels}), {rate} Hz
  sound_events.txt, played_sounds.txt            Audacity label tracks: sounds (subject type @place) and played sounds
  <subject>_places_rfid.txt / _places_labels.txt  Audacity label tracks: where each subject was (RFID / your labels)
  timeline.csv                                   all of it in one table (session seconds and clock seconds)

Audacity: File > Import > Audio ({wav}), then File > Import > Labels for each .txt.
Ethograph: drag and drop the video and {wav}; import the .txt files as Audacity labels.
BORIS: add the video and {wav} as two media files of one observation, both starting at 0.
"""


def _labels(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for a, b, txt in rows:
            f.write(f"{a:.6f}\t{b:.6f}\t{txt}\n")


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def export_timeline(P, labels: pd.DataFrame | None = None, out_dir: str | Path | None = None, audio: bool = True,
                    progress=None) -> Path:
    """Write analysis/timeline/ for session `P` (from `prepare`). Returns the folder."""
    from ..audio.source import AudioSource
    from .sounds import link_events, sound_table

    sess = P.session
    out = Path(out_dir or sess.analysis_dir / "timeline")
    out.mkdir(parents=True, exist_ok=True)
    src = AudioSource(sess, P.audio) if sess.audio and P.audio is not None else None
    video = P.frame_t is not None and len(P.frame_t) > 1
    if not video and src is None:
        raise ValueError(f"{sess.path.name} has neither video nor audio")
    if video:
        from .render import audio_on_video_time, video_timeline

        cam = sess.camera()
        fps = float(cam.get("fps", 30))
        tau_k, t_k = video_timeline(P.frame_t, 0, len(P.frame_t), fps)
        t_k = np.maximum.accumulate(t_k)  # the inverse map needs times that never go back

        def clock(t):
            return np.interp(np.asarray(t, float), t_k, tau_k)

        clock_name = f"video {cam['id']}"
        zero = f"0 s = the first frame of {cam['id']}; frame k plays at k / {fps:g} s ({cam['id']}_compressed.mp4)"
        wav = f"audio_{cam['id']}.wav"
    else:
        def clock(t):
            return src.sample_at(t) / src.sr

        clock_name = "audio"
        zero = "0 s = the first audio sample"
        wav = "audio.wav"

    rows = []  # track, subject, place, label, start_s, stop_s
    for r in (P.bouts.itertuples() if len(P.bouts) else []):
        if getattr(r, "curated", "") != "spurious":
            rows.append(["place_rfid", r.pigeon, r.roi, r.roi, r.start_s, r.stop_s])
    for r in (labels.itertuples() if labels is not None and len(labels) else []):
        rows.append(["place_label", r.pigeon, r.roi, r.roi, r.start_s, r.stop_s])
    if src is not None:
        ev = link_events(sound_table(sess, P.audio), P.bouts, labels, sess.tags.pigeons)
        for r in ev.itertuples():
            if r.emitter_kind == "system":
                rows.append(["played", r.emitter.split(":", 1)[1], "", r.note or "played", r.start_s, r.stop_s])
            else:
                rows.append(["sound", r.emitter, r.place_rfid, r.type, r.start_s, r.stop_s])
    df = pd.DataFrame(rows, columns=TIMELINE_COLUMNS[:6])
    df["start_clock_s"] = clock(df["start_s"]) if len(df) else []
    df["stop_clock_s"] = clock(df["stop_s"]) if len(df) else []
    df = df.sort_values(["start_s", "track"]).reset_index(drop=True)
    df.to_csv(out / "timeline.csv", index=False)

    def lab(d, fmt):
        return [(a, b, fmt(r)) for a, b, r in zip(d["start_clock_s"], d["stop_clock_s"], d.itertuples())]

    s = df[df["track"] == "sound"]
    _labels(out / "sound_events.txt", lab(s, lambda r: f"{r.subject} {r.label}" + (f" @{r.place}" if r.place else "")))
    s = df[df["track"] == "played"]
    _labels(out / "played_sounds.txt", lab(s, lambda r: f"{r.label} on {r.subject}"))
    for subj in sess.tags.pigeons:
        for track, suffix in (("place_rfid", "rfid"), ("place_label", "labels")):
            s = df[(df["track"] == track) & (df["subject"] == subj)]
            if len(s):
                _labels(out / f"{_safe(subj)}_places_{suffix}.txt", lab(s, lambda r: r.place))

    channels = ""
    if src is not None and audio:
        import soundfile as sf

        cols = src.mic_idx
        channels = ", ".join(src.channels[c] for c in cols)
        n_total = (tau_k[-1] if video else src.n_samples / src.sr) * src.sr
        big = n_total * len(cols) * 3 > 3.9e9  # past 4 GB a plain WAV cannot count its samples: RF64
        with sf.SoundFile(str(out / wav), "w", int(src.sr), len(cols), "PCM_24", format="RF64" if big else "WAV") as f:
            if video:
                chunks = audio_on_video_time(src, tau_k, t_k, cols, chunk_s=5.0)
            else:
                chunks = (src.read_samples(k, k + int(5 * src.sr), cols)
                          for k in range(0, src.n_samples, int(5 * src.sr)))
            done = 0
            for y in chunks:
                f.write(y)
                done += len(y)
                if progress:
                    progress(min(1.0, done / max(n_total, 1)))
    if src is not None:
        src.close()
    (out / "README.txt").write_text(README.format(session=sess.path.name, clock=clock_name, zero=zero, wav=wav,
                                                  channels=channels or "not written",
                                                  rate=int(src.sr) if src is not None else "-"), encoding="utf-8")
    return out
