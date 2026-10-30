"""Video settings (cameras, raw recording, compression defaults) and the compression dialog."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ..config import resolve_path
from ..video.compress import ffmpeg_command, options, session_jobs
from ..video.settings import RAW_CODECS, RECORD_FORMATS, Param, VideoSettings, params_for, video_cfg
from .common import Worker, message

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


def _widget(p: Param, value):
    if p.kind == "bool":
        w = QtWidgets.QCheckBox()
        w.setChecked(bool(value))
        return w, w.toggled, lambda: bool(w.isChecked())
    if p.kind == "enum":
        w = QtWidgets.QComboBox()
        w.addItems([str(c) for c in p.choices])
        w.setCurrentText(str(value))
        return w, w.currentIndexChanged, lambda: w.currentText()
    if p.kind == "int":
        w = QtWidgets.QSpinBox()
        w.setRange(int(p.lo if p.lo is not None else 0), int(p.hi if p.hi is not None else 1_000_000))
        w.setValue(int(value or 0))
        if p.unit:
            w.setSuffix(f" {p.unit}")
        return w, w.valueChanged, lambda: int(w.value())
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(float(p.lo if p.lo is not None else -1e9), float(p.hi if p.hi is not None else 1e9))
    w.setDecimals(2 if p.key in ("gamma", "black_level", "fps") else 1)
    w.setValue(float(value if value is not None else p.default))
    w.setKeyboardTracking(False)  # live settings change on Enter / arrows, not on every typed digit
    if p.unit:
        w.setSuffix(f" {p.unit}")
    return w, w.valueChanged, lambda: float(w.value())


class VideoSettingsDialog(QtWidgets.QDialog):
    """Per-camera settings (live where the camera allows it), raw recording format, compression defaults."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ctl = win.ctl
        self.setWindowTitle("Video settings")
        self.resize(820, 640)
        self.path = resolve_path(self.ctl.rig["paths"].get("video_settings_file", "config/video.yaml"))
        self.cams = copy.deepcopy(self.ctl.rig["cameras"])
        self.vcfg = video_cfg(self.ctl.rig)
        self._getters: dict[str, callable] = {}
        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        self.cam_list = QtWidgets.QListWidget()
        self.cam_list.setFixedWidth(150)
        for c in self.cams:
            self.cam_list.addItem(c["id"])
        top.addWidget(self.cam_list)
        right = QtWidgets.QVBoxLayout()
        self.cam_info = QtWidgets.QLabel("")
        self.cam_info.setWordWrap(True)
        right.addWidget(self.cam_info)
        self.form_box = QtWidgets.QGroupBox("Camera")
        self.form = QtWidgets.QFormLayout(self.form_box)
        right.addWidget(self.form_box)
        row = QtWidgets.QHBoxLayout()
        self.b_apply = QtWidgets.QPushButton("Apply to camera (reconnects if needed)")
        self.b_apply.clicked.connect(self.apply_camera)
        row.addWidget(self.b_apply)
        row.addStretch(1)
        right.addLayout(row)
        top.addLayout(right, 1)
        lay.addLayout(top, 1)

        g = QtWidgets.QGroupBox("Recording")
        f = QtWidgets.QFormLayout(g)
        self.fmt = QtWidgets.QComboBox()
        for k, label in RECORD_FORMATS.items():
            self.fmt.addItem(label, k)
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(self.vcfg["record_format"])))
        self.raw_codec = QtWidgets.QComboBox()
        for k, label in RAW_CODECS.items():
            self.raw_codec.addItem(label, k)
        self.raw_codec.setCurrentIndex(max(0, self.raw_codec.findData(self.vcfg["raw_codec"])))
        self.chunk = QtWidgets.QSpinBox()
        self.chunk.setRange(50, 100_000)
        self.chunk.setSingleStep(250)
        self.chunk.setSuffix(" MB")
        self.chunk.setValue(int(self.vcfg["raw_chunk_mb"]))
        self.rate_label = QtWidgets.QLabel("")
        self.rate_label.setWordWrap(True)
        f.addRow("Format", self.fmt)
        f.addRow("Raw codec", self.raw_codec)
        f.addRow("Raw file size", self.chunk)
        f.addRow("", self.rate_label)
        for w in (self.fmt, self.raw_codec):
            w.currentIndexChanged.connect(self._update_rate)
        lay.addWidget(g)

        g = QtWidgets.QGroupBox("Compression defaults (mavric compress, Compress…)")
        f = QtWidgets.QFormLayout(g)
        self.c_widgets = CompressOptions(self.vcfg["compress"])
        f.addRow(self.c_widgets)
        b_comp = QtWidgets.QPushButton("Compress a session…")
        b_comp.clicked.connect(self.compress_session)
        f.addRow(b_comp)
        lay.addWidget(g)

        bb = QtWidgets.QDialogButtonBox()
        b_save = bb.addButton("Save", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close)
        b_save.clicked.connect(self.save)
        bb.rejected.connect(self.reject)
        note = QtWidgets.QLabel(f"Saved to <code>{self.path}</code> (overrides rig.yaml). Exposure, gain, brightness, "
                                "gamma and white balance change live; size, frame rate and pixel format reconnect the "
                                "camera.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        lay.addWidget(note)
        lay.addWidget(bb)
        self.cam_list.currentRowChanged.connect(self._show_camera)
        self.cam_list.setCurrentRow(0)
        self._update_rate()
        from .tips import VIDEO, apply_tips

        apply_tips(self, VIDEO)
        self.cam_list.setToolTip("Cameras from rig.yaml; ⚡ settings change live")

    # ------------------------------------------------------------------ camera form
    def _cam(self) -> dict:
        return self.cams[self.cam_list.currentRow()]

    def _show_camera(self, i: int) -> None:
        if i < 0:
            return
        while self.form.rowCount():
            self.form.removeRow(0)
        self._getters = {}
        cam = self.cams[i]
        self._update_info()
        for p in params_for(cam["backend"]):
            w, sig, get = _widget(p, cam.get(p.key, p.default))
            w.setToolTip(p.help)
            label = p.label + (" ⚡" if p.live else "")
            self.form.addRow(label, w)
            self._getters[p.key] = get
            sig.connect(lambda *_, key=p.key, live=p.live: self._changed(key, live))
        self.b_apply.setEnabled(not self.ctl.recording)

    def _update_info(self) -> None:
        cam = self._cam()
        th = self.ctl.cams.get(cam["id"])
        info = th.backend.info() if th is not None else {}
        state = (f"connected: {info.get('model', cam['backend'])} {info.get('width')}x{info.get('height')} at "
                 f"{info.get('fps', 0):.1f} fps") if th is not None else "not connected (settings apply on connect)"
        self.cam_info.setText(f"<b>{cam['id']}</b> · {cam['backend']} {cam.get('device', '')} · {state}")

    def _changed(self, key: str, live: bool) -> None:
        cam = self._cam()
        cam[key] = self._getters[key]()
        th = self.ctl.cams.get(cam["id"])
        if live and th is not None:
            done = th.backend.apply_live({key: cam[key]})
            if key in done:
                self.win.statusBar().showMessage(f"{cam['id']}: {key} = {cam[key]} applied live", 3000)
        self._update_rate()

    def apply_camera(self) -> None:
        cam = self._cam()
        rig_cam = next(c for c in self.ctl.rig["cameras"] if c["id"] == cam["id"])
        rig_cam.update(cam)
        if cam["id"] in self.ctl.cams:
            if self.ctl.recording:
                message(self, "Recording", "Stop the recording before reconnecting a camera.")
                return
            self.cam_info.setText(f"<b>{cam['id']}</b> · reconnecting…")
            self.win.reconnect_camera(dict(rig_cam), on_done=self._update_info)
        self.win.reload_camera_list()

    # ------------------------------------------------------------------ recording / compression
    def _video_values(self) -> dict:
        return {"record_format": self.fmt.currentData(), "raw_codec": self.raw_codec.currentData(),
                "raw_chunk_mb": int(self.chunk.value()), "compress": self.c_widgets.values()}

    def _update_rate(self) -> None:
        from ..acquire import raw_mb_per_s

        v = self._video_values()
        if v["record_format"] != "raw":
            self.rate_label.setText("H.264 while recording: about 1 GB/h per camera at 1280x1024, 30 fps.")
            return
        rate = raw_mb_per_s(self.ctl.cams, v)
        sess_dir = resolve_path(self.ctl.rig["paths"]["sessions_dir"])
        free = shutil.disk_usage(sess_dir if sess_dir.exists() else Path.cwd()).free / 1e9
        if rate:
            self.rate_label.setText(f"Connected cameras: about {rate * 3.6:.0f} GB/h of raw video "
                                    f"({'FFV1 estimate' if v['raw_codec'] == 'ffv1' else 'uncompressed'}); "
                                    f"{free:.0f} GB free = {free / (rate * 3.6):.1f} h.")
        else:
            self.rate_label.setText(f"Connect cameras to estimate the data rate; {free:.0f} GB free.")

    def save(self) -> None:
        vs = VideoSettings.load(self.path)
        for cam in self.cams:
            keys = {p.key for p in params_for(cam["backend"])}
            vs.cameras[cam["id"]] = {k: cam[k] for k in keys if k in cam}
        vs.video = self._video_values()
        vs.save(self.path)
        for cam in self.cams:  # the running rig follows the saved settings
            next(c for c in self.ctl.rig["cameras"] if c["id"] == cam["id"]).update(cam)
        vs.apply(self.ctl.rig)
        self.win.reload_camera_list()
        self.win.statusBar().showMessage(f"Video settings saved to {self.path}", 5000)

    def compress_session(self) -> None:
        start = str(resolve_path(self.ctl.rig["paths"]["sessions_dir"]))
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Session (or folder of videos) to compress", start)
        if folder:
            CompressDialog(self, folder, self.c_widgets.values()).exec()


class CompressOptions(QtWidgets.QWidget):
    def __init__(self, defaults: dict, parent=None):
        super().__init__(parent)
        o = options(**defaults)
        f = QtWidgets.QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.codec = QtWidgets.QComboBox()
        self.codec.addItems(["libx264", "libx265"])
        self.codec.setCurrentText(o["codec"])
        self.crf = QtWidgets.QSpinBox()
        self.crf.setRange(0, 51)
        self.crf.setValue(int(o["crf"]))
        self.crf.setToolTip("Lower = better quality, larger files. 18 is visually lossless; 23 is the x264 default.")
        self.preset = QtWidgets.QComboBox()
        self.preset.addItems(PRESETS)
        self.preset.setCurrentText(str(o["preset"]))
        self.preset.setToolTip("Slower presets make smaller files at the same quality.")
        self.gop = QtWidgets.QSpinBox()
        self.gop.setRange(1, 600)
        self.gop.setValue(int(o["gop"]))
        self.gop.setSuffix(" frames")
        self.gop.setToolTip("Keyframe interval: short intervals keep frame seeking exact (labeling, DeepLabCut).")
        self.down = QtWidgets.QComboBox()
        for k in (1, 2, 3, 4):
            self.down.addItem("full size" if k == 1 else f"1/{k} width and height", k)
        self.down.setCurrentIndex(max(0, self.down.findData(int(o.get("downscale") or 1))))
        self.down.setToolTip("Smaller files and faster DeepLabCut; ROIs no longer match the downscaled video.")
        f.addRow("Codec", self.codec)
        f.addRow("Quality (CRF)", self.crf)
        f.addRow("Preset", self.preset)
        f.addRow("Keyframe every", self.gop)
        f.addRow("Size", self.down)

    def values(self) -> dict:
        d = int(self.down.currentData())
        return {"codec": self.codec.currentText(), "crf": int(self.crf.value()), "preset": self.preset.currentText(),
                "gop": int(self.gop.value()), "pix_fmt": "yuv420p", "downscale": d if d > 1 else None}


class CompressDialog(QtWidgets.QDialog):
    """Compress a session's raw recordings (or a folder of videos) in the background."""

    progress = QtCore.Signal(str)

    def __init__(self, parent, target: str | Path, defaults: dict):
        super().__init__(parent)
        self.target = Path(target)
        self.setWindowTitle(f"Compress · {self.target.name}")
        self.resize(560, 420)
        self.session = None
        if (self.target / "session.json").exists():
            from ..session import Session

            self.session = Session.open(self.target)
        lay = QtWidgets.QVBoxLayout(self)
        self.opts = CompressOptions(defaults)
        lay.addWidget(self.opts)
        self.cam_checks = {}
        self.cb_clips = QtWidgets.QCheckBox("triggered clips")
        if self.session is not None:
            row = QtWidgets.QHBoxLayout()
            for job in session_jobs(self.session, clips=False):
                cb = QtWidgets.QCheckBox(job["id"])
                cb.setChecked(True)
                self.cam_checks[job["id"]] = cb
                row.addWidget(cb)
            self.cb_clips.setChecked(True)
            self.cb_clips.setEnabled(bool(self.session.clips))
            row.addWidget(self.cb_clips)
            row.addStretch(1)
            lay.addLayout(row)
        self.cb_over = QtWidgets.QCheckBox("Overwrite existing _compressed files")
        lay.addWidget(self.cb_over)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        self.log.setPlainText(self._plan_text())
        bb = QtWidgets.QDialogButtonBox()
        self.b_start = bb.addButton("Compress", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        b_ff = bb.addButton("Show ffmpeg commands", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close)
        self.b_start.clicked.connect(self.start)
        b_ff.clicked.connect(self.show_ffmpeg)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.progress.connect(lambda s: self.log.appendPlainText(s))
        self._workers: list[Worker] = []
        from .tips import VIDEO, apply_tips

        apply_tips(self, VIDEO)
        for cid, cb in self.cam_checks.items():
            cb.setToolTip(f"Compress {cid}")
        self.log.setToolTip("What will be written, progress and results")

    def _cams(self) -> list[str] | None:
        return [k for k, cb in self.cam_checks.items() if cb.isChecked()] if self.cam_checks else None

    def _plan_text(self) -> str:
        if self.session is None:
            return f"Every video in {self.target} becomes <name>_compressed.mp4 next to it (raw chunks are joined)."
        lines = ["Raw files are kept. Output, next to the raw files:"]
        for job in session_jobs(self.session):
            lines.append(f"  {job['out'].relative_to(self.session.path)}  <- {len(job['parts'])} file(s)"
                         + (f", {job['frames']} frames" if job["frames"] else ""))
        return "\n".join(lines)

    def show_ffmpeg(self) -> None:
        o = self.opts.values()
        if self.session is not None:
            cmds = [ffmpeg_command(j["parts"], j["out"], j["fps"], o)
                    for j in session_jobs(self.session, self._cams(), self.cb_clips.isChecked())]
        else:
            cmds = [ffmpeg_command([self.target], self.target.with_name(self.target.stem + "_compressed.mp4"), 30, o)]
        self.log.appendPlainText("\n".join(cmds))

    def start(self) -> None:
        from ..video.compress import compress_path, compress_session

        o = self.opts.values()
        self.b_start.setEnabled(False)
        last = {}

        def prog(label, k, n):
            if k - last.get(label, 0) >= 1000 or (n and k == n):
                last[label] = k
                self.progress.emit(f"{label}: {k}" + (f"/{n} frames" if n else " frames"))

        if self.session is not None:
            fn = lambda: compress_session(self.session, o, self._cams(), self.cb_clips.isChecked(),  # noqa: E731
                                          self.cb_over.isChecked(), prog)
        else:
            fn = lambda: compress_path(self.target, o, self.cb_over.isChecked(), prog)  # noqa: E731
        w = Worker(fn)

        def done(res):
            self.b_start.setEnabled(True)
            for r in res:
                if r.get("skipped"):
                    self.log.appendPlainText(f"{r['file']}: skipped ({r['skipped']})")
                else:
                    self.log.appendPlainText(f"{r['file']}: {r['frames']} frames, {r['width']}x{r['height']}, "
                                             f"{r['bytes'] / 1e6:.0f} MB in {r['seconds']:.0f} s")
            self.log.appendPlainText("done.")

        w.done.connect(done)
        w.failed.connect(lambda e: (self.b_start.setEnabled(True), self.log.appendPlainText(f"ERROR {e}")))
        self._workers.append(w)
        self.log.appendPlainText(f"compressing with {o['codec']} crf {o['crf']} preset {o['preset']} …")
        w.start()
