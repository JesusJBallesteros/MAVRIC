"""RFID-triggered video clips.

A rule watches RFID presence changes and records a clip on one camera while a
matching tag is present, plus `pre_s` seconds before the first read (from the
camera's ring buffer) and `post_s` seconds after the last one. Arrivals during
the post-roll extend the clip; clips are capped at `max_s` and then split.

Rules are edited in the acquire GUI (Outputs… → Camera clips, saved in
config/outputs.yaml under `clips:`) or written in rig.yaml::

    triggers:
      - name: perchB_any
        camera: cam1          # any connected camera; record: triggered cameras only record clips
        antennas: [2]         # reader numbers, or "any"
        match: any            # "any", or a list of pigeon names / tag IDs
        pre_s: 3
        post_s: 5
        max_s: 300

Clips go to <session>/clips/<camera>_<rule>_<nnn>.mp4 with a frame sidecar
(same columns and session clock as frames_<cam>.csv) and are listed in
session.json["clips"], so the offline pipeline can place them on the timeline.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field

from .config import TagMap
from .core import EventBus, now
from .rfid.serial_reader import Presence
from .session import Session
from .video.recorder import VideoRecorder

CLIPS_DIR = "clips"


@dataclass
class TriggerRule:
    name: str
    camera: str
    antennas: list[int] | None = None  # None = any antenna
    match: list[str] | None = None  # None = any tag; else pigeon names and/or tag IDs
    pre_s: float = 3.0
    post_s: float = 5.0
    max_s: float = 300.0

    @classmethod
    def from_cfg(cls, d: dict) -> "TriggerRule":
        ants = d.get("antennas", "any")
        match = d.get("match", "any")
        return cls(
            name=str(d["name"]),
            camera=str(d["camera"]),
            antennas=None if ants in (None, "any") else [int(a) for a in (ants if isinstance(ants, list) else [ants])],
            match=None if match in (None, "any") else [str(m) for m in (match if isinstance(match, list) else [match])],
            pre_s=float(d.get("pre_s", 3.0)),
            post_s=float(d.get("post_s", 5.0)),
            max_s=float(d.get("max_s", 300.0)),
        )

    def to_cfg(self) -> dict:
        return {"name": self.name, "camera": self.camera,
                "antennas": "any" if self.antennas is None else list(self.antennas),
                "match": "any" if self.match is None else list(self.match),
                "pre_s": self.pre_s, "post_s": self.post_s, "max_s": self.max_s}

    def describe(self) -> str:
        ants = "any antenna" if self.antennas is None else "antenna " + ", ".join(map(str, self.antennas))
        who = "any subject" if self.match is None else ", ".join(self.match)
        return f"{who} at {ants} -> clip on {self.camera} ({self.pre_s:g} s before, {self.post_s:g} s after)"

    def matches(self, reader: int, tag: str, tags: TagMap) -> bool:
        if not tag:
            return False
        if self.antennas is not None and reader not in self.antennas:
            return False
        if self.match is None:
            return True
        return tag.upper() in {m.upper() for m in self.match} or tags.name(tag) in self.match


@dataclass
class _Clip:
    rec: VideoRecorder
    index: int  # position in session.json["clips"]
    start_abs: float
    trigger_abs: float


@dataclass
class _RuleState:
    rule: TriggerRule
    active: set = field(default_factory=set)  # {(reader, tag)} currently present and matching
    clip: _Clip | None = None
    stop_at: float | None = None
    n_clips: int = 0
    error: str | None = None


def ring_seconds(rules: list[TriggerRule], cam_id: str) -> float:
    """Ring buffer length a camera needs for its rules (0 = no buffer)."""
    pre = [r.pre_s for r in rules if r.camera == cam_id]
    return max(pre) + 0.5 if pre else 0.0


class TriggerManager(threading.Thread):
    """Runs the rules for one recording session in its own thread.

    Presence events arrive from the RFID thread through the event bus and are
    queued, so opening a clip file never delays serial reading.
    """

    def __init__(self, rules: list[TriggerRule], cams: dict, tags: TagMap, session: Session, t0_abs: float,
                 video_cfg: dict, led_bboxes: dict | None = None, bus: EventBus | None = None):
        super().__init__(daemon=True, name="triggers")
        self.states = [_RuleState(r) for r in rules]
        self.cams = cams
        self.tags = tags
        self.session = session
        self.t0 = t0_abs
        self.video_cfg = video_cfg
        self.led_bboxes = led_bboxes or {}
        self.bus = bus
        self.q: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        (session.path / CLIPS_DIR).mkdir(exist_ok=True)

    # ------------------------------------------------------------------ inputs
    def on_presence(self, p: Presence) -> None:
        self.q.put(p)

    def seed(self, presence: dict[int, str]) -> None:
        """Subjects already present when recording starts count as arrivals now."""
        t = now()
        for reader, tag in presence.items():
            if tag:
                self.q.put(Presence(reader, tag, "", t))

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                p = self.q.get(timeout=0.05)
            except queue.Empty:
                p = None
            with self._lock:
                for st in self.states:
                    try:
                        if p is not None:
                            self._apply_presence(st, p)
                        self._check_timers(st)
                    except Exception as e:  # one bad rule/clip must not stop the others
                        st.error = f"{type(e).__name__}: {e}"

    def _apply_presence(self, st: _RuleState, p: Presence) -> None:
        if p.prev_tag and (p.reader, p.prev_tag) in st.active:
            st.active.discard((p.reader, p.prev_tag))
            if not st.active and st.clip is not None:
                st.stop_at = p.host_abs + st.rule.post_s
        if p.tag and st.rule.matches(p.reader, p.tag, self.tags):
            st.active.add((p.reader, p.tag))
            st.stop_at = None
            if st.clip is None:
                self._open(st, p.host_abs, st.rule.pre_s, (p.reader, p.tag))

    def _check_timers(self, st: _RuleState) -> None:
        if st.clip is None:
            return
        t = now()
        if st.stop_at is not None and t >= st.stop_at:
            self._close(st, "post_roll_done")
            st.stop_at = None
        elif t - st.clip.start_abs >= st.rule.max_s:
            self._close(st, "max_length")
            if st.active:  # still present: continue in a new clip, no overlap
                self._open(st, t, 0.0, sorted(st.active)[0])

    # ------------------------------------------------------------------ clips
    def _open(self, st: _RuleState, trigger_abs: float, pre_s: float, reason: tuple[int, str]) -> None:
        r = st.rule
        th = self.cams.get(r.camera)
        if th is None:
            st.error = f"camera {r.camera} not connected"
            return
        be = th.backend
        n = st.n_clips + 1
        stem = f"{r.camera}_{r.name}_{n:03d}"
        vid, frames = f"{CLIPS_DIR}/{stem}.mp4", f"{CLIPS_DIR}/{stem}_frames.csv"
        try:
            rec = VideoRecorder(self.session.file(vid), self.session.file(frames), be.fps, be.size, self.t0,
                                self.video_cfg, self.led_bboxes.get(r.camera), queue_s=pre_s + 10)
        except Exception as e:
            st.error = f"{type(e).__name__}: {e}"
            return
        rec.start()
        start_abs = trigger_abs - pre_s
        n_pre = th.start_clip(rec, start_abs)
        reader, tag = reason
        info = {
            "file": vid, "frames_file": frames, "camera": r.camera, "rule": r.name,
            "trigger_t": trigger_abs - self.t0, "start_t": start_abs - self.t0, "stop_t": None,
            # pre_frames: frames taken from the ring buffer (pre-roll + the ~1 round until the clip opened)
            "antenna": reader, "tag": tag, "pigeon": self.tags.name(tag), "pre_frames": n_pre,
            "pre_s": pre_s, "post_s": r.post_s,
        }
        idx = self.session.add_clip(info)
        st.clip = _Clip(rec, idx, start_abs, trigger_abs)
        st.n_clips = n
        st.error = None
        if self.bus:
            self.bus.publish("clip", {"event": "start", **info})

    def _close(self, st: _RuleState, why: str) -> None:
        clip = st.clip
        if clip is None:
            return
        th = self.cams.get(st.rule.camera)
        if th is not None:
            th.stop_clip(clip.rec)
        stats = clip.rec.finish()
        info = {"stop_t": now() - self.t0, "end_reason": why, "frames": stats["frames"],
                "recorder_dropped": stats["recorder_dropped"], "error": stats["error"]}
        if "files" in stats:  # raw clip: lossless chunks next to the frames sidecar
            files = [{**f, "file": f"{CLIPS_DIR}/{f['file']}"} for f in stats["files"]]
            info.update(files=files, file=files[0]["file"] if files else None, record_format="raw")
        self.session.update_clip(clip.index, info)
        st.clip = None
        if self.bus:
            self.bus.publish("clip", {"event": "stop", "rule": st.rule.name, **info})

    # ------------------------------------------------------------------ control / status
    def stop(self) -> list[dict]:
        """Close open clips and end the thread. Returns the session's clip list."""
        self._stop_evt.set()
        self.join(timeout=3)
        with self._lock:
            for st in self.states:
                self._close(st, "session_stopped")
        return list(self.session.meta.get("clips", []))

    def status(self) -> list[dict]:
        t = now()
        out = []
        with self._lock:
            for st in self.states:
                d = {"rule": st.rule.name, "camera": st.rule.camera, "clips": st.n_clips,
                     "recording": st.clip is not None, "error": st.error}
                if st.clip is not None:
                    d["clip_s"] = t - st.clip.start_abs
                    d["post_roll_left_s"] = (st.stop_at - t) if st.stop_at else None
                out.append(d)
        return out
