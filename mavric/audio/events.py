"""Sound events: the labeled table, reviewed spans, event types, emitters, and exchange formats.

    sound_events.csv     annotator, start_s, stop_s, type, emitter, channel, f_low_hz, f_high_hz,
                         confidence, source, note
    sound_coverage.csv   annotator, start_s, stop_s   (time actually reviewed for sounds)
    audio_playback.csv   (written by playback, AU4) speaker, sound, sample, n_samples[, rule]

Times are seconds on the session timeline, like perch labels. `emitter` is a
tagged subject (pigeon name), `system:<speaker>` for sounds the rig played, or
`unknown`. `source` says where an event came from: manual, proposal (an
accepted automatic proposal), playback (from the playback log) or import.

Audacity label tracks and Raven selection tables use seconds from the start
of one audio file; conversion goes through the session's audio time map.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from ..session import Session

EVENTS_FILE = "sound_events.csv"
COVERAGE_FILE = "sound_coverage.csv"
PLAYBACK_FILE = "audio_playback.csv"
UNKNOWN = "unknown"
SYSTEM = "system"
DEFAULT_TYPES = ["call", "noise", "other"]


@dataclass
class SoundEvent:
    start_s: float
    stop_s: float
    type: str = "other"
    emitter: str = UNKNOWN
    channel: str = ""  # microphone it was marked on ("" = all)
    f_low_hz: float = float("nan")
    f_high_hz: float = float("nan")
    confidence: str = ""  # e.g. "sure", "unsure"
    source: str = "manual"
    annotator: str = ""
    note: str = ""

    @property
    def duration_s(self) -> float:
        return self.stop_s - self.start_s


COLUMNS = ["annotator", "start_s", "stop_s", "type", "emitter", "channel", "f_low_hz", "f_high_hz", "confidence",
           "source", "note"]


def events_to_df(events: list[SoundEvent]) -> pd.DataFrame:
    df = pd.DataFrame([asdict(e) for e in events], columns=[f.name for f in fields(SoundEvent)])
    return df[COLUMNS].sort_values("start_s").reset_index(drop=True)


def df_to_events(df: pd.DataFrame) -> list[SoundEvent]:
    out = []
    for r in df.to_dict(orient="records"):
        out.append(SoundEvent(
            start_s=float(r["start_s"]), stop_s=float(r["stop_s"]), type=str(r.get("type") or "other"),
            emitter=str(r.get("emitter") or UNKNOWN), channel=str(r.get("channel") or ""),
            f_low_hz=_num(r.get("f_low_hz")), f_high_hz=_num(r.get("f_high_hz")),
            confidence=str(r.get("confidence") or ""), source=str(r.get("source") or "manual"),
            annotator=str(r.get("annotator") or ""), note=str(r.get("note") or "")))
    return sorted(out, key=lambda e: e.start_s)


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def load_events(sess: Session) -> list[SoundEvent]:
    p = sess.file(EVENTS_FILE)
    if not p.exists():
        return []
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    return df_to_events(df)


def save_events(sess: Session, events: list[SoundEvent]) -> Path:
    p = sess.file(EVENTS_FILE)
    tmp = p.with_suffix(".tmp")
    events_to_df(events).to_csv(tmp, index=False)
    tmp.replace(p)
    return p


def load_sound_coverage(sess: Session) -> list[tuple[float, float]]:
    p = sess.file(COVERAGE_FILE)
    if not p.exists():
        return []
    df = pd.read_csv(p)
    return [(float(a), float(b)) for a, b in zip(df["start_s"], df["stop_s"]) if b > a]


def save_sound_coverage(sess: Session, spans: list[tuple[float, float]], annotator: str = "") -> Path:
    from ..analysis.labels_io import merge_spans

    p = sess.file(COVERAGE_FILE)
    pd.DataFrame([{"annotator": annotator, "start_s": a, "stop_s": b} for a, b in merge_spans(spans)],
                 columns=["annotator", "start_s", "stop_s"]).to_csv(p, index=False)
    return p


# --------------------------------------------------------------------------- types and emitters


def load_types(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return list(DEFAULT_TYPES)
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    types = [str(t) for t in (d.get("types") or []) if str(t).strip()]
    return types or list(DEFAULT_TYPES)


def save_types(path: str | Path, types: list[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# Sound event types for labeling (mavric review -> Sound events -> Types...).\n")
        yaml.safe_dump({"types": list(types)}, f, sort_keys=False, allow_unicode=True)


def emitters(pigeons: list[str], speakers: list[str]) -> list[str]:
    """Tagged subjects, the rig's speakers, unknown."""
    return list(pigeons) + [f"{SYSTEM}:{s}" for s in speakers] + [UNKNOWN]


# --------------------------------------------------------------------------- playback log -> events


def playback_events(sess: Session, tmap) -> list[SoundEvent]:
    """Sounds the rig played (audio_playback.csv), as events with emitter system:<speaker>."""
    p = sess.file(PLAYBACK_FILE)
    if not p.exists():
        return []
    df = pd.read_csv(p, dtype={"speaker": str, "sound": str}, keep_default_na=False)
    sr = float(sess.audio["sample_rate"]) if sess.audio else 48000.0
    out = []
    for r in df.to_dict(orient="records"):
        if "sample" in r and tmap is not None and str(r["sample"]) != "":
            s0 = float(r["sample"])
            s1 = s0 + float(r.get("n_samples") or 0)
            t0, t1 = (float(x) for x in tmap(np.array([s0, s1])))
        else:
            t0 = float(r["t"])
            t1 = t0 + float(r.get("dur_s") or 0)
        if not np.isfinite(t0):
            continue
        out.append(SoundEvent(t0, max(t1, t0 + 1 / sr), type="playback", emitter=f"{SYSTEM}:{r['speaker']}",
                              source="playback", note=str(r.get("sound", ""))))
    return out


def merge_playback(events: list[SoundEvent], played: list[SoundEvent]) -> tuple[list[SoundEvent], int]:
    """Add playback events that are not in the table yet (same speaker, start within 1 ms)."""
    have = [(e.emitter, e.start_s) for e in events if e.source == "playback"]
    added = 0
    for p in played:
        if not any(em == p.emitter and abs(t - p.start_s) < 1e-3 for em, t in have):
            events.append(p)
            added += 1
    return sorted(events, key=lambda e: e.start_s), added


# --------------------------------------------------------------------------- Audacity / Raven


def _label(e: SoundEvent) -> str:
    return f"{e.type}|{e.emitter}" + (f"|{e.note}" if e.note else "")


def _parse_label(s: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in str(s).split("|")]
    return (parts[0] or "other", parts[1] if len(parts) > 1 and parts[1] else UNKNOWN,
            "|".join(parts[2:]) if len(parts) > 2 else "")


def _file_times(sess: Session, tmap, events: list[SoundEvent]):
    """Per event: (audio file index, start and stop in seconds from that file's start)."""
    a = sess.audio
    sr = float(a["sample_rate"])
    starts = np.array([f["first_sample"] for f in a["files"]], float)
    out = []
    for e in events:
        s0, s1 = (float(x) for x in tmap.sample_at(np.array([e.start_s, e.stop_s])))
        if not np.isfinite(s0) or not np.isfinite(s1):
            out.append(None)
            continue
        i = int(np.searchsorted(starts, s0, side="right") - 1)
        out.append((i, (s0 - starts[i]) / sr, (s1 - starts[i]) / sr))
    return out


def export_events(sess: Session, tmap, events: list[SoundEvent], fmt: str, out_dir: Path | None = None) -> list[Path]:
    """One file per audio file: Audacity label track (.txt) or Raven selection table (.selections.txt)."""
    out_dir = Path(out_dir or sess.analysis_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    a = sess.audio
    ft = _file_times(sess, tmap, events)
    paths = []
    for i, f in enumerate(a["files"]):
        rows = [(e, x[1], x[2]) for e, x in zip(events, ft) if x is not None and x[0] == i]
        stem = Path(f["file"]).stem
        if fmt == "audacity":
            p = out_dir / f"{stem}_labels.txt"
            with open(p, "w", encoding="utf-8") as fh:
                for e, t0, t1 in rows:
                    fh.write(f"{t0:.6f}\t{t1:.6f}\t{_label(e)}\n")
                    if np.isfinite(e.f_low_hz) and np.isfinite(e.f_high_hz):
                        fh.write(f"\\\t{e.f_low_hz:.1f}\t{e.f_high_hz:.1f}\n")
        elif fmt == "raven":
            p = out_dir / f"{stem}.selections.txt"
            chans = a["channels"]
            recs = [{"Selection": k + 1, "View": "Spectrogram 1",
                     "Channel": (chans.index(e.channel) + 1) if e.channel in chans else 1,
                     "Begin Time (s)": round(t0, 6), "End Time (s)": round(t1, 6),
                     "Low Freq (Hz)": round(e.f_low_hz, 1) if np.isfinite(e.f_low_hz) else 0.0,
                     "High Freq (Hz)": round(e.f_high_hz, 1) if np.isfinite(e.f_high_hz)
                     else float(a["sample_rate"]) / 2,
                     "Type": e.type, "Emitter": e.emitter, "Note": e.note}
                    for k, (e, t0, t1) in enumerate(rows)]
            pd.DataFrame(recs, columns=["Selection", "View", "Channel", "Begin Time (s)", "End Time (s)",
                                        "Low Freq (Hz)", "High Freq (Hz)", "Type", "Emitter", "Note"]).to_csv(
                p, sep="\t", index=False)
        else:
            raise ValueError(f"unknown format {fmt!r} (audacity | raven)")
        paths.append(p)
    return paths


def import_events(sess: Session, tmap, path: str | Path, fmt: str | None = None, file_index: int = 0,
                  annotator: str = "") -> list[SoundEvent]:
    """Read an Audacity label track or Raven selection table made on audio file `file_index`."""
    path = Path(path)
    a = sess.audio
    sr = float(a["sample_rate"])
    first = float(a["files"][file_index]["first_sample"])
    fmt = fmt or ("raven" if path.name.endswith(".selections.txt") else "audacity")
    rows = []  # (t0_file_s, t1_file_s, type, emitter, note, f_low, f_high, channel)
    if fmt == "audacity":
        lines = path.read_text(encoding="utf-8").splitlines()
        for ln in lines:
            parts = ln.split("\t")
            if parts[0] == "\\" and rows:  # spectral selection line for the previous label
                r = rows[-1]
                rows[-1] = r[:5] + (_num(parts[1]), _num(parts[2])) + r[7:]
                continue
            if len(parts) < 2:
                continue
            typ, em, note = _parse_label(parts[2] if len(parts) > 2 else "")
            rows.append((float(parts[0]), float(parts[1]), typ, em, note, float("nan"), float("nan"), ""))
    elif fmt == "raven":
        df = pd.read_csv(path, sep="\t", dtype=str, keep_default_na=False)
        chans = a["channels"]
        for r in df.to_dict(orient="records"):
            typ = r.get("Type") or r.get("Annotation") or "other"
            ch = int(r["Channel"]) if str(r.get("Channel", "")).isdigit() else 0
            rows.append((float(r["Begin Time (s)"]), float(r["End Time (s)"]), typ, r.get("Emitter") or UNKNOWN,
                         r.get("Note", ""), _num(r.get("Low Freq (Hz)")), _num(r.get("High Freq (Hz)")),
                         chans[ch - 1] if 0 < ch <= len(chans) else ""))
    else:
        raise ValueError(f"unknown format {fmt!r} (audacity | raven)")
    out = []
    for t0, t1, typ, em, note, fl, fh, ch in rows:
        s = first + np.array([t0, t1]) * sr
        a0, a1 = (float(x) for x in tmap(s))
        out.append(SoundEvent(a0, a1, typ, em, ch, fl, fh, source="import", annotator=annotator, note=note))
    return out
