"""Settings → Storage…: where sessions are recorded, and the archive folder they are moved to afterwards.

Saved in config/video.yaml (storage:), over rig.yaml's paths.sessions_dir, paths.archive_dir and archive:.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from ..acquire import raw_mb_per_s
from ..archive import local_sessions, session_size, state
from ..config import resolve_path
from ..video.settings import VideoSettings, video_cfg
from .common import message

WAITING = ("waiting", "partly copied", "changed since archived")


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.0f} MB"


def _drive(p: str) -> str:
    return os.path.splitdrive(str(Path(p).expanduser().resolve()))[0].lower()


def _free(p: str) -> float | None:
    """Free bytes on the drive of `p` (or of its nearest existing parent)."""
    q = Path(p).expanduser()
    while not q.exists() and q.parent != q:
        q = q.parent
    try:
        return shutil.disk_usage(q).free
    except OSError:
        return None


class StorageDialog(QtWidgets.QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win, self.ctl = win, win.ctl
        self.setWindowTitle("Storage")
        self.resize(760, 600)
        self.path = resolve_path(self.ctl.rig["paths"].get("video_settings_file", "config/video.yaml"))
        a = self.ctl.archive_settings
        v = QtWidgets.QVBoxLayout(self)

        g = QtWidgets.QGroupBox("Recording folder")
        gl = QtWidgets.QGridLayout(g)
        self.rec_edit = QtWidgets.QLineEdit(self.ctl.rig["paths"]["sessions_dir"])
        self.b_rec = QtWidgets.QPushButton("Browse…")
        self.b_rec.clicked.connect(lambda: self._browse(self.rec_edit, "Record sessions in"))
        self.rec_info = QtWidgets.QLabel()
        self.rec_info.setWordWrap(True)
        gl.addWidget(self.rec_edit, 0, 0)
        gl.addWidget(self.b_rec, 0, 1)
        gl.addWidget(self.rec_info, 1, 0, 1, 2)
        if self.ctl.recording:
            self.rec_edit.setEnabled(False)
            self.b_rec.setEnabled(False)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Archive folder")
        gl = QtWidgets.QGridLayout(g)
        self.arch_edit = QtWidgets.QLineEdit(a["dir"])
        self.arch_edit.setPlaceholderText("none: sessions stay in the recording folder")
        self.b_arch = QtWidgets.QPushButton("Browse…")
        self.b_arch.clicked.connect(lambda: self._browse(self.arch_edit, "Move finished sessions to"))
        self.cb_after = QtWidgets.QCheckBox("Move each session here after Stop")
        self.cb_after.setChecked(a["after_stop"])
        self.cb_keep = QtWidgets.QCheckBox("Keep the copy in the recording folder too")
        self.cb_keep.setChecked(a["keep_local"])
        self.arch_info = QtWidgets.QLabel()
        self.arch_info.setWordWrap(True)
        note = QtWidgets.QLabel(
            "<small>A session is copied in the background, with the recording's disk writes going first. Each file "
            "is read back and its SHA-256 compared before the recording folder's copy is deleted; "
            "<code>archive.json</code> in the archived session lists them. A copy that stops half-way resumes after "
            "the next Stop, here, or with <code>mavric archive --all</code>.</small>")
        note.setWordWrap(True)
        gl.addWidget(self.arch_edit, 0, 0)
        gl.addWidget(self.b_arch, 0, 1)
        gl.addWidget(self.cb_after, 1, 0, 1, 2)
        gl.addWidget(self.cb_keep, 2, 0, 1, 2)
        gl.addWidget(self.arch_info, 3, 0, 1, 2)
        gl.addWidget(note, 4, 0, 1, 2)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Sessions in the recording folder")
        gl = QtWidgets.QVBoxLayout(g)
        self.list = QtWidgets.QTreeWidget()
        self.list.setHeaderLabels(["Session", "Size", "Archive"])
        self.list.setRootIsDecorated(False)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        row = QtWidgets.QHBoxLayout()
        self.b_sel = QtWidgets.QPushButton("Archive selected")
        self.b_sel.clicked.connect(lambda: self._archive(self._selected()))
        self.b_all = QtWidgets.QPushButton("Archive all waiting")
        self.b_all.clicked.connect(lambda: self._archive(self._waiting()))
        b_ref = QtWidgets.QPushButton("↻")
        b_ref.setToolTip("Look at the recording folder again")
        b_ref.setFixedWidth(32)
        b_ref.clicked.connect(self.refresh)
        row.addWidget(self.b_sel)
        row.addWidget(self.b_all)
        row.addStretch(1)
        row.addWidget(b_ref)
        self.prog = QtWidgets.QLabel()
        self.prog.setWordWrap(True)
        gl.addWidget(self.list, 1)
        gl.addLayout(row)
        gl.addWidget(self.prog)
        v.addWidget(g, 1)

        bb = QtWidgets.QDialogButtonBox()
        self.b_save = bb.addButton("Save", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("Close", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        self.b_save.clicked.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        from .tips import STORAGE, apply_tips

        apply_tips(self, STORAGE)
        self.rec_edit.editingFinished.connect(self.refresh)
        self.arch_edit.editingFinished.connect(self.refresh)
        self._seen = (-1, None)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self.refresh()

    # ------------------------------------------------------------------ values
    @property
    def rec_dir(self) -> str:
        return self.rec_edit.text().strip()

    @property
    def arch_dir(self) -> str:
        return self.arch_edit.text().strip()

    def _browse(self, edit: QtWidgets.QLineEdit, title: str) -> None:
        start = edit.text().strip() or self.rec_dir or str(Path.home())
        d = QtWidgets.QFileDialog.getExistingDirectory(self, title, start)
        if d:
            edit.setText(str(Path(d)))
            self.refresh()

    def _problem(self) -> str | None:
        """Why the archive folder cannot be used, or None."""
        if not self.arch_dir:
            return None
        rec, arch = Path(self.rec_dir).expanduser().resolve(), Path(self.arch_dir).expanduser().resolve()
        if arch == rec:
            return "the archive folder is the recording folder"
        if arch.is_relative_to(rec):
            return "the archive folder is inside the recording folder"
        return None

    # ------------------------------------------------------------------ display
    def refresh(self) -> None:
        free = _free(self.rec_dir) if self.rec_dir else None
        vcfg = video_cfg(self.ctl.rig)
        rate = raw_mb_per_s(self.ctl.cams, vcfg) if vcfg.get("record_format") == "raw" else 0
        txt = "" if free is None else f"{_gb(free)} free"
        if free is not None and rate:
            txt += f": about {free / 1e6 / rate / 3600:.1f} h of raw video from the connected cameras " \
                   f"({rate * 3.6:.0f} GB/h)"
        if self.ctl.recording:
            txt += (". " if txt else "") + "Recording now: change the folder after Stop."
        self.rec_info.setText(txt + ("." if txt and not txt.endswith(".") else ""))
        if not self.arch_dir:
            self.arch_info.setText("")
        else:
            afree = _free(self.arch_dir)
            parts = [f"{_gb(afree)} free" if afree is not None else "not reachable now (unplugged, or offline)"]
            if problem := self._problem():
                parts.append(f"<b>{problem}</b>")
            elif self.rec_dir and _drive(self.rec_dir) == _drive(self.arch_dir):
                parts.append("<b>on the same drive as the recording folder</b>: moving gains no room")
            self.arch_info.setText("; ".join(parts) + ".")
        cur = str(self.ctl.session.path) if self.ctl.session else None
        sel = {it.data(0, QtCore.Qt.ItemDataRole.UserRole) for it in self.list.selectedItems()}
        self.list.clear()
        for p in reversed(local_sessions(self.rec_dir)) if self.rec_dir else []:
            st = "recording" if str(p) == cur else state(p, self.arch_dir or None)
            it = QtWidgets.QTreeWidgetItem([p.name, _gb(session_size(p)), st])
            it.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(p))
            it.setData(2, QtCore.Qt.ItemDataRole.UserRole, st)
            it.setTextAlignment(1, QtCore.Qt.AlignmentFlag.AlignRight)
            self.list.addTopLevelItem(it)
            it.setSelected(str(p) in sel)
        for c in (1, 2):
            self.list.resizeColumnToContents(c)
        self._update_buttons()
        self._tick()

    def _rows(self):
        return [self.list.topLevelItem(i) for i in range(self.list.topLevelItemCount())]

    def _selected(self) -> list[Path]:
        return [Path(it.data(0, QtCore.Qt.ItemDataRole.UserRole)) for it in self.list.selectedItems()]

    def _waiting(self) -> list[Path]:
        return [Path(it.data(0, QtCore.Qt.ItemDataRole.UserRole)) for it in self._rows()
                if it.data(2, QtCore.Qt.ItemDataRole.UserRole) in WAITING]

    def _update_buttons(self) -> None:
        ok = bool(self.arch_dir) and self._problem() is None
        self.b_sel.setEnabled(ok and any(it.data(2, QtCore.Qt.ItemDataRole.UserRole) in WAITING
                                         for it in self.list.selectedItems()))
        self.b_all.setEnabled(ok and bool(self._waiting()))

    def _tick(self) -> None:
        a = self.ctl.archiver.status()
        cur = a["current"]
        if cur:
            pct = f"{cur['done'] / cur['total']:.0%}" if cur["total"] else "starting"
            txt = f"Moving <b>{cur['session']}</b>: {pct}" + (f" ({len(a['queued'])} more waiting)"
                                                              if a["queued"] else "")
        elif a["last"]:
            r = a["last"]
            txt = (f"<span style='color:#c0392b'>{r['session']} not moved: {r['error']}</span>" if r.get("error")
                   else f"{r['session']}: moved to {r['to']}, {r['files']} files checked"
                   + (f" (kept here, changed after the copy: {', '.join(r['kept'])})" if r.get("kept") else ""))
        else:
            txt = ""
        self.prog.setText(txt)
        seen = (a["n_results"], cur["session"] if cur else None)
        if seen != self._seen:  # a session started or finished moving: its state changed
            self._seen = seen
            if seen[0] >= 0:
                QtCore.QTimer.singleShot(0, self._refresh_states)

    def _refresh_states(self) -> None:
        moving = (self.ctl.archiver.status()["current"] or {}).get("session")
        keep = []
        for it in self._rows():
            p = Path(it.data(0, QtCore.Qt.ItemDataRole.UserRole))
            if not p.exists():
                keep.append(it)
                continue
            st = "moving" if p.name == moving else state(p, self.arch_dir or None)
            it.setText(2, st)
            it.setData(2, QtCore.Qt.ItemDataRole.UserRole, st)
        for it in keep:  # moved away
            self.list.takeTopLevelItem(self.list.indexOfTopLevelItem(it))
        self._update_buttons()

    # ------------------------------------------------------------------ actions
    def _archive(self, sessions: list[Path]) -> None:
        if problem := self._problem():
            message(self, "Archive", problem[0].upper() + problem[1:] + ".")
            return
        sessions = [p for p in sessions if state(p, self.arch_dir) in WAITING]
        if not sessions:
            return
        size = sum(session_size(p) for p in sessions)
        keep = self.cb_keep.isChecked()
        r = QtWidgets.QMessageBox.question(
            self, "Archive",
            f"{'Copy' if keep else 'Move'} {len(sessions)} session(s), {_gb(size)}, to {self.arch_dir}?"
            + ("" if keep else " Each is deleted from the recording folder once every file is checked in the "
                               "archive."))
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctl.archive(sessions, self.arch_dir, keep)
        except ValueError as e:
            message(self, "Archive", str(e), error=True)
            return
        self._refresh_states()

    def save(self) -> None:
        if problem := self._problem():
            message(self, "Storage", problem[0].upper() + problem[1:] + ".", error=True)
            return
        if not self.rec_dir:
            message(self, "Storage", "Choose a recording folder.", error=True)
            return
        try:
            Path(self.rec_dir).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as e:
            message(self, "Storage", f"Cannot create the recording folder: {e}", error=True)
            return
        vs = VideoSettings.load(self.path)
        vs.storage = {"sessions_dir": self.rec_dir, "archive_dir": self.arch_dir,
                      "after_stop": self.cb_after.isChecked(), "keep_local": self.cb_keep.isChecked()}
        vs.save(self.path)
        if self.ctl.recording:  # the session being recorded stays where it is
            vs.storage.pop("sessions_dir")
        vs.apply(self.ctl.rig)
        self.win.update_archive_label(force=True)
        self.win.statusBar().showMessage(f"Storage saved to {self.path}", 5000)
        self.refresh()
