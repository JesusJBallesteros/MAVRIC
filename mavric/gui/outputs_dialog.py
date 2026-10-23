"""Outputs menu: output devices and the rules that drive them from RFID events.

Devices tab  add / edit / remove / test devices (I/O Arduino, Intan RHX, UDP, TCP, MATLAB, program, sim)
Rules tab    which antennas and subjects, which event (enter / leave / while present), which device action
Save         writes config/outputs.yaml and restarts the output engine (disarmed)
"""

from __future__ import annotations

import copy

from PySide6 import QtCore, QtWidgets

from ..config import TagMap
from ..outputs.config import (
    ACTIONS,
    DEVICE_TYPES,
    EVENTS,
    TEMPLATE_FIELDS,
    Rule,
    action_defaults,
    device_defaults,
)
from ..triggers import TriggerRule
from .common import Worker, message
from .tips import OUTPUTS, apply_tips

TEMPLATE_HELP = ("Text fields accept placeholders: " + ", ".join("{" + f + "}" for f in TEMPLATE_FIELDS) +
                 ". {t} is seconds since the recording started (format with {t:.3f}). "
                 "{event} is enter / leave / start / stop.")


class ParamForm(QtWidgets.QWidget):
    """Widgets for a (key, label, default, kind) schema."""

    def __init__(self, schema: list, values: dict | None = None, port_choices: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.schema = schema
        self.widgets: dict[str, QtWidgets.QWidget] = {}
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        values = values or {}
        for key, label, default, kind in schema:
            v = values.get(key, default)
            if kind == "bool":
                w = QtWidgets.QCheckBox()
                w.setChecked(bool(v))
            elif kind == "int":
                w = QtWidgets.QSpinBox()
                w.setRange(-1_000_000, 1_000_000)
                w.setValue(int(v))
            elif kind == "float":
                w = QtWidgets.QDoubleSpinBox()
                w.setRange(-1e6, 1e6)
                w.setDecimals(3)
                w.setValue(float(v))
            elif key == "port" and port_choices is not None:
                w = QtWidgets.QComboBox()
                w.setEditable(True)
                w.addItems(port_choices)
                w.setCurrentText(str(v))
            else:
                w = QtWidgets.QLineEdit(str(v))
                w.setMinimumWidth(320)
            self.widgets[key] = w
            form.addRow(label, w)

    def values(self) -> dict:
        out = {}
        for key, _label, _default, kind in self.schema:
            w = self.widgets[key]
            if kind == "bool":
                out[key] = w.isChecked()
            elif kind in ("int", "float"):
                out[key] = w.value()
            elif isinstance(w, QtWidgets.QComboBox):
                out[key] = w.currentText().strip()
            else:
                out[key] = w.text()
        return out


class DeviceEditor(QtWidgets.QDialog):
    def __init__(self, parent, name: str = "", cfg: dict | None = None, taken: set[str] = frozenset()):
        super().__init__(parent)
        self.setWindowTitle("Output device")
        self.taken = taken
        cfg = cfg or {"type": "arduino_io"}
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(name or "io")
        self.type = QtWidgets.QComboBox()
        for k, d in DEVICE_TYPES.items():
            self.type.addItem(d["label"], k)
        self.type.setCurrentIndex(list(DEVICE_TYPES).index(cfg.get("type", "arduino_io")))
        top.addRow("Name", self.name)
        top.addRow("Type", self.type)
        lay.addLayout(top)
        self.box = QtWidgets.QGroupBox("Settings")
        self.box_lay = QtWidgets.QVBoxLayout(self.box)
        lay.addWidget(self.box)
        self.hint = QtWidgets.QLabel("")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.form = None
        self._values = {k: v for k, v in cfg.items() if k != "type"}
        self.type.currentIndexChanged.connect(lambda _: self._rebuild({}))
        self._rebuild(self._values)
        apply_tips(self, OUTPUTS)

    def _rebuild(self, values: dict) -> None:
        if self.form is not None:
            self.form.setParent(None)
        dtype = self.type.currentData()
        ports = None
        if dtype == "arduino_io":
            from ..rfid.serial_reader import list_ports

            ports = ["SIM"] + [p["device"] for p in list_ports()]
        self.form = ParamForm(DEVICE_TYPES[dtype]["params"], {**device_defaults(dtype), **values}, ports)
        apply_tips(self.form, OUTPUTS)
        self.box_lay.addWidget(self.form)
        self.hint.setText({
            "arduino_io": "Flash firmware/MAVRIC_IO on the board (Uno/Nano/MEGA). Port SIM = software emulator.",
            "intan_rhx": "In Intan RHX: Network > Remote TCP Control > Commands tab: same host/port, Connect.",
            "matlab": "In MATLAB run matlab.engine.shareEngine('mavric') first. Needs the MATLAB Engine for "
                      "Python in the mavric environment.",
            "command": "The program starts once per action, without waiting for it to finish.",
            "udp": "Fire-and-forget datagrams (e.g. to a MATLAB udpport listener).",
            "tcp": "Connects at start; reconnects if the peer restarts.",
            "sim": "Logs actions without driving anything.",
        }.get(dtype, ""))

    def _accept(self):
        name = self.name.text().strip()
        if not name or name in self.taken:
            message(self, "Device name", "Choose a unique, non-empty name.", error=True)
            return
        self.accept()

    def result_cfg(self) -> tuple[str, dict]:
        return self.name.text().strip(), {"type": self.type.currentData(), **self.form.values()}


class RuleEditor(QtWidgets.QDialog):
    def __init__(self, parent, rule: Rule | None, devices: dict[str, dict], antennas: dict[int, str],
                 tags: TagMap, taken: set[str] = frozenset()):
        super().__init__(parent)
        self.setWindowTitle("Output rule")
        self.devices, self.taken = devices, taken
        rule = rule or Rule(name="rule1", device=next(iter(devices)), action="")
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(rule.name)
        self.enabled = QtWidgets.QCheckBox("enabled")
        self.enabled.setChecked(rule.enabled)
        form.addRow("Name", self.name)
        form.addRow("", self.enabled)

        # when: antennas, who, event
        ant_box = QtWidgets.QWidget()
        al = QtWidgets.QHBoxLayout(ant_box)
        al.setContentsMargins(0, 0, 0, 0)
        self.ant_any = QtWidgets.QCheckBox("any")
        self.ant_checks = {}
        al.addWidget(self.ant_any)
        for a, nm in sorted(antennas.items()):
            cb = QtWidgets.QCheckBox(f"{a} {nm}")
            cb.setChecked(rule.antennas is not None and a in rule.antennas)
            self.ant_checks[a] = cb
            al.addWidget(cb)
        self.ant_any.toggled.connect(lambda on: [c.setEnabled(not on) for c in self.ant_checks.values()])
        self.ant_any.setChecked(rule.antennas is None)
        form.addRow("Antennas (RFID)", ant_box)

        who_box = QtWidgets.QWidget()
        wl = QtWidgets.QHBoxLayout(who_box)
        wl.setContentsMargins(0, 0, 0, 0)
        self.who_any = QtWidgets.QCheckBox("any subject")
        wl.addWidget(self.who_any)
        self.who_checks = {}
        for p in tags.pigeons:
            cb = QtWidgets.QCheckBox(p)
            cb.setChecked(rule.who is not None and p in rule.who)
            self.who_checks[p] = cb
            wl.addWidget(cb)
        self.who_any.toggled.connect(lambda on: [c.setEnabled(not on) for c in self.who_checks.values()])
        self.who_any.setChecked(rule.who is None)
        form.addRow("Who", who_box)

        self.event = QtWidgets.QComboBox()
        for k, label in EVENTS.items():
            self.event.addItem(label, k)
        self.event.setCurrentIndex(list(EVENTS).index(rule.event))
        form.addRow("Event", self.event)

        # then: device, action, params
        self.device = QtWidgets.QComboBox()
        for n, d in devices.items():
            self.device.addItem(f"{n} ({DEVICE_TYPES[d['type']]['label']})", n)
        self.device.setCurrentIndex(max(0, list(devices).index(rule.device) if rule.device in devices else 0))
        form.addRow("Device", self.device)
        self.action = QtWidgets.QComboBox()
        form.addRow("Action", self.action)

        timing = QtWidgets.QWidget()
        tl = QtWidgets.QHBoxLayout(timing)
        tl.setContentsMargins(0, 0, 0, 0)
        self.refractory = QtWidgets.QDoubleSpinBox()
        self.refractory.setRange(0, 86400)
        self.refractory.setValue(rule.refractory_s)
        self.delay = QtWidgets.QDoubleSpinBox()
        self.delay.setRange(0, 3600)
        self.delay.setDecimals(3)
        self.delay.setValue(rule.delay_s)
        self.maxpm = QtWidgets.QSpinBox()
        self.maxpm.setRange(0, 10000)
        self.maxpm.setValue(rule.max_per_min)
        for lbl, w in (("refractory s", self.refractory), ("delay s", self.delay), ("max/min (0=∞)", self.maxpm)):
            tl.addWidget(QtWidgets.QLabel(lbl))
            tl.addWidget(w)
        form.addRow("Timing", timing)
        lay.addLayout(form)

        self.pbox = QtWidgets.QGroupBox("Action settings")
        self.pbox_lay = QtWidgets.QVBoxLayout(self.pbox)
        lay.addWidget(self.pbox)
        help_ = QtWidgets.QLabel(TEMPLATE_HELP + " 'while present' runs the action at start and at stop; "
                                 "'hold' keeps an output on in between.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color: gray")
        lay.addWidget(help_)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.pform = None
        self._init_params = dict(rule.params)
        self._init_action = rule.action
        self.device.currentIndexChanged.connect(self._fill_actions)
        self.action.currentIndexChanged.connect(self._fill_params)
        self._fill_actions()
        apply_tips(self, OUTPUTS)
        for w in (*self.ant_checks.values(), self.ant_any):
            w.setToolTip(OUTPUTS["Antennas (RFID)"])
        for w in (*self.who_checks.values(), self.who_any):
            w.setToolTip(OUTPUTS["Who"])
        for w in (self.refractory, self.delay, self.maxpm):
            w.setToolTip(OUTPUTS["Timing"])
        self.enabled.setToolTip("Disabled rules are kept but never fire")

    def _dtype(self) -> str:
        return self.devices[self.device.currentData()]["type"]

    def _fill_actions(self) -> None:
        self.action.blockSignals(True)
        self.action.clear()
        for a in ACTIONS[self._dtype()]:
            self.action.addItem(a, a)
        i = self.action.findData(self._init_action)
        self.action.setCurrentIndex(max(0, i))
        self.action.blockSignals(False)
        self._fill_params()

    def _fill_params(self) -> None:
        if self.pform is not None:
            self.pform.setParent(None)
        dtype, act = self._dtype(), self.action.currentData()
        vals = {**action_defaults(dtype, act), **(self._init_params if act == self._init_action else {})}
        self.pform = ParamForm(ACTIONS[dtype][act], vals)
        apply_tips(self.pform, OUTPUTS)
        self.pbox_lay.addWidget(self.pform)
        self.pbox.setVisible(bool(ACTIONS[dtype][act]))

    def _accept(self):
        name = self.name.text().strip()
        if not name or name in self.taken:
            message(self, "Rule name", "Choose a unique, non-empty name.", error=True)
            return
        if not self.ant_any.isChecked() and not any(c.isChecked() for c in self.ant_checks.values()):
            message(self, "Antennas", "Tick 'any' or at least one antenna.", error=True)
            return
        if not self.who_any.isChecked() and not any(c.isChecked() for c in self.who_checks.values()):
            message(self, "Who", "Tick 'any subject' or at least one subject.", error=True)
            return
        self.accept()

    def result_rule(self) -> Rule:
        return Rule(
            name=self.name.text().strip(), device=self.device.currentData(), action=self.action.currentData(),
            params=self.pform.values(), source="rfid",
            antennas=None if self.ant_any.isChecked() else [a for a, c in self.ant_checks.items() if c.isChecked()],
            who=None if self.who_any.isChecked() else [p for p, c in self.who_checks.items() if c.isChecked()],
            event=self.event.currentData(), refractory_s=self.refractory.value(), delay_s=self.delay.value(),
            max_per_min=self.maxpm.value(), enabled=self.enabled.isChecked(),
        )


class ClipEditor(QtWidgets.QDialog):
    """One RFID-triggered clip rule: which subjects at which antennas start a clip on which camera."""

    def __init__(self, parent, rule: TriggerRule, cameras: list[dict], antennas: dict[int, str], tags: TagMap,
                 taken: set[str] = frozenset()):
        super().__init__(parent)
        self.setWindowTitle("Camera clip rule")
        self.taken = taken
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(rule.name)
        form.addRow("Name", self.name)
        self.camera = QtWidgets.QComboBox()
        for c in cameras:
            mode = "clips only" if c.get("record") == "triggered" else "also records continuously"
            self.camera.addItem(f"{c['id']} ({c.get('backend')} {c.get('device', '')}; {mode})", c["id"])
        if rule.camera and self.camera.findData(rule.camera) < 0:
            self.camera.addItem(f"{rule.camera} (not in rig.yaml)", rule.camera)
        self.camera.setCurrentIndex(max(0, self.camera.findData(rule.camera)))
        form.addRow("Camera", self.camera)

        ant_box = QtWidgets.QWidget()
        al = QtWidgets.QHBoxLayout(ant_box)
        al.setContentsMargins(0, 0, 0, 0)
        self.ant_any = QtWidgets.QCheckBox("any")
        al.addWidget(self.ant_any)
        self.ant_checks = {}
        for a, nm in sorted(antennas.items()):
            cb = QtWidgets.QCheckBox(f"{a} {nm}")
            cb.setChecked(rule.antennas is not None and a in rule.antennas)
            self.ant_checks[a] = cb
            al.addWidget(cb)
        self.ant_any.toggled.connect(lambda on: [c.setEnabled(not on) for c in self.ant_checks.values()])
        self.ant_any.setChecked(rule.antennas is None)
        form.addRow("Antennas (RFID)", ant_box)

        who_box = QtWidgets.QWidget()
        wl = QtWidgets.QHBoxLayout(who_box)
        wl.setContentsMargins(0, 0, 0, 0)
        self.who_any = QtWidgets.QCheckBox("any subject")
        wl.addWidget(self.who_any)
        self.who_checks = {}
        for pg_ in tags.pigeons:
            cb = QtWidgets.QCheckBox(pg_)
            cb.setChecked(rule.match is not None and pg_ in rule.match)
            self.who_checks[pg_] = cb
            wl.addWidget(cb)
        self.who_any.toggled.connect(lambda on: [c.setEnabled(not on) for c in self.who_checks.values()])
        self.who_any.setChecked(rule.match is None)
        form.addRow("Who", who_box)

        self.spins = {}
        for key, label, val, hi in (("pre_s", "Seconds before the first read", rule.pre_s, 60),
                                    ("post_s", "Seconds after the subject leaves", rule.post_s, 600),
                                    ("max_s", "Split clips longer than (s)", rule.max_s, 86400)):
            sp = QtWidgets.QDoubleSpinBox()
            sp.setRange(0 if key != "max_s" else 1, hi)
            sp.setDecimals(1)
            sp.setValue(val)
            self.spins[key] = sp
            form.addRow(label, sp)
        lay.addLayout(form)
        help_ = QtWidgets.QLabel(
            "A clip records while a matching subject is at one of the antennas, from 'before' seconds earlier (kept in "
            "memory) to 'after' seconds after it leaves. Clips are written only while a session is recording, into "
            "the session's clips/ folder. Set a camera to 'record: triggered' in rig.yaml to keep only its clips.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color: gray")
        lay.addWidget(help_)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        apply_tips(self, OUTPUTS)
        for w in (*self.ant_checks.values(), self.ant_any):
            w.setToolTip(OUTPUTS["Antennas (RFID)"])
        for w in (*self.who_checks.values(), self.who_any):
            w.setToolTip(OUTPUTS["Who"])

    def _accept(self):
        name = self.name.text().strip()
        if not name or name in self.taken:
            message(self, "Rule name", "Choose a unique, non-empty name.", error=True)
            return
        if self.camera.currentData() is None:
            message(self, "Camera", "Add the camera to rig.yaml first.", error=True)
            return
        if not self.ant_any.isChecked() and not any(c.isChecked() for c in self.ant_checks.values()):
            message(self, "Antennas", "Tick 'any' or at least one antenna.", error=True)
            return
        if not self.who_any.isChecked() and not any(c.isChecked() for c in self.who_checks.values()):
            message(self, "Who", "Tick 'any subject' or at least one subject.", error=True)
            return
        self.accept()

    def result_cfg(self) -> dict:
        return TriggerRule(
            name=self.name.text().strip(), camera=self.camera.currentData(),
            antennas=None if self.ant_any.isChecked() else [a for a, c in self.ant_checks.items() if c.isChecked()],
            match=None if self.who_any.isChecked() else [p for p, c in self.who_checks.items() if c.isChecked()],
            pre_s=self.spins["pre_s"].value(), post_s=self.spins["post_s"].value(), max_s=self.spins["max_s"].value(),
        ).to_cfg()


class OutputsDialog(QtWidgets.QDialog):
    """Edits a copy of the configuration; Save writes it and restarts the controller's engine."""

    def __init__(self, parent, ctl):
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Outputs: devices, rules and camera clips")
        self.resize(1100, 560)
        self.cfg = copy.deepcopy(ctl.outputs.cfg if ctl.outputs else ctl.load_outputs_config())
        self._workers: list[Worker] = []
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QLabel(f"Saved to <code>{ctl.outputs_path}</code>. Outputs are only driven while "
                               "<b>armed</b> (button in the main window); disarming releases every held output.")
        top.setWordWrap(True)
        lay.addWidget(top)
        tabs = QtWidgets.QTabWidget()
        lay.addWidget(tabs, 1)

        # devices
        dw = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(dw)
        self.dev_table = QtWidgets.QTableWidget(0, 4)
        self.dev_table.setHorizontalHeaderLabels(["Name", "Type", "Settings", "Status"])
        self._table_style(self.dev_table)
        dl.addWidget(self.dev_table)
        dl.addLayout(self._buttons([("Add…", self.add_device), ("Edit…", self.edit_device),
                                    ("Remove", self.remove_device), ("Test", self.test_device)]))
        tabs.addTab(dw, "Devices")

        # rules
        rw = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(rw)
        self.rule_table = QtWidgets.QTableWidget(0, 8)
        self.rule_table.setHorizontalHeaderLabels(["On", "Name", "Event", "Antennas", "Who", "Device → action",
                                                   "Settings", "Timing"])
        self._table_style(self.rule_table)
        rl.addWidget(self.rule_table)
        rl.addLayout(self._buttons([("Add…", self.add_rule), ("Edit…", self.edit_rule), ("Remove", self.remove_rule),
                                    ("Test (fire once)", self.test_rule)]))
        tabs.addTab(rw, "Rules")

        # camera clips
        cw = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(cw)
        self.clip_table = QtWidgets.QTableWidget(0, 6)
        self.clip_table.setHorizontalHeaderLabels(["Name", "Camera", "Antennas", "Who", "Before / after (s)",
                                                   "Split at (s)"])
        self._table_style(self.clip_table)
        cl.addWidget(self.clip_table)
        cl.addLayout(self._buttons([("Add…", self.add_clip), ("Edit…", self.edit_clip), ("Remove", self.remove_clip)]))
        rig_rules = [r["name"] for r in (ctl.rig.get("triggers") or [])]
        note = QtWidgets.QLabel(
            "Record a video clip on a camera while RFID detects a subject. Applies from the next recording; "
            "the camera must be connected." + (f" Also active from rig.yaml: {', '.join(rig_rules)}." if rig_rules
                                              else ""))
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        cl.addWidget(note)
        tabs.addTab(cw, "Camera clips")

        bb = QtWidgets.QDialogButtonBox()
        self.b_save = bb.addButton("Save && restart outputs", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.StandardButton.Close)
        self.b_save.clicked.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh()
        apply_tips(self, OUTPUTS)
        self.dev_table.setToolTip("Output devices; Test checks the selected one answers")
        self.rule_table.setToolTip("Rules: when RFID sees a matching subject, a device acts")
        self.clip_table.setToolTip("Camera clip rules: record video while a matching subject is present")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _table_style(t: QtWidgets.QTableWidget) -> None:
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)

    def _buttons(self, items) -> QtWidgets.QHBoxLayout:
        h = QtWidgets.QHBoxLayout()
        for label, fn in items:
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        return h

    def _selected(self, table) -> int | None:
        rows = table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def refresh(self) -> None:
        status = self.ctl.outputs.status() if self.ctl.outputs else {"devices": {}}
        names = list(self.cfg.devices)
        self.dev_table.setRowCount(len(names))
        for i, n in enumerate(names):
            d = self.cfg.devices[n]
            st = status["devices"].get(n)
            st_txt = "not started" if st is None else (f"ERROR {st['error']}" if st.get("error") else
                                                       ("open" if st.get("opened") else "closed"))
            cells = [n, DEVICE_TYPES[d["type"]]["label"], ", ".join(f"{k}={v}" for k, v in d.items() if k != "type"),
                     st_txt]
            for j, c in enumerate(cells):
                self.dev_table.setItem(i, j, QtWidgets.QTableWidgetItem(str(c)))
        self.rule_table.setRowCount(len(self.cfg.rules))
        for i, r in enumerate(self.cfg.rules):
            cells = ["✔" if r.enabled else "", r.name, r.event,
                     "any" if r.antennas is None else ", ".join(f"{a} {self.ctl.rig['antennas'].get(a, '')}"
                                                                for a in r.antennas),
                     "any" if r.who is None else ", ".join(r.who), f"{r.device} → {r.action}",
                     ", ".join(f"{k}={v}" for k, v in r.params.items()),
                     f"refr {r.refractory_s:g}s · delay {r.delay_s:g}s" + (f" · ≤{r.max_per_min}/min" if r.max_per_min else "")]
            for j, c in enumerate(cells):
                self.rule_table.setItem(i, j, QtWidgets.QTableWidgetItem(str(c)))
        self.clip_table.setRowCount(len(self.cfg.clips))
        for i, c in enumerate(self.cfg.clips):
            r = TriggerRule.from_cfg(c)
            cells = [r.name, r.camera,
                     "any" if r.antennas is None else ", ".join(f"{a} {self.ctl.rig['antennas'].get(a, '')}"
                                                                for a in r.antennas),
                     "any" if r.match is None else ", ".join(r.match), f"{r.pre_s:g} / {r.post_s:g}", f"{r.max_s:g}"]
            for j, v in enumerate(cells):
                self.clip_table.setItem(i, j, QtWidgets.QTableWidgetItem(str(v)))
        for t in (self.dev_table, self.rule_table, self.clip_table):
            t.resizeColumnsToContents()

    # ------------------------------------------------------------------ devices
    def add_device(self) -> None:
        dlg = DeviceEditor(self, name=f"dev{len(self.cfg.devices) + 1}", taken=set(self.cfg.devices))
        if dlg.exec():
            n, d = dlg.result_cfg()
            self.cfg.devices[n] = d
            self.refresh()

    def edit_device(self) -> None:
        i = self._selected(self.dev_table)
        if i is None:
            return
        old = list(self.cfg.devices)[i]
        dlg = DeviceEditor(self, old, self.cfg.devices[old], taken=set(self.cfg.devices) - {old})
        if dlg.exec():
            n, d = dlg.result_cfg()
            devices = {}
            for k, v in self.cfg.devices.items():  # keep order, apply rename
                devices[n if k == old else k] = d if k == old else v
            self.cfg.devices = devices
            for r in self.cfg.rules:
                if r.device == old:
                    r.device = n
                    if r.action not in ACTIONS[d["type"]]:
                        r.action = next(iter(ACTIONS[d["type"]]))
                        r.params = action_defaults(d["type"], r.action)
            self.refresh()

    def remove_device(self) -> None:
        i = self._selected(self.dev_table)
        if i is None:
            return
        n = list(self.cfg.devices)[i]
        users = [r.name for r in self.cfg.rules if r.device == n]
        if users:
            message(self, "Device in use", f"Rules {users} use {n!r}; remove or change them first.", error=True)
            return
        del self.cfg.devices[n]
        self.refresh()

    def test_device(self) -> None:
        i = self._selected(self.dev_table)
        eng = self.ctl.outputs
        if i is None:
            return
        n = list(self.cfg.devices)[i]
        if eng is None or n not in eng.devices or eng.cfg.devices.get(n) != self.cfg.devices[n]:
            message(self, "Test", "Save first: tests use the running (saved) configuration.")
            return
        self._run(lambda: eng.test_device(n), f"Test {n}")

    # ------------------------------------------------------------------ rules
    def add_rule(self) -> None:
        if not self.cfg.devices:
            message(self, "No devices", "Add a device first.", error=True)
            return
        dlg = RuleEditor(self, None, self.cfg.devices, self.ctl.rig["antennas"], self.ctl.tags,
                         taken={r.name for r in self.cfg.rules})
        dlg.name.setText(f"rule{len(self.cfg.rules) + 1}")
        if dlg.exec():
            self.cfg.rules.append(dlg.result_rule())
            self.refresh()

    def edit_rule(self) -> None:
        i = self._selected(self.rule_table)
        if i is None:
            return
        r = self.cfg.rules[i]
        dlg = RuleEditor(self, r, self.cfg.devices, self.ctl.rig["antennas"], self.ctl.tags,
                         taken={x.name for x in self.cfg.rules} - {r.name})
        if dlg.exec():
            self.cfg.rules[i] = dlg.result_rule()
            self.refresh()

    def remove_rule(self) -> None:
        i = self._selected(self.rule_table)
        if i is not None:
            del self.cfg.rules[i]
            self.refresh()

    def test_rule(self) -> None:
        i = self._selected(self.rule_table)
        eng = self.ctl.outputs
        if i is None:
            return
        r = self.cfg.rules[i]
        if eng is None or r.name not in eng.states or eng.states[r.name].rule != r:
            message(self, "Test", "Save first: tests use the running (saved) configuration.")
            return
        eng.fire_now(r.name)
        QtCore.QTimer.singleShot(1500, lambda: message(self, f"Test {r.name}", self._last_result(r.name)))

    def _last_result(self, rule: str) -> str:
        rows = [x for x in self.ctl.outputs.log if x.get("rule") == rule]
        return "\n".join(f"{x['event']}: {x['status']} {x['result']}" for x in rows[-2:]) or "no result yet"

    # ------------------------------------------------------------------ camera clips
    def _clip_editor(self, rule: TriggerRule, taken: set[str]) -> ClipEditor:
        return ClipEditor(self, rule, self.ctl.rig["cameras"], self.ctl.rig["antennas"], self.ctl.tags, taken)

    def add_clip(self) -> None:
        cams = self.ctl.rig["cameras"]
        if not cams:
            message(self, "No cameras", "Add cameras to rig.yaml first.", error=True)
            return
        cam = next((c["id"] for c in cams if c.get("record") == "triggered"), cams[-1]["id"])
        rule = TriggerRule(name=f"clip{len(self.cfg.clips) + 1}", camera=cam)
        dlg = self._clip_editor(rule, {c["name"] for c in self.cfg.clips})
        if dlg.exec():
            self.cfg.clips.append(dlg.result_cfg())
            self.refresh()

    def edit_clip(self) -> None:
        i = self._selected(self.clip_table)
        if i is None:
            return
        c = self.cfg.clips[i]
        dlg = self._clip_editor(TriggerRule.from_cfg(c), {x["name"] for x in self.cfg.clips} - {c["name"]})
        if dlg.exec():
            self.cfg.clips[i] = dlg.result_cfg()
            self.refresh()

    def remove_clip(self) -> None:
        i = self._selected(self.clip_table)
        if i is not None:
            del self.cfg.clips[i]
            self.refresh()

    # ------------------------------------------------------------------ save
    def save(self) -> None:
        try:
            warns = self.cfg.validate()
        except ValueError as e:
            message(self, "Invalid configuration", str(e), error=True)
            return
        if self.ctl.outputs and self.ctl.outputs.armed:
            r = QtWidgets.QMessageBox.question(self, "Outputs armed", "Saving restarts the outputs and disarms "
                                               "them (held outputs are released). Continue?")
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self.cfg.save(self.ctl.outputs_path)
        self.b_save.setEnabled(False)
        cfg = copy.deepcopy(self.cfg)

        def done(errors):
            self.b_save.setEnabled(True)
            self.refresh()
            lines = [f"Saved {len(cfg.devices)} device(s), {len(cfg.rules)} rule(s) and {len(cfg.clips)} camera "
                     "clip rule(s). Outputs restarted (disarmed); clip rules apply from the next recording."]
            lines += [f"WARNING {w}" for w in warns]
            known = {c["id"] for c in self.ctl.rig["cameras"]}
            lines += [f"WARNING clip rule {c['name']}: camera {c['camera']} is not in rig.yaml" for c in cfg.clips
                      if c["camera"] not in known]
            lines += [f"Device {k} failed to open: {v}" for k, v in (errors or {}).items()]
            message(self, "Outputs", "\n".join(lines), error=bool(errors))
            if self.parent() is not None and hasattr(self.parent(), "outputs_changed"):
                self.parent().outputs_changed()

        self._run(lambda: self.ctl.start_outputs(cfg), "Restarting outputs", done)

    def _run(self, fn, label, done=None) -> None:
        w = Worker(fn)
        w.done.connect(done or (lambda res: message(self, label, str(res))))
        w.failed.connect(lambda e: (self.b_save.setEnabled(True), message(self, label, e, error=True)))
        self._workers.append(w)
        w.start()
