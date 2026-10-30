"""Rig configuration, tag map and ROI definitions."""

from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd
import yaml

SOURCE_ROOT = Path(__file__).resolve().parent.parent  # the checkout, for editable installs


def project_root() -> Path:
    r"""The folder holding config/ and sessions/ (the "rig folder").

    1. $MAVRIC_ROOT, e.g. set once per conda env:
       conda env config vars set MAVRIC_ROOT=C:\Code\MAVRIC
    2. the current directory, if it contains config/rig.yaml
    3. the source checkout this package runs from (pip install -e .)
    4. the current directory
    """
    env = os.environ.get("MAVRIC_ROOT") or os.environ.get("VRFID_ROOT")  # VRFID_ROOT: the name before 0.8
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    if (cwd / "config" / "rig.yaml").exists():
        return cwd
    if (SOURCE_ROOT / "config" / "rig.yaml").exists() or EXAMPLES_DIR.is_dir():
        return SOURCE_ROOT
    return cwd


EXAMPLES_DIR = SOURCE_ROOT / "config" / "examples"  # tracked; the lab's own config/* files are not


def init_config(root: Path | None = None) -> list[Path]:
    """First run of a fresh clone: copy config/examples/* into config/, never over an existing file."""
    dest = (root or project_root()) / "config"
    made = []
    if EXAMPLES_DIR.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for src in sorted(EXAMPLES_DIR.iterdir()):
            if src.is_file() and not (dest / src.name).exists():
                shutil.copyfile(src, dest / src.name)
                made.append(dest / src.name)
    return made


class ConfigError(ValueError):
    """A config file that cannot be used; the message names the file and line and how to fix it."""


def read_yaml(path: str | Path) -> dict:
    """Parse a YAML config file. A syntax error becomes a ConfigError that shows the offending line."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        msg = f"{path} is not valid YAML"
        mark = getattr(e, "problem_mark", None)
        if mark is not None:
            lines = text.splitlines()
            prefix = f"  line {mark.line + 1}: "
            msg += (f": {getattr(e, 'problem', None) or e}\n{prefix}{lines[mark.line] if mark.line < len(lines) else ''}"
                    f"\n{' ' * (len(prefix) + mark.column)}^")
        msg += ("\nUsually this is the indentation: use spaces, not tabs, and start the items of one list"
                " (e.g. each '- id:' under cameras:) in the same column."
                f"\nFix that line, or rename the file to fall back to the defaults (fresh copies are in {EXAMPLES_DIR}).")
        raise ConfigError(msg) from None


# Fallback colours for pigeons without one in tags.csv: validated categorical order
# (adjacent-pair CVD-safe; identity is always also given by name labels)
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
UNKNOWN = "UNKNOWN"
EMPTY = "EMPTY"
MULTI = "MULTI"

DEFAULT_RIG = {
    "rfid": {"port": "auto", "baud": 500000, "reset_wait_s": 2.0, "round_period_s": 0.125},
    "antennas": {1: "PlaceA", 2: "PlaceB", 3: "PlaceC", 4: "PlaceD"},
    "cameras": [
        {
            "id": "cam0",
            "backend": "opencv",
            "device": "0",
            "fps": 30,
            "exposure_us": 8000,
            "gain_db": 0.0,
            "pixel_format": "Mono8",
            "line_status": False,
            "record": "continuous",
        }
    ],
    "triggers": [],
    "video": {
        "record_format": "raw",  # raw: lossless chunks, compress afterwards (mavric compress) | h264: while recording
        "raw_codec": "ffv1",  # ffv1 (lossless, ~2x smaller) | rawvideo (uncompressed)
        "raw_chunk_mb": 2000,  # start a new raw file every ~2 GB
        "codec": "libx264",  # h264 recording and RFID-triggered clips in h264 mode
        "crf": 20,
        "gop_s": 1.0,
        "preset": "veryfast",
        "compress": {"codec": "libx264", "crf": 18, "preset": "medium", "gop": 30, "pix_fmt": "yuv420p"},
    },
    "sync": {"led_roi_name": "SYNC", "led_pulse_s": 0.080},
    "calibration": {  # camera calibration board (ChArUco: a checkerboard with an ArUco marker in each white square)
        "board": {"squares": [8, 10], "square_mm": 39.0, "marker_mm": 29.0, "dictionary": "DICT_4X4_50"},
    },
    "audio": {
        "enabled": False,  # headless acquisition connects audio only when enabled (GUI: Connect audio)
        "device": "MOTU",  # name substring, see `mavric audio devices`
        "host_api": "ASIO",  # ASIO | WASAPI | MME | DirectSound | WDM-KS | sim
        "sample_rate": 48000,
        "block": 1024,  # samples per callback
        "latency": "high",  # PortAudio suggested latency: high | low | seconds
        "inputs": {1: "micA", 2: "micB", 3: "sync"},  # input channel (1-based) -> name
        "sync_channel": "sync",  # input that carries the RFID Arduino sync pulse ("" = none)
        "speakers": {1: "spk1", 2: "spk2", 3: "spk3", 4: "spk4"},  # output channel (1-based) -> name
        "format": "FLAC",  # FLAC | WAV
        "subtype": "PCM_24",
        "split_s": 3600,  # start a new file every split_s seconds
        "max_level_dbfs": -6.0,  # playback limiter
        "display_fmax_hz": 10000,  # live spectrogram range
        "mic_near": {},  # mic name -> antennas next to it, e.g. {micA: [1, 3]}: ranks emitter suggestions
    },
    "paths": {
        "sessions_dir": "sessions",
        "tags_file": "config/tags.csv",
        "rois_file": "config/rois.json",
        "outputs_file": "config/outputs.yaml",
        "sound_types_file": "config/sound_types.yaml",  # sound event types (review GUI)
        "video_settings_file": "config/video.yaml",  # camera/recording settings from the acquire GUI
        "calibration_file": "config/calibration.yaml",  # camera calibration (acquire GUI: Calibrate cameras...)
    },
}


# maps the user lists in full: a key left out of rig.yaml must not come back from the defaults
_REPLACE_KEYS = ("antennas", "inputs", "speakers", "mic_near")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in _REPLACE_KEYS:
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_path(p: str | Path, root: Path | None = None) -> Path:
    p = Path(p).expanduser()
    return p if p.is_absolute() else (root or project_root()) / p


def load_rig(path: str | Path | None = None) -> dict:
    """Load rig.yaml merged over defaults.

    Antenna keys are normalised to int. Relative entries under `paths:` are
    made absolute against the rig folder (the parent of config/), so the tool
    behaves the same from any working directory and from a non-editable install.
    """
    if path is not None and Path(path).expanduser().exists():
        path = Path(path).expanduser().resolve()  # given relative to the current directory
    else:
        if path is None and not resolve_path("config/rig.yaml").exists():
            init_config()
        path = resolve_path(path or "config/rig.yaml")
    user = read_yaml(path) if path.exists() else {}
    rig = _deep_merge(DEFAULT_RIG, user)
    rig["cameras"] = rig["cameras"] or []  # "cameras:" with every entry commented out
    if not isinstance(rig["cameras"], list) or not all(isinstance(c, dict) for c in rig["cameras"]):
        raise ConfigError(f"{path}: cameras: must be a list of entries that each start with '- id:'")
    ids = [str(c.get("id", "cam0")) for c in rig["cameras"]]
    if dup := sorted({i for i in ids if ids.count(i) > 1}):
        raise ConfigError(f"{path}: camera id {', '.join(dup)} is used by more than one entry under cameras:."
                          " Give each camera its own id (cam0, cam1, ...).")
    rig["antennas"] = {int(k): str(v) for k, v in rig["antennas"].items()}
    for k in ("inputs", "speakers"):
        rig["audio"][k] = {int(c): str(n) for c, n in (rig["audio"].get(k) or {}).items()}
    root = path.parent.parent if path.parent.name == "config" else path.parent
    if not path.exists():
        root = project_root()
    rig["paths"] = {k: str(resolve_path(v, root)) for k, v in rig["paths"].items()}
    vs = Path(rig["paths"].get("video_settings_file", ""))
    if vs.is_file():  # settings saved from the acquire GUI override rig.yaml
        from .video.settings import VideoSettings

        VideoSettings.load(vs).apply(rig)
    rig["_path"] = str(path)
    rig["_root"] = str(root)
    return rig


# --------------------------------------------------------------------------- tags


@dataclass
class TagMap:
    """Tag ID -> subject name and display colour."""

    ids: dict[str, str] = field(default_factory=dict)
    colors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TagMap":
        path = resolve_path(path)
        if not path.exists():
            return cls()
        df = pd.read_csv(path, dtype=str).fillna("")
        tm = cls()
        for i, row in df.iterrows():
            tag = row["tag_id"].strip().upper()
            name = (row.get("subject", "") or row.get("pigeon", "")).strip() or f"UNLABELED_{i + 1:02d}"  # pigeon: before 0.8
            if tag in tm.ids:
                raise ValueError(f"Duplicate tag id {tag} in {path}")
            tm.ids[tag] = name
            color = row.get("color", "").strip()
            tm.colors[name] = color or CATEGORICAL[len(tm.colors) % len(CATEGORICAL)]
        return tm

    def save(self, path: str | Path) -> None:
        rows = [{"tag_id": t, "subject": p, "color": self.colors.get(p, "")} for t, p in self.ids.items()]
        pd.DataFrame(rows, columns=["tag_id", "subject", "color"]).to_csv(path, index=False)

    def name(self, tag: str) -> str:
        if tag in ("", "NONE"):
            return EMPTY
        return self.ids.get(tag.upper(), UNKNOWN)

    @property
    def pigeons(self) -> list[str]:
        seen: list[str] = []
        for p in self.ids.values():
            if p not in seen:
                seen.append(p)
        return seen

    def color(self, pigeon: str) -> str:
        if pigeon in self.colors:
            return self.colors[pigeon]
        if pigeon in (EMPTY, ""):
            return "#d9d9d9"
        return "#7f7f7f"


# --------------------------------------------------------------------------- ROIs


@dataclass
class Roi:
    name: str
    points: list[tuple[float, float]]  # polygon vertices in image pixels (x, y)
    antenna: int | None = None  # firmware reader number; None for the sync-LED ROI
    kind: str = "perch"  # "perch" | "sync"
    plane_z_mm: float | None = None  # 3D: height of the place's surface (or of the sync LED) above the floor, mm
    box_mm: float = 0.0  # 3D: the place is the box this tall standing on its surface (0: the surface only)
    points3d: list[tuple[float, float, float]] | None = None  # 3D outline in the calibration's world (mm)

    def bbox(self) -> tuple[int, int, int, int]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1


@dataclass
class RoiSet:
    rois: list[Roi] = field(default_factory=list)
    image_size: tuple[int, int] | None = None  # (width, height) the ROIs were drawn on

    @classmethod
    def load(cls, path: str | Path) -> "RoiSet":
        path = resolve_path(path)
        if not path.exists():
            return cls()
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rois = [
            Roi(
                name=r["name"],
                points=[tuple(p) for p in r["points"]],
                antenna=r.get("antenna"),
                kind=r.get("kind", "perch"),
                plane_z_mm=r.get("plane_z_mm"),
                box_mm=float(r.get("box_mm") or 0.0),
                points3d=[tuple(p) for p in r["points3d"]] if r.get("points3d") else None,
            )
            for r in d.get("rois", [])
        ]
        size = tuple(d["image_size"]) if d.get("image_size") else None
        return cls(rois=rois, image_size=size)

    def save(self, path: str | Path) -> None:
        d = {"image_size": list(self.image_size) if self.image_size else None, "rois": [asdict(r) for r in self.rois]}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)

    @property
    def perches(self) -> list[Roi]:
        return [r for r in self.rois if r.kind == "perch"]

    @property
    def sync(self) -> Roi | None:
        return next((r for r in self.rois if r.kind == "sync"), None)

    def by_antenna(self, antenna: int) -> Roi | None:
        return next((r for r in self.perches if r.antenna == antenna), None)

    def set_roi(self, roi: Roi) -> None:
        self.rois = [r for r in self.rois if r.name != roi.name] + [roi]
