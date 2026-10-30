"""Acquisition controller (no GUI): devices, preview, synchronized recording.

All streams are stamped with the same host monotonic clock (mavric.core.now):
serial lines on arrival in the RFID thread, frames on hand-over in each camera
thread, audio blocks on delivery in the audio callback. A recording session
stores everything relative to one anchor taken at start, plus the UTC time
of that anchor.
"""

from __future__ import annotations

import threading
from collections import deque

from .audio.config import AudioConfig
from .config import RoiSet, TagMap, resolve_path
from .outputs.config import OutputsConfig
from .outputs.engine import ActionEngine
from .core import EventBus, clock_anchor, now
from .rfid.serial_reader import LineSource, RfidReaderThread, SerialLineSource
from .session import RFID_FILE, Session
from .triggers import TriggerManager, TriggerRule, ring_seconds
from .video.backends import CameraBackend, Frame, make_backend
from .video.check import led_pulse_warning
from .video.recorder import VideoRecorder
from .video.settings import video_cfg


class CameraThread(threading.Thread):
    """Grab loop for one camera.

    Each frame goes to the continuous recorder (if any), to every open
    triggered clip, and into a ring buffer of the last `ring_s` seconds, so a
    triggered clip can start with the frames from *before* the trigger.
    """

    def __init__(self, backend: CameraBackend, bus: EventBus | None = None, ring_s: float = 0.0):
        super().__init__(daemon=True, name=f"camera-{backend.id}")
        self.backend = backend
        self.bus = bus
        self.latest: Frame | None = None
        self.n_frames = 0
        self._times: deque = deque(maxlen=60)
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._recorder: VideoRecorder | None = None
        self._clips: list[VideoRecorder] = []
        self._ring: deque[Frame] = deque()
        self.ring_s = ring_s
        self.error: str | None = None
        self.started_abs = now()
        self.last_frame_abs: float | None = None

    @property
    def no_frame_s(self) -> float:
        """Seconds since the last frame (since connecting, before the first one)."""
        return now() - (self.last_frame_abs or self.started_abs)

    @property
    def record_mode(self) -> str:
        return str(self.backend.cfg.get("record", "continuous"))

    def start_clip(self, rec: VideoRecorder, since_abs: float) -> int:
        """Feed `rec` the buffered frames newer than `since_abs`, then every new frame.

        Done under the grab lock, so no frame is lost or duplicated between the
        pre-trigger buffer and the live stream. Returns the number of buffered frames used.
        """
        with self._lock:
            pre = [f for f in self._ring if f.host_abs >= since_abs]
            for f in pre:
                rec.submit(f)
            self._clips.append(rec)
            return len(pre)

    def stop_clip(self, rec: VideoRecorder) -> None:
        with self._lock:
            if rec in self._clips:
                self._clips.remove(rec)

    @property
    def fps(self) -> float:
        if len(self._times) < 2:
            return 0.0
        dt = self._times[-1] - self._times[0]
        return (len(self._times) - 1) / dt if dt > 0 else 0.0

    def attach_recorder(self, rec: VideoRecorder) -> None:
        with self._lock:
            self._recorder = rec

    def detach_recorder(self) -> VideoRecorder | None:
        with self._lock:
            rec, self._recorder = self._recorder, None
            return rec

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                fr = self.backend.grab(1.0)
                if fr is None:
                    continue
                self.latest = fr
                self.last_frame_abs = fr.host_abs
                self.n_frames += 1
                self._times.append(fr.host_abs)
                with self._lock:
                    if self._recorder is not None:
                        self._recorder.submit(fr)
                    for clip in self._clips:
                        clip.submit(fr)
                    if self.ring_s > 0:
                        self._ring.append(fr)
                        while self._ring and fr.host_abs - self._ring[0].host_abs > self.ring_s:
                            self._ring.popleft()
                    elif self._ring:
                        self._ring.clear()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            if self.bus:
                self.bus.publish("status", {"source": self.backend.id, "error": self.error})
        finally:
            try:
                self.backend.close()
            except Exception:
                pass


class AcquisitionController:
    def __init__(self, rig: dict, tags: TagMap | None = None, rois: RoiSet | None = None, bus: EventBus | None = None):
        self.rig = rig
        self.tags = tags or TagMap.load(rig["paths"]["tags_file"])
        self.rois = rois or RoiSet.load(rig["paths"]["rois_file"])
        self.bus = bus or EventBus()
        self.rfid: RfidReaderThread | None = None
        self.cams: dict[str, CameraThread] = {}
        self.session: Session | None = None
        self._rec_t0: float | None = None
        self._recorders: dict[str, VideoRecorder] = {}
        self.rules: list[TriggerRule] = []  # camera clip rules: rig.yaml triggers + outputs.yaml clips
        self.triggers: TriggerManager | None = None
        self.outputs: ActionEngine | None = None  # event-driven outputs (start_outputs)
        self.audio = None  # AudioStream (connect_audio)
        self.player = None  # audio Player on the speakers (connect_audio, when speakers are configured)
        self._audio_rec = None
        self.world = None  # SimWorld in simulation mode
        self._refs: dict = {}  # camera -> (file stamp, drift.Reference)
        self.positions: dict[str, list] = {}  # camera -> its last few drift.Shift measurements
        try:
            clip_cfg = self.load_outputs_config()
        except (OSError, ValueError) as e:  # a broken outputs.yaml must not stop acquisition
            clip_cfg = None
            self.bus.publish("status", {"source": "outputs", "error": f"outputs.yaml: {e}"})
        self.set_clip_rules(clip_cfg)

    @property
    def roi_camera(self) -> str | None:
        """The camera the perch ROIs and the sync LED box are drawn on: the first one in rig.yaml."""
        cams = self.rig.get("cameras") or []
        return cams[0].get("id", "cam0") if cams else None

    def camera_models(self) -> tuple[dict, dict]:
        """Lens and position of every calibrated camera and the `world:` section of calibration.yaml (reloaded when
        the file changes)."""
        from .video.places import load_cameras

        path = resolve_path(self.rig["paths"].get("calibration_file", "config/calibration.yaml"))
        stamp = path.stat().st_mtime_ns if path.exists() else None
        if getattr(self, "_cal_stamp", -1) != stamp:
            self._cal_stamp = stamp
            self._cal_models = load_cameras(path) if stamp is not None else ({}, {})
        return self._cal_models

    # ------------------------------------------------------------------ camera positions (drift)
    @property
    def calibration_path(self):
        return resolve_path(self.rig["paths"].get("calibration_file", "config/calibration.yaml"))

    def reference(self, cam_id: str):
        """The camera's reference view (drift.Reference), reloaded when its file changes; None without one."""
        from .video.drift import load_reference, reference_path

        p = reference_path(self.calibration_path, cam_id)
        stamp = p.stat().st_mtime_ns if p.exists() else None
        if cam_id not in self._refs or self._refs[cam_id][0] != stamp:
            self._refs[cam_id] = (stamp, load_reference(self.calibration_path, cam_id) if stamp else None)
            self.positions.pop(cam_id, None)
        return self._refs[cam_id][1]

    def take_reference(self, cam_id: str):
        """The camera's view now becomes its reference view."""
        from .video.drift import save_reference

        th = self.cams.get(cam_id)
        if th is None or th.latest is None:
            raise RuntimeError(f"{cam_id}: no picture yet")
        return save_reference(self.calibration_path, cam_id, th.latest.image)

    def check_positions(self, cams=None, keep: int = 3) -> dict:
        """Measure the connected cameras (default all) against their reference views: {camera: the steady one of the
        last `keep` measurements}. While recording, a camera whose level changes is noted in the session."""
        from .video.drift import measure, steady

        out = {}
        for cid in cams or list(self.cams):
            th, ref = self.cams.get(cid), self.reference(cid)
            if th is None or ref is None or th.latest is None:
                continue
            hist = self.positions.setdefault(cid, [])
            prev = steady(hist)
            hist.append(measure(ref, th.latest.image))
            del hist[:-keep]
            out[cid] = s = steady(hist)
            if self.session is not None and prev is not None and s.level != prev.level and s.level != "unknown":
                row = {"t_s": round(self.elapsed(), 1), "camera": cid, **s.as_dict()}
                self.session.meta.setdefault("position_checks", []).append(row)
                self.session.save_meta()
                if s.level != "ok":
                    self.bus.publish("status", {"source": "session", "warning": f"{cid} {s.text()}"})
        return out

    def rois_in(self, cam_id: str, rois: RoiSet | None = None) -> RoiSet:
        """The ROIs (default: the current ones) as seen by a camera: as drawn in the ROI camera, the places' 3D
        outlines in the others."""
        from .video.places import rois_in_camera

        rs = rois if rois is not None else self.rois
        if cam_id == self.roi_camera:
            return rs
        return rois_in_camera(rs, cam_id, self.camera_models()[0], self.roi_camera)

    def set_clip_rules(self, cfg: OutputsConfig | None) -> None:
        """Clip rules = rig.yaml `triggers` + outputs.yaml `clips` (the GUI's, which win on equal names).

        Applies from the next recording; connected cameras get the ring buffer their rules need now.
        """
        gui = [TriggerRule.from_cfg(d) for d in (cfg.clips if cfg else [])]
        names = {r.name for r in gui}
        rig = [TriggerRule.from_cfg(d) for d in (self.rig.get("triggers") or [])]
        self.rules = [r for r in rig if r.name not in names] + gui
        for cid, th in self.cams.items():
            th.ring_s = ring_seconds(self.rules, cid)

    # ------------------------------------------------------------------ devices
    def connect_rfid(self, source: LineSource | None = None) -> None:
        self.disconnect_rfid()
        if source is None:
            r = self.rig["rfid"]
            source = SerialLineSource(r["port"], int(r["baud"]), float(r.get("reset_wait_s", 2.0)))
        source.open()  # blocks ~2 s for the MEGA reset
        self.rfid = RfidReaderThread(source, self.bus, n_readers=max(4, max(self.rig["antennas"], default=4)))
        self.rfid.start()

    def disconnect_rfid(self) -> None:
        if self.rfid is not None:
            self.rfid.stop()
            self.rfid.join(timeout=2)
            self.rfid = None

    def connect_camera(self, cam_cfg: dict | None = None) -> CameraThread:
        cfg = cam_cfg or self.rig["cameras"][0]
        self.disconnect_camera(cfg.get("id", "cam0"))
        be = make_backend(cfg, world=self.world)
        be.open()
        th = CameraThread(be, self.bus, ring_s=ring_seconds(self.rules, be.id))
        th.start()
        self.cams[be.id] = th
        return th

    def disconnect_camera(self, cam_id: str) -> None:
        th = self.cams.pop(cam_id, None)
        if th is not None:
            th.stop()
            th.join(timeout=3)

    @property
    def audio_cfg(self) -> AudioConfig:
        return AudioConfig.from_rig(self.rig)

    def connect_audio(self, cfg: AudioConfig | None = None):
        """Open the audio interface (inputs, plus the speakers when configured) and start the live stream."""
        from .audio.stream import AudioStream

        self.disconnect_audio()
        cfg = cfg or self.audio_cfg
        if not cfg.inputs:
            raise RuntimeError("No audio inputs configured (rig.yaml audio.inputs)")
        st = AudioStream(cfg, self.bus, world=self.world)
        st.open()
        self.audio = st
        if cfg.speakers:
            from .audio.library import SoundLibrary
            from .audio.player import Player

            self.player = Player(st, SoundLibrary(resolve_path(cfg.sounds_dir), cfg.sample_rate))
        return st

    def disconnect_audio(self) -> None:
        if self.audio is not None:
            if self._audio_rec is not None:
                raise RuntimeError("Stop the recording before disconnecting audio")
            self.player = None
            self.audio.close()
            self.audio = None

    def play_sound(self, sound: str, speakers="all", gain_db: float = 0.0, loop: bool = False) -> str:
        """Soundboard: play by hand. Logged in actions.csv (event "manual") and audio_playback.csv."""
        if self.player is None:
            raise RuntimeError("Connect audio first (with speakers in rig.yaml audio.speakers)")
        try:
            _ids, text = self.player.play(sound, speakers, gain_db, loop, source="soundboard")
            status = "ok"
        except (KeyError, ValueError, RuntimeError) as e:
            text, status = str(e.args[0] if e.args else e), "error"
        if self.outputs is not None:
            self.outputs.log_manual("soundboard", "play", {"sound": sound, "speakers": speakers,
                                                           "gain_db": gain_db, "loop": loop}, text, status)
        if status == "error":
            raise RuntimeError(text)
        return text

    def stop_sounds(self) -> None:
        """Fade out every sound (soundboard and rules)."""
        if self.player is not None:
            busy = self.player.stream.mixer.busy
            self.player.stop_all()
            if busy and self.outputs is not None:
                self.outputs.log_manual("soundboard", "stop", {}, "stopped all sounds")

    # ------------------------------------------------------------------ outputs
    @property
    def outputs_path(self):
        return resolve_path(self.rig["paths"].get("outputs_file", "config/outputs.yaml"))

    def load_outputs_config(self) -> OutputsConfig:
        return OutputsConfig.load(self.outputs_path)

    def start_outputs(self, cfg: OutputsConfig | None = None) -> dict[str, str]:
        """(Re)start the output engine, disarmed. Returns {device: error} for devices that failed to open."""
        self.stop_outputs()
        cfg = cfg or self.load_outputs_config()
        self.set_clip_rules(cfg)
        self.outputs = ActionEngine(cfg, self.tags, self.rig["antennas"], self.bus,
                                    resources={"player": lambda: self.player})
        errors = self.outputs.start()
        if self.session is not None:
            self.outputs.attach_session(self.session, self._rec_t0)
        return errors

    def stop_outputs(self) -> None:
        if self.outputs is not None:
            self.outputs.stop()
            self.outputs = None

    # ------------------------------------------------------------------ recording
    @property
    def recording(self) -> bool:
        return self.session is not None

    def elapsed(self) -> float:
        return now() - self._rec_t0 if self._rec_t0 is not None else 0.0

    def start_recording(self, name: str, notes: str = "") -> Session:
        if self.recording:
            raise RuntimeError("Already recording")
        if self.rfid is None and not self.cams and self.audio is None:
            raise RuntimeError("Nothing connected")
        anchor = clock_anchor()
        t0 = anchor["perf_s"]
        rig = dict(self.rig)
        if self.rfid is not None:
            rig["rfid"] = {**rig["rfid"], "port": getattr(self.rfid.source, "port", rig["rfid"]["port"])}
        sess = Session.create(rig["paths"]["sessions_dir"], name, rig, self.tags, self.rois, anchor, notes)
        if self.rfid is not None:
            self.rfid.attach_writer(sess.file(RFID_FILE), t0)
        led_bboxes = {}
        for cid, th in self.cams.items():
            be = th.backend
            on_rois = cid == self.roi_camera  # ROIs are drawn on the first camera of rig.yaml
            sync = self.rois_in(cid).sync  # other cameras: the LED's 3D box seen from there, if placed
            led_bbox = sync.bbox() if sync is not None else None
            led_bboxes[cid] = led_bbox
            info = {**be.info(), "record": th.record_mode, "led_bbox": led_bbox, "rois_camera": on_rois}
            if th.record_mode == "continuous":
                vid, frames = f"{cid}.mp4", f"frames_{cid}.csv"
                vcfg = video_cfg(self.rig)
                raw = vcfg["record_format"] == "raw"
                rec = VideoRecorder(sess.file(vid), sess.file(frames), be.fps, be.size, t0, vcfg, led_bbox,
                                    on_chunk=(lambda files, c=cid, s=sess: s.update_camera(c, video_files=files))
                                    if raw else None)
                rec.start()
                th.attach_recorder(rec)
                self._recorders[cid] = rec
                if raw:
                    info.update(video_file=None, video_files=[], frames_file=frames, record_format="raw",
                                raw_codec=vcfg["raw_codec"])
                else:
                    info.update(video_file=vid, frames_file=frames, record_format="h264")
            sess.add_camera(info)
        if self.audio is not None:
            from .audio.recorder import AudioRecorder

            arec = AudioRecorder(sess.path, self.audio.cfg, t0, stream_info=self.audio.info())
            arec.start()
            sess.add_audio(arec.info())
            if self.player is not None:  # what each playable sound is (name, file, sha1)
                lib = self.player.library
                sess.update_audio({"sounds_dir": str(lib.folder), "sounds": lib.manifest(),
                                   "playback_latency_samples": self.audio.play_offset})
            self.audio.attach(arec)
            self._audio_rec = arec
        warnings = self._positions_at_start(sess)
        w = disk_warning(sess.path, self.cams, video_cfg(self.rig))
        if w:
            warnings.append(w)
        pulse = float(self.rig.get("sync", {}).get("led_pulse_s", 0.08))
        for cid, th in self.cams.items():
            w = led_pulse_warning(th.backend.fps, pulse)
            if w:
                warnings.append(f"{cid}: {w}")
            warnings += [f"{cid}: {x}" for x in (th.backend.info().get("warnings") or [])]
        if warnings:
            sess.meta["warnings"] = warnings
            sess.save_meta()
            for w in warnings:
                self.bus.publish("status", {"source": "session", "warning": w})
        self.session = sess
        self._rec_t0 = t0
        if self.outputs is not None:
            self.outputs.attach_session(sess, t0)
        if self.rules:
            tm = TriggerManager(self.rules, self.cams, self.tags, sess, t0, self.rig["video"], led_bboxes, self.bus)
            self.bus.subscribe("presence", tm.on_presence)
            tm.seed(self.presence())
            tm.start()
            self.triggers = tm
        self.bus.publish("status", {"source": "session", "recording": True, "path": str(sess.path)})
        return sess

    def _positions_at_start(self, sess: Session) -> list[str]:
        """Each recording camera against its reference view (taken now when it has none: the places and the position
        saved last belong to this view); noted in the session, warnings for the ones that moved."""
        from .video.drift import copy_references

        warnings, taken = [], []
        for cid, th in self.cams.items():
            if th.latest is None:
                continue
            if self.reference(cid) is None:
                self.take_reference(cid)
                taken.append(cid)
                continue
            s = self.check_positions([cid]).get(cid)
            if s is not None:
                sess.update_camera(cid, position_check=s.as_dict())
                if s.level in ("shifted", "moved"):
                    warnings.append(f"{cid} {s.text()}: the places drawn on it are off by that much (acquire window: "
                                    "Alignment overlay)")
        if taken:
            copy_references(self.calibration_path, sess.path)
            sess.meta["reference_views_taken"] = taken
            sess.save_meta()
        return warnings

    def stop_recording(self) -> Session | None:
        if not self.recording:
            return None
        sess = self.session
        stats = {"cameras": {}}
        if self.triggers is not None:
            self.bus.unsubscribe("presence", self.triggers.on_presence)
            clips = self.triggers.stop()
            stats["clips"] = len(clips)
            self.triggers = None
        if self.outputs is not None:
            self.outputs.detach_session()
            stats["actions"] = sum(1 for r in self.outputs.log if r.get("status") == "ok")
        for th in self.cams.values():
            th.detach_recorder()
        for cid, rec in self._recorders.items():
            stats["cameras"][cid] = rec.finish()
            if "files" in stats["cameras"][cid]:
                sess.update_camera(cid, video_files=stats["cameras"][cid]["files"])
        self._recorders.clear()
        if self._audio_rec is not None:
            self.audio.detach()
            stats["audio"] = self._audio_rec.finish()
            sess.update_audio({"files": self._audio_rec.files, "stats": stats["audio"]})
            if self.player is not None:  # the library may have been reloaded during the session
                sess.update_audio({"sounds": self.player.library.manifest()})
            self._audio_rec = None
        if self.rfid is not None:
            stats["rfid_lines"] = self.rfid.detach_writer()
            stats["rfid_bad_lines_total"] = self.rfid.n_bad
        sess.finalize(self.elapsed(), stats)
        self.session = None
        self._rec_t0 = None
        self.bus.publish("status", {"source": "session", "recording": False, "path": str(sess.path)})
        return sess

    def shutdown(self) -> None:
        try:
            self.stop_recording()
        finally:
            self.stop_outputs()
            self.disconnect_audio()
            self.disconnect_rfid()
            for cid in list(self.cams):
                self.disconnect_camera(cid)

    # ------------------------------------------------------------------ status
    def presence(self) -> dict[int, str]:
        return dict(self.rfid.presence.state) if self.rfid else {}

    def status(self) -> dict:
        s = {"recording": self.recording, "elapsed_s": self.elapsed(),
             "session": str(self.session.path) if self.session else None}
        if self.rfid:
            age = now() - self.rfid.last_line_abs if self.rfid.last_line_abs else None
            s["rfid"] = {"port": getattr(self.rfid.source, "port", "?"), "lines": self.rfid.n_lines,
                         "bad": self.rfid.n_bad, "rate_hz": self.rfid.line_rate, "last_line_age_s": age,
                         "error": self.rfid.error}
        s["cameras"] = {}
        for cid, th in self.cams.items():
            rec = self._recorders.get(cid)
            be = th.backend
            s["cameras"][cid] = {"fps": th.fps, "frames": th.n_frames, "error": th.error,
                                 "no_frame_s": th.no_frame_s, "incomplete": be.n_failed,
                                 "timeouts": be.n_timeouts, "problem": be.last_problem,
                                 "recorded": rec.writer.n if rec else None,
                                 "rec_dropped": rec.n_dropped if rec else None,
                                 "queue": rec.q.qsize() if rec else None, "record": th.record_mode}
        s["triggers"] = self.triggers.status() if self.triggers else []
        s["outputs"] = self.outputs.status() if self.outputs else None
        if self.audio is not None:
            a = self.audio.status()
            if self._audio_rec is not None:
                a["recorded_s"] = self._audio_rec.n / self._audio_rec.sr
                a["rec_queue"] = self._audio_rec.q.qsize()
                a["rec_error"] = self._audio_rec.error
            s["audio"] = a
        return s


def stall_text(cam_status: dict, after_s: float = 2.0) -> str | None:
    """'NO FRAMES for N s (why)' for a connected camera that has stopped delivering, else None."""
    c = cam_status
    if c.get("no_frame_s", 0.0) < after_s:
        return None
    why = f"{c.get('incomplete', 0)} incomplete, {c.get('timeouts', 0)} timeouts"
    if c.get("problem"):
        why += f"; last: {c['problem']}"
    return f"NO FRAMES for {c['no_frame_s']:.0f} s ({why})"


def raw_mb_per_s(cams: dict, vcfg: dict) -> float:
    """Expected raw video data rate of the continuously recording cameras.

    FFV1 measured 2.8:1 on the Chameleon3 (1280x1024 Mono8, gain 0); 0.4 keeps the estimate a bit
    on the safe side for noisier (higher gain) images.
    """
    rate = 0.0
    for th in cams.values():
        be = th.backend
        if th.record_mode != "continuous" or not be.size:
            continue
        if th.latest is not None:
            frame_bytes = th.latest.image.nbytes
        else:
            mono = str(be.cfg.get("pixel_format", "Mono8")).upper().startswith("MONO")
            frame_bytes = be.size[0] * be.size[1] * (1 if mono else 3)
        rate += frame_bytes * be.fps / 1e6
    return rate * (0.4 if vcfg.get("raw_codec") == "ffv1" else 1.0)


def disk_warning(folder, cams: dict, vcfg: dict, min_hours: float = 2.0) -> str | None:
    """Warn when raw recording would fill the disk within `min_hours`."""
    import shutil

    if vcfg.get("record_format") != "raw":
        return None
    rate = raw_mb_per_s(cams, vcfg)
    if rate <= 0:
        return None
    free_mb = shutil.disk_usage(folder).free / 1e6
    hours = free_mb / rate / 3600
    if hours < min_hours:
        return (f"raw video needs ~{rate * 3.6:.0f} GB/h; the disk has room for {hours:.1f} h. "
                f"Free space, use H.264 recording, or a smaller image / frame rate (Video settings)")
    return None


def simulation_controller(rig: dict, duration_s: float = 3600, seed: int = 0) -> AcquisitionController:
    """Controller wired to a simulated cage (synthetic cameras + simulated Arduino).

    With two synthetic cameras or more, the cage is 3D and each camera sees it from its own place (SimScene3D); its
    ROIs and camera calibration are kept apart from the one-camera simulation's (sessions/_sim3d_*)."""
    from .sim import RfidModel, SimulatedArduino, SimWorld

    tags = TagMap.load(rig["paths"]["tags_file"])
    world = SimWorld.random(tags.pigeons, {p: tags.color(p) for p in tags.pigeons}, duration_s, seed,
                            mean_dwell_s=12, mean_away_s=8)
    rig = dict(rig)
    if not rig["cameras"] or any(c.get("backend") != "synthetic" for c in rig["cameras"]):
        rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30}]
    rig["rfid"] = {**rig["rfid"], "port": "SIM"}
    rig["audio"] = {**rig["audio"], "host_api": "sim", "device": "simulated"}
    sim_dir = resolve_path(rig["paths"]["sessions_dir"])
    if len(rig["cameras"]) >= 2:  # several cameras see one cage in 3D, each from its own place
        from .video.calibration import BoardSpec
        from .video.extrinsics import SimulatedRig

        sim_rig = SimulatedRig(BoardSpec.from_rig(rig), {c["id"]: tuple(world.image_size) for c in rig["cameras"]},
                               near=1.0)
        world.attach_rig(sim_rig, rig["cameras"][0]["id"])
        rois_file, cal_file = sim_dir / "_sim3d_rois.json", sim_dir / "_sim3d_calibration.yaml"
        rois = RoiSet.load(rois_file)
    else:
        rois_file, cal_file = sim_dir / "_sim_rois.json", sim_dir / "_sim_calibration.yaml"
        rois = RoiSet.load(rig["paths"]["rois_file"])
    if not rois.rois or rois.image_size != tuple(world.image_size):
        rois = world.roi_set(rig["antennas"])
    rig["paths"] = {**rig["paths"], "rois_file": str(rois_file), "calibration_file": str(cal_file)}
    ctl = AcquisitionController(rig, tags, rois)
    ctl.world = world
    ctl._sim_source = SimulatedArduino(world, tags, RfidModel(boot_offset_s=0.0))
    return ctl
