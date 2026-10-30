"""Camera and recording settings edited in the acquire GUI (Video settings…).

They live in config/video.yaml and are merged over rig.yaml when the rig is
loaded (rig.yaml keeps its comments; the GUI never rewrites it)::

    cameras:              # per camera id, any camera key of rig.yaml
      cam0: {fps: 60, exposure_us: 4000, width: 1024, height: 768}
    video:                # recording and compression
      record_format: raw  # raw (lossless chunks, compress later) | h264 (compressed while recording)
      raw_codec: ffv1     # ffv1 (lossless, ~2x smaller) | rawvideo (uncompressed)
      raw_chunk_mb: 2000
      compress: {codec: libx264, crf: 18, preset: medium, gop: 30}

Each backend exposes the parameters it can set; `live` ones change while
the camera streams, the rest need a reconnect.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import read_yaml


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    kind: str  # int | float | enum | bool
    default: object
    live: bool = False
    choices: tuple = ()
    lo: float | None = None
    hi: float | None = None
    unit: str = ""
    help: str = ""


FPS = Param("fps", "Frame rate", "float", 30.0, lo=1, hi=500, unit="fps",
            help="Raising it may need a shorter exposure or a smaller image")
EXPOSURE = Param("exposure_us", "Exposure", "float", 8000.0, live=True, lo=6, hi=1e6, unit="µs",
                 help="Must be shorter than 1/frame rate; short exposures avoid motion blur")
GAIN = Param("gain_db", "Gain", "float", 0.0, live=True, lo=0, hi=48, unit="dB", help="Brightens, but adds noise")
RECORD = Param("record", "Recording", "enum", "continuous", choices=("continuous", "triggered"),
               help="triggered: only RFID-triggered clips")
WIDTH = Param("width", "Width (0 = full)", "int", 0, lo=0, hi=8192, unit="px",
              help="Region of interest: a smaller image allows a higher frame rate and less data")
HEIGHT = Param("height", "Height (0 = full)", "int", 0, lo=0, hi=8192, unit="px")
OFFSET_X = Param("offset_x", "Offset X", "int", 0, lo=0, hi=8192, unit="px")
OFFSET_Y = Param("offset_y", "Offset Y", "int", 0, lo=0, hi=8192, unit="px")

PARAMS: dict[str, list[Param]] = {
    "flir": [
        FPS, EXPOSURE, GAIN,
        Param("black_level", "Brightness (black level)", "float", 0.0, live=True, lo=0, hi=100,
              help="Lifts dark areas; the camera's 'Brightness'"),
        Param("gamma", "Gamma (0 = off)", "float", 0.0, live=True, lo=0, hi=4,
              help="Off keeps the response linear (stable sync-LED threshold)"),
        Param("pixel_format", "Pixel format", "enum", "Mono8", choices=("Mono8", "BGR8", "BayerRG8"),
              help="Colour models: BGR8 (colour made in the camera) or BayerRG8 (made on the PC: a third of the USB "
                   "bandwidth); Mono8 gives grey from either"),
        Param("white_balance", "White balance (colour models)", "enum", "once", live=True,
              choices=("once", "continuous", "off"),
              help="once: measured when the camera connects (or now), then kept; continuous: the camera keeps "
                   "adjusting; off: kept as it is"),
        WIDTH, HEIGHT, OFFSET_X, OFFSET_Y, RECORD,
    ],
    "basler": [FPS, EXPOSURE, GAIN, Param("pixel_format", "Pixel format", "enum", "Mono8", choices=("Mono8", "BGR8")),
               RECORD],
    "opencv": [
        FPS,
        Param("width", "Width (0 = camera default)", "int", 0, lo=0, hi=8192, unit="px"),
        Param("height", "Height (0 = camera default)", "int", 0, lo=0, hi=8192, unit="px"),
        Param("brightness", "Brightness", "float", -1.0, live=True, lo=-1, hi=255, help="-1 = leave as is"),
        Param("contrast", "Contrast", "float", -1.0, live=True, lo=-1, hi=255, help="-1 = leave as is"),
        Param("gain", "Gain", "float", -1.0, live=True, lo=-1, hi=255, help="-1 = leave as is"),
        RECORD,
    ],
    "synthetic": [FPS, RECORD],
    "file": [RECORD],
}

RECORD_FORMATS = {"raw": "Raw, lossless chunks (compress afterwards)", "h264": "H.264 while recording (small, lossy)"}
RAW_CODECS = {"ffv1": "FFV1 lossless (about 1/3 of uncompressed)", "rawvideo": "Uncompressed (largest, least CPU)"}
VIDEO_DEFAULTS = {
    "record_format": "raw",
    "raw_codec": "ffv1",
    "raw_chunk_mb": 2000,
    "compress": {"codec": "libx264", "crf": 18, "preset": "medium", "gop": 30, "pix_fmt": "yuv420p"},
}


def params_for(backend: str) -> list[Param]:
    return PARAMS.get(backend, [FPS, RECORD])


@dataclass
class VideoSettings:
    cameras: dict[str, dict] = field(default_factory=dict)
    video: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "VideoSettings":
        p = Path(path)
        if not p.exists():
            return cls()
        d = read_yaml(p)
        return cls({str(k): dict(v or {}) for k, v in (d.get("cameras") or {}).items()}, dict(d.get("video") or {}))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("# Camera and recording settings from the acquire GUI (Video settings...).\n"
                    "# Merged over config/rig.yaml when the rig is loaded.\n")
            yaml.safe_dump({"cameras": self.cameras, "video": self.video}, f, sort_keys=False, allow_unicode=True)

    def apply(self, rig: dict) -> dict:
        """Merge into a loaded rig (in place) and return it."""
        for cam in rig.get("cameras") or []:
            cam.update(copy.deepcopy(self.cameras.get(cam.get("id"), {})))
        video = rig.setdefault("video", {})
        for k, v in self.video.items():
            if isinstance(v, dict) and isinstance(video.get(k), dict):
                video[k] = {**video[k], **v}
            else:
                video[k] = copy.deepcopy(v)
        return rig


def video_cfg(rig: dict) -> dict:
    """The rig's video section with every default filled in."""
    v = {**copy.deepcopy(VIDEO_DEFAULTS), **copy.deepcopy(rig.get("video") or {})}
    v["compress"] = {**VIDEO_DEFAULTS["compress"], **(rig.get("video") or {}).get("compress", {})}
    return v
