"""Session folders: one recording = one folder with metadata, raw streams and derived outputs.

Layout::

    sessions/<YYYYmmdd_HHMMSS>_<name>/
        session.json          metadata, clock anchor, rig snapshot, camera info, stats
        tags.csv  rois.json   snapshot of the tag map and ROIs used
        calibration.yaml      snapshot of the camera calibration (lenses, positions), if there is one
        rfid_raw.csv          every serial line (host_t = s since session start)
        cam0.mp4              video, PTS = frame_idx / nominal fps
        frames_cam0.csv       true per-frame timing (host + camera hardware clock)
        audio_000.flac        audio inputs (microphones, sync pulse), one file per hour
        audio_clock.csv       host time of audio block deliveries (audio clock fit)
        audio_edges.csv       sync pulse edges found in the audio, in samples
        manual_labels.csv     human labels (review GUI or BORIS import)
        label_coverage.csv    time spans actually reviewed by the annotator
        analysis/             derived tables, plots, annotated video
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import RoiSet, TagMap, resolve_path

RFID_FILE = "rfid_raw.csv"
CALIBRATION_FILE = "calibration.yaml"
LABELS_FILE = "manual_labels.csv"
COVERAGE_FILE = "label_coverage.csv"
CURATION_FILE = "rfid_curation.csv"
ANALYSIS_DIR = "analysis"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s.strip()).strip("_") or "session"


class Session:
    def __init__(self, path: str | Path, meta: dict | None = None):
        self.path = Path(path)
        self.meta = meta if meta is not None else self._read_meta()
        self._lock = threading.RLock()  # triggered clips update metadata from another thread

    # ------------------------------------------------------------------ create/open
    @classmethod
    def create(
        cls,
        sessions_dir: str | Path,
        name: str,
        rig: dict,
        tags: TagMap,
        rois: RoiSet,
        anchor: dict,
        notes: str = "",
    ) -> "Session":
        start = datetime.fromtimestamp(anchor["utc_ns"] / 1e9, tz=timezone.utc)
        local = start.astimezone()
        folder = resolve_path(sessions_dir) / f"{local:%Y%m%d_%H%M%S}_{_slug(name)}"
        folder.mkdir(parents=True, exist_ok=False)
        rig_snapshot = {k: v for k, v in rig.items() if not k.startswith("_")}
        meta = {
            "name": name,
            "notes": notes,
            "software": {"mavric": __version__, "python": platform.python_version(), "host": platform.node()},
            "start_utc": start.isoformat(),
            "start_local": local.isoformat(),
            "clock": {"type": "perf_counter", "t0_perf_s": anchor["perf_s"], "t0_utc_ns": anchor["utc_ns"]},
            "rig": rig_snapshot,
            "antennas": {str(k): v for k, v in rig["antennas"].items()},
            "rfid": {"file": RFID_FILE, "port": rig["rfid"]["port"], "baud": rig["rfid"]["baud"]},
            "cameras": [],
            "stop_utc": None,
            "duration_s": None,
            "stats": {},
        }
        s = cls(folder, meta)
        tags.save(folder / "tags.csv")
        rois.save(folder / "rois.json")
        cal = resolve_path(rig["paths"].get("calibration_file", "config/calibration.yaml"))
        if cal.exists():
            shutil.copy2(cal, folder / CALIBRATION_FILE)
        s.save_meta()
        return s

    @classmethod
    def open(cls, path: str | Path) -> "Session":
        p = Path(path)
        if p.is_file():
            p = p.parent
        if not (p / "session.json").exists():
            raise FileNotFoundError(f"No session.json in {p}")
        return cls(p)

    # ------------------------------------------------------------------ metadata
    def _read_meta(self) -> dict:
        with open(self.path / "session.json", encoding="utf-8") as f:
            return json.load(f)

    def save_meta(self) -> None:
        with self._lock:
            tmp = self.path / "session.json.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.meta, f, indent=2, default=str)
            tmp.replace(self.path / "session.json")

    def add_camera(self, info: dict) -> None:
        with self._lock:
            self.meta["cameras"] = [c for c in self.meta["cameras"] if c["id"] != info["id"]] + [info]
            self.save_meta()

    def update_camera(self, cam_id: str, **info) -> None:
        with self._lock:
            for c in self.meta["cameras"]:
                if c["id"] == cam_id:
                    c.update(info)
            self.save_meta()

    def add_clip(self, info: dict) -> int:
        """Register a triggered clip (written immediately, so a crash keeps the entry)."""
        with self._lock:
            clips = self.meta.setdefault("clips", [])
            clips.append(info)
            self.save_meta()
            return len(clips) - 1

    def update_clip(self, index: int, info: dict) -> None:
        with self._lock:
            self.meta["clips"][index].update(info)
            self.save_meta()

    def add_audio(self, info: dict) -> None:
        with self._lock:
            self.meta["audio"] = info
            self.save_meta()

    def update_audio(self, info: dict) -> None:
        with self._lock:
            self.meta.setdefault("audio", {}).update(info)
            self.save_meta()

    def finalize(self, duration_s: float, stats: dict) -> None:
        with self._lock:
            self.meta["stop_utc"] = datetime.now(timezone.utc).isoformat()
            self.meta["duration_s"] = duration_s
            self.meta["stats"] = stats
            self.save_meta()

    # ------------------------------------------------------------------ accessors
    @property
    def name(self) -> str:
        return self.meta.get("name", self.path.name)

    @property
    def tags(self) -> TagMap:
        return TagMap.load(self.path / "tags.csv")

    @property
    def rois(self) -> RoiSet:
        return RoiSet.load(self.path / "rois.json")

    @property
    def roi_camera(self) -> str | None:
        """The camera the ROIs were drawn on."""
        cams = self.meta.get("cameras", [])
        roi = [c for c in cams if c.get("rois_camera")]
        return (roi or self.cameras or cams or [{}])[0].get("id")

    def rois_in(self, cam_id: str | None = None) -> RoiSet:
        """The ROIs in a camera: as drawn in the ROI camera, the places' 3D outlines seen from any other camera
        calibrated in this session's calibration.yaml (places it does not see are left out)."""
        rs = self.rois
        if cam_id is None or cam_id == self.roi_camera:
            return rs
        from .video.places import load_cameras, rois_in_camera

        cams, _ = load_cameras(self.path / CALIBRATION_FILE)
        return rois_in_camera(rs, cam_id, cams, self.roi_camera)

    @property
    def antennas(self) -> dict[int, str]:
        return {int(k): v for k, v in self.meta.get("antennas", {}).items()}

    @property
    def round_period_s(self) -> float:
        return float(self.meta.get("rig", {}).get("rfid", {}).get("round_period_s", 0.125))

    @property
    def cameras(self) -> list[dict]:
        """Cameras with a continuous recording (triggered-only cameras are left out)."""
        return [c for c in self.meta.get("cameras", [])
                if c.get("video_file") or c.get("video_files") or c.get("record_format") == "raw"]

    @property
    def audio(self) -> dict | None:
        """Audio recording metadata, or None when the session has no audio."""
        a = self.meta.get("audio")
        return a if a and a.get("files") else None

    @property
    def clips(self) -> list[dict]:
        return self.meta.get("clips", [])

    def camera(self, cam_id: str | None = None) -> dict:
        if cam_id is not None:
            return next(c for c in self.meta.get("cameras", []) if c["id"] == cam_id)
        cams = self.cameras
        if not cams:
            raise ValueError(f"Session {self.path.name} has no continuously recorded camera")
        roi_cams = [c for c in cams if c.get("rois_camera")]  # the one with the ROIs and the sync LED box
        return (roi_cams or cams)[0]

    def file(self, name: str) -> Path:
        return self.path / name

    @property
    def analysis_dir(self) -> Path:
        d = self.path / ANALYSIS_DIR
        d.mkdir(exist_ok=True)
        return d

    def copy_in(self, src: str | Path, name: str | None = None) -> Path:
        dst = self.path / (name or Path(src).name)
        shutil.copy2(src, dst)
        return dst
