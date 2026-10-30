"""Calibrate cameras, tab 4: microphones and speakers in the space, seen or not by the cameras, checked by sound."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ..audio.geometry import Device, aim_at, confusable, load_devices, place_fingerprints, place_sources, \
    save_devices, speaker_levels, speed_of_sound, suggested_layout
from ..audio.ranging import BANDS, fit, levels, measure
from ..config import RoiSet
from ..video.places import place_3d, problem
from .common import Worker, message

HOW_TO_DEVICES = (
    "<b>How</b>: the space from above and from the side, around what the cameras see. Drag each microphone and speaker "
    "to where it is, or type its position in mm from the floor origin (the board's corner), and aim the microphones. "
    "Then <i>Play and measure</i>: each speaker plays a short sweep, and the distances to the microphones check the "
    "positions. <i>Fit positions</i> moves the devices you are least sure of to agree with the sounds. Then "
    "<i>Save devices</i>.")

COLOURS = {"mic": "#2a78d6", "speaker": "#d0761f"}
COLUMNS = ["Device", "x", "y", "z", "Aim az", "Aim el", "Placed"]


class SpaceView(pg.GraphicsLayoutWidget):
    """The space from above (x, y) and from the side (x, z), in mm: the floor origin, the places, the cameras and the
    devices, which can be dragged."""

    moved = QtCore.Signal(str, object)  # device name, new position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground("#fcfcfb")
        self.top = self.addPlot(row=0, col=0, title="From above")
        self.side = self.addPlot(row=1, col=0, title="From the side (looking along y)")
        for p, (a, b) in ((self.top, ("x (mm)", "y (mm)")), (self.side, ("x (mm)", "z (mm)"))):
            p.setAspectLocked(True)
            p.showGrid(x=True, y=True, alpha=0.2)
            p.setLabel("bottom", a)
            p.setLabel("left", b)
            p.setMenuEnabled(False)
        self.ci.layout.setRowStretchFactor(0, 3)
        self.ci.layout.setRowStretchFactor(1, 2)
        self._static: list = []
        self._dev: dict[str, dict] = {}
        self._busy = False

    def _add(self, plot, item):
        plot.addItem(item)
        self._static.append((plot, item))

    def set_scene(self, places: RoiSet, cams: dict) -> None:
        for plot, it in self._static:
            plot.removeItem(it)
        self._static = []
        for plot in (self.top, self.side):  # the floor origin: x red, y / z green
            self._add(plot, pg.PlotDataItem([0, 150], [0, 0], pen=pg.mkPen("#c0392b", width=2)))
            self._add(plot, pg.PlotDataItem([0, 0], [0, 150], pen=pg.mkPen("#1e7d32", width=2)))
        self._add(self.side, pg.InfiniteLine(0, angle=0, pen=pg.mkPen("#8a8986", width=1)))  # the floor
        for r in places.rois:
            if not r.points3d:
                continue
            P = np.asarray(r.points3d, float)
            col = "#eda100" if r.kind == "sync" else "#7a5230"
            loop = np.vstack([P, P[:1]])
            self._add(self.top, pg.PlotDataItem(loop[:, 0], loop[:, 1], pen=pg.mkPen(col, width=2)))
            z0, z1 = P[:, 2].min(), P[:, 2].max() + (r.box_mm or 0)
            x0, x1 = P[:, 0].min(), P[:, 0].max()
            self._add(self.side, pg.PlotDataItem([x0, x1, x1, x0, x0], [z0, z0, max(z1, z0 + 5), max(z1, z0 + 5), z0],
                                                 pen=pg.mkPen(col, width=2)))
            for plot, y in ((self.top, P[:, 1].max()), (self.side, max(z1, z0 + 5))):
                t = pg.TextItem(r.name, color=col, anchor=(0, 1))
                t.setPos(x0, y)
                self._add(plot, t)
        for cid, cam in cams.items():
            c, ax = cam.centre, cam.R[2]
            for plot, (i, j) in ((self.top, (0, 1)), (self.side, (0, 2))):
                self._add(plot, pg.ScatterPlotItem([c[i]], [c[j]], symbol="t1", size=12, brush=pg.mkBrush("#52514e")))
                self._add(plot, pg.PlotDataItem([c[i], c[i] + 300 * ax[i]], [c[j], c[j] + 300 * ax[j]],
                                                pen=pg.mkPen("#52514e", width=1, style=QtCore.Qt.PenStyle.DashLine)))
                t = pg.TextItem(cid, color="#52514e", anchor=(0.5, 1.2))
                t.setPos(c[i], c[j])
                self._add(plot, t)

    def set_devices(self, devices: dict[str, Device]) -> None:
        for d in self._dev.values():
            for plot, items in ((self.top, d["top"]), (self.side, d["side"])):
                for it in items:
                    plot.removeItem(it)
        self._dev = {}
        for name, dev in devices.items():
            col = COLOURS.get(dev.kind, "#52514e")
            entry = {}
            for key, plot, (i, j) in (("top", self.top, (0, 1)), ("side", self.side, (0, 2))):
                tgt = pg.TargetItem((dev.position[i], dev.position[j]), size=14, movable=True,
                                    symbol="o" if dev.kind == "mic" else "s", pen=pg.mkPen(col, width=2),
                                    brush=pg.mkBrush(col + "60"), label=name,
                                    labelOpts={"color": col, "offset": (8, -8)})
                tgt.sigPositionChanged.connect(lambda t, n=name, k=key: self._dragged(n, k, t))
                aim = pg.PlotDataItem(pen=pg.mkPen(col, width=2))
                plot.addItem(aim)
                plot.addItem(tgt)
                entry[key] = (tgt, aim)
            self._dev[name] = {"dev": dev, "top": entry["top"], "side": entry["side"]}
            self._aim(name)

    def _aim(self, name: str) -> None:
        e = self._dev[name]
        p, ax = e["dev"].position, e["dev"].axis * 160
        for key, (i, j) in (("top", (0, 1)), ("side", (0, 2))):
            e[key][1].setData([p[i], p[i] + ax[i]], [p[j], p[j] + ax[j]])

    def update_device(self, dev: Device) -> None:
        e = self._dev.get(dev.name)
        if e is None:
            return
        e["dev"] = dev
        self._busy = True
        try:
            e["top"][0].setPos((dev.position[0], dev.position[1]))
            e["side"][0].setPos((dev.position[0], dev.position[2]))
        finally:
            self._busy = False
        self._aim(dev.name)

    def _dragged(self, name: str, key: str, tgt) -> None:
        if self._busy:
            return
        p = self._dev[name]["dev"].position.copy()
        q = tgt.pos()
        if key == "top":
            p[0], p[1] = q.x(), q.y()
        else:
            p[0], p[2] = q.x(), q.y()
        self.moved.emit(name, p)

    def fit_view(self) -> None:
        for p in (self.top, self.side):
            p.autoRange(padding=0.12)


class DevicesTab(QtWidgets.QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg, self.win, self.ctl = dlg, dlg.win, dlg.ctl
        self.cfg = self.ctl.audio_cfg
        self.mics = [self.cfg.names[j] for j in self.cfg.mic_indices]
        self.speakers = [self.cfg.speakers[k] for k in sorted(self.cfg.speakers)]
        self.devices: dict[str, Device] = {}
        self.places = RoiSet()
        self.cams: dict = {}
        self.ranging = None
        self.fitted = None
        self._loaded = False
        self._filling = False
        self._workers: list[Worker] = []
        lay = QtWidgets.QHBoxLayout(self)
        self.space = SpaceView()
        self.space.moved.connect(self._moved)
        lay.addWidget(self.space, 1)
        side = QtWidgets.QWidget()
        side.setFixedWidth(450)
        v = QtWidgets.QVBoxLayout(side)
        scroll = QtWidgets.QScrollArea()  # the sound check's text can be long
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(side.width() + 20)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        lay.addWidget(scroll)

        b = QtWidgets.QGroupBox("Calibration")
        f = QtWidgets.QVBoxLayout(b)
        self.ready_label = QtWidgets.QLabel("")
        self.ready_label.setWordWrap(True)
        f.addWidget(self.ready_label)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Microphones and speakers (mm, degrees)")
        f = QtWidgets.QGridLayout(b)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setToolTip("Each device's position from the floor origin (x along the board's columns, y towards its "
                              "top edge, z up) and where it points (azimuth from +x towards +y, elevation up)")
        f.addWidget(self.table, 0, 0, 1, 3)
        bt = QtWidgets.QPushButton("Suggested layout")
        bt.clicked.connect(self._suggest)
        f.addWidget(bt, 1, 0)
        bt = QtWidgets.QPushButton("Aim at the places")
        bt.clicked.connect(self._aim_all)
        f.addWidget(bt, 1, 1)
        self.b_truth = QtWidgets.QPushButton("True positions (simulation)")
        self.b_truth.clicked.connect(self._use_truth)
        self.b_truth.setVisible(self._scene() is not None)
        f.addWidget(self.b_truth, 1, 2)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Check with sound")
        f = QtWidgets.QGridLayout(b)
        self.band_combo = QtWidgets.QComboBox()
        for k, band in BANDS.items():
            self.band_combo.addItem(k, band)
        self.band_combo.setToolTip("The sweep each speaker plays. Above 12 kHz the pigeons hardly hear it, but small "
                                   "speakers may give less there")
        f.addWidget(self.band_combo, 0, 0)
        self.level_spin = QtWidgets.QDoubleSpinBox()
        self.level_spin.setRange(-50.0, -6.0)
        self.level_spin.setValue(-20.0)
        self.level_spin.setSuffix(" dBFS")
        self.level_spin.setToolTip("Level of the sweep; raise it if a pair is not heard")
        f.addWidget(self.level_spin, 0, 1)
        self.b_measure = QtWidgets.QPushButton("Play and measure")
        self.b_measure.clicked.connect(self._measure)
        f.addWidget(self.b_measure, 1, 0, 1, 2)
        self.cb_fit_mics = QtWidgets.QCheckBox("microphones")
        self.cb_fit_mics.setChecked(True)
        self.cb_fit_mics.setToolTip("Let the fit move the microphones")
        self.cb_fit_spk = QtWidgets.QCheckBox("speakers")
        self.cb_fit_spk.setToolTip("Let the fit move the speakers (each held near its typed position)")
        h = QtWidgets.QHBoxLayout()
        h.addWidget(QtWidgets.QLabel("Fit:"))
        h.addWidget(self.cb_fit_mics)
        h.addWidget(self.cb_fit_spk)
        f.addLayout(h, 2, 0, 1, 2)
        self.b_fit = QtWidgets.QPushButton("Fit positions")
        self.b_fit.clicked.connect(self._fit)
        self.b_fit.setEnabled(False)
        f.addWidget(self.b_fit, 3, 0)
        self.b_apply = QtWidgets.QPushButton("Use the fitted positions")
        self.b_apply.clicked.connect(self._apply_fit)
        self.b_apply.setEnabled(False)
        f.addWidget(self.b_apply, 3, 1)
        self.sound_label = QtWidgets.QLabel("")
        self.sound_label.setWordWrap(True)
        self.sound_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.sound_label, 4, 0, 1, 2)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Places as the microphones hear them")
        f = QtWidgets.QVBoxLayout(b)
        self.fp_label = QtWidgets.QLabel("")
        self.fp_label.setWordWrap(True)
        self.fp_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        f.addWidget(self.fp_label)
        v.addWidget(b)

        b = QtWidgets.QGroupBox("Speaker level at each place")
        f = QtWidgets.QVBoxLayout(b)
        self.spl_label = QtWidgets.QLabel("")
        self.spl_label.setWordWrap(True)
        self.spl_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.spl_label.setToolTip("Level at a bird's head above each place, from the distance and each speaker's "
                                  "measured output; not the speakers' directivity (the angle off each speaker's axis "
                                  "shows where it matters) or the room's echoes")
        f.addWidget(self.spl_label)
        v.addWidget(b)

        self.b_save = QtWidgets.QPushButton("Save devices")
        self.b_save.clicked.connect(self._save)
        v.addWidget(self.b_save)
        how = QtWidgets.QLabel(HOW_TO_DEVICES)
        how.setWordWrap(True)
        how.setStyleSheet("color: #52514e")
        v.addWidget(how)
        v.addStretch(1)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600")
        v.addWidget(self.status)

    # ------------------------------------------------------------------ state
    def _scene(self):
        return getattr(self.ctl.world, "scene", None)

    def activate(self) -> None:
        """On showing the tab: the calibration, the places placed in 3D, and the devices (saved, else suggested)."""
        self.cams, world = self.ctl.camera_models()
        rs = self.win.current_rois() if hasattr(self.win, "current_rois") else self.ctl.rois
        why = problem(rs, self.cams, world, self.ctl.roi_camera)
        cam0 = self.cams.get(self.ctl.roi_camera)
        self.places = RoiSet([place_3d(r, cam0) for r in rs.rois], rs.image_size) if cam0 else RoiSet()
        names = self.mics + self.speakers
        if why:
            self.ready_label.setText(f"<b style='color:#c0392b'>Not ready</b>: {why}.")
        elif not names:
            self.ready_label.setText("No microphones or speakers in rig.yaml (audio: inputs, speakers).")
        else:
            n = len(place_sources(self.places))
            self.ready_label.setText(f"✓ world origin on the floor; {n} place(s) placed in 3D"
                                     + ("" if n else " (tab 3), needed for the suggested layout and the place check"))
        for w in (self.table, self.b_save, self.b_measure):
            w.setEnabled(why is None and bool(names))
        if not self._loaded:
            self._loaded = True
            saved, _ = load_devices(self.dlg.path)
            sug = suggested_layout(place_sources(self.places), self.mics, self.speakers)
            self.devices = {n: saved.get(n) or Device(n, sug[n].kind, sug[n].position, sug[n].aim, "suggested")
                            for n in names}
        self.space.set_scene(self.places, self.cams)
        self.space.set_devices(self.devices)
        self.space.fit_view()
        self._fill_table()
        self._fingerprints()
        audio = self.ctl.audio
        self.b_measure.setEnabled(why is None and audio is not None and bool(self.speakers) and bool(self.mics))
        if audio is None:
            self.sound_label.setText("Connect the audio in the acquire window to check the positions by sound.")

    def _fill_table(self) -> None:
        """One row per device; its spin boxes are made once and then only updated."""
        self._filling = True
        try:
            if self.table.rowCount() != len(self.devices):
                self.table.setRowCount(len(self.devices))
                for i, dev in enumerate(self.devices.values()):
                    self.table.setItem(i, 0, QtWidgets.QTableWidgetItem(("🎤 " if dev.kind == "mic" else "🔈 ")
                                                                        + dev.name))
                    for k in range(5):
                        sp = QtWidgets.QDoubleSpinBox()
                        sp.setDecimals(0)
                        lim = 10000.0 if k < 3 else 180.0
                        sp.setRange(-lim, lim)
                        sp.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
                        sp.setToolTip(["x (mm)", "y (mm)", "z (mm, up from the floor)",
                                       "aim azimuth (° from +x towards +y)", "aim elevation (° up from the horizontal)"][k])
                        sp.valueChanged.connect(lambda _v, n=dev.name: self._edited(n))
                        self.table.setCellWidget(i, 1 + k, sp)
                    self.table.setItem(i, 6, QtWidgets.QTableWidgetItem(""))
                self.table.resizeColumnsToContents()
                self.table.setFixedHeight(self.table.horizontalHeader().height() + 4
                                          + sum(self.table.rowHeight(i) for i in range(self.table.rowCount())))
            for i, dev in enumerate(self.devices.values()):
                for k, val in enumerate([*dev.position, *dev.aim]):
                    self.table.cellWidget(i, 1 + k).setValue(float(val))
                self.table.item(i, 6).setText(dev.how)
        finally:
            self._filling = False

    def _row(self, name: str) -> int:
        return list(self.devices).index(name)

    def _edited(self, name: str) -> None:
        if self._filling:
            return
        i = self._row(name)
        v = [self.table.cellWidget(i, 1 + k).value() for k in range(5)]
        d = self.devices[name]
        self.devices[name] = replace(d, position=np.array(v[:3]), aim=(v[3], v[4]), how="typed", sd_mm=None)
        self.table.item(i, 6).setText("typed")
        self.space.update_device(self.devices[name])
        self._fingerprints()

    def _moved(self, name: str, pos) -> None:
        d = self.devices[name]
        self.devices[name] = replace(d, position=np.asarray(pos, float), how="typed", sd_mm=None)
        i = self._row(name)
        self._filling = True
        try:
            for k in range(3):
                self.table.cellWidget(i, 1 + k).setValue(float(pos[k]))
        finally:
            self._filling = False
        self.table.item(i, 6).setText("typed")
        self.space.update_device(self.devices[name])
        self._fingerprints()

    def _set_all(self, devices: dict[str, Device]) -> None:
        self.devices = {n: devices.get(n, d) for n, d in self.devices.items()}
        self._fill_table()
        for d in self.devices.values():
            self.space.update_device(d)
        self._fingerprints()

    def _suggest(self) -> None:
        sug = suggested_layout(place_sources(self.places), self.mics, self.speakers)
        self._set_all({n: replace(self.devices.get(n, d), position=d.position, aim=d.aim, how="suggested", sd_mm=None)
                       for n, d in sug.items()})
        self.status.setText("Suggested layout: speakers at the corners, microphones above the long midline. Move "
                            "each device to where it really is.")

    def _aim_all(self) -> None:
        src = place_sources(self.places)
        if not src:
            message(self, "Aim", "Place the places in 3D first (tab 3).")
            return
        mid = np.mean(list(src.values()), axis=0)
        self._set_all({n: replace(d, aim=aim_at(d.position, mid)) for n, d in self.devices.items()})

    def _use_truth(self) -> None:
        self._set_all(self._scene().devices())

    def _fingerprints(self) -> None:
        """The places as the microphones hear them, and each speaker's level at each place."""
        src = place_sources(self.places)
        spk = [self.devices[n] for n in self.speakers if n in self.devices]
        self.spl_label.setText(self._levels_table(spk, src) if spk and src else "Needs speakers and places placed "
                               "in 3D.")
        mics = [self.devices[n] for n in self.mics if n in self.devices]
        if len(mics) < 2 or not src:
            self.fp_label.setText("Needs two microphones and places placed in 3D.")
            return
        fp = place_fingerprints(mics, self.places, self._c())
        a, b = mics[0].name, mics[1].name
        lines = [f"A call from each place reaches {b} later (+) or earlier than {a}, and quieter (−) or louder "
                 "(± over the place):"]
        lines += [f"{n}: {dt_:+.2f} ± {sdt:.2f} ms, {dl:+.1f} ± {sdl:.1f} dB" for n, (dt_, dl, sdt, sdl) in fp.items()]
        g = (mics[1].level_db or 0.0) - (mics[0].level_db or 0.0)
        if mics[0].level_db is not None and mics[1].level_db is not None:
            lines.append(f"<small>with {b} recording {g:+.1f} dB against {a} (measured by sound)</small>")
        else:
            lines.append("<small>the microphones' recording gains are not measured yet (Play and measure)</small>")
        bad = confusable({n: v[:2] for n, v in fp.items()})
        if bad:
            lines.append("<b style='color:#c0392b'>Hard to tell apart by sound:</b> "
                         + ", ".join(f"{p} and {q}" for p, q in bad) + ". Moving or turning a microphone helps.")
        else:
            lines.append("✓ every place sounds different at the two microphones: review grades the emitter "
                         "suggestions by them")
        self.fp_label.setText("<br>".join(lines))

    @staticmethod
    def _levels_table(spk: list[Device], src: dict) -> str:
        lv = speaker_levels(spk, src)
        head = "".join(f"<th>&nbsp;{s.name}&nbsp;</th>" for s in spk)
        rows = "".join(f"<tr><td>{p}</td>" + "".join(f"<td align='right'>{lv[s.name][p][2]:+.1f}"
                                                     f" <small>({lv[s.name][p][1]:.0f}°)</small></td>" for s in spk)
                       + "</tr>" for p in src)
        ref = spk[0].name
        note = ("" if all(s.level_db is not None for s in spk) else
                " The speakers' outputs are not measured yet (Play and measure): taken as equal.")
        return (f"<table cellspacing='4'><tr><th></th>{head}</tr>{rows}</table>"
                f"<small>dB relative to 1 m in front of {ref} (angle off each speaker's axis).{note}</small>")

    # ------------------------------------------------------------------ sound
    def _measure(self) -> None:
        st = self.ctl.audio
        if st is None:
            return
        w = Worker(measure, st, self.band_combo.currentData(), self.level_spin.value())
        w.done.connect(self._measured)
        w.failed.connect(lambda e: (self.b_measure.setEnabled(True), message(self, "Check with sound", e, error=True)))
        self._workers.append(w)
        self.b_measure.setEnabled(False)
        self.sound_label.setText("Playing a sweep on each speaker in turn …")
        w.start()

    def _measured(self, r) -> None:
        self.ranging = r
        self.b_measure.setEnabled(True)
        missing = [f"{s} → {m}" for (s, m), d in r.pairs.items() if not d.get("ok")]
        f0 = fit(r, self.devices, (), self._c())
        lines = [f"{len(r.ok_pairs())} of {len(r.pairs)} speaker → microphone pairs heard "
                 f"(driver round trip {r.round_trip_ms:.1f} ms)."]
        if missing:
            lines.append("<b style='color:#c0392b'>Not heard:</b> " + ", ".join(missing)
                         + ". Raise the level, or check the speaker and the band.")
        lines.append("<b>The typed positions against the sounds:</b> " + self._fit_text(f0, show_moves=False))
        lines.append(self._measure_levels())
        self.sound_label.setText("<br>".join(lines))
        self.b_fit.setEnabled(len(r.ok_pairs()) >= 4)

    def _measure_levels(self) -> str:
        """The microphones' recording gains and the speakers' outputs from the sweeps, at the current positions."""
        lv = levels(self.ranging, self.devices) if self.ranging is not None else None
        if lv is None:
            return "<b>Levels:</b> not enough pairs heard."
        self.devices = lv.apply(self.devices)
        self._fingerprints()
        return "<b>Levels:</b> " + lv.summary()

    def _c(self) -> float:
        return speed_of_sound(float((self.ctl.rig.get("audio") or {}).get("temperature_c", 20.0)))

    def _fit_text(self, f, show_moves: bool = True) -> str:
        ok, why = f.verdict()
        txt = f.summary().splitlines()
        head = txt[0].replace("PASS", "<b style='color:#1e7d32'>PASS</b>").replace(
            "FAIL", "<b style='color:#c0392b'>FAIL</b>")
        out = [head] + (txt[1:] if show_moves else [])
        worst = sorted(f.residual_mm.items(), key=lambda kv: -abs(kv[1]))[:3]
        out.append("largest: " + ", ".join(f"{s} → {m} {d:+.0f} mm" for (s, m), d in worst))
        out += [f"• {x}" for x in why]
        return "<br>".join(out)

    def _fit(self) -> None:
        if self.ranging is None:
            return
        free = (self.mics if self.cb_fit_mics.isChecked() else []) + (
            self.speakers if self.cb_fit_spk.isChecked() else [])
        self.fitted = fit(self.ranging, self.devices, free, self._c())
        txt = self._fit_text(self.fitted)
        scene = self._scene()
        if scene is not None:
            truth = scene.devices()
            err = max((float(np.linalg.norm(self.fitted.devices[n].position - truth[n].position)) for n in free
                       if n in truth), default=0.0)
            txt += f"<br><i>simulated: every fitted device within {err:.0f} mm of where it really is</i>"
        self.sound_label.setText(txt)
        self.b_apply.setEnabled(bool(free))

    def _apply_fit(self) -> None:
        if self.fitted is None:
            return
        self._set_all({n: self.fitted.devices[n] for n in self.fitted.free})
        self.b_apply.setEnabled(False)
        self.sound_label.setText(self.sound_label.text() + "<br>" + self._measure_levels())
        self.status.setText("Fitted positions in the table, levels measured again there. Save devices to keep them.")

    def _save(self) -> None:
        meta = {"sound_check": self.ranging.as_dict()} if self.ranging is not None else {}
        if self.fitted is not None:
            meta["sound_fit"] = {"free": list(self.fitted.free), "rms_mm": round(self.fitted.rms_mm, 1),
                                 "latency_ms": round(self.fitted.latency_ms, 3)}
        p = save_devices(self.dlg.path, self.devices, meta)
        self.status.setText(f"Saved {len(self.devices)} devices to {p}.")
        if hasattr(self.win, "update_calibration_label"):
            self.win.update_calibration_label()
