"""Output devices and rules: schema, defaults, YAML persistence.

config/outputs.yaml::

    devices:
      io:      {type: arduino_io, port: COM5}
      intan:   {type: intan_rhx, host: 127.0.0.1, port: 5000}
    rules:
      - name: intan_perchB
        device: io
        action: hold            # TTL high while a bird is on PerchB -> Intan DIGITAL IN 1
        params: {pin: 30}
        antennas: [2]
        who: any
        event: while
    clips:                      # RFID-triggered video clips (mavric.triggers), same fields as rig.yaml triggers
      - name: perchB_cam1
        camera: cam1
        antennas: [2]
        match: any
        pre_s: 3
        post_s: 5
        max_s: 300
"""

from __future__ import annotations

import copy
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ..config import read_yaml

# (key, label, default, kind) per parameter; kind in str|int|float|bool
DEVICE_TYPES: dict[str, dict] = {
    "arduino_io": {
        "label": "I/O Arduino (TTL pins)",
        "params": [("port", "COM port", "COM5", "str"), ("baud", "Baud rate", 115200, "int"),
                   ("watchdog_s", "Watchdog: all low if PC silent (s, 0 = off)", 2.0, "float"),
                   ("reset_wait_s", "Wait after opening (board reset, s)", 2.0, "float")],
    },
    "intan_rhx": {
        "label": "Intan RHX (TCP remote control)",
        "params": [("host", "Host", "127.0.0.1", "str"), ("port", "Command port", 5000, "int")],
    },
    "udp": {
        "label": "UDP message",
        "params": [("host", "Host", "127.0.0.1", "str"), ("port", "Port", 9000, "int")],
    },
    "tcp": {
        "label": "TCP message",
        "params": [("host", "Host", "127.0.0.1", "str"), ("port", "Port", 9001, "int"),
                   ("newline", "Append newline", True, "bool")],
    },
    "matlab": {
        "label": "MATLAB (Engine API)",
        "params": [("session", "Shared session name ('' = start a new MATLAB)", "mavric", "str"),
                   ("cwd", "Working folder", "", "str")],
    },
    "command": {
        "label": "Run a program / script",
        "params": [("program", "Program (e.g. python.exe, a .bat)", "", "str"), ("cwd", "Working folder", "", "str")],
    },
    "sim": {"label": "Simulated (log only)", "params": []},
}

# action -> parameter list, per device type
ACTIONS: dict[str, dict[str, list]] = {
    "arduino_io": {
        "pulse": [("pin", "Pin", 30, "int"), ("ms", "Pulse length (ms)", 50, "int")],
        "hold": [("pin", "Pin (high while the rule is active)", 30, "int")],
        "set": [("pin", "Pin", 30, "int"), ("value", "Value (0/1)", 1, "int")],
        "toggle": [("pin", "Pin", 30, "int")],
    },
    "intan_rhx": {
        "record": [],
        "run": [],
        "stop": [],
        "hold_record": [],  # record while active, stop after
        "send": [("text", "Command", "set runmode record", "str")],
    },
    "udp": {
        "send": [("text", "Text", "{event} {roi} {subject} {t:.3f}", "str")],
        "hold": [("start_text", "Text at start", "start {roi} {subject}", "str"),
                 ("stop_text", "Text at stop", "stop {roi}", "str")],
    },
    "tcp": {
        "send": [("text", "Text", "{event} {roi} {subject} {t:.3f}", "str")],
        "hold": [("start_text", "Text at start", "start {roi} {subject}", "str"),
                 ("stop_text", "Text at stop", "stop {roi}", "str")],
    },
    "matlab": {
        "eval": [("code", "MATLAB code", "disp('{event} {roi} {subject} {t:.3f}')", "str")],
        "call": [("function", "Function", "", "str"), ("args", "Arguments", "'{subject}', '{roi}', {t}", "str")],
        "run": [("script", "Script (.m file)", "", "str")],
    },
    "command": {
        "run": [("args", "Arguments", "{event} {roi} {subject} {t:.3f}", "str")],
    },
    "sim": {
        "mark": [("text", "Note", "{event} {roi} {subject}", "str")],
        "hold": [],
    },
}

HOLD_ACTIONS = {"hold", "hold_record"}
EVENTS = {
    "enter": "on enter (a matching subject arrives)",
    "leave": "on leave (a matching subject leaves)",
    "while": "while present (start ... stop)",
}
SOURCES = {"rfid": "RFID antenna"}  # "camera_roi" is added with the camera ROI detector (Phase C)

TEMPLATE_FIELDS = ["event", "rule", "antenna", "roi", "subject", "pigeon", "tag", "t", "utc", "session"]


@dataclass
class Rule:
    name: str
    device: str
    action: str
    params: dict = field(default_factory=dict)
    source: str = "rfid"
    antennas: list[int] | None = None  # None = any antenna
    who: list[str] | None = None  # pigeon names and/or tag IDs; None = any tag (known or not)
    event: str = "enter"  # enter | leave | while
    refractory_s: float = 0.0  # minimum time between firings
    delay_s: float = 0.0  # act this long after the event
    max_per_min: int = 0  # 0 = unlimited
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        ants = d.get("antennas", "any")
        who = d.get("who", "any")
        return cls(
            name=str(d["name"]), device=str(d["device"]), action=str(d["action"]),
            params=dict(d.get("params") or {}), source=str(d.get("source", "rfid")),
            antennas=None if ants in (None, "any", []) else [int(a) for a in (ants if isinstance(ants, list) else [ants])],
            who=None if who in (None, "any", []) else [str(w) for w in (who if isinstance(who, list) else [who])],
            event=str(d.get("event", "enter")), refractory_s=float(d.get("refractory_s", 0.0)),
            delay_s=float(d.get("delay_s", 0.0)), max_per_min=int(d.get("max_per_min", 0)),
            enabled=bool(d.get("enabled", True)),
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["antennas"] = "any" if self.antennas is None else self.antennas
        d["who"] = "any" if self.who is None else self.who
        return d

    def describe(self) -> str:
        ants = "any antenna" if self.antennas is None else "antenna " + "/".join(map(str, self.antennas))
        who = "any subject" if self.who is None else ",".join(self.who)
        return f"{self.event} {ants} ({who}) -> {self.device}.{self.action}"


@dataclass
class OutputsConfig:
    devices: dict[str, dict] = field(default_factory=dict)  # name -> {"type": ..., params...}
    rules: list[Rule] = field(default_factory=list)
    clips: list[dict] = field(default_factory=list)  # camera clip rules (TriggerRule.from_cfg / to_cfg)

    @classmethod
    def load(cls, path: str | Path) -> "OutputsConfig":
        path = Path(path)
        if not path.exists():
            return cls()
        d = read_yaml(path)
        devices ={str(k): dict(v or {}) for k, v in (d.get("devices") or {}).items()}
        rules = [Rule.from_dict(r) for r in (d.get("rules") or [])]
        clips = [dict(c) for c in (d.get("clips") or [])]
        cfg = cls(devices, rules, clips)
        cfg.validate()
        return cfg

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        d = {"devices": copy.deepcopy(self.devices), "rules": [r.to_dict() for r in self.rules],
             "clips": copy.deepcopy(self.clips)}
        with open(path, "w", encoding="utf-8") as f:
            f.write("# MAVRIC event-driven outputs (edit in the acquire GUI: Outputs...)\n")
            yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True)

    def validate(self) -> list[str]:
        """Raise ValueError on structural errors; return warnings."""
        warns = []
        for name, dev in self.devices.items():
            if dev.get("type") not in DEVICE_TYPES:
                raise ValueError(f"device {name!r}: unknown type {dev.get('type')!r} (known: {sorted(DEVICE_TYPES)})")
        names = set()
        for r in self.rules:
            if r.name in names:
                raise ValueError(f"duplicate rule name {r.name!r}")
            names.add(r.name)
            if r.device not in self.devices:
                raise ValueError(f"rule {r.name!r}: unknown device {r.device!r}")
            dtype = self.devices[r.device]["type"]
            if r.action not in ACTIONS[dtype]:
                raise ValueError(f"rule {r.name!r}: device type {dtype} has no action {r.action!r} "
                                 f"(known: {sorted(ACTIONS[dtype])})")
            if r.event not in EVENTS:
                raise ValueError(f"rule {r.name!r}: unknown event {r.event!r}")
            if r.source not in SOURCES:
                raise ValueError(f"rule {r.name!r}: unknown source {r.source!r}")
            if r.action in HOLD_ACTIONS and r.event != "while":
                warns.append(f"rule {r.name!r}: '{r.action}' is meant for 'while' rules")
        from ..triggers import TriggerRule

        clip_names = set()
        for c in self.clips:
            if not c.get("name") or not c.get("camera"):
                raise ValueError(f"camera clip rule {c!r}: needs a name and a camera")
            if c["name"] in clip_names:
                raise ValueError(f"duplicate camera clip rule name {c['name']!r}")
            clip_names.add(c["name"])
            try:
                TriggerRule.from_cfg(c)
            except (TypeError, ValueError) as e:
                raise ValueError(f"camera clip rule {c['name']!r}: {e}") from e
        return warns


def device_defaults(dtype: str) -> dict:
    return {k: default for k, _label, default, _kind in DEVICE_TYPES[dtype]["params"]}


def action_defaults(dtype: str, action: str) -> dict:
    return {k: default for k, _label, default, _kind in ACTIONS[dtype][action]}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, ctx: dict) -> str:
    """Fill {placeholders} from the event context; unknown fields stay as written."""
    try:
        return string.Formatter().vformat(str(template), (), _SafeDict(ctx))
    except (ValueError, TypeError, KeyError, IndexError):
        return str(template)
