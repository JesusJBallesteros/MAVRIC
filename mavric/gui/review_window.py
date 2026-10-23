"""Review GUI: blind ground-truth labeling of perch occupancy, RFID curation, sound events, analysis.

Labels are *states per ROI*: pressing a pigeon key at time t means "from t on,
this pigeon is on the selected perch" until the next change; 0 = empty.
In blind mode RFID is hidden so the ground truth is not biased by it; curate
mode reveals RFID, jumps between disagreements and lets you mark RFID bouts
as spurious or valid. Time the annotator actually watched is tracked
automatically and saved as the labeled coverage.

Sessions with audio get waveform and spectrogram lanes under the timeline and
a Sound events panel (mavric/gui/sound_panel.py): listening, start/end marking,
event types, emitter suggestions from the two microphones and RFID, automatic
proposals, Audacity/Raven exchange.
"""

from __future__ import annotations

import bisect
import sys

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ..analysis.compare import occupancy
from ..video.reader import open_session_video
from ..analysis.labels_io import (
    changes_to_intervals,
    intervals_to_changes,
    load_coverage,
    load_labels,
    merge_spans,
    save_coverage,
    save_labels,
)
from ..analysis.pipeline import prepare
from ..config import EMPTY
from ..session import CURATION_FILE, Session
from .common import GRAY, RED, VideoView, Worker, message, qcolor

SPEEDS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]


class ReviewWindow(QtWidgets.QMainWindow):
    def __init__(self, session: Session, annotator: str = ""):
        super().__init__()
        self.sess = session
        self.annotator = annotator
        self.setWindowTitle(f"MAVRIC - Review - {session.path.name}")
        self.resize(1500, 950)

        self.P = prepare(session)
        self.tags = session.tags
        self.pigeons = self.tags.pigeons
        self.roi_objs = session.rois.perches
        self.rois = [r.name for r in self.roi_objs] or list(session.antennas.values())
        self.src = open_session_video(session)  # raw chunks or a single video
        fps = float(session.camera().get("fps", 30))
        self.fps = fps
        ft = self.P.frame_t if self.P.frame_t is not None else np.arange(self.src.n) / fps
        self.frame_t = np.asarray(ft[: self.src.n] if len(ft) >= self.src.n else ft, float)
        self.n_frames = len(self.frame_t)
        self.t_end = float(self.frame_t[-1] + 1 / fps) if self.n_frames else 0.0

        labels = load_labels(session)
        self.changes = intervals_to_changes(labels, self.rois) if labels is not None else {r: [] for r in self.rois}
        for r in self.rois:
            self.changes.setdefault(r, [])
        self.flags: dict[str, list[tuple[float, str]]] = {r: [] for r in self.rois}
        if labels is not None:
            for _, row in labels[labels["flag"] != ""].iterrows():
                self.flags.setdefault(row["roi"], []).append((row["start_s"], row["flag"]))
        self.coverage = load_coverage(session)
        self.curation = self._load_curation()
        self.dirty = False

        self.frame = 0
        self.sel = 0
        self.blind = True
        self.playing = False
        self.speed = 1.0
        self._watch_from: float | None = None
        self._workers: list[Worker] = []

        self._build()
        self._shortcuts()
        self.play_timer = QtCore.QTimer(self)
        self.play_timer.timeout.connect(self._play_step)
        self.autosave = QtCore.QTimer(self)
        self.autosave.timeout.connect(lambda: self.save(quiet=True) if self._unsaved() else None)
        self.autosave.start(60_000)
        self._rebuild_timeline()
        self.seek_frame(0)

    # ================================================================== UI
    def _build(self) -> None:
        self.video = VideoView()
        self.video.load_rois(self.sess.rois, editable=False)

        self.plot = pg.PlotWidget()
        self.plot.setBackground("#fcfcfb")
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.invertY(True)
        self.plot.showGrid(x=True, y=False, alpha=0.25)
        self.plot.setLabel("bottom", "time (s)")
        self.plot.setMinimumHeight(60 + 34 * len(self.rois))
        self.plot.setMaximumHeight(90 + 46 * len(self.rois))
        self.playhead = pg.InfiniteLine(0, angle=90, movable=True, pen=pg.mkPen("#0b0b0b", width=1.5))
        self.playhead.setZValue(50)
        self.playhead.sigDragged.connect(lambda l: self.seek_time(l.value()))
        self.plot.addItem(self.playhead)
        self.plot.scene().sigMouseClicked.connect(self._timeline_click)
        self.sel_band = pg.LinearRegionItem(orientation="horizontal", movable=False,
                                            brush=qcolor("#2a78d6", 28), pen=pg.mkPen(None))
        self.sel_band.setZValue(-10)
        self.plot.addItem(self.sel_band)
        self._bar_items: list = []
        self._cov_items: list = []
        self.plot.setXRange(0, min(self.t_end, 120))
        self.sound = None
        if self.sess.audio and self.P.audio is not None:
            try:
                from .sound_panel import AXIS_W, SoundLabeler

                self.plot.getAxis("left").setWidth(AXIS_W)
                self.sound = SoundLabeler(self)
            except Exception as e:  # labeling perches must keep working without the audio
                self.sound = None
                print(f"[review] sound lanes unavailable: {type(e).__name__}: {e}", file=sys.stderr)

        side = QtWidgets.QWidget()
        side.setFixedWidth(370)
        v = QtWidgets.QVBoxLayout(side)
        info = QtWidgets.QLabel(f"<b>{self.sess.name}</b><br>{self.sess.path.name}<br>"
                                f"{self.n_frames} frames, {self.t_end / 60:.1f} min")
        info.setWordWrap(True)
        v.addWidget(info)

        g = QtWidgets.QGroupBox("Mode")
        h = QtWidgets.QVBoxLayout(g)
        self.rb_blind = QtWidgets.QRadioButton("Blind labeling (RFID hidden)")
        self.rb_cur = QtWidgets.QRadioButton("Curate (show RFID, find disagreements)")
        self.rb_blind.setChecked(True)
        self.rb_cur.toggled.connect(lambda on: self.set_blind(not on))
        for w in (self.rb_blind, self.rb_cur):
            w.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            h.addWidget(w)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Place (Up/Down)")
        h = QtWidgets.QVBoxLayout(g)
        self.roi_list = QtWidgets.QListWidget()
        self.roi_list.addItems(self.rois)
        self.roi_list.setFixedHeight(max(24, self.roi_list.sizeHintForRow(0)) * len(self.rois) + 8)
        self.roi_list.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.roi_list.currentRowChanged.connect(self.select_roi)
        h.addWidget(self.roi_list)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Subject keys")
        h = QtWidgets.QVBoxLayout(g)
        keys = "".join(
            f"<span style='background:{self.tags.color(p)}'>&nbsp;&nbsp;&nbsp;</span> <b>{i + 1}</b> {p}&nbsp;&nbsp; "
            for i, p in enumerate(self.pigeons[:9]))
        lab = QtWidgets.QLabel(keys + "<br><b>0</b> empty &nbsp; <b>Del</b> remove change &nbsp; <b>T</b> two-subjects flag")
        lab.setWordWrap(True)
        h.addWidget(lab)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Now")
        h = QtWidgets.QVBoxLayout(g)
        self.now_label = QtWidgets.QLabel("")
        self.now_label.setWordWrap(True)
        self.now_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        h.addWidget(self.now_label)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Playback")
        h = QtWidgets.QHBoxLayout(g)
        self.b_play = QtWidgets.QPushButton("▶ Play")
        self.b_play.clicked.connect(self.toggle_play)
        self.speed_box = QtWidgets.QComboBox()
        for s in SPEEDS:
            self.speed_box.addItem(f"{s:g}×", s)
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_box.currentIndexChanged.connect(lambda i: self.set_speed(SPEEDS[i]))
        self.cb_follow = QtWidgets.QCheckBox("follow")
        self.cb_follow.setChecked(True)
        for w in (self.b_play, self.speed_box, self.cb_follow):
            w.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            h.addWidget(w)
        v.addWidget(g)

        g = QtWidgets.QGroupBox("Session")
        h = QtWidgets.QGridLayout(g)
        buttons = [("Save (Ctrl+S)", self.save), ("Analyze", self.analyze), ("Render annotated video", self.render),
                   ("Reset reviewed spans", self.reset_coverage), ("Compress videos…", self.compress)]
        for i, (txt, fn) in enumerate(buttons):
            b = QtWidgets.QPushButton(txt)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            b.clicked.connect(fn)
            h.addWidget(b, i // 2, i % 2)
        v.addWidget(g)

        if self.sound is not None:
            v.addWidget(self.sound.panel)
        sound_help = ("<br>Sounds: S start · E end · A top suggestion · Y/R accept/reject proposal · , . prev/next · "
                      "L listen · Shift+Del delete · F1–F9 type · Alt+1–9 emitter, Alt+0 unknown"
                      if self.sound is not None else "")
        help_ = QtWidgets.QLabel(
            "<small>Space play/pause · ←/→ 1 frame · Shift ±1 s · Ctrl ±10 s · [ ] speed<br>"
            "N/P next/prev change (this place) · D next disagreement (curate)<br>"
            "X mark RFID bout spurious · V mark valid (curate) · Ctrl+S save<br>"
            "Click or drag on the timeline to seek; wheel zooms." + sound_help + "</small>")
        help_.setWordWrap(True)
        v.addWidget(help_)
        v.addStretch(1)
        from .help_dialogs import help_about_row

        v.addLayout(help_about_row(self, "review"))

        left = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        left.addWidget(self.video)
        left.addWidget(self.plot)
        if self.sound is not None:
            left.addWidget(self.sound.lanes)
            left.setSizes([520, 160, 300])
        left.setStretchFactor(0, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(side.width() + 22)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(w)
        hl.addWidget(left, 1)
        hl.addWidget(scroll)
        self.setCentralWidget(w)
        self.roi_list.setCurrentRow(0)
        from .tips import REVIEW, REVIEW_ATTR, apply_tips

        apply_tips(self, REVIEW, REVIEW_ATTR, self)

    def _shortcuts(self) -> None:
        def sc(key, fn):
            s = QtGui.QShortcut(QtGui.QKeySequence(key), self)
            s.setContext(QtCore.Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(fn)

        sc("Space", self.toggle_play)
        sc("Right", lambda: self.step(1))
        sc("Left", lambda: self.step(-1))
        sc("Shift+Right", lambda: self.seek_time(self.t + 1, watched=True))
        sc("Shift+Left", lambda: self.seek_time(self.t - 1))
        sc("Ctrl+Right", lambda: self.seek_time(self.t + 10))
        sc("Ctrl+Left", lambda: self.seek_time(self.t - 10))
        sc("Up", lambda: self.roi_list.setCurrentRow(max(0, self.sel - 1)))
        sc("Down", lambda: self.roi_list.setCurrentRow(min(len(self.rois) - 1, self.sel + 1)))
        for i in range(min(9, len(self.pigeons))):
            sc(str(i + 1), lambda p=self.pigeons[i]: self.set_state(p))
        sc("0", lambda: self.set_state(EMPTY))
        sc("Delete", self.delete_change)
        sc("Backspace", self.delete_change)
        sc("T", self.toggle_two_birds)
        sc("N", lambda: self.jump_change(+1))
        sc("P", lambda: self.jump_change(-1))
        sc("D", self.next_disagreement)
        sc("X", lambda: self.curate("spurious"))
        sc("V", lambda: self.curate("valid"))
        sc("[", lambda: self.speed_box.setCurrentIndex(max(0, self.speed_box.currentIndex() - 1)))
        sc("]", lambda: self.speed_box.setCurrentIndex(min(len(SPEEDS) - 1, self.speed_box.currentIndex() + 1)))
        sc("Ctrl+S", self.save)
        snd = self.sound
        if snd is not None:
            sc("S", snd.mark_start)
            sc("E", snd.mark_end)
            sc("A", snd.accept_suggestion)
            sc("Y", snd.accept_proposal)
            sc("R", snd.reject_proposal)
            sc("L", snd.listen)
            sc(",", lambda: snd.jump(-1))
            sc(".", lambda: snd.jump(+1))
            sc("Shift+Delete", snd.delete_selected)
            for i in range(9):
                sc(f"F{i + 1}", lambda k=i: snd.set_type(k))
            for i in range(min(9, len(self.pigeons))):
                sc(f"Alt+{i + 1}", lambda p=self.pigeons[i]: snd.set_emitter(p))
            sc("Alt+0", lambda: snd.set_emitter("unknown"))

    # ================================================================== time / playback
    @property
    def t(self) -> float:
        return float(self.frame_t[self.frame]) if self.n_frames else 0.0

    def frame_at(self, t: float) -> int:
        i = int(np.searchsorted(self.frame_t, t, side="right")) - 1
        return int(np.clip(i, 0, self.n_frames - 1))

    def seek_frame(self, idx: int, watched: bool = False) -> None:
        idx = int(np.clip(idx, 0, self.n_frames - 1))
        t_prev = self.t
        self.frame = idx
        if watched:
            self._track_watch(t_prev, self.t)
        else:
            self._watch_from = None
        img = self.src.get(idx)
        if img is not None:
            self.video.set_image(img)
        self._refresh_now()
        if self.sound is not None:
            self.sound.on_time(self.t)
        if self.playing and not watched:  # a jump while playing: video and audio continue from here
            self._play_t0 = self.t
            self._play_clock.restart()
            if self.sound is not None:
                self.sound.on_play(True, self.t, self.speed)

    def seek_time(self, t: float, watched: bool = False) -> None:
        self.seek_frame(self.frame_at(t), watched=watched)

    def step(self, n: int) -> None:
        self.seek_frame(self.frame + n, watched=n > 0)

    def toggle_play(self) -> None:
        self.playing = not self.playing
        self.b_play.setText("❚❚ Pause" if self.playing else "▶ Play")
        if self.playing:
            self._play_clock = QtCore.QElapsedTimer()
            self._play_clock.start()
            self._play_t0 = self.t
            self.play_timer.start(max(10, int(1000 / min(60.0, self.fps * self.speed))))
        else:
            self.play_timer.stop()
        if self.sound is not None:
            self.sound.on_play(self.playing, self.t, self.speed)

    def set_speed(self, s: float) -> None:
        self.speed = s
        if self.playing:
            self.toggle_play()
            self.toggle_play()

    def _play_step(self) -> None:
        target = self._play_t0 + self._play_clock.elapsed() / 1000 * self.speed
        if target >= self.t_end:
            self.toggle_play()
            return
        self.seek_frame(self.frame_at(target), watched=True)

    def _track_watch(self, t_prev: float, t_new: float) -> None:
        """Contiguous forward viewing extends the reviewed coverage."""
        if t_new <= t_prev or t_new - t_prev > 2.0:
            self._watch_from = None
            return
        if self._watch_from is None:
            self._watch_from = t_prev
        self.coverage = merge_spans(self.coverage + [(self._watch_from, t_new)])
        if self.sound is not None:
            self.sound.watched(self._watch_from, t_new)
        self._watch_from = t_new
        self.dirty = True
        if int(t_new) != int(t_prev):
            self._rebuild_timeline()

    # ================================================================== labels
    def state_at(self, roi: str, t: float) -> str:
        pts = self.changes.get(roi, [])
        i = bisect.bisect_right([p[0] for p in pts], t + 1e-9) - 1
        return pts[i][1] if i >= 0 else EMPTY

    def set_state(self, who: str) -> None:
        roi = self.rois[self.sel]
        t = self.t
        half = 0.5 / self.fps
        pts = [p for p in self.changes[roi] if abs(p[0] - t) > half]
        pts.append((t, who))
        pts.sort()
        clean = []  # drop change points that do not change anything
        for p in pts:
            prev = clean[-1][1] if clean else EMPTY
            if p[1] != prev:
                clean.append(p)
        self.changes[roi] = clean
        self.dirty = True
        self._rebuild_timeline(only_labels=True)
        self._refresh_now()

    def delete_change(self) -> None:
        roi = self.rois[self.sel]
        pts = self.changes[roi]
        i = bisect.bisect_right([p[0] for p in pts], self.t + 1e-9) - 1
        if i >= 0:
            del pts[i]
            self.dirty = True
            self._rebuild_timeline(only_labels=True)
            self._refresh_now()

    def toggle_two_birds(self) -> None:
        roi = self.rois[self.sel]
        fl = self.flags.setdefault(roi, [])
        near = [f for f in fl if abs(f[0] - self.t) < 0.5]
        if near:
            fl.remove(near[0])
        else:
            fl.append((self.t, "two_birds"))
        self.dirty = True
        self._refresh_now()

    def jump_change(self, direction: int) -> None:
        roi = self.rois[self.sel]
        ts = [p[0] for p in self.changes[roi]]
        if not self.blind and len(self.P.bouts):
            b = self.P.bouts[self.P.bouts["roi"] == roi]
            ts += b["start_s"].tolist() + b["stop_s"].tolist()
        ts = sorted(ts)
        t = self.t
        cand = [x for x in ts if x > t + 1e-3] if direction > 0 else [x for x in ts if x < t - 1e-3]
        if cand:
            self.seek_time(cand[0] if direction > 0 else cand[-1])

    def labels_df(self) -> pd.DataFrame:
        """Label intervals, clipped to the reviewed coverage (a state only counts where it was watched)."""
        df = changes_to_intervals(self.changes, self.t_end, self.annotator, self.flags)
        if not self.coverage or not len(df):
            return df
        parts = []
        for a, b in self.coverage:
            c = df[(df["stop_s"] > a) & (df["start_s"] < b)].copy()
            c["start_s"] = c["start_s"].clip(lower=a)
            c["stop_s"] = c["stop_s"].clip(upper=b)
            parts.append(c)
        return pd.concat(parts, ignore_index=True) if parts else df.iloc[0:0]

    # ================================================================== curation
    def _load_curation(self) -> pd.DataFrame:
        p = self.sess.file(CURATION_FILE)
        cols = ["antenna", "tag", "t", "start_s", "stop_s", "verdict"]
        return pd.read_csv(p) if p.exists() else pd.DataFrame(columns=cols)

    def _bouts_at(self, roi: str, t: float) -> pd.DataFrame:
        b = self.P.bouts
        return b[(b["roi"] == roi) & (b["start_s"] <= t) & (b["stop_s"] > t)]

    def curate(self, verdict: str) -> None:
        if self.blind:
            self.statusBar().showMessage("Curation is available in curate mode", 3000)
            return
        roi = self.rois[self.sel]
        hit = self._bouts_at(roi, self.t)
        if not len(hit):
            self.statusBar().showMessage(f"No RFID bout at {roi} now", 3000)
            return
        for _, b in hit.iterrows():
            mid = (b["start_s"] + b["stop_s"]) / 2
            cur = self.curation
            keep = ~((cur["antenna"] == b["antenna"]) & (cur["tag"] == b["tag"]) &
                     (cur["t"] > b["start_s"]) & (cur["t"] < b["stop_s"]))
            row = pd.DataFrame([{"antenna": b["antenna"], "tag": b["tag"], "t": mid, "start_s": b["start_s"],
                                 "stop_s": b["stop_s"], "verdict": verdict}])
            self.curation = pd.concat([cur[keep], row], ignore_index=True)
            self.P.bouts.loc[hit.index, "curated"] = verdict
        self.dirty = True
        self._rebuild_timeline()
        self.statusBar().showMessage(f"RFID bout at {roi} marked {verdict}", 3000)

    def next_disagreement(self) -> None:
        if self.blind:
            self.statusBar().showMessage("Switch to curate mode to see disagreements", 3000)
            return
        cov = self.coverage or [(0.0, self.t_end)]
        step = 0.25
        centers = np.concatenate([np.arange(a + step / 2, b, step) for a, b in cov])
        centers = centers[centers > self.t + 0.5]
        if not len(centers):
            self.statusBar().showMessage("No disagreement ahead in the reviewed spans", 3000)
            return
        lab = occupancy(self.labels_df(), self.rois, centers)
        rf = occupancy(self.P.bouts[self.P.bouts["curated"] != "spurious"], self.rois, centers)
        first, which = None, None
        for i, r in enumerate(self.rois):
            d = np.flatnonzero(lab[r] != rf[r])
            if len(d) and (first is None or d[0] < first):
                first, which = int(d[0]), i
        if first is None:
            self.statusBar().showMessage("No disagreement ahead in the reviewed spans", 3000)
            return
        self.roi_list.setCurrentRow(which)
        self.seek_time(centers[first] - step / 2)

    # ================================================================== drawing
    def select_roi(self, i: int) -> None:
        if i < 0:
            return
        self.sel = i
        y0 = self._lane_y(i, 0) - 0.5
        self.sel_band.setRegion((y0, y0 + (1 if self.blind else 2)))
        self._refresh_now()

    def set_blind(self, blind: bool) -> None:
        self.blind = blind
        self._rebuild_timeline()
        self.select_roi(self.sel)

    def _lane_y(self, i: int, kind: int) -> float:
        return i * (1 if self.blind else 2.4) + kind

    def _rebuild_timeline(self, only_labels: bool = False) -> None:
        for it in self._bar_items:
            self.plot.removeItem(it)
        self._bar_items = []
        h = 0.7
        lab = self.labels_df()
        for i, roi in enumerate(self.rois):
            g = lab[lab["roi"] == roi]
            for pig, gg in g.groupby("pigeon"):
                bar = pg.BarGraphItem(x0=gg["start_s"].to_numpy(), width=(gg["stop_s"] - gg["start_s"]).to_numpy(),
                                      y=np.full(len(gg), self._lane_y(i, 0)), height=h,
                                      brush=qcolor(self.tags.color(pig)), pen=pg.mkPen(None))
                self.plot.addItem(bar)
                self._bar_items.append(bar)
            if not self.blind:
                b = self.P.bouts[self.P.bouts["roi"] == roi]
                for (pig, cur), gg in b.groupby(["pigeon", "curated"]):
                    spur = cur == "spurious"
                    bar = pg.BarGraphItem(x0=gg["start_s"].to_numpy(), width=gg["duration_s"].to_numpy(),
                                          y=np.full(len(gg), self._lane_y(i, 1)), height=h * 0.6,
                                          brush=qcolor(GRAY if spur else self.tags.color(pig), 120 if spur else 255),
                                          pen=pg.mkPen(RED if spur else None))
                    self.plot.addItem(bar)
                    self._bar_items.append(bar)
        ticks = []
        for i, roi in enumerate(self.rois):
            ticks.append((self._lane_y(i, 0), f"{roi} label"))
            if not self.blind:
                ticks.append((self._lane_y(i, 1), f"{roi} RFID"))
        ax = self.plot.getAxis("left")
        ax.setTicks([ticks])
        n = len(self.rois)
        self.plot.setYRange(-0.6, self._lane_y(n - 1, 0 if self.blind else 1) + 0.6, padding=0)
        self._draw_coverage()

    def _draw_coverage(self) -> None:
        for it in self._cov_items:
            self.plot.removeItem(it)
        self._cov_items = []
        for a, b in self.coverage:
            r = pg.LinearRegionItem((a, b), movable=False, brush=qcolor("#898781", 30), pen=pg.mkPen(None))
            r.setZValue(-20)
            self.plot.addItem(r)
            self._cov_items.append(r)

    def _refresh_now(self) -> None:
        t = self.t
        self.playhead.setValue(t)
        if self.cb_follow.isChecked():
            x0, x1 = self.plot.viewRange()[0]
            if t < x0 or t > x1 - (x1 - x0) * 0.1:
                w = x1 - x0
                self.plot.setXRange(t - 0.2 * w, t + 0.8 * w, padding=0)
        lines = [f"<b>t = {t:.2f} s</b> &nbsp; frame {self.frame}/{self.n_frames - 1} &nbsp; {self.speed:g}×"]
        b = self.P.bouts
        for i, roi in enumerate(self.rois):
            who = self.state_at(roi, t)
            rf = b[(b["roi"] == roi) & (b["start_s"] <= t) & (b["stop_s"] > t)]
            rf_names = sorted(rf.loc[rf["curated"] != "spurious", "pigeon"].unique().tolist())
            rf_txt = "+".join(rf_names) if rf_names else "-"
            lab_txt = who if who != EMPTY else "-"
            flagged = any(abs(f[0] - t) < 0.5 for f in self.flags.get(roi, []))
            mark = "▶ " if i == self.sel else "&nbsp;&nbsp;&nbsp;"
            disagree = (not self.blind) and ((who if who != EMPTY else None) not in (rf_names or [None]) or
                                             (who == EMPTY and rf_names))
            line = f"{mark}<b>{roi}</b>: label <b>{lab_txt}</b>"
            if not self.blind:
                line += f" &nbsp; RFID <b>{rf_txt}</b>" + (" <span style='color:#e34948'>≠</span>" if disagree else "")
            if flagged:
                line += " (two subjects)"
            lines.append(line)
            if self.blind:
                color = self.tags.color(who) if who != EMPTY else GRAY
                text = f"{roi}: {lab_txt}"
            else:
                color = RED if disagree else (self.tags.color(who) if who != EMPTY else GRAY)
                text = f"{roi}: {lab_txt} | RFID {rf_txt}"
            self.video.set_roi_state(roi, color, 4 if i == self.sel else 2, text)
        cov_s = sum(b2 - a for a, b2 in self.coverage)
        lines.append(f"<small>reviewed: {cov_s / 60:.1f} min{' · unsaved changes' if self.dirty else ''}</small>")
        self.now_label.setText("<br>".join(lines))

    def _timeline_click(self, ev) -> None:
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        pos = self.plot.getViewBox().mapSceneToView(ev.scenePos())
        self.seek_time(pos.x())
        kinds = (0,) if self.blind else (0, 1)
        d = [min(abs(pos.y() - self._lane_y(i, k)) for k in kinds) for i in range(len(self.rois))]
        if d and min(d) < 0.8:
            self.roi_list.setCurrentRow(int(np.argmin(d)))

    # ================================================================== persistence / actions
    def save(self, quiet: bool = False) -> None:
        save_labels(self.sess, self.labels_df())
        save_coverage(self.sess, self.coverage, self.annotator)
        self.curation.to_csv(self.sess.file(CURATION_FILE), index=False)
        if self.sound is not None:
            self.sound.save()
        self.dirty = False
        self._refresh_now()
        if not quiet:
            self.statusBar().showMessage(f"Saved labels ({len(self.labels_df())} intervals), coverage, curation", 4000)

    def reset_coverage(self) -> None:
        if QtWidgets.QMessageBox.question(self, "Reset", "Forget all reviewed spans?") == \
                QtWidgets.QMessageBox.StandardButton.Yes:
            self.coverage = []
            self.dirty = True
            self._draw_coverage()
            self._refresh_now()

    def analyze(self) -> None:
        from ..analysis.pipeline import run_analysis

        self.save(quiet=True)
        w = Worker(run_analysis, self.sess)
        w.done.connect(lambda s: ResultsDialog(self, self.sess, s).exec())
        w.failed.connect(lambda e: message(self, "Analysis failed", e, error=True))
        self._workers.append(w)
        self.statusBar().showMessage("Running analysis ...")
        w.start()

    def render(self) -> None:
        from ..analysis.render import render_annotated

        self.save(quiet=True)
        dlg = QtWidgets.QProgressDialog("Rendering annotated video ...", None, 0, 100, self)
        dlg.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dlg.show()
        w = Worker(lambda: render_annotated(self.P, self.labels_df(), progress=lambda f: w.progress.emit(f)))
        w.progress.connect(lambda f: dlg.setValue(int(f * 100)))
        w.done.connect(lambda p: (dlg.close(), message(self, "Rendered", str(p))))
        w.failed.connect(lambda e: (dlg.close(), message(self, "Render failed", e, error=True)))
        self._workers.append(w)
        w.start()

    def compress(self) -> None:
        from ..config import load_rig
        from ..video.settings import video_cfg
        from .video_dialogs import CompressDialog

        CompressDialog(self, self.sess.path, video_cfg(load_rig())["compress"]).exec()

    def _unsaved(self) -> bool:
        return self.dirty or (self.sound is not None and self.sound.dirty)

    def closeEvent(self, ev: QtGui.QCloseEvent) -> None:
        if self._unsaved():
            r = QtWidgets.QMessageBox.question(
                self, "Unsaved labels", "Save labels before closing?",
                QtWidgets.QMessageBox.StandardButton.Save | QtWidgets.QMessageBox.StandardButton.Discard |
                QtWidgets.QMessageBox.StandardButton.Cancel)
            if r == QtWidgets.QMessageBox.StandardButton.Cancel:
                ev.ignore()
                return
            if r == QtWidgets.QMessageBox.StandardButton.Save:
                self.save(quiet=True)
        if self.sound is not None:
            self.sound.close()
        ev.accept()


class ResultsDialog(QtWidgets.QDialog):
    def __init__(self, parent, sess: Session, summary: dict):
        super().__init__(parent)
        self.setWindowTitle(f"Analysis - {sess.path.name}")
        self.resize(1200, 820)
        tabs = QtWidgets.QTabWidget()
        txt = QtWidgets.QPlainTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(_summary_text(summary))
        tabs.addTab(txt, "Summary")
        for png in sorted(sess.analysis_dir.glob("*.png")):
            lab = QtWidgets.QLabel()
            lab.setPixmap(QtGui.QPixmap(str(png)))
            sc = QtWidgets.QScrollArea()
            sc.setWidget(lab)
            tabs.addTab(sc, png.stem.replace("_", " "))
        b = QtWidgets.QPushButton("Open analysis folder")
        b.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(sess.analysis_dir))))
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(b)


def _summary_text(s: dict) -> str:
    out = [f"Session {s['session']}"]
    sy = s["sync"]
    for seg, f in sy.get("rfid_segments", {}).items():
        out.append(f"RFID clock (segment {seg}): drift {f['drift_ppm']:+.1f} ppm, serial latency median "
                   f"{f['latency_med_s'] * 1e3:.1f} ms, p95 {f['latency_p95_s'] * 1e3:.1f} ms")
    if sy.get("camera"):
        c = sy["camera"]
        out.append(f"Camera: {c['n_frames']} frames, measured {c.get('measured_fps', 0):.2f} fps, "
                   f"camera drops {c['camera_dropped']}, recorder drops {c['recorder_dropped']}")
    led = sy.get("led")
    if led:
        if led.get("validated"):
            out.append(f"LED/SYNC check: {led['n_matched']}/{led['n_sync']} pulses matched ({led['method']}), "
                       f"offset {led['offset_s'] * 1e3:+.1f} ms applied, |residual| median "
                       f"{led['resid_med_abs_s'] * 1e3:.1f} ms, p95 {led['resid_p95_abs_s'] * 1e3:.1f} ms -> "
                       f"{'PASS' if led.get('pass') else 'FAIL'}")
        else:
            out.append(f"LED/SYNC check: not validated ({led['n_led']} LED onsets, {led['n_sync']} SYNC lines)")
    h = s["stream_health"]
    out.append(f"RFID stream: {h['n_lines']} lines, {h['n_tag_reads']} tag reads, {h['n_bad_lines']} bad lines, "
               f"{h['n_checksum_fail']} checksum failures, {h['missing_rounds_total']} missing rounds, "
               f"unknown tags: {h.get('unknown_tags') or 'none'}")
    if "comparison" in s:
        c = s["comparison"]
        o = c["overall"]
        out += ["", f"Compared over {c['coverage_s'] / 60:.1f} min of labeled video (bins {c['bin_s']} s):",
                f"  presence sensitivity {o['presence_sensitivity']:.1%}, precision {o['presence_precision']:.1%}, "
                f"specificity {o['presence_specificity']:.1%}",
                f"  identity correct when detected {o['id_accuracy_when_detected']:.1%}, overall agreement "
                f"{o['overall_agreement']:.1%}, kappa {o['kappa']:.2f}",
                f"  false presence: crosstalk {o['fp_crosstalk_s']:.1f} s, phantom {o['fp_phantom_s']:.1f} s"]
        e = c.get("events") or {}
        if e:
            out.append(f"  bouts: {e['manual_bouts_detected']}/{e['manual_bouts']} detected, precision "
                       f"{e['event_precision']:.1%}, arrival latency median {e['onset_latency_med_s']:+.2f} s, "
                       f"departure {e['offset_latency_med_s']:+.2f} s")
        out.append("")
        out.append("Per place:")
        for r in s.get("accuracy_per_roi", []):
            out.append(f"  {r['roi']:10s} sens {r['presence_sensitivity']:.1%}  prec {r['presence_precision']:.1%}  "
                       f"ID {r['id_accuracy_when_detected']:.1%}  kappa {r['kappa']:.2f}")
    else:
        out += ["", "No manual labels yet."]
    return "\n".join(out)


def main(session_path: str, annotator: str = "") -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    if not session_path:
        session_path = QtWidgets.QFileDialog.getExistingDirectory(None, "Open session folder", "sessions")
        if not session_path:
            return
    sess = Session.open(session_path)
    if not annotator:
        annotator, ok = QtWidgets.QInputDialog.getText(None, "Annotator", "Your initials:")
        annotator = annotator if ok else ""
    win = ReviewWindow(sess, annotator)
    win.show()
    app.exec()
