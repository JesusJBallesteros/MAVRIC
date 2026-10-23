"""Sound events in the review window: audio lanes, listening, labeling, proposals, suggestions.

Lanes (under the perch timeline, sharing its time axis):
- an overview of both microphones' waveforms (min/max envelope) with the sound events as
  coloured spans (emitter colour; the selected one can be dragged) and automatic proposals
  as dashed spans;
- one spectrogram per microphone, computed for the visible span when it is 60 s or less.

Keys: S start / E end an event at the playhead · A use the top emitter suggestion ·
Y accept / R reject the current proposal · , . previous / next event or proposal ·
L listen around the playhead (or the selected event) · Shift+Del delete the selected event ·
F1-F9 event type · Alt+1-9 emitter = pigeon 1-9, Alt+0 unknown.
"""

from __future__ import annotations

import queue
import threading

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ..audio.detect import detect_events, measure_event, occupancy, overlapping_playback, suggest_emitters
from ..audio.events import (
    SYSTEM,
    UNKNOWN,
    SoundEvent,
    emitters,
    export_events,
    import_events,
    load_events,
    load_sound_coverage,
    load_types,
    merge_playback,
    playback_events,
    save_events,
    save_sound_coverage,
    save_types,
)
from ..audio.source import AudioSource
from ..config import CATEGORICAL, load_rig, resolve_path
from .audio_widgets import SPEC_STOPS
from .common import Worker, message, qcolor

PROPOSALS_FILE = "sound_proposals.csv"
SPEC_MAX_S = 60.0
AXIS_W = 120  # left axis width shared with the perch timeline, so the time axes line up
NO_MOUSE = QtCore.Qt.MouseButton.NoButton


# =========================================================================== listening


class AudioPlayer:
    """Plays session audio from a time, at a speed (pitch follows), on the default output device."""

    def __init__(self, src: AudioSource):
        self.src = src
        self.stream = None
        self.gain = 1.0
        self._q: queue.Queue = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._feeder: threading.Thread | None = None
        self._buf = np.zeros((0, 1), np.float32)

    @property
    def active(self) -> bool:
        return self.stream is not None

    def start(self, t: float, speed: float = 1.0, cols: list[int] | None = None, stop_t: float | None = None) -> None:
        from ..audio.devices import import_sounddevice

        self.stop()
        sd = import_sounddevice()
        cols = cols or self.src.mic_idx[:1]
        n_out = 2 if len(cols) >= 2 else 1
        self._stop.clear()
        self._q = queue.Queue(maxsize=8)
        self._buf = np.zeros((0, n_out), np.float32)
        s0 = float(self.src.sample_at([t])[0])
        s_end = float(self.src.sample_at([stop_t])[0]) if stop_t is not None else float(self.src.n_samples)
        self._feeder = threading.Thread(target=self._feed, args=(s0, s_end, speed, cols[:2], n_out), daemon=True)
        self._feeder.start()
        self.stream = sd.OutputStream(samplerate=self.src.sr, channels=n_out, dtype="float32", blocksize=2048,
                                      callback=self._callback, finished_callback=self._finished)
        self.stream.start()

    def _feed(self, pos: float, s_end: float, speed: float, cols: list[int], n_out: int) -> None:
        block = 8192  # output samples per chunk
        while not self._stop.is_set() and pos < s_end:
            need = int(np.ceil(block * speed)) + 2
            x = self.src.read_samples(int(pos), int(pos) + need, cols)
            if not len(x):
                break
            pos_frac = pos - int(pos)
            idx = pos_frac + np.arange(block) * speed
            idx = idx[idx < len(x) - 1]
            y = np.stack([np.interp(idx, np.arange(len(x)), x[:, c]) for c in range(x.shape[1])], axis=1)
            if n_out == 2 and y.shape[1] == 1:
                y = np.repeat(y, 2, axis=1)
            y = np.clip(y * self.gain, -1, 1).astype(np.float32)
            pos += len(idx) * speed
            if pos >= s_end:
                y = y[: max(0, len(y) - int((pos - s_end) / speed))]
            while not self._stop.is_set():
                try:
                    self._q.put(y, timeout=0.1)
                    break
                except queue.Full:
                    continue
        self._q.put(None)

    def _callback(self, outdata, frames, time_info, status) -> None:
        import sounddevice as sd

        while len(self._buf) < frames:
            try:
                y = self._q.get_nowait()
            except queue.Empty:
                break
            if y is None:
                outdata[: len(self._buf)] = self._buf
                outdata[len(self._buf):] = 0
                self._buf = self._buf[:0]
                raise sd.CallbackStop
            self._buf = np.concatenate([self._buf, y])
        n = min(frames, len(self._buf))
        outdata[:n] = self._buf[:n]
        outdata[n:] = 0
        self._buf = self._buf[n:]

    def _finished(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        if self._feeder is not None:
            self._feeder.join(timeout=1)
            self._feeder = None


# =========================================================================== lanes


class SoundLanes(QtWidgets.QWidget):
    """Envelope overview and spectrograms, x-linked to the review timeline."""

    def __init__(self, src: AudioSource, master: pg.PlotWidget, fmax_hz: float = 10000.0, parent=None):
        super().__init__(parent)
        self.src = src
        self.master = master
        self.fmax = fmax_hz
        self.env = None
        self._env_ymax = 1.0
        self._spec_key = None
        self.on_click = None  # callback(t)
        self.on_region = None  # callback(index, t0, t1) when the selected event is dragged
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.env_plot = self._plot("waveform")
        self.env_plot.setMinimumHeight(90)
        lay.addWidget(self.env_plot)
        self.env_text = pg.TextItem("computing the audio overview…", color="#52514e", anchor=(0, 0))
        self.env_plot.addItem(self.env_text)
        self.env_curves = []
        for i, mic in enumerate(src.mics):
            col = CATEGORICAL[i % len(CATEGORICAL)]
            hi = pg.PlotCurveItem(pen=pg.mkPen(col, width=1))
            lo = pg.PlotCurveItem(pen=pg.mkPen(col, width=1))
            fill = pg.FillBetweenItem(hi, lo, brush=qcolor(col, 90))
            for it in (hi, lo, fill):
                self.env_plot.addItem(it)
            self.env_curves.append((hi, lo))
        self.spec_plots, self.spec_imgs, self.spec_text = [], [], []
        cmap = pg.ColorMap([p for p, _ in SPEC_STOPS], [c for _, c in SPEC_STOPS])
        for mic in src.mics:
            p = self._plot(f"{mic} kHz")
            p.setMinimumHeight(100)
            img = pg.ImageItem()
            img.setLookupTable(cmap.getLookupTable(nPts=256))
            p.addItem(img)
            p.setYRange(0, fmax_hz / 1000, padding=0)
            txt = pg.TextItem(f"zoom in to {SPEC_MAX_S:.0f} s or less for the spectrogram", color="#52514e",
                              anchor=(0, 0))
            p.addItem(txt)
            lay.addWidget(p)
            self.spec_plots.append(p)
            self.spec_imgs.append(img)
            self.spec_text.append(txt)
        self.plots = [self.env_plot] + self.spec_plots
        self.playheads = []
        for p in self.plots:
            ln = pg.InfiniteLine(0, angle=90, movable=False, pen=pg.mkPen("#0b0b0b", width=1.5))
            ln.setZValue(50)
            p.addItem(ln)
            self.playheads.append(ln)
            p.scene().sigMouseClicked.connect(lambda ev, pp=p: self._clicked(pp, ev))
        dash = pg.mkPen("#e34948", width=1.5, style=QtCore.Qt.PenStyle.DashLine)
        self.pending = pg.InfiniteLine(0, angle=90, movable=False, pen=dash)
        self.pending.setVisible(False)
        self.env_plot.addItem(self.pending)
        self._items: list = []
        self._sel_region = None
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh_view)
        master.getViewBox().sigXRangeChanged.connect(lambda *_: self._timer.start(120))
        self.draw_events([], [], None)

    def _plot(self, label: str) -> pg.PlotWidget:
        p = pg.PlotWidget()
        p.setBackground("#fcfcfb")
        p.setMouseEnabled(x=True, y=False)
        p.setMenuEnabled(False)
        p.hideButtons()
        p.setXLink(self.master)
        p.setLabel("left", label)
        p.getAxis("left").enableAutoSIPrefix(False)
        p.getAxis("left").setWidth(AXIS_W)
        return p

    # ------------------------------------------------------------------ data
    def set_envelope(self, env: dict) -> None:
        self.env = env
        self.env_plot.removeItem(self.env_text)
        hi = np.abs(np.concatenate([env["hi"].ravel(), env["lo"].ravel()]))
        self._env_ymax = float(max(np.percentile(hi, 99.9) * 1.1, 1e-4)) if len(hi) else 1.0
        self.env_plot.setYRange(-self._env_ymax, self._env_ymax, padding=0)
        self.refresh_view()

    def refresh_view(self) -> None:
        x0, x1 = self.master.getViewBox().viewRange()[0]
        if self.env is not None:
            t = self.env["t"]
            i0, i1 = np.searchsorted(t, [x0, x1])
            i0, i1 = max(0, i0 - 1), min(len(t), i1 + 1)
            n = i1 - i0
            step = max(1, n // 3000)
            m = n // step * step
            if m > 0:
                tt = t[i0:i0 + m].reshape(-1, step).mean(axis=1)
                for c, (hi_c, lo_c) in enumerate(self.env_curves):
                    hi_c.setData(tt, self.env["hi"][i0:i0 + m, c].reshape(-1, step).max(axis=1))
                    lo_c.setData(tt, self.env["lo"][i0:i0 + m, c].reshape(-1, step).min(axis=1))
        self._refresh_spectrograms(x0, x1)
        self.redraw_events()

    def _refresh_spectrograms(self, x0: float, x1: float) -> None:
        show = (x1 - x0) <= SPEC_MAX_S
        for img, txt in zip(self.spec_imgs, self.spec_text):
            img.setVisible(show)
            txt.setVisible(not show)
            txt.setPos(x0, self.fmax / 1000)
        if not show:
            return
        a, b = max(x0, self.src.t_start), min(x1, self.src.t_stop)
        if b - a < 0.05:
            return
        width = max(200, self.spec_plots[0].width())
        key = (round(a, 3), round(b, 3), width)
        if key == self._spec_key:
            return
        self._spec_key = key
        for k, (col, img) in enumerate(zip(self.src.mic_idx, self.spec_imgs)):
            S, ta, tb, ftop = self.src.spectrogram(a, b, col, n_cols=width, fmax=self.fmax)
            lo = float(np.percentile(S, 20))
            hi = max(lo + 40.0, float(S.max()))
            img.setImage(S, levels=(lo, hi), autoLevels=False)
            img.setRect(QtCore.QRectF(ta, 0, max(tb - ta, 1e-6), ftop / 1000))

    # ------------------------------------------------------------------ events
    def draw_events(self, events: list[SoundEvent], proposals: list[tuple[float, float]], selected: int | None,
                    colors=None) -> None:
        self._events, self._proposals, self._selected = events, proposals, selected
        self._colors = colors or (lambda e: "#898781")
        self.redraw_events()

    def redraw_events(self) -> None:
        for it in self._items:
            self.env_plot.removeItem(it)
        self._items = []
        self._sel_region = None
        x0, x1 = self.master.getViewBox().viewRange()[0]
        y = self._env_ymax
        visible = [(i, e) for i, e in enumerate(self._events) if e.stop_s > x0 and e.start_s < x1]
        if len(visible) > 400:  # too many to draw individually: show them only when zoomed in
            visible = []
        for a, b in self._proposals:
            if b > x0 and a < x1:
                r = pg.LinearRegionItem((a, b), movable=False, brush=qcolor("#898781", 25),
                                        pen=pg.mkPen("#52514e", width=1, style=QtCore.Qt.PenStyle.DashLine))
                self._static(r)
        for i, e in visible:
            col = self._colors(e)
            sel = i == self._selected
            r = pg.LinearRegionItem((e.start_s, e.stop_s), movable=sel, brush=qcolor(col, 110 if sel else 60),
                                    pen=pg.mkPen(col, width=3 if sel else 1))
            if sel:
                r.setZValue(20)
                r.sigRegionChangeFinished.connect(lambda reg, idx=i: self.on_region and self.on_region(
                    idx, *reg.getRegion()))
                self.env_plot.addItem(r)
                self._items.append(r)
                self._sel_region = r
            else:
                self._static(r)
            txt = pg.TextItem(f"{e.type} · {e.emitter}", color="#0b0b0b", anchor=(0, 0))
            txt.setPos(e.start_s, y)
            txt.setZValue(21)
            self.env_plot.addItem(txt)
            self._items.append(txt)

    def _static(self, r: pg.LinearRegionItem) -> None:
        r.setZValue(5)
        r.setAcceptedMouseButtons(NO_MOUSE)
        for ln in r.lines:
            ln.setAcceptedMouseButtons(NO_MOUSE)
        self.env_plot.addItem(r)
        self._items.append(r)

    def set_time(self, t: float) -> None:
        for ln in self.playheads:
            ln.setValue(t)

    def set_pending(self, t: float | None) -> None:
        self.pending.setVisible(t is not None)
        if t is not None:
            self.pending.setValue(t)

    def _clicked(self, plot: pg.PlotWidget, ev) -> None:
        if ev.button() != QtCore.Qt.MouseButton.LeftButton or ev.isAccepted() or self.on_click is None:
            return
        vb = plot.getViewBox()
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        self.on_click(vb.mapSceneToView(ev.scenePos()).x())


# =========================================================================== types dialog


class TypesDialog(QtWidgets.QDialog):
    def __init__(self, parent, types: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Sound event types")
        lay = QtWidgets.QVBoxLayout(self)
        self.lst = QtWidgets.QListWidget()
        for t in types:
            self._add(t)
        lay.addWidget(self.lst)
        row = QtWidgets.QHBoxLayout()
        for label, fn in (("Add", lambda: self._add("new type", edit=True)), ("Remove", self._remove),
                          ("Up", lambda: self._move(-1)), ("Down", lambda: self._move(1))):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        hint = QtWidgets.QLabel("Double-click to rename. F1–F9 select the first nine types while labeling. "
                                "Renaming does not change events already labeled.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        lay.addWidget(hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _add(self, text: str, edit: bool = False) -> None:
        it = QtWidgets.QListWidgetItem(text)
        it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
        self.lst.addItem(it)
        if edit:
            self.lst.setCurrentItem(it)
            self.lst.editItem(it)

    def _remove(self) -> None:
        r = self.lst.currentRow()
        if r >= 0:
            self.lst.takeItem(r)

    def _move(self, d: int) -> None:
        r = self.lst.currentRow()
        if 0 <= r + d < self.lst.count() and r >= 0:
            it = self.lst.takeItem(r)
            self.lst.insertItem(r + d, it)
            self.lst.setCurrentRow(r + d)

    def types(self) -> list[str]:
        out = []
        for i in range(self.lst.count()):
            t = self.lst.item(i).text().strip()
            if t and t not in out:
                out.append(t)
        return out


# =========================================================================== controller + side panel


class SoundLabeler(QtCore.QObject):
    """Sound events of one session in the review window."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        sess, P = win.sess, win.P
        self.sess = sess
        self.src = AudioSource(sess, P.audio)
        rig = load_rig()
        self.types_path = resolve_path(rig["paths"].get("sound_types_file", "config/sound_types.yaml"))
        self.types = load_types(self.types_path)
        self.speakers = list((sess.audio.get("speakers") or {}).values())
        self.emitters = emitters(win.pigeons, self.speakers)
        self.events = load_events(sess)
        self.events, n_played = merge_playback(self.events, playback_events(sess, P.audio))
        self.coverage = load_sound_coverage(sess)
        self.proposals = self._load_proposals()
        self.selected: int | None = None
        self.pending_start: float | None = None
        self.dirty = n_played > 0
        self._last_focus = None
        self.player = AudioPlayer(AudioSource(sess, P.audio))  # own file handles: it reads in its own thread
        fmax = float((rig.get("audio") or {}).get("display_fmax_hz", 10000))
        self.lanes = SoundLanes(self.src, win.plot, fmax_hz=fmax)
        self.lanes.on_click = self._lane_click
        self.lanes.on_region = self._region_moved
        self.panel = self._build_panel()
        self._redraw()
        w = Worker(lambda: AudioSource(sess, P.audio).envelope())
        w.done.connect(self._envelope_ready)
        w.failed.connect(lambda e: win.statusBar().showMessage(f"Audio overview failed: {e}", 8000))
        win._workers.append(w)
        w.start()

    # ------------------------------------------------------------------ panel
    def _build_panel(self) -> QtWidgets.QGroupBox:
        g = QtWidgets.QGroupBox("Sound events")
        f = QtWidgets.QGridLayout(g)
        nofocus = QtCore.Qt.FocusPolicy.NoFocus
        self.cb_active = QtWidgets.QCheckBox("Labeling sounds (playback counts as reviewed audio)")
        self.cb_active.setChecked(True)
        f.addWidget(self.cb_active, 0, 0, 1, 3)
        self.type_box = QtWidgets.QComboBox()
        self.type_box.addItems(self.types)
        self.type_box.currentTextChanged.connect(lambda v: self._edit_selected(type=v))
        b_types = QtWidgets.QPushButton("Types…")
        b_types.clicked.connect(self.edit_types)
        f.addWidget(QtWidgets.QLabel("Type (F1–F9)"), 1, 0)
        f.addWidget(self.type_box, 1, 1)
        f.addWidget(b_types, 1, 2)
        self.emitter_box = QtWidgets.QComboBox()
        self.emitter_box.addItems(self.emitters)
        self.emitter_box.setCurrentText(UNKNOWN)
        self.emitter_box.currentTextChanged.connect(lambda v: self._edit_selected(emitter=v))
        f.addWidget(QtWidgets.QLabel("Emitter (Alt+1–9)"), 2, 0)
        f.addWidget(self.emitter_box, 2, 1, 1, 2)
        row = QtWidgets.QHBoxLayout()
        for label, fn in (("Start (S)", self.mark_start), ("End (E)", self.mark_end), ("Delete", self.delete_selected)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addLayout(row, 3, 0, 1, 3)
        self.sugg_label = QtWidgets.QLabel("")
        self.sugg_label.setWordWrap(True)
        self.sugg_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.sugg_label, 4, 0, 1, 3)
        b_sugg = QtWidgets.QPushButton("Use top suggestion (A)")
        b_sugg.clicked.connect(self.accept_suggestion)
        f.addWidget(b_sugg, 5, 0, 1, 3)
        row = QtWidgets.QHBoxLayout()
        self.b_detect = QtWidgets.QPushButton("Detect proposals")
        self.b_detect.clicked.connect(self.detect)
        b_yes = QtWidgets.QPushButton("Accept (Y)")
        b_yes.clicked.connect(self.accept_proposal)
        b_no = QtWidgets.QPushButton("Reject (R)")
        b_no.clicked.connect(self.reject_proposal)
        for b in (self.b_detect, b_yes, b_no):
            row.addWidget(b)
        f.addLayout(row, 6, 0, 1, 3)
        self.cb_listen = QtWidgets.QCheckBox("Play audio with the video")
        self.listen_box = QtWidgets.QComboBox()
        self.listen_box.addItems(self.src.mics + (["both (stereo)"] if len(self.src.mics) >= 2 else []))
        b_listen = QtWidgets.QPushButton("Listen (L)")
        b_listen.clicked.connect(self.listen)
        f.addWidget(self.cb_listen, 7, 0, 1, 2)
        f.addWidget(self.listen_box, 8, 0, 1, 2)
        f.addWidget(b_listen, 8, 2)
        row = QtWidgets.QHBoxLayout()
        for label, fn in (("Import…", self.import_file), ("Export…", self.export)):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addLayout(row, 9, 0, 1, 3)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.info, 10, 0, 1, 3)
        for w in g.findChildren(QtWidgets.QWidget):
            if isinstance(w, (QtWidgets.QPushButton, QtWidgets.QCheckBox, QtWidgets.QComboBox)):
                w.setFocusPolicy(nofocus)
        from .tips import SOUND, SOUND_ATTR, apply_tips

        apply_tips(g, SOUND, SOUND_ATTR, self)
        return g

    # ------------------------------------------------------------------ helpers
    def _color(self, e: SoundEvent) -> str:
        if e.emitter.startswith(SYSTEM):
            return "#52514e"
        if e.emitter == UNKNOWN:
            return "#898781"
        return self.win.tags.color(e.emitter)

    def _redraw(self) -> None:
        self.events.sort(key=lambda e: e.start_s)
        self.lanes.draw_events(self.events, [(r.start_s, r.stop_s) for r in self.proposals.itertuples()],
                               self.selected, self._color)
        self._update_info()

    def _update_info(self) -> None:
        cov = sum(b - a for a, b in self.coverage)
        lines = [f"{len(self.events)} event(s), {len(self.proposals)} proposal(s), reviewed {cov / 60:.1f} min"
                 + (" · unsaved" if self.dirty else "")]
        if self.selected is not None and self.selected < len(self.events):
            e = self.events[self.selected]
            lines.append(f"selected: <b>{e.type}</b> · <b>{e.emitter}</b> {e.start_s:.2f}–{e.stop_s:.2f} s "
                         f"({e.duration_s * 1e3:.0f} ms, {e.source})")
        if self.pending_start is not None:
            lines.append(f"<span style='color:#e34948'>start marked at {self.pending_start:.2f} s, press E</span>")
        self.info.setText("<br>".join(lines))

    def _event_at(self, t: float) -> int | None:
        hits = [i for i, e in enumerate(self.events) if e.start_s <= t <= e.stop_s]
        return hits[-1] if hits else None

    def _proposal_at(self, t: float, forward: bool = True) -> int | None:
        p = self.proposals
        if not len(p):
            return None
        inside = np.flatnonzero((p["start_s"].to_numpy() <= t) & (p["stop_s"].to_numpy() >= t))
        if len(inside):
            return int(inside[0])
        if forward:
            ahead = np.flatnonzero(p["start_s"].to_numpy() > t)
            return int(ahead[0]) if len(ahead) else None
        return None

    def select(self, i: int | None) -> None:
        self.selected = i
        if i is not None:
            e = self.events[i]
            for box, val in ((self.type_box, e.type), (self.emitter_box, e.emitter)):
                box.blockSignals(True)
                if box.findText(val) < 0:
                    box.addItem(val)
                box.setCurrentText(val)
                box.blockSignals(False)
            self._suggest(e.start_s, e.stop_s)
        self._redraw()

    def _edit_selected(self, **kw) -> None:
        if self.selected is None:
            return
        e = self.events[self.selected]
        for k, v in kw.items():
            setattr(e, k, v)
        self.dirty = True
        self._redraw()

    # ------------------------------------------------------------------ time hooks
    def on_time(self, t: float) -> None:
        self.lanes.set_time(t)
        i = self._event_at(t)
        focus = ("e", i) if i is not None else None
        if focus is None:
            j = self._proposal_at(t, forward=False)
            focus = ("p", j) if j is not None else None
        if focus != self._last_focus:
            self._last_focus = focus
            if focus and focus[0] == "e":
                e = self.events[focus[1]]
                self._suggest(e.start_s, e.stop_s)
            elif focus:
                r = self.proposals.iloc[focus[1]]
                self._suggest(float(r["start_s"]), float(r["stop_s"]))
            else:
                self.sugg_label.setText("")

    def watched(self, a: float, b: float) -> None:
        if self.cb_active.isChecked():
            from ..analysis.labels_io import merge_spans

            self.coverage = merge_spans(self.coverage + [(a, b)])
            self.dirty = True

    def on_play(self, playing: bool, t: float, speed: float) -> None:
        if not playing or not self.cb_listen.isChecked():
            self.player.stop()
            return
        try:
            self.player.start(t, speed, self._listen_cols())
        except Exception as e:
            self.cb_listen.setChecked(False)
            message(self.win, "Audio playback", f"Cannot play audio: {e}", error=True)

    def _listen_cols(self) -> list[int]:
        i = self.listen_box.currentIndex()
        return self.src.mic_idx[:2] if i >= len(self.src.mics) else [self.src.mic_idx[i]]

    # ------------------------------------------------------------------ suggestions
    def _suggest(self, t0: float, t1: float) -> None:
        try:
            m = measure_event(self.src, t0, t1)
        except Exception as ex:
            self.sugg_label.setText(f"<small>no measurement: {ex}</small>")
            self._sugg = []
            return
        perched = occupancy(self.win.P.bouts, t0, t1)
        played = overlapping_playback(self.events, t0, t1)
        self._sugg = suggest_emitters(m, perched, self.src.mic_near, played)
        lv = " · ".join(f"{k} {v:.0f} dBFS" for k, v in m["levels_db"].items())
        rows = [f"<b>{em}</b> <small>({why})</small>" if why else f"<b>{em}</b>" for em, _s, why in self._sugg[:3]]
        self.sugg_label.setText("<small>suggested emitter:</small><br>" + "<br>".join(rows)
                                + f"<br><small>{lv}</small>")

    def accept_suggestion(self) -> None:
        if getattr(self, "_sugg", None):
            self.emitter_box.setCurrentText(self._sugg[0][0])  # also edits the selected event

    # ------------------------------------------------------------------ editing
    def mark_start(self) -> None:
        self.pending_start = self.win.t
        self.lanes.set_pending(self.pending_start)
        self._update_info()

    def mark_end(self) -> None:
        t = self.win.t
        if self.pending_start is None:
            if self.selected is not None:  # move the end of the selected event
                e = self.events[self.selected]
                if t > e.start_s:
                    e.stop_s = t
                    self.dirty = True
                    self._redraw()
            return
        a, b = sorted((self.pending_start, t))
        if b - a < 0.005:
            b = a + 0.005
        e = SoundEvent(a, b, self.type_box.currentText(), self.emitter_box.currentText(),
                       annotator=self.win.annotator)
        self.events.append(e)
        self.pending_start = None
        self.lanes.set_pending(None)
        self.dirty = True
        self.events.sort(key=lambda x: x.start_s)
        self.select(self.events.index(e))

    def delete_selected(self) -> None:
        if self.selected is None:
            return
        del self.events[self.selected]
        self.selected = None
        self.dirty = True
        self._redraw()

    def set_type(self, i: int) -> None:
        if i < self.type_box.count():
            self.type_box.setCurrentIndex(i)

    def set_emitter(self, name: str) -> None:
        if self.emitter_box.findText(name) >= 0:
            self.emitter_box.setCurrentText(name)

    def _region_moved(self, i: int, a: float, b: float) -> None:
        if i < len(self.events):
            e = self.events[i]
            e.start_s, e.stop_s = float(a), float(b)
            self.dirty = True
            self.events.sort(key=lambda x: x.start_s)
            self.selected = self.events.index(e)
            self._redraw()

    def _lane_click(self, t: float) -> None:
        self.win.seek_time(t)
        self.select(self._event_at(t))

    def jump(self, direction: int) -> None:
        t = self.win.t
        ts = sorted({e.start_s for e in self.events} | set(self.proposals["start_s"].tolist()))
        cand = [x for x in ts if x > t + 1e-3] if direction > 0 else [x for x in ts if x < t - 1e-3]
        if cand:
            target = cand[0] if direction > 0 else cand[-1]
            self.win.seek_time(target + 1e-4)
            self.select(self._event_at(target + 1e-4))

    # ------------------------------------------------------------------ proposals
    def _load_proposals(self) -> pd.DataFrame:
        p = self.sess.analysis_dir / PROPOSALS_FILE
        df = pd.read_csv(p) if p.exists() else pd.DataFrame(columns=["start_s", "stop_s", "peak_db", "loudest"])
        return self._without_labeled(df)

    def _without_labeled(self, df: pd.DataFrame) -> pd.DataFrame:
        if not len(df) or not self.events:
            return df.reset_index(drop=True)
        keep = [not any(e.start_s < r.stop_s and e.stop_s > r.start_s for e in self.events) for r in df.itertuples()]
        return df[keep].reset_index(drop=True)

    def _save_proposals(self) -> None:
        self.proposals.to_csv(self.sess.analysis_dir / PROPOSALS_FILE, index=False)

    def detect(self) -> None:
        self.b_detect.setEnabled(False)
        dlg = QtWidgets.QProgressDialog("Detecting sound events …", None, 0, 100, self.win)
        dlg.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dlg.show()
        w = Worker(lambda: detect_events(AudioSource(self.sess, self.win.P.audio),
                                         progress=lambda f: w.progress.emit(f)))
        w.progress.connect(lambda f: dlg.setValue(int(f * 100)))

        def done(df):
            dlg.close()
            self.b_detect.setEnabled(True)
            self.proposals = self._without_labeled(df)
            self._save_proposals()
            self._redraw()
            self.win.statusBar().showMessage(f"{len(df)} proposals, {len(self.proposals)} not yet labeled. "
                                             "Y accepts, R rejects, . jumps to the next.", 8000)

        w.done.connect(done)
        w.failed.connect(lambda e: (dlg.close(), self.b_detect.setEnabled(True),
                                    message(self.win, "Detection failed", e, error=True)))
        self.win._workers.append(w)
        w.start()

    def accept_proposal(self) -> None:
        j = self._proposal_at(self.win.t)
        if j is None:
            self.win.statusBar().showMessage("No proposal at or after the playhead", 3000)
            return
        r = self.proposals.iloc[j]
        e = SoundEvent(float(r["start_s"]), float(r["stop_s"]), self.type_box.currentText(),
                       self.emitter_box.currentText(), channel=str(r.get("loudest", "")), source="proposal",
                       annotator=self.win.annotator)
        self.events.append(e)
        self.proposals = self.proposals.drop(self.proposals.index[j]).reset_index(drop=True)
        self._save_proposals()
        self.dirty = True
        self.events.sort(key=lambda x: x.start_s)
        self.win.seek_time(e.start_s + 1e-4)
        self.select(self.events.index(e))

    def reject_proposal(self) -> None:
        j = self._proposal_at(self.win.t)
        if j is None:
            return
        self.proposals = self.proposals.drop(self.proposals.index[j]).reset_index(drop=True)
        self._save_proposals()
        self._redraw()
        self.jump(+1)

    # ------------------------------------------------------------------ listening
    def listen(self) -> None:
        t = self.win.t
        if self.selected is not None:
            e = self.events[self.selected]
            a, b = e.start_s - 0.3, e.stop_s + 0.3
        else:
            a, b = t - 0.5, t + 2.0
        try:
            self.player.start(a, self.win.speed, self._listen_cols(), stop_t=b)
        except Exception as ex:
            message(self.win, "Audio playback", f"Cannot play audio: {ex}", error=True)

    def _envelope_ready(self, env: dict) -> None:
        self.lanes.set_envelope(env)
        peak = float(np.percentile(np.abs(np.concatenate([env["hi"].ravel(), env["lo"].ravel()])), 99.9)) \
            if env["hi"].size else 1.0
        self.player.gain = float(np.clip(0.5 / max(peak, 1e-6), 1.0, 30.0))  # quiet recordings: louder playback

    # ------------------------------------------------------------------ types, import / export, save
    def edit_types(self) -> None:
        dlg = TypesDialog(self.win, self.types)
        if dlg.exec():
            self.types = dlg.types() or self.types
            save_types(self.types_path, self.types)
            cur = self.type_box.currentText()
            self.type_box.blockSignals(True)
            self.type_box.clear()
            self.type_box.addItems(self.types)
            self.type_box.setCurrentText(cur if cur in self.types else self.types[0])
            self.type_box.blockSignals(False)

    def export(self) -> None:
        fmt, ok = QtWidgets.QInputDialog.getItem(self.win, "Export sound events", "Format:",
                                                 ["audacity", "raven"], 0, False)
        if not ok:
            return
        paths = export_events(self.sess, self.win.P.audio, self.events, fmt)
        message(self.win, "Exported", "\n".join(str(p) for p in paths))

    def import_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.win, "Import sound labels", str(self.sess.path),
                                                        "Labels (*.txt);;All files (*)")
        if not path:
            return
        idx = 0
        files = self.sess.audio["files"]
        if len(files) > 1:
            names = [f["file"] for f in files]
            name, ok = QtWidgets.QInputDialog.getItem(self.win, "Audio file", "Labels were made on:", names, 0, False)
            if not ok:
                return
            idx = names.index(name)
        new = import_events(self.sess, self.win.P.audio, path, file_index=idx, annotator=self.win.annotator)
        self.events += new
        self.dirty = True
        self._redraw()
        self.win.statusBar().showMessage(f"Imported {len(new)} sound events", 5000)

    def save(self) -> None:
        save_events(self.sess, self.events)
        save_sound_coverage(self.sess, self.coverage, self.win.annotator)
        self.dirty = False
        self._update_info()

    def close(self) -> None:
        self.player.stop()
        self.player.src.close()
        self.src.close()
