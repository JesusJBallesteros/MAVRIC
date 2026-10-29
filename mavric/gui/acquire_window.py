"""Acquisition GUI: live cameras, place ROIs tied to antennas, live RFID and audio, soundboard, synchronized
recording."""

from __future__ import annotations

import sys
import time

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from ..acquire import AcquisitionController, simulation_controller, stall_text
from ..config import Roi, project_root, resolve_path
from ..core import now
from ..rfid.serial_reader import Presence, list_ports
from .camera_grid import CameraGrid
from .common import GRAY, RED, VideoView, Worker, message, swatch

GRID = "__all__"  # view_combo data for the all-cameras grid


class AcquireWindow(QtWidgets.QMainWindow):
    presence_sig = QtCore.Signal(object)
    action_sig = QtCore.Signal(object)

    def __init__(self, ctl: AcquisitionController, sim: bool = False):
        super().__init__()
        self.ctl = ctl
        self.sim = sim
        self.rig = ctl.rig
        self.antennas: dict[int, str] = dict(self.rig["antennas"])
        self.setWindowTitle("MAVRIC - Acquire" + ("  [SIMULATION]" if sim else ""))
        self.resize(1400, 860)
        self._last_grab = -1
        self._since: dict[int, float] = {}
        self._ring_count = 0
        self._roi_size = None  # image size of the ROI camera, for saving ROIs while another camera is shown
        self._last_roi_grab = -1
        self._view_id = None
        self.meters = None
        self.spec = None
        self._workers: list[Worker] = []

        self.video = VideoView()
        self.video.load_rois(ctl.rois, editable=False)
        self.setCentralWidget(self._build_layout())
        self.presence_sig.connect(self._on_presence)
        ctl.bus.subscribe("presence", lambda p: self.presence_sig.emit(p))
        self.action_sig.connect(self._on_action)
        ctl.bus.subscribe("action", lambda row: self.action_sig.emit(row))
        self._start_outputs()

        from .tips import ACQUIRE, ACQUIRE_ATTR, apply_tips

        apply_tips(self, ACQUIRE, ACQUIRE_ATTR, self)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self.statusBar().showMessage("Connect the RFID reader and the camera to start the preview.")

    # ------------------------------------------------------------------ layout
    def _build_layout(self) -> QtWidgets.QWidget:
        side = QtWidgets.QWidget()
        side.setFixedWidth(430)
        v = QtWidgets.QVBoxLayout(side)

        # devices
        g = QtWidgets.QGroupBox("Devices")
        f = QtWidgets.QGridLayout(g)
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self._refresh_ports()
        b_refresh = QtWidgets.QPushButton("↻")
        b_refresh.setFixedWidth(30)
        b_refresh.clicked.connect(self._refresh_ports)
        self.b_rfid = QtWidgets.QPushButton("Connect RFID")
        self.b_rfid.clicked.connect(self._toggle_rfid)
        f.addWidget(QtWidgets.QLabel("RFID port"), 0, 0)
        f.addWidget(self.port_combo, 0, 1)
        f.addWidget(b_refresh, 0, 2)
        f.addWidget(self.b_rfid, 0, 3)
        self.cam_combo = QtWidgets.QComboBox()
        self.reload_camera_list()
        self.b_cam = QtWidgets.QPushButton("Connect camera")
        self.b_cam.clicked.connect(self._toggle_camera)
        self.cam_combo.currentIndexChanged.connect(
            lambda _: self.b_cam.setText("Disconnect camera" if self.cam_combo.currentData()["id"] in self.ctl.cams
                                         else "Connect camera"))
        f.addWidget(QtWidgets.QLabel("Camera"), 1, 0)
        f.addWidget(self.cam_combo, 1, 1, 1, 2)
        f.addWidget(self.b_cam, 1, 3)
        self.view_combo = QtWidgets.QComboBox()
        self.view_combo.setToolTip("Connected camera shown in the live view. ROIs are drawn on "
                                   f"{self.ctl.roi_camera} (the first camera in rig.yaml).")
        self.view_combo.currentIndexChanged.connect(self._view_changed)
        f.addWidget(QtWidgets.QLabel("View"), 3, 0)
        f.addWidget(self.view_combo, 3, 1, 1, 2)
        b_vs = QtWidgets.QPushButton("Video settings…")
        b_vs.clicked.connect(self._open_video_settings)
        f.addWidget(b_vs, 3, 3)
        acfg = self.ctl.audio_cfg
        self.b_audio = QtWidgets.QPushButton("Connect audio")
        self.b_audio.clicked.connect(self._toggle_audio)
        f.addWidget(QtWidgets.QLabel("Audio"), 2, 0)
        f.addWidget(QtWidgets.QLabel(acfg.describe() if acfg.inputs else "not configured"), 2, 1, 1, 2)
        f.addWidget(self.b_audio, 2, 3)
        self.b_audio.setEnabled(bool(acfg.inputs))
        v.addWidget(g)

        # audio levels (filled when audio connects)
        self.audio_box = QtWidgets.QGroupBox("Audio inputs")
        self.audio_layout = QtWidgets.QVBoxLayout(self.audio_box)
        self.audio_label = QtWidgets.QLabel("")
        self.audio_label.setWordWrap(True)
        self.audio_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.audio_layout.addWidget(self.audio_label)
        self.audio_box.setVisible(False)
        v.addWidget(self.audio_box)

        # soundboard (shown when audio is connected with speakers)
        self.sound_box = QtWidgets.QGroupBox("Soundboard")
        f = QtWidgets.QGridLayout(self.sound_box)
        self.sb_sound = QtWidgets.QComboBox()
        b_reload = QtWidgets.QPushButton("↻ sounds")
        b_reload.clicked.connect(self._reload_sounds)
        self.sb_speaker = QtWidgets.QComboBox()
        self.sb_gain = QtWidgets.QDoubleSpinBox()
        self.sb_gain.setRange(-60.0, 20.0)
        self.sb_gain.setSingleStep(1.0)
        self.sb_gain.setSuffix(" dB")
        self.sb_loop = QtWidgets.QCheckBox("loop")
        b_play = QtWidgets.QPushButton("▶ Play sound")
        b_play.clicked.connect(self._play_sound)
        b_stop = QtWidgets.QPushButton("■ Stop all sounds")
        b_stop.clicked.connect(self._stop_sounds)
        self.sb_label = QtWidgets.QLabel("")
        self.sb_label.setWordWrap(True)
        f.addWidget(self.sb_sound, 0, 0, 1, 3)
        f.addWidget(b_reload, 0, 3)
        f.addWidget(self.sb_speaker, 1, 0)
        f.addWidget(self.sb_gain, 1, 1)
        f.addWidget(self.sb_loop, 1, 2)
        f.addWidget(b_play, 2, 0, 1, 2)
        f.addWidget(b_stop, 2, 2, 1, 2)
        f.addWidget(self.sb_label, 3, 0, 1, 4)
        self.sound_box.setVisible(False)
        v.addWidget(self.sound_box)

        # ROIs
        g = QtWidgets.QGroupBox("Place ROIs (one per antenna) and sync LED")
        f = QtWidgets.QGridLayout(g)
        for i, (ant, name) in enumerate(sorted(self.antennas.items())):
            f.addWidget(QtWidgets.QLabel(f"Antenna {ant}: {name}"), i, 0)
            b = QtWidgets.QPushButton("Draw / reset")
            b.clicked.connect(lambda _=False, a=ant: self._new_roi(a))
            f.addWidget(b, i, 1)
        n = len(self.antennas)
        b = QtWidgets.QPushButton("Draw / reset sync LED box")
        b.clicked.connect(lambda: self._new_roi(None))
        f.addWidget(b, n, 0, 1, 2)
        self.cb_edit = QtWidgets.QCheckBox("Edit ROIs (drag corners; click an edge to add a corner)")
        self.cb_edit.toggled.connect(self.video.set_editable)
        f.addWidget(self.cb_edit, n + 1, 0, 1, 2)
        b_save = QtWidgets.QPushButton("Save ROIs to rig")
        b_save.clicked.connect(self._save_rois)
        f.addWidget(b_save, n + 2, 0, 1, 2)
        self.led_label = QtWidgets.QLabel("Sync LED box: -")
        f.addWidget(self.led_label, n + 3, 0, 1, 2)
        v.addWidget(g)

        # live RFID
        g = QtWidgets.QGroupBox("Live RFID")
        f = QtWidgets.QVBoxLayout(g)
        self.table = QtWidgets.QTableWidget(len(self.antennas), 4)
        self.table.setHorizontalHeaderLabels(["Place", "Subject", "Tag", "For (s)"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, (ant, name) in enumerate(sorted(self.antennas.items())):
            self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(f"{ant} {name}"))
            for c in (1, 2, 3):
                self.table.setItem(i, c, QtWidgets.QTableWidgetItem(""))
        vh = self.table.verticalHeader()
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height()
                                  + vh.defaultSectionSize() * len(self.antennas) + 6)
        f.addWidget(self.table)
        self.events = QtWidgets.QListWidget()
        self.events.setFixedHeight(110)
        f.addWidget(self.events)
        v.addWidget(g)

        # session
        g = QtWidgets.QGroupBox("Recording")
        f = QtWidgets.QGridLayout(g)
        self.name_edit = QtWidgets.QLineEdit(time.strftime("session"))
        self.notes_edit = QtWidgets.QLineEdit()
        self.notes_edit.setPlaceholderText("notes (subjects, conditions, ...)")
        self.b_rec = QtWidgets.QPushButton("● Start recording")
        self.b_rec.setMinimumHeight(40)
        self.b_rec.clicked.connect(self._toggle_record)
        self.rec_label = QtWidgets.QLabel("not recording")
        self.rec_label.setWordWrap(True)
        f.addWidget(QtWidgets.QLabel("Name"), 0, 0)
        f.addWidget(self.name_edit, 0, 1)
        f.addWidget(QtWidgets.QLabel("Notes"), 1, 0)
        f.addWidget(self.notes_edit, 1, 1)
        f.addWidget(self.b_rec, 2, 0, 1, 2)
        f.addWidget(self.rec_label, 3, 0, 1, 2)
        v.addWidget(g)

        # event-driven outputs
        g = QtWidgets.QGroupBox("Outputs and camera clips (TTL, Intan, MATLAB, sounds, messages, clips)")
        f = QtWidgets.QGridLayout(g)
        self.b_outputs = QtWidgets.QPushButton("Outputs…")
        self.b_outputs.clicked.connect(self._open_outputs)
        self.b_arm = QtWidgets.QPushButton("Arm outputs")
        self.b_arm.setCheckable(True)
        self.b_arm.toggled.connect(self._toggle_arm)
        self.out_label = QtWidgets.QLabel("starting…")
        self.out_label.setWordWrap(True)
        self.out_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.b_outputs, 0, 0)
        f.addWidget(self.b_arm, 0, 1)
        f.addWidget(self.out_label, 1, 0, 1, 2)
        v.addWidget(g)

        self.stat_label = QtWidgets.QLabel("")
        self.stat_label.setWordWrap(True)
        v.addWidget(self.stat_label)
        v.addStretch(1)
        from .help_dialogs import help_about_row

        v.addLayout(help_about_row(self, "acquire"))

        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        self.left = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.grid = CameraGrid()
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.video)
        self.stack.addWidget(self.grid)
        self.left.addWidget(self.stack)
        h.addWidget(self.left, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(side.width() + 20)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        h.addWidget(scroll)
        return w

    # ------------------------------------------------------------------ devices
    def _refresh_ports(self) -> None:
        cur = self.port_combo.currentText() or str(self.rig["rfid"]["port"])
        self.port_combo.clear()
        if self.sim:
            self.port_combo.addItem("SIM")
        for p in list_ports():
            self.port_combo.addItem(p["device"])
        self.port_combo.addItem("auto")
        i = self.port_combo.findText(cur)
        self.port_combo.setCurrentIndex(max(0, i))
        if i < 0:
            self.port_combo.setEditText(cur)

    def _run(self, fn, done, label: str) -> None:
        w = Worker(fn)
        w.done.connect(done)
        w.failed.connect(lambda e: (self.statusBar().clearMessage(), message(self, label, e, error=True), done(None)))
        self._workers.append(w)
        self.statusBar().showMessage(f"{label} ...")
        w.start()

    def _toggle_rfid(self) -> None:
        if self.ctl.rfid is not None:
            if self.ctl.recording:
                return
            self.ctl.disconnect_rfid()
            self.b_rfid.setText("Connect RFID")
            return
        port = self.port_combo.currentText()
        self.b_rfid.setEnabled(False)
        if port == "SIM" and hasattr(self.ctl, "_sim_source"):
            src = self.ctl._sim_source
        else:
            from ..rfid.serial_reader import SerialLineSource

            r = self.rig["rfid"]
            src = SerialLineSource(port, int(r["baud"]), float(r.get("reset_wait_s", 2.0)))

        def done(_):
            self.b_rfid.setEnabled(True)
            if self.ctl.rfid is not None:
                self.b_rfid.setText("Disconnect RFID")
                self.statusBar().showMessage(f"RFID connected on {port}", 4000)

        self._run(lambda: self.ctl.connect_rfid(src), done, f"Connecting RFID on {port}")

    def _toggle_camera(self) -> None:
        cfg = self.cam_combo.currentData()
        if cfg["id"] in self.ctl.cams:
            if self.ctl.recording:
                return
            self.ctl.disconnect_camera(cfg["id"])
            self.b_cam.setText("Connect camera")
            self._refresh_views()
            return
        self.b_cam.setEnabled(False)

        def done(_):
            self.b_cam.setEnabled(True)
            if cfg["id"] in self.ctl.cams:
                self.b_cam.setText("Disconnect camera")
                self._refresh_views(select=cfg["id"])
                info = self.ctl.cams[cfg["id"]].backend.info()
                warn = info.get("warnings") or []
                self.statusBar().showMessage(
                    f"Camera {cfg['id']}: {info.get('width')}x{info.get('height')} @ {info.get('fps'):.1f} fps"
                    + (f"  ({'; '.join(warn[:2])})" if warn else ""), 10000)

        self._run(lambda: self.ctl.connect_camera(cfg), done, f"Connecting camera {cfg['id']}")

    def reload_camera_list(self) -> None:
        """Camera choices from the (possibly just edited) rig."""
        cur = self.cam_combo.currentData()["id"] if self.cam_combo.count() else None
        self.cam_combo.blockSignals(True)
        self.cam_combo.clear()
        for c in self.rig["cameras"]:
            dev = f" {c['device']}" if c.get("device") else ""
            mode = "clips only" if c.get("record") == "triggered" else "continuous"
            roi = ", ROIs" if c.get("id") == self.ctl.roi_camera else ""
            self.cam_combo.addItem(f"{c['id']} ({c['backend']}{dev}; {mode}{roi})", c)
        i = next((k for k, c in enumerate(self.rig["cameras"]) if c["id"] == cur), 0)
        self.cam_combo.setCurrentIndex(i)
        self.cam_combo.blockSignals(False)

    def reconnect_camera(self, cfg: dict, on_done=None) -> None:
        """Close and reopen a connected camera with new settings."""
        def work():
            self.ctl.disconnect_camera(cfg["id"])
            return self.ctl.connect_camera(cfg)

        def done(_):
            self._refresh_views(select=cfg["id"])
            if cfg["id"] in self.ctl.cams:
                i = self.ctl.cams[cfg["id"]].backend.info()
                warn = i.get("warnings") or []
                self.statusBar().showMessage(
                    f"{cfg['id']} reconnected: {i.get('width')}x{i.get('height')} at {i.get('fps', 0):.1f} fps"
                    + (f"  ({'; '.join(warn[:2])})" if warn else ""), 8000)
            if on_done is not None:
                on_done()

        self._run(work, done, f"Reconnecting {cfg['id']}")

    def _open_video_settings(self) -> None:
        from .video_dialogs import VideoSettingsDialog

        VideoSettingsDialog(self).exec()

    def _refresh_views(self, select: str | None = None) -> None:
        cur = select or self._view_id
        self.view_combo.blockSignals(True)
        self.view_combo.clear()
        for cid in self.ctl.cams:
            self.view_combo.addItem(cid + ("  (ROIs)" if cid == self.ctl.roi_camera else ""), cid)
        if len(self.ctl.cams) >= 2:
            self.view_combo.addItem("All cameras (monitor, no ROIs)", GRID)
        i = self.view_combo.findData(cur)
        if i < 0:
            i = self.view_combo.findData(self.ctl.roi_camera)
        self.view_combo.setCurrentIndex(max(i, 0))
        self.view_combo.blockSignals(False)
        self._view_changed()

    def _view_changed(self, *_) -> None:
        self._view_id = self.view_combo.currentData()
        self._last_grab = -1
        grid = self._view_id == GRID
        self.stack.setCurrentWidget(self.grid if grid else self.video)
        if grid:
            self.grid.set_cameras(list(self.ctl.cams))
        on_rois = self._view_id in (None, self.ctl.roi_camera)
        self.video.set_rois_visible(on_rois)
        if not on_rois:
            self.cb_edit.setChecked(False)
        self.cb_edit.setEnabled(on_rois)

    def _on_roi_camera(self) -> bool:
        if self._view_id not in (None, self.ctl.roi_camera):
            message(self, "ROIs", f"ROIs are drawn on {self.ctl.roi_camera}. Switch the view to it first.")
            return False
        return True

    def _toggle_audio(self) -> None:
        if self.ctl.audio is not None:
            if self.ctl.recording:
                return
            self.ctl.disconnect_audio()
            self.b_audio.setText("Connect audio")
            self._remove_audio_views()
            return
        self.b_audio.setEnabled(False)

        def done(_):
            self.b_audio.setEnabled(True)
            if self.ctl.audio is not None:
                self.b_audio.setText("Disconnect audio")
                self._build_audio_views()
                i = self.ctl.audio.info()
                lat = i.get("latency_s") or [None]
                self.statusBar().showMessage(
                    f"Audio: {i.get('device')} [{i.get('host_api')}] {i.get('sample_rate') or 0:.0f} Hz"
                    + (f", input latency {lat[0] * 1e3:.0f} ms" if lat[0] else ""), 6000)

        self._run(self.ctl.connect_audio, done, f"Connecting audio {self.ctl.audio_cfg.describe()}")

    def _build_audio_views(self) -> None:
        from .audio_widgets import LevelMeters, SpectrogramView

        st = self.ctl.audio
        cfg = st.cfg
        self._remove_audio_views()
        self.meters = LevelMeters(cfg.names, cfg.in_channels)
        self.audio_layout.insertWidget(0, self.meters)
        self.audio_box.setVisible(True)
        mics = [cfg.names[j] for j in cfg.mic_indices]
        if mics:
            self.spec = SpectrogramView(mics, cfg.sample_rate, cfg.display_fmax_hz)
            self.left.addWidget(self.spec)
            self.left.setSizes([3 * self.left.height() // 4, self.left.height() // 4])
        self._ring_count = st.ring.count
        if self.ctl.player is not None:
            self.sb_speaker.clear()
            self.sb_speaker.addItems(["all"] + self.ctl.player.speakers)
            self._fill_sounds()
            self.sound_box.setVisible(True)

    def _remove_audio_views(self) -> None:
        if self.meters is not None:
            self.meters.setParent(None)
            self.meters = None
        if self.spec is not None:
            self.spec.setParent(None)
            self.spec = None
        self.audio_box.setVisible(False)
        self.sound_box.setVisible(False)

    # ------------------------------------------------------------------ soundboard
    def _fill_sounds(self) -> None:
        lib = self.ctl.player.library
        keep = self.sb_sound.currentText()
        self.sb_sound.clear()
        for name in lib.names:
            s = lib.sounds[name]
            self.sb_sound.addItem(f"{name}  ({s.duration_s * 1e3:.0f} ms, peak {s.peak_dbfs:.0f} dBFS)", name)
        i = self.sb_sound.findData(keep.split("  (")[0]) if keep else -1
        self.sb_sound.setCurrentIndex(max(i, 0))
        n = len(lib.sounds) - 1
        try:
            shown = lib.folder.resolve().relative_to(project_root()).as_posix() + "/"
        except ValueError:
            shown = str(lib.folder)
        self.sb_label.setText(f"{n} sound(s) in {shown} + test_tone" + (
            "" if lib.folder.is_dir() else " (folder missing: create it and add WAV/FLAC files)")
                              + "".join(f"<br><small>{x}</small>" for x in lib.notes[:3]))

    def _reload_sounds(self) -> None:
        if self.ctl.player is not None:
            self.ctl.player.library.reload()
            self._fill_sounds()

    def _play_sound(self) -> None:
        name = self.sb_sound.currentData()
        if not name:
            return
        try:
            text = self.ctl.play_sound(name, self.sb_speaker.currentText(), self.sb_gain.value(),
                                       self.sb_loop.isChecked())
        except RuntimeError as e:
            message(self, "Play", str(e), error=True)
            return
        self.statusBar().showMessage(f"Playing {text}", 5000)

    def _stop_sounds(self) -> None:
        self.ctl.stop_sounds()
        self.statusBar().showMessage("Stopped all sounds", 3000)

    def _update_audio(self) -> None:
        st = self.ctl.audio
        if st is None or self.meters is None:
            return
        peak, _rms = st.take_levels()
        self.meters.update_levels(peak, st.clipped.tolist())
        if self.spec is not None:
            x, self._ring_count = st.ring.since(self._ring_count)
            self.spec.feed(x[:, st.cfg.mic_indices])
            self.spec.refresh()
        a = st.status()
        parts = []
        if st.cfg.sync_index is not None:
            age = a["last_sync_age_s"]
            txt = (f"sync pulses on {st.cfg.sync_channel}: {a['sync_edges'] // 2}"
                   + (f", last {age:.1f} s ago" if age is not None else ""))
            if (age if age is not None else a["seconds"]) < 8:
                parts.append(txt if age is not None else "waiting for sync pulses…")
            else:
                parts.append(f"<span style='color:{RED}'>⚠ {txt} (RFID Arduino running? sync cable?)</span>")
        if a["flags"]:
            parts.append(f"<span style='color:{RED}'>⚠ driver: {a['flags']}</span>")
        if a["error"]:
            parts.append(f"<span style='color:{RED}'>⚠ {a['error']}</span>")
        if a["playing"]:
            spk = st.cfg.speakers
            parts.append("playing: " + ", ".join(f"{p['name']} on {spk.get(p['channel'] + 1, p['channel'] + 1)}"
                                                 + (" (loop)" if p["loop"] else "") for p in a["playing"][:4]))
        if any(a["limited"]):
            parts.append(f"<span style='color:{RED}'>⚠ playback limiter hit (audio.max_level_dbfs): lower the "
                         f"gain</span>")
        self.audio_label.setText("<br>".join(parts))

    # ------------------------------------------------------------------ ROIs
    def _new_roi(self, antenna: int | None) -> None:
        if not self._on_roi_camera():
            return
        size = self.video.image_size() or (640, 480)
        w, h = size
        if antenna is None:
            s = max(10, w // 40)
            pts = [(w - 3 * s, s), (w - 2 * s, s), (w - 2 * s, 2 * s), (w - 3 * s, 2 * s)]
            roi = Roi(self.rig["sync"]["led_roi_name"], pts, None, "sync")
        else:
            k = sorted(self.antennas).index(antenna)
            cx, cy = w * (0.25 + 0.5 * (k % 2)), h * (0.25 + 0.5 * (k // 2 % 2))
            dx, dy = w * 0.12, h * 0.08
            pts = [(cx - dx, cy - dy), (cx + dx, cy - dy), (cx + dx, cy + dy), (cx - dx, cy + dy)]
            roi = Roi(self.antennas[antenna], pts, antenna, "perch")
        self.cb_edit.setChecked(True)
        self.video.add_roi(roi)
        self.video.set_editable(True)

    def _save_rois(self) -> None:
        if not self._on_roi_camera():
            return
        rs = self.video.roiset(self._roi_size)
        self.ctl.rois = rs
        path = resolve_path(self.rig["paths"]["rois_file"])
        rs.save(path)
        self.statusBar().showMessage(f"Saved {len(rs.rois)} ROIs to {path}", 5000)

    # ------------------------------------------------------------------ recording
    def _toggle_record(self) -> None:
        if self.ctl.recording:
            sess = self.ctl.stop_recording()
            self.b_rec.setText("● Start recording")
            raw = any(c.get("record_format") == "raw" for c in sess.meta.get("cameras", []))
            self.rec_label.setText(f"saved: {sess.path}" + ("<br><small>raw video kept; make the shareable / "
                                                            "DeepLabCut copy with Video settings → Compress a "
                                                            "session…, or <code>mavric compress &lt;session&gt;</code>"
                                                            "</small>" if raw else ""))
            self._set_devices_enabled(True)
            QtWidgets.QApplication.clipboard().setText(str(sess.path))
            self.statusBar().showMessage("Recording stopped (path copied to clipboard)", 6000)
            return
        if self.ctl.rfid is None and not self.ctl.cams and self.ctl.audio is None:
            message(self, "Nothing to record", "Connect the RFID reader, a camera or the audio interface first.")
            return
        rs = self.video.roiset(self._roi_size)
        if len(rs.perches) < len(self.antennas):
            r = QtWidgets.QMessageBox.question(
                self, "ROIs incomplete",
                f"Only {len(rs.perches)} of {len(self.antennas)} place ROIs are drawn. Record anyway?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        if rs.sync is None:
            self.statusBar().showMessage("No sync LED box drawn: video/RFID offset cannot be validated", 8000)
        self.ctl.rois = rs
        self.cb_edit.setChecked(False)
        try:
            sess = self.ctl.start_recording(self.name_edit.text() or "session", self.notes_edit.text())
        except Exception as e:
            message(self, "Cannot start recording", str(e), error=True)
            return
        self.b_rec.setText("■ Stop recording")
        self.rec_label.setText(f"recording to {sess.path}")
        self._set_devices_enabled(False)

    def _set_devices_enabled(self, on: bool) -> None:
        for b in (self.b_rfid, self.b_cam, self.b_audio):
            b.setEnabled(on)
        self.cam_combo.setEnabled(on)

    # ------------------------------------------------------------------ live updates
    def _on_presence(self, p: Presence) -> None:
        name = self.antennas.get(p.reader, str(p.reader))
        pig = self.ctl.tags.name(p.tag) if p.tag else ""
        self._since[p.reader] = p.host_abs
        txt = f"{time.strftime('%H:%M:%S')}  {name}: " + (f"{pig} arrived" if p.tag else
                                                          f"{self.ctl.tags.name(p.prev_tag)} left")
        self.events.insertItem(0, txt)
        if self.events.count() > 200:
            self.events.takeItem(200)

    def _tick(self) -> None:
        st = self.ctl.status()
        roi_th = self.ctl.cams.get(self.ctl.roi_camera)  # sync LED readout, whatever is shown
        if roi_th is not None and roi_th.latest is not None and roi_th.latest.grab_idx != self._last_roi_grab:
            fr = roi_th.latest
            self._last_roi_grab = fr.grab_idx
            h, w = fr.image.shape[:2]
            self._roi_size = (w, h)
            self._update_led(fr.image)
        if self._view_id == GRID:
            self._update_grid(st)
        else:
            th = self.ctl.cams.get(self._view_id) if self._view_id else None
            if th is not None and th.latest is not None and th.latest.grab_idx != self._last_grab:
                self._last_grab = th.latest.grab_idx
                self.video.set_image(th.latest.image)

        pres = self.ctl.presence()
        t = now()
        for i, ant in enumerate(sorted(self.antennas)):
            tag = pres.get(ant, "")
            pig = self.ctl.tags.name(tag) if tag else ""
            name = self.antennas[ant]
            if tag:
                self.video.set_roi_state(name, self.ctl.tags.color(pig), 4, f"{name}: {pig}")
            else:
                self.video.set_roi_state(name, GRAY, 2, name)
            self.table.item(i, 1).setText(pig)
            self.table.item(i, 1).setIcon(swatch(self.ctl.tags.color(pig)) if tag else QtGui.QIcon())
            self.table.item(i, 2).setText(tag)
            since = self._since.get(ant)
            self.table.item(i, 3).setText(f"{t - since:.1f}" if (tag and since) else "")

        self._update_audio()
        parts = []
        if "rfid" in st:
            r = st["rfid"]
            age = r["last_line_age_s"]
            parts.append(f"RFID {r['port']}: {r['rate_hz']:.1f} lines/s, {r['lines']} lines, {r['bad']} bad"
                         + (f", last line {age:.1f} s ago" if age is not None and age > 0.5 else "")
                         + (f"  ERROR {r['error']}" if r["error"] else ""))
        for cid, c in st["cameras"].items():
            s = f"{cid}: {c['fps']:.1f} fps" + (" (triggered clips only)" if c.get("record") == "triggered" else "")
            if c["recorded"] is not None:
                s += f", recorded {c['recorded']}, dropped {c['rec_dropped']}, queue {c['queue']}"
            if c["error"]:
                s += f"  ERROR {c['error']}"
            elif stall := stall_text(c):
                s += f"  {stall}"
            parts.append(s)
        au = st.get("audio")
        if au is not None and "recorded_s" in au:
            parts.append(f"audio: recorded {au['recorded_s']:.1f} s, queue {au['rec_queue']}"
                         + (f"  ERROR {au['rec_error']}" if au["rec_error"] else ""))
        for tr in st.get("triggers", []):
            s = f"trigger {tr['rule']} → {tr['camera']}: {tr['clips']} clips"
            if tr["recording"]:
                s += f", <b>recording</b> {tr['clip_s']:.1f} s"
                if tr.get("post_roll_left_s") is not None:
                    s += f" (post-roll {tr['post_roll_left_s']:.1f} s)"
            if tr["error"]:
                s += f"  ERROR {tr['error']}"
            parts.append(s)
        if not st["recording"] and self.ctl.rules:
            parts.append(f"{len(self.ctl.rules)} trigger rule(s) armed when recording starts: "
                         + ", ".join(f"{r.name}→{r.camera}" for r in self.ctl.rules))
        if st["recording"]:
            el = st["elapsed_s"]
            parts.insert(0, f"<b>REC {int(el // 3600):02d}:{int(el % 3600 // 60):02d}:{int(el % 60):02d}</b>")
        self.stat_label.setText("<br>".join(parts))

    def _update_grid(self, st: dict) -> None:
        clips = {}
        for tr in st.get("triggers", []):
            if tr["recording"]:
                clips[tr["camera"]] = max(clips.get(tr["camera"], 0.0), tr.get("clip_s") or 0.0)
        for cid, th in self.ctl.cams.items():
            c = st["cameras"].get(cid, {})
            parts = [cid, f"{c.get('fps', 0):.1f} fps"]
            if c.get("record") == "triggered":
                parts.append("clips only")
            elif c.get("recorded") is not None:
                drop = f", {c['rec_dropped']} dropped" if c["rec_dropped"] else ""
                parts.append(f"● REC {c['recorded']} fr{drop}")
            if cid in clips:
                parts.append(f"● CLIP {clips[cid]:.0f} s")
            if cid == self.ctl.roi_camera:
                parts.append("ROIs")
            err = c.get("error")
            stall = None if err else stall_text(c)
            if err:
                parts.append(f"ERROR {err}")
            elif stall:
                parts.append(stall)
            self.grid.update_tile(cid, th.latest, "  ·  ".join(parts),
                                  alert=bool(err or stall or c.get("rec_dropped")))

    def _update_led(self, img: np.ndarray) -> None:
        sync = self.video.roiset().sync
        if sync is None:
            self.led_label.setText("Sync LED box: not drawn")
            return
        x0, y0, x1, y1 = sync.bbox()
        patch = img[max(0, y0):y1, max(0, x0):x1]
        if patch.size:
            self.led_label.setText(f"Sync LED box brightness: {patch.mean():.0f} (should jump when the LED flashes)")

    # ------------------------------------------------------------------ outputs
    def _start_outputs(self) -> None:
        def done(errors):
            self.outputs_changed()
            if errors:
                self.statusBar().showMessage("Output devices failed: " + "; ".join(f"{k}: {v}" for k, v in errors.items()),
                                             15000)

        self._run(self.ctl.start_outputs, done, "Starting outputs")

    def outputs_changed(self) -> None:
        """After (re)start: the engine is disarmed."""
        self.b_arm.blockSignals(True)
        self.b_arm.setChecked(False)
        self.b_arm.blockSignals(False)
        self._style_arm(False)
        self._update_outputs_label()

    def _open_outputs(self) -> None:
        from .outputs_dialog import OutputsDialog

        OutputsDialog(self, self.ctl).exec()
        self._update_outputs_label()

    def _toggle_arm(self, on: bool) -> None:
        eng = self.ctl.outputs
        if eng is None:
            self.b_arm.blockSignals(True)
            self.b_arm.setChecked(False)
            self.b_arm.blockSignals(False)
            return
        if on:
            eng.arm(self.ctl.presence())
        else:
            eng.disarm()
            self.ctl.stop_sounds()  # soundboard sounds too
        self._style_arm(on)
        self._update_outputs_label()

    def _style_arm(self, on: bool) -> None:
        self.b_arm.setText("● ARMED (click to disarm)" if on else "Arm outputs")
        self.b_arm.setStyleSheet("background:#e34948; color:white; font-weight:bold" if on else "")

    def _update_outputs_label(self) -> None:
        eng = self.ctl.outputs
        if eng is None:
            self.out_label.setText("not started")
            return
        st = eng.status()
        bad = {k: v["error"] for k, v in st["devices"].items() if v.get("error")}
        rules = st["rules"]
        n_on = sum(1 for r in rules.values() if r["enabled"])
        lines = [f"{len(st['devices'])} device(s), {n_on}/{len(rules)} rule(s) enabled — "
                 + ("<b style='color:#e34948'>ARMED</b>" if st["armed"] else "disarmed")]
        for name, r in rules.items():
            if r["enabled"]:
                active = " <b>active</b>" if r["active"] else ""
                lines.append(f"{name}: fired {r['count']}{active}" + (f", suppressed {r['suppressed']}"
                                                                     if r["suppressed"] else ""))
        lines += [f"<span style='color:#e34948'>{k}: {v}</span>" for k, v in bad.items()]
        self.out_label.setText("<br>".join(lines))

    def _on_action(self, row: dict) -> None:
        if row.get("status") in ("ok", "error", "warning"):
            txt = (f"{time.strftime('%H:%M:%S')}  ⚡ {row.get('rule') or row.get('device')}: "
                   f"{row.get('event')} {row.get('status')} {row.get('result', '')}")
            self.events.insertItem(0, txt)
        self._update_outputs_label()

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        if self.ctl.recording:
            r = QtWidgets.QMessageBox.question(self, "Recording", "Stop the recording and quit?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        self.timer.stop()
        self.ctl.shutdown()
        ev.accept()


def main(rig: dict, sim: bool = False) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    ctl = simulation_controller(rig) if sim else AcquisitionController(rig)
    win = AcquireWindow(ctl, sim)
    win.show()
    app.exec()

