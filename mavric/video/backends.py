"""Camera backends behind one small interface.

Every backend stamps the host monotonic clock right after a frame is handed
over, and (where the camera has one) also returns the camera's hardware
timestamp and frame counter. Offline, the hardware clock is fitted to the host
clock, which removes USB/driver buffering jitter from frame times.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

from ..core import now


@dataclass(slots=True)
class Frame:
    image: np.ndarray  # HxW (mono) or HxWx3 (BGR), uint8
    host_abs: float  # host monotonic seconds, stamped on hand-over
    grab_idx: int  # host-side running count of delivered frames
    hw_ticks: int | None = None  # camera timestamp (ticks of the camera clock)
    hw_counter: int | None = None  # camera frame counter / block id (gaps = camera-side drops)
    line_status: int | None = None  # camera GPIO line state bitmask (sync pin wired to an input)


class CameraBackend:
    kind = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = cfg.get("id", "cam0")
        self.fps = float(cfg.get("fps", 30))
        self.size: tuple[int, int] | None = None  # (width, height)
        self._grab_idx = 0
        self.n_failed = 0
        self.hw_tick_hz: float | None = None

    def open(self) -> None:
        raise NotImplementedError

    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def apply_live(self, changes: dict) -> set[str]:
        """Change settings while streaming; returns the keys applied (the rest need a reconnect)."""
        return set()

    def info(self) -> dict:
        w, h = self.size or (None, None)
        return {
            "id": self.id,
            "backend": self.kind,
            "fps": self.fps,
            "width": w,
            "height": h,
            "hw_tick_hz": self.hw_tick_hz,
            "device": str(self.cfg.get("device", "")),
        }

    def _next_idx(self) -> int:
        i = self._grab_idx
        self._grab_idx += 1
        return i


# --------------------------------------------------------------------------- OpenCV / files


class OpenCVCamera(CameraBackend):
    """UVC webcams or video URLs. No hardware timestamps; for development and fallback."""

    kind = "opencv"

    def open(self) -> None:
        dev = str(self.cfg.get("device", "0"))
        if dev.isdigit():
            api = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            self.cap = cv2.VideoCapture(int(dev), api)
        else:
            self.cap = cv2.VideoCapture(dev)
        if not self.cap.isOpened():
            raise RuntimeError(f"OpenCV could not open camera {dev!r}")
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        if int(self.cfg.get("width") or 0) and int(self.cfg.get("height") or 0):
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.cfg["width"]))
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.cfg["height"]))
        self.apply_live({k: self.cfg[k] for k in self._LIVE if k in self.cfg})
        ok, img = self.cap.read()
        if not ok:
            raise RuntimeError(f"OpenCV camera {dev!r} returned no frame")
        self.size = (img.shape[1], img.shape[0])

    _LIVE = {"brightness": cv2.CAP_PROP_BRIGHTNESS, "contrast": cv2.CAP_PROP_CONTRAST, "gain": cv2.CAP_PROP_GAIN}

    def apply_live(self, changes: dict) -> set[str]:
        done = set()
        for k, v in changes.items():
            if k in self._LIVE and float(v) >= 0:  # -1 = leave the camera's own value
                self.cap.set(self._LIVE[k], float(v))
                done.add(k)
            elif k in self._LIVE:
                done.add(k)
        return done

    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        ok, img = self.cap.read()
        t = now()
        if not ok:
            self.n_failed += 1
            return None
        return Frame(img, t, self._next_idx())

    def close(self) -> None:
        if getattr(self, "cap", None) is not None:
            self.cap.release()


class FileCamera(CameraBackend):
    """Replays a video file in real time (loops). For testing the acquire GUI without a camera."""

    kind = "file"

    def open(self) -> None:
        self.path = str(self.cfg["device"])
        self.cap = cv2.VideoCapture(self.path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open video file {self.path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or self.fps
        self.size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        self._t_start = now()

    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        due = self._t_start + self._grab_idx / self.fps
        delay = due - now()
        if delay > 0:
            time.sleep(delay)
        ok, img = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, img = self.cap.read()
            if not ok:
                return None
        return Frame(img, now(), self._next_idx())

    def close(self) -> None:
        if getattr(self, "cap", None) is not None:
            self.cap.release()


class SyntheticCamera(CameraBackend):
    """Renders a simulated cage (see mavric.sim) in real time, with a drifting hardware clock."""

    kind = "synthetic"

    def __init__(self, cfg: dict, world=None):
        super().__init__(cfg)
        self.world = world
        self.hw_tick_hz = 1e9
        self.drift_ppm = float(cfg.get("drift_ppm", -20.0))
        self.drop_prob = float(cfg.get("drop_prob", 0.0))

    def open(self) -> None:
        if self.world is None:
            from ..sim import SimWorld

            self.world = SimWorld.random(duration_s=3600, seed=1)
        self.size = self.world.image_size
        self._world_t0 = self.world.ensure_started()
        self._t_start = now()
        self._counter = 0
        self._rng = np.random.default_rng(7)

    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        due = self._t_start + self._counter / self.fps
        delay = due - now()
        if delay > 0:
            time.sleep(delay)
        exposure_mid = due  # frame content corresponds to the scheduled time
        self._counter += 1
        if self._rng.random() < self.drop_prob:
            return None  # camera-side drop: counter advances, no frame delivered
        t_world = exposure_mid - self._world_t0
        img = self.world.render(t_world)
        ticks = int((exposure_mid - self._t_start) * 1e9 * (1 + self.drift_ppm * 1e-6))
        line = int(self.world.led_on(t_world))
        return Frame(img, now(), self._next_idx(), hw_ticks=ticks, hw_counter=self._counter - 1, line_status=line)


# --------------------------------------------------------------------------- Basler (pypylon)


class BaslerCamera(CameraBackend):
    """Basler ace/dart via pypylon. Free-running at fixed fps, hardware timestamp + block id.

    Set PYLON_CAMEMU=1 in the environment to test against pylon's camera emulator.
    """

    kind = "basler"

    def open(self) -> None:
        from pypylon import pylon

        self._pylon = pylon
        tlf = pylon.TlFactory.GetInstance()
        serial = str(self.cfg.get("device", "") or "")
        if serial:
            di = pylon.DeviceInfo()
            di.SetSerialNumber(serial)
            dev = tlf.CreateFirstDevice(di)
        else:
            dev = tlf.CreateFirstDevice()
        self.cam = pylon.InstantCamera(dev)
        self.cam.Open()
        self.warnings: list[str] = []
        want_chunks = bool(self.cfg.get("line_status", False))
        self._configure(want_chunks)
        try:
            self.cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
        except Exception as e:
            if not want_chunks:
                raise
            # some devices/emulators accept ChunkModeActive but cannot parse chunks: reopen without
            self.warnings.append(f"chunk mode disabled: {e}")
            self.cam.Close()
            self.cam.Open()
            self._configure(False)
            self.cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
        self._ticks_ok: bool | None = None

    def _configure(self, chunks: bool) -> None:
        self._set("PixelFormat", self.cfg.get("pixel_format", "Mono8"))
        self._set("ExposureAuto", "Off")
        self._set_first(["ExposureTime", "ExposureTimeAbs"], float(self.cfg.get("exposure_us", 8000)))
        self._set("GainAuto", "Off")
        self._set_first(["Gain", "GainRaw"], float(self.cfg.get("gain_db", 0.0)))
        self._set("AcquisitionFrameRateEnable", True)
        self._set_first(["AcquisitionFrameRate", "AcquisitionFrameRateAbs"], float(self.cfg.get("fps", 30)))
        self.line_status = False
        if chunks and self._set("ChunkModeActive", True) and self._set("ChunkSelector", "LineStatusAll"):
            self.line_status = self._set("ChunkEnable", True)
        elif not chunks:
            try:
                self.cam.ChunkModeActive.Value = False
            except Exception:
                pass
        self.hw_tick_hz = 1e9
        try:
            if self.cam.GetNodeMap().GetNode("GevTimestampTickFrequency") is not None:
                self.hw_tick_hz = float(self.cam.GevTimestampTickFrequency.Value)
        except Exception:
            pass
        try:
            self.fps = float(self.cam.ResultingFrameRate.Value)
        except Exception:
            try:
                self.fps = float(self.cam.ResultingFrameRateAbs.Value)
            except Exception:
                pass
        self.size = (int(self.cam.Width.Value), int(self.cam.Height.Value))
        self.model = self.cam.GetDeviceInfo().GetModelName()
        self.serial = self.cam.GetDeviceInfo().GetSerialNumber()
        self.cam.MaxNumBuffer.Value = max(50, int(self.fps * 3))

    def _set(self, name: str, value) -> bool:
        try:
            node = getattr(self.cam, name)
            node.Value = value
            return True
        except Exception as e:
            self.warnings.append(f"{name}={value!r}: {e}")
            return False

    def _set_first(self, names: list[str], value) -> bool:
        for n in names:
            try:
                getattr(self.cam, n).Value = value
                return True
            except Exception:
                continue
        self.warnings.append(f"none of {names} accepted {value!r}")
        return False

    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        pylon = self._pylon
        res = self.cam.RetrieveResult(int(timeout_s * 1000), pylon.TimeoutHandling_Return)
        t = now()
        if res is None or not res.IsValid():
            return None
        try:
            if not res.GrabSucceeded():
                self.n_failed += 1
                return None
            img = res.GetArray().copy()
            ticks = int(res.GetTimeStamp())
            if self._ticks_ok is None and self._grab_idx >= 1:
                self._ticks_ok = ticks != 0  # emulators report 0: no hardware clock
            if ticks == 0 and not self._ticks_ok:
                ticks = None
            try:
                counter = int(res.GetBlockID())
                if counter >= 2**63:  # "unavailable" marker
                    counter = int(res.GetImageNumber())
            except Exception:
                counter = int(res.GetImageNumber())
            line = None
            if self.line_status:
                try:
                    line = int(res.ChunkLineStatusAll.Value)
                except Exception:
                    line = None
        finally:
            res.Release()
        if img.ndim == 3 and str(self.cfg.get("pixel_format", "")).upper().startswith("RGB"):
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return Frame(img, t, self._next_idx(), hw_ticks=ticks, hw_counter=counter, line_status=line)

    def close(self) -> None:
        cam = getattr(self, "cam", None)
        if cam is not None:
            try:
                if cam.IsGrabbing():
                    cam.StopGrabbing()
                cam.Close()
            except Exception:
                pass

    def info(self) -> dict:
        d = super().info()
        d.update(
            {"model": getattr(self, "model", ""), "serial": getattr(self, "serial", ""),
             "line_status": self.line_status, "warnings": getattr(self, "warnings", [])}
        )
        return d


class IdsCamera(CameraBackend):
    kind = "ids"

    def open(self) -> None:
        raise NotImplementedError(
            "The IDS backend is not implemented yet. It needs the IDS peak SDK and the camera model; "
            "implement it like FlirCamera (mavric/video/flir.py): hardware timestamp + frame id per frame."
        )


def _flir():
    from .flir import FlirCamera  # lazy: flir.py imports this module

    return FlirCamera


BACKENDS = {
    "opencv": OpenCVCamera,
    "file": FileCamera,
    "synthetic": SyntheticCamera,
    "basler": BaslerCamera,
    "flir": _flir,  # FLIR / Point Grey via Spinnaker (Chameleon3, Blackfly S, ...)
    "ids": IdsCamera,
}


def make_backend(cfg: dict, world=None) -> CameraBackend:
    kind = cfg.get("backend", "opencv")
    if kind not in BACKENDS:
        raise ValueError(f"Unknown camera backend {kind!r}; choose from {sorted(BACKENDS)}")
    if kind == "synthetic":
        return SyntheticCamera(cfg, world)
    if kind == "flir":
        return _flir()(cfg)
    return BACKENDS[kind](cfg)
