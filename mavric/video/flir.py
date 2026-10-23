"""FLIR / Point Grey cameras through Spinnaker (PySpin): Chameleon3, Blackfly S, ...

The camera free-runs at a fixed frame rate with fixed exposure and gain. Each
frame comes with the device timestamp (USB3 Vision stream leader; tick length
already converted to nanoseconds by Spinnaker) and a frame counter (the camera's own
`FrameCounter` chunk where available, else the stream block id), so the
offline clock fit and the camera-side drop count work as for Basler.

Feature names differ between camera generations, so every setting tries the
known spellings in order:

=================  ==========================================  ===========================
setting            Chameleon3 / Flea3 / Blackfly (Spinnaker)   Blackfly S / newer (SFNC)
=================  ==========================================  ===========================
frame-rate on/off  AcquisitionFrameRateEnabled,                AcquisitionFrameRateEnable
                   AcquisitionFrameRateControlEnabled
frame-rate auto    AcquisitionFrameRateAuto, FrameRateAuto     (none)
colour 24-bit      RGB8Packed / RGB8 / BGR8                    BGR8 / RGB8
=================  ==========================================  ===========================

Per-frame GPIO line state: the Chameleon3 has no line-status chunk (its chunk
list is image CRC, frame counter, offsets, size, exposure, gain, black level),
so `line_status: true` only works on models with `ExposureEndLineStatusAll`
(e.g. Blackfly S). On the Chameleon3 the sync LED in the image is used.

Several cameras: Spinnaker's System is one object per process, and
ReleaseInstance() fails while any camera is still in use. Every camera (and
`list_flir_cameras`) therefore borrows one shared System, released by the
last user.

Install: Spinnaker SDK (Teledyne FLIR) + the matching PySpin wheel, see README.
"""

from __future__ import annotations

import sys
import threading

import cv2
import numpy as np

from ..core import now
from .backends import CameraBackend, Frame

# OpenCV names Bayer patterns by the 2x2 block starting at (1,1), hence the shift
_BAYER_TO_BGR = {
    "BayerRG8": cv2.COLOR_BayerBG2BGR,
    "BayerGR8": cv2.COLOR_BayerGB2BGR,
    "BayerGB8": cv2.COLOR_BayerGR2BGR,
    "BayerBG8": cv2.COLOR_BayerRG2BGR,
}
_COLOR_FORMATS = ["BGR8", "RGB8Packed", "RGB8", "BayerRG8", "BayerGR8", "BayerGB8", "BayerBG8"]


_SYS_LOCK = threading.Lock()
_SYS: dict = {"ps": None, "system": None, "users": 0}


def _acquire_system(ps):
    with _SYS_LOCK:
        if _SYS["system"] is None or _SYS["ps"] is not ps:
            _SYS.update(ps=ps, system=ps.System.GetInstance(), users=0)
        _SYS["users"] += 1
        return _SYS["system"]


def _release_system_ref(system) -> None:
    """Drop one user of the shared System; the last one releases it."""
    with _SYS_LOCK:
        if system is not _SYS["system"]:
            system.ReleaseInstance()  # not the shared one (should not happen)
            return
        _SYS["users"] -= 1
        if _SYS["users"] > 0:
            return
        _SYS.update(system=None, users=0)
    system.ReleaseInstance()


def _import_pyspin():
    try:
        import PySpin  # noqa: N813  (FLIR's module name)
    except ImportError as e:
        raise RuntimeError(
            "PySpin is not installed. Install the Spinnaker SDK and the PySpin wheel that matches it and "
            "this Python version (see README, 'FLIR / Point Grey cameras')."
        ) from e
    return PySpin


class FlirCamera(CameraBackend):
    kind = "flir"

    # ------------------------------------------------------------------ GenApi helpers
    def _node(self, nodemap, name: str, kind: str):
        ptr_cls = {"enum": "CEnumerationPtr", "float": "CFloatPtr", "bool": "CBooleanPtr",
                   "int": "CIntegerPtr", "str": "CStringPtr"}[kind]
        node = nodemap.GetNode(name)
        if node is None:
            return None
        ptr = getattr(self._ps, ptr_cls)(node)
        return ptr if self._ps.IsAvailable(ptr) else None

    def _set(self, name: str, kind: str, value, nodemap=None, quiet: bool = False) -> bool:
        ps = self._ps
        nm = nodemap if nodemap is not None else self.nodemap
        ptr = self._node(nm, name, kind)
        try:
            if ptr is None or not ps.IsWritable(ptr):
                raise LookupError("not available or not writable")
            if kind == "enum":
                entry = ptr.GetEntryByName(str(value))
                if entry is None or not ps.IsReadable(entry):
                    raise LookupError(f"no entry {value!r}")
                ptr.SetIntValue(entry.GetValue())
            elif kind in ("float", "int"):
                lo, hi = ptr.GetMin(), ptr.GetMax()
                v = min(max(value, lo), hi)
                if v != value:
                    self.warnings.append(f"{name}={value} clamped to {v}")
                ptr.SetValue(int(v) if kind == "int" else float(v))
            else:
                ptr.SetValue(value)
            return True
        except Exception as e:
            if not quiet:
                self.warnings.append(f"{name}={value!r}: {e}")
            return False

    def _set_first(self, names: list[str], kind: str, value, nodemap=None) -> str | None:
        for n in names:
            if self._set(n, kind, value, nodemap, quiet=True):
                return n
        self.warnings.append(f"none of {names} accepted {value!r}")
        return None

    def _get(self, name: str, kind: str, nodemap=None, default=None):
        ptr = self._node(nodemap if nodemap is not None else self.nodemap, name, kind)
        try:
            if ptr is None or not self._ps.IsReadable(ptr):
                return default
            if kind == "enum":
                return ptr.GetCurrentEntry().GetSymbolic()
            return ptr.GetValue()
        except Exception:
            return default

    def _enum_entries(self, name: str) -> list[str]:
        ptr = self._node(self.nodemap, name, "enum")
        if ptr is None:
            return []
        out = []
        for e in ptr.GetEntries():
            try:
                if self._ps.IsReadable(e):
                    out.append(self._ps.CEnumEntryPtr(e).GetSymbolic())
            except Exception:
                continue
        return out

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        ps = self._ps = _import_pyspin()
        self.warnings: list[str] = []
        self.system = _acquire_system(ps)
        self.cam_list = self.system.GetCameras()
        serial = str(self.cfg.get("device", "") or "")
        n = self.cam_list.GetSize()
        if n == 0:
            self._release_system()
            raise RuntimeError("Spinnaker found no camera (check USB3 cable/driver, or open SpinView)")
        try:
            self.cam = self.cam_list.GetBySerial(serial) if serial else self.cam_list.GetByIndex(0)
        except ps.SpinnakerException:
            self.cam = None
        if self.cam is not None and hasattr(self.cam, "IsValid") and not self.cam.IsValid():
            self.cam = None
        if self.cam is None:
            self._release_system()
            raise RuntimeError(f"No FLIR camera with serial {serial!r} ({n} camera(s) found)")
        try:
            self.cam.Init()  # fails e.g. when SpinView or another program has the camera open
            self.nodemap = self.cam.GetNodeMap()
            tl = self.cam.GetTLDeviceNodeMap()
            self.model = self._get("DeviceModelName", "str", tl, "") or self._get("DeviceModelName", "str", default="")
            self.serial = self._get("DeviceSerialNumber", "str", tl, "") or serial
            self.firmware = self._get("DeviceFirmwareVersion", "str", default="")
            del tl
            self._configure()
            self._configure_stream()
            self.cam.BeginAcquisition()
        except Exception:
            self.close()
            raise
        self._ticks_ok: bool | None = None

    def _configure(self) -> None:
        cfg = self.cfg
        self._set("AcquisitionMode", "enum", "Continuous")
        self._set("TriggerMode", "enum", "Off", quiet=True)  # free-running

        # pixel format: mono, or colour with on-board processing, else raw Bayer (demosaic here)
        want = str(cfg.get("pixel_format", "Mono8"))
        self.pixel_format = None
        if want.upper().startswith("MONO"):
            if self._set("PixelFormat", "enum", want):
                self.pixel_format = want
        else:
            available = self._enum_entries("PixelFormat")
            order = [want] + [f for f in _COLOR_FORMATS if f != want]
            for f in order:
                if (not available or f in available) and self._set("PixelFormat", "enum", f, quiet=True):
                    self.pixel_format = f
                    break
            if self.pixel_format is None:
                self.warnings.append(f"no colour pixel format accepted (tried {order})")
        if self.pixel_format is None:
            self.pixel_format = self._get("PixelFormat", "enum", default="Mono8")
        self._configure_roi()

        # fixed exposure and gain (auto exposure would change frame timing and brightness)
        self._set("ExposureAuto", "enum", "Off")
        self._set("GainAuto", "enum", "Off")
        self._set_first(["AcquisitionFrameRateAuto", "FrameRateAuto"], "enum", "Off")
        self._set_first(["AcquisitionFrameRateEnable", "AcquisitionFrameRateEnabled",
                         "AcquisitionFrameRateControlEnabled"], "bool", True)
        self._set("AcquisitionFrameRate", "float", float(cfg.get("fps", 30)))
        exp = float(cfg.get("exposure_us", 8000))
        fps_set = self._get("AcquisitionFrameRate", "float", default=float(cfg.get("fps", 30))) or 30.0
        if exp > 0.9e6 / fps_set:
            self.warnings.append(f"exposure {exp:.0f} us is longer than the frame interval at {fps_set:.1f} fps")
        self._set("ExposureTime", "float", exp)
        self._set("Gain", "float", float(cfg.get("gain_db", 0.0)))
        self._apply_image({"black_level": cfg.get("black_level"), "gamma": float(cfg.get("gamma") or 0.0)})

        # resulting frame rate and size
        self.fps = float(self._get("AcquisitionResultingFrameRate", "float", default=None)
                         or self._get("AcquisitionFrameRate", "float", default=self.fps) or self.fps)
        self.size = (int(self._get("Width", "int", default=0)), int(self._get("Height", "int", default=0)))

        # Spinnaker delivers image timestamps in nanoseconds for every camera. TimestampIncrement
        # describes the device's own Timestamp register instead (Chameleon3: 125000 ns = the
        # 125 us IIDC cycle; Blackfly S: 1 ns), so it is recorded but not applied to frame times.
        self.hw_tick_hz = 1e9
        self.timestamp_increment_ns = self._get("TimestampIncrement", "int", default=None)

        # chunks: the camera's own frame counter (true camera-side drops), line status where supported
        self._chunk_counter = False
        self.line_status = False
        want_counter = bool(cfg.get("chunk_frame_counter", True))
        want_lines = bool(cfg.get("line_status", False))
        if (want_counter or want_lines) and self._set("ChunkModeActive", "bool", True, quiet=True):
            if want_counter and self._set("ChunkSelector", "enum", "FrameCounter", quiet=True):
                self._chunk_counter = self._set("ChunkEnable", "bool", True, quiet=True)
            if want_lines:
                if self._set("ChunkSelector", "enum", "ExposureEndLineStatusAll", quiet=True):
                    self.line_status = self._set("ChunkEnable", "bool", True, quiet=True)
                if not self.line_status:
                    self.warnings.append("per-frame line status not supported by this camera "
                                         "(Chameleon3: none); the sync LED in the image is used")
        elif want_lines:
            self.warnings.append("chunk mode not available: no per-frame line status")

    def _set_int(self, name: str, value: int) -> bool:
        """Integer node, rounded down to the node's increment and clamped to its range."""
        ptr = self._node(self.nodemap, name, "int")
        if ptr is None:
            return False
        try:
            inc = int(ptr.GetInc())
        except Exception:
            inc = 1
        lo, hi = int(ptr.GetMin()), int(ptr.GetMax())
        v = max(lo, min(hi, int(value) // max(inc, 1) * max(inc, 1)))
        return self._set(name, "int", v, quiet=True)

    def _configure_roi(self) -> None:
        """Width/height/offsets (0 = full sensor); offsets go to 0 first so the size can grow.

        Always applied: the camera keeps the last region it was given while powered, so
        "full size" must be set explicitly, not assumed.
        """
        cfg = self.cfg
        w, h = int(cfg.get("width") or 0), int(cfg.get("height") or 0)
        ox, oy = int(cfg.get("offset_x") or 0), int(cfg.get("offset_y") or 0)
        self._set("OffsetX", "int", 0, quiet=True)
        self._set("OffsetY", "int", 0, quiet=True)
        for name, val in (("Width", w), ("Height", h)):
            ptr = self._node(self.nodemap, name, "int")
            if ptr is not None:
                self._set_int(name, val or int(ptr.GetMax()))
        if ox:
            self._set_int("OffsetX", ox)
        if oy:
            self._set_int("OffsetY", oy)

    def _apply_image(self, changes: dict) -> set[str]:
        done = set()
        if changes.get("black_level") is not None:
            if self._set("BlackLevel", "float", float(changes["black_level"]), quiet=True):
                done.add("black_level")
        if "gamma" in changes and changes["gamma"] is not None:
            g = float(changes["gamma"])
            if g > 0:
                ok = self._set("GammaEnabled", "bool", True, quiet=True) or self._get("GammaEnabled", "bool") is None
                if ok and self._set("Gamma", "float", g, quiet=True):
                    done.add("gamma")
            else:
                self._set("GammaEnabled", "bool", False, quiet=True)  # linear response: stable LED threshold
                done.add("gamma")
        return done

    def apply_live(self, changes: dict) -> set[str]:
        if self.cam is None:
            return set()
        done = set()
        if "exposure_us" in changes and self._set("ExposureTime", "float", float(changes["exposure_us"])):
            done.add("exposure_us")
        if "gain_db" in changes and self._set("Gain", "float", float(changes["gain_db"])):
            done.add("gain_db")
        done |= self._apply_image({k: changes[k] for k in ("black_level", "gamma") if k in changes})
        self.cfg.update({k: changes[k] for k in done})
        return done

    def _configure_stream(self) -> None:
        sn = self.cam.GetTLStreamNodeMap()
        self._set("StreamBufferHandlingMode", "enum", "OldestFirst", sn, quiet=True)  # keep every frame, in order
        if self._set("StreamBufferCountMode", "enum", "Manual", sn, quiet=True):
            self._set("StreamBufferCountManual", "int", max(50, int(self.fps * 3)), sn, quiet=True)

    # ------------------------------------------------------------------ grabbing
    def grab(self, timeout_s: float = 1.0) -> Frame | None:
        ps = self._ps
        try:
            img = self.cam.GetNextImage(int(timeout_s * 1000))
        except ps.SpinnakerException:
            return None  # timeout
        t = now()
        try:
            if img.IsIncomplete():
                self.n_failed += 1
                return None
            arr = np.array(img.GetNDArray(), copy=True)
            ticks = int(img.GetTimeStamp())
            counter = None
            if self._chunk_counter:
                counter = self._get("ChunkFrameCounter", "int", default=None)
            if counter is None:
                counter = int(img.GetFrameID())
            line = int(self._get("ChunkExposureEndLineStatusAll", "int", default=0)) if self.line_status else None
        finally:
            img.Release()
        if self._ticks_ok is None and self._grab_idx >= 1:
            self._ticks_ok = ticks != 0
        hw = None if (ticks == 0 and not self._ticks_ok) else ticks
        return Frame(self._to_bgr(arr), t, self._next_idx(), hw_ticks=hw, hw_counter=int(counter), line_status=line)

    def _to_bgr(self, arr: np.ndarray) -> np.ndarray:
        pf = self.pixel_format or ""
        if arr.ndim == 3 and arr.shape[2] == 1:
            arr = arr[:, :, 0]
        if pf in _BAYER_TO_BGR and arr.ndim == 2:
            return cv2.cvtColor(arr, _BAYER_TO_BGR[pf])
        if arr.ndim == 3 and pf.upper().startswith("RGB"):
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        return arr

    # ------------------------------------------------------------------ teardown / info
    def _release_system(self) -> None:
        try:
            if getattr(self, "cam_list", None) is not None:
                self.cam_list.Clear()
        finally:
            self.cam_list = None
            system, self.system = getattr(self, "system", None), None
            if system is not None:
                _release_system_ref(system)

    def close(self) -> None:
        """Stop, de-initialise and release everything; never raises.

        Spinnaker refuses ReleaseInstance() while any camera handle (CameraPtr)
        is still referenced, so the only handle is detached from `self`, used
        for EndAcquisition/DeInit and deleted *before* the system is released.
        """
        cam, self.cam = getattr(self, "cam", None), None
        self.nodemap = None
        if cam is not None:
            try:
                if cam.IsStreaming():
                    cam.EndAcquisition()
            except Exception as e:
                self._close_problem(f"EndAcquisition: {e}")
            try:
                if cam.IsInitialized():
                    cam.DeInit()
            except Exception as e:
                self._close_problem(f"DeInit: {e}")
            del cam
        if getattr(self, "system", None) is not None or getattr(self, "cam_list", None) is not None:
            try:
                self._release_system()
            except Exception as e:
                self._close_problem(f"ReleaseInstance: {e}")

    def _close_problem(self, msg: str) -> None:
        if not hasattr(self, "warnings"):
            self.warnings = []
        self.warnings.append(f"close: {msg}")
        print(f"[FlirCamera] {msg}", file=sys.stderr)

    def info(self) -> dict:
        d = super().info()
        d.update({"model": getattr(self, "model", ""), "serial": getattr(self, "serial", ""),
                  "firmware": getattr(self, "firmware", ""), "pixel_format": getattr(self, "pixel_format", None),
                  "line_status": getattr(self, "line_status", False),
                  "chunk_frame_counter": getattr(self, "_chunk_counter", False),
                  "timestamp_increment_ns": getattr(self, "timestamp_increment_ns", None),
                  "offset_x": self._get("OffsetX", "int", default=0) if getattr(self, "nodemap", None) else None,
                  "offset_y": self._get("OffsetY", "int", default=0) if getattr(self, "nodemap", None) else None,
                  "exposure_us": self.cfg.get("exposure_us"), "gain_db": self.cfg.get("gain_db"),
                  "black_level": self.cfg.get("black_level"), "gamma": self.cfg.get("gamma"),
                  "warnings": getattr(self, "warnings", [])})
        return d


def list_flir_cameras() -> list[dict]:
    """Model/serial of every camera Spinnaker sees (empty if PySpin is missing)."""
    try:
        ps = _import_pyspin()
    except RuntimeError:
        return []
    def text(nodemap, name):
        node = nodemap.GetNode(name)
        ptr = ps.CStringPtr(node) if node is not None else None
        return ptr.GetValue() if ptr is not None and ps.IsReadable(ptr) else ""

    system = _acquire_system(ps)  # shared: listing works while cameras are streaming
    cams = system.GetCameras()
    out = []
    try:
        for i in range(cams.GetSize()):
            cam = cams.GetByIndex(i)
            tl = cam.GetTLDeviceNodeMap()
            out.append({"model": text(tl, "DeviceModelName"), "serial": text(tl, "DeviceSerialNumber")})
            del tl, cam  # no camera handle may survive until ReleaseInstance()
    finally:
        cams.Clear()
        _release_system_ref(system)
    return out
