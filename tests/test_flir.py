"""FLIR/Spinnaker backend against a fake PySpin (no camera needed).

The fake models just the GenApi surface the backend uses, with two node
layouts: Chameleon3 under Spinnaker (AcquisitionFrameRateEnabled +
AcquisitionFrameRateAuto, no line-status chunk) and Blackfly S (SFNC names,
ExposureEndLineStatusAll chunk).
"""

import sys
import time
import types
import weakref

import numpy as np
import pytest

from mavric.video.backends import make_backend
from mavric.video.check import camera_check, led_pulse_warning


# --------------------------------------------------------------------------- fake PySpin


class SpinnakerException(Exception):
    pass


class Entry:
    def __init__(self, sym, val, readable=True):
        self.sym, self.val, self.readable = sym, val, readable

    def GetValue(self):
        return self.val

    def GetSymbolic(self):
        return self.sym


class Node:
    def __init__(self, kind, value=None, entries=(), lo=None, hi=None, writable=True):
        self.kind, self.value, self.lo, self.hi, self.writable = kind, value, lo, hi, writable
        self.available = self.readable = True
        self.entries = [Entry(s, i) for i, s in enumerate(entries)]
        self.writes = []


class _Ptr:
    def __init__(self, node):
        self.n = node

    def GetValue(self):
        return self.n.value

    def SetValue(self, v):
        if self.n.lo is not None and not (self.n.lo <= v <= self.n.hi):
            raise SpinnakerException(f"out of range {v}")
        self.n.value = v
        self.n.writes.append(v)

    def GetMin(self):
        return self.n.lo

    def GetMax(self):
        return self.n.hi


class CEnumerationPtr(_Ptr):
    def GetEntryByName(self, name):
        return next((e for e in self.n.entries if e.sym == name), None)

    def SetIntValue(self, v):
        self.n.value = next(e.sym for e in self.n.entries if e.val == v)
        self.n.writes.append(self.n.value)

    def GetCurrentEntry(self):
        return self.GetEntryByName(self.n.value)

    def GetEntries(self):
        return self.n.entries


CFloatPtr = CIntegerPtr = CBooleanPtr = CStringPtr = _Ptr


def CEnumEntryPtr(e):
    return e


def IsAvailable(p):
    return getattr(p, "n", p).available if p is not None else False


def IsReadable(p):
    return getattr(p, "n", p).readable


def IsWritable(p):
    return getattr(p, "n", p).writable


SPINNAKER_IMAGE_STATUS_IMAGE_CHUNK_DATA_INVALID = 11


def Image_GetImageStatusDescription(status):
    return {0: "Image has no error", 11: "Image has invalid chunk data"}.get(status, "Image is incomplete")


class NodeMap(dict):
    def GetNode(self, name):
        return self.get(name)


class Image:
    def __init__(self, arr, ts, fid, incomplete=False, status=0):
        self.arr, self.ts, self.fid, self.incomplete, self.status = arr, ts, fid, incomplete, status
        self.released = False

    def IsIncomplete(self):
        return self.incomplete

    def GetImageStatus(self):
        return self.status

    def GetNDArray(self):
        return self.arr

    def GetTimeStamp(self):
        return self.ts

    def GetFrameID(self):
        return self.fid

    def Release(self):
        self.released = True


def chameleon3_nodes(color=False):
    fmts = ["Mono8", "Mono16", "RGB8Packed", "BayerRG8"] if color else ["Mono8", "Mono16"]
    return NodeMap({
        "AcquisitionMode": Node("enum", "Continuous", ["Continuous", "SingleFrame", "MultiFrame"]),
        "TriggerMode": Node("enum", "Off", ["Off", "On"]),
        "PixelFormat": Node("enum", "Mono8", fmts),
        "ExposureAuto": Node("enum", "Continuous", ["Off", "Once", "Continuous"]),
        "GainAuto": Node("enum", "Continuous", ["Off", "Once", "Continuous"]),
        "AcquisitionFrameRateAuto": Node("enum", "Continuous", ["Off", "Continuous"]),
        "AcquisitionFrameRateEnabled": Node("bool", False),
        "AcquisitionFrameRate": Node("float", 149.0, lo=1.0, hi=149.0),
        "ExposureTime": Node("float", 5000.0, lo=6.0, hi=999240.0),
        "Gain": Node("float", 0.0, lo=0.0, hi=18.0),
        "GammaEnabled": Node("bool", True),
        "Width": Node("int", 1280, lo=8, hi=1280),
        "Height": Node("int", 1024, lo=8, hi=1024),
        # real CM3-U3-13Y3M: device Timestamp register ticks are 125 us; image timestamps are ns
        "TimestampIncrement": Node("int", 125000, lo=125000, hi=125000, writable=False),
        "DeviceFirmwareVersion": Node("str", "1.13.3.0", writable=False),
        "ChunkModeActive": Node("bool", False),
        "ChunkSelector": Node("enum", "Image", ["Image", "CRC", "FrameCounter", "OffsetX", "OffsetY",
                                                "Width", "Height", "ExposureTime", "Gain", "BlackLevel"]),
        "ChunkEnable": Node("bool", False),
        "ChunkFrameCounter": Node("int", 0, lo=0, hi=2**32, writable=False),
    })


def blackfly_s_nodes():
    nm = chameleon3_nodes(color=True)
    del nm["AcquisitionFrameRateAuto"], nm["AcquisitionFrameRateEnabled"]
    nm["AcquisitionFrameRateEnable"] = Node("bool", False)
    nm["PixelFormat"] = Node("enum", "Mono8", ["Mono8", "BGR8", "BayerRG8"])
    nm["ChunkSelector"] = Node("enum", "Image", ["Image", "FrameID", "Timestamp", "ExposureEndLineStatusAll"])
    nm["ChunkExposureEndLineStatusAll"] = Node("int", 0, lo=0, hi=255, writable=False)
    nm["TimestampIncrement"] = Node("int", 1, lo=1, hi=1, writable=False)
    return nm


class Camera:
    def __init__(self, serial="20123456", model="Chameleon3 CM3-U3-13Y3M", nodes=None, realtime=False):
        self.nodes = nodes or chameleon3_nodes()
        self.tl = NodeMap({"DeviceSerialNumber": Node("str", serial), "DeviceModelName": Node("str", model)})
        self.stream = NodeMap({
            "StreamBufferHandlingMode": Node("enum", "NewestOnly", ["OldestFirst", "NewestOnly", "NewestFirst"]),
            "StreamBufferCountMode": Node("enum", "Auto", ["Auto", "Manual"]),
            "StreamBufferCountManual": Node("int", 10, lo=1, hi=1000),
        })
        self.serial, self.realtime = serial, realtime
        self.inited = self.streaming = False
        self.queue: list = []
        self.issued: list[Image] = []
        self.k = 0
        self.calls: list[str] = []

    # lifecycle
    fail_init = False

    def Init(self):
        if self.fail_init:
            raise SpinnakerException("Spinnaker: Camera is already streaming or in use [-1022]")
        self.inited = True

    def DeInit(self):
        self.inited = False
        self.calls.append("DeInit")

    def IsInitialized(self):
        return self.inited

    def IsValid(self):
        return True

    def BeginAcquisition(self):
        assert self.inited
        self.streaming = True
        self.t0 = time.perf_counter()

    def EndAcquisition(self):
        self.streaming = False
        self.calls.append("EndAcquisition")

    def IsStreaming(self):
        return self.streaming

    def GetNodeMap(self):
        return self.nodes

    def GetTLDeviceNodeMap(self):
        return self.tl

    def GetTLStreamNodeMap(self):
        return self.stream

    # frames
    def _make(self, k):
        fmt = self.nodes["PixelFormat"].value
        h, w = 48, 64
        if fmt in ("RGB8Packed", "RGB8"):
            arr = np.zeros((h, w, 3), np.uint8)
            arr[..., 0] = 200  # red
        elif fmt == "BGR8":
            arr = np.zeros((h, w, 3), np.uint8)
            arr[..., 2] = 200  # red in BGR
        else:
            arr = np.full((h, w), k % 256, np.uint8)
        fps = self.nodes["AcquisitionFrameRate"].value
        ts = 5_000_000_000 + int(k / fps * 1e9)  # ns, device clock
        return Image(arr, ts, fid=1000 + k)

    def GetNextImage(self, timeout_ms):
        if not self.streaming:
            raise SpinnakerException("not streaming")
        if self.queue:
            img = self.queue.pop(0)
        elif self.realtime:
            fps = self.nodes["AcquisitionFrameRate"].value
            delay = self.t0 + self.k / fps - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            img = self._make(self.k)
            self.k += 1
        else:
            raise SpinnakerException("timeout")
        self.nodes["ChunkFrameCounter"].value = img.fid - 1000 + 7  # camera counter differs from block id
        if "ChunkExposureEndLineStatusAll" in self.nodes:
            self.nodes["ChunkExposureEndLineStatusAll"].value = 1 if img.fid % 2 else 0
        self.issued.append(img)
        return img


class CameraPtr:
    """Like PySpin's CameraPtr: a handle that must be gone before System.ReleaseInstance()."""

    def __init__(self, cam):
        self._cam = cam

    def __getattr__(self, name):
        return getattr(self._cam, name)


class CameraList:
    def __init__(self, cams, system):
        self.cams = list(cams)
        self.system = system
        self.cleared = False

    def _ptr(self, cam):
        p = CameraPtr(cam)
        self.system.handles.add(p)
        return p

    def GetSize(self):
        return len(self.cams)

    def GetByIndex(self, i):
        return self._ptr(self.cams[i])

    def GetBySerial(self, s):
        for c in self.cams:
            if c.serial == s:
                return self._ptr(c)
        raise SpinnakerException(f"no camera {s}")

    def Clear(self):
        self.cams = []
        self.cleared = True


class System:
    current = None

    def __init__(self, cams):
        self.cams = cams
        self.released = False
        self.lists = []
        self.handles = weakref.WeakSet()  # live CameraPtr objects

    @classmethod
    def GetInstance(cls):
        return cls.current

    def GetCameras(self):
        cl = CameraList(self.cams, self)
        self.lists.append(cl)
        return cl

    def ReleaseInstance(self):
        if len(self.handles):  # CPython frees unreferenced handles immediately
            raise SpinnakerException(
                "Spinnaker: Can't clear a camera because something still holds a reference to the camera [-1004]")
        self.released = True


def fake_pyspin(cams):
    mod = types.ModuleType("PySpin")
    for name in ("SpinnakerException", "CEnumerationPtr", "CFloatPtr", "CIntegerPtr", "CBooleanPtr",
                 "CStringPtr", "CEnumEntryPtr", "IsAvailable", "IsReadable", "IsWritable", "System",
                 "SPINNAKER_IMAGE_STATUS_IMAGE_CHUNK_DATA_INVALID", "Image_GetImageStatusDescription"):
        setattr(mod, name, globals()[name])
    System.current = System(cams)
    return mod


@pytest.fixture
def pyspin(monkeypatch):
    def install(*cams):
        mod = fake_pyspin(list(cams))
        monkeypatch.setitem(sys.modules, "PySpin", mod)
        return System.current

    return install


CFG = {"id": "cam0", "backend": "flir", "device": "", "fps": 30, "exposure_us": 8000, "gain_db": 2.0,
       "pixel_format": "Mono8", "line_status": False}


# --------------------------------------------------------------------------- tests


def test_chameleon3_is_configured_for_fixed_free_running_capture(pyspin):
    cam = Camera()
    pyspin(cam)
    be = make_backend(dict(CFG, line_status=True))
    be.open()
    n = cam.nodes
    assert n["PixelFormat"].value == "Mono8"
    assert n["ExposureAuto"].value == "Off" and n["GainAuto"].value == "Off"
    assert n["AcquisitionFrameRateAuto"].value == "Off"
    assert n["AcquisitionFrameRateEnabled"].value is True
    assert n["AcquisitionFrameRate"].value == 30.0
    assert n["ExposureTime"].value == 8000.0 and n["Gain"].value == 2.0
    assert n["GammaEnabled"].value is False
    assert cam.stream["StreamBufferHandlingMode"].value == "OldestFirst"
    assert cam.stream["StreamBufferCountManual"].value == 90
    assert n["ChunkModeActive"].value is True and "FrameCounter" in n["ChunkSelector"].writes
    info = be.info()
    assert info["model"] == "Chameleon3 CM3-U3-13Y3M" and info["serial"] == "20123456"
    assert info["firmware"] == "1.13.3.0"
    assert (info["width"], info["height"], info["fps"]) == (1280, 1024, 30.0)
    assert be.hw_tick_hz == 1e9  # image timestamps are ns, whatever TimestampIncrement says
    assert info["timestamp_increment_ns"] == 125000
    assert info["line_status"] is False  # Chameleon3 has no line-status chunk ...
    assert any("line status" in w for w in info["warnings"])  # ... and says so
    assert cam.streaming
    be.close()


def test_grab_returns_hw_timestamp_camera_counter_and_skips_incomplete(pyspin):
    cam = Camera()
    sysobj = pyspin(cam)
    be = make_backend(CFG)
    be.open()
    cam.queue = [cam._make(0), cam._make(1), Image(np.zeros((48, 64), np.uint8), 0, 1002, incomplete=True),
                 cam._make(3)]
    frames = [be.grab(0.1) for _ in range(4)]
    assert frames[2] is None and be.n_failed == 1
    good = [f for f in frames if f is not None]
    assert [f.grab_idx for f in good] == [0, 1, 2]
    assert [f.hw_counter for f in good] == [7, 8, 10]  # ChunkFrameCounter, not the stream block id
    assert good[1].hw_ticks - good[0].hw_ticks == pytest.approx(1e9 / 30, abs=1)
    assert good[0].image.shape == (48, 64) and good[0].line_status is None
    assert good[0].host_abs <= good[1].host_abs
    assert be.grab(0.01) is None  # timeout -> None, no exception
    assert all(img.released for img in cam.issued)
    be.close()
    assert cam.calls == ["EndAcquisition", "DeInit"]
    assert all(cl.cleared for cl in sysobj.lists) and sysobj.released


def test_frame_id_used_when_no_counter_chunk(pyspin):
    nodes = chameleon3_nodes()
    del nodes["ChunkModeActive"]
    cam = Camera(nodes=nodes)
    pyspin(cam)
    be = make_backend(CFG)
    be.open()
    cam.queue = [cam._make(0), cam._make(1)]
    assert [be.grab(0.1).hw_counter for _ in range(2)] == [1000, 1001]
    be.close()


def test_colour_chameleon3_rgb_is_converted_to_bgr(pyspin):
    cam = Camera(nodes=chameleon3_nodes(color=True), model="Chameleon3 CM3-U3-13Y3C")
    pyspin(cam)
    be = make_backend(dict(CFG, pixel_format="BGR8"))
    be.open()
    assert be.pixel_format == "RGB8Packed"  # no BGR8 on Chameleon3: next best on-board format
    cam.queue = [cam._make(0)]
    img = be.grab(0.1).image
    assert img.shape == (48, 64, 3) and img[0, 0, 2] == 200 and img[0, 0, 0] == 0  # red ends up in BGR[2]
    be.close()


def test_bayer_fallback_is_demosaiced(pyspin):
    nodes = chameleon3_nodes(color=True)
    nodes["PixelFormat"] = Node("enum", "Mono8", ["Mono8", "BayerRG8"])
    cam = Camera(nodes=nodes)
    pyspin(cam)
    be = make_backend(dict(CFG, pixel_format="BGR8"))
    be.open()
    assert be.pixel_format == "BayerRG8"
    cam.queue = [cam._make(5)]
    assert be.grab(0.1).image.shape == (48, 64, 3)
    be.close()


def test_blackfly_s_names_and_line_status_chunk(pyspin):
    cam = Camera(nodes=blackfly_s_nodes(), model="Blackfly S BFS-U3-16S2M")
    pyspin(cam)
    be = make_backend(dict(CFG, line_status=True))
    be.open()
    assert cam.nodes["AcquisitionFrameRateEnable"].value is True
    assert be.line_status is True
    cam.queue = [cam._make(0), cam._make(1)]
    assert [be.grab(0.1).line_status for _ in range(2)] == [0, 1]
    be.close()


def test_exposure_longer_than_frame_interval_is_reported_and_clamped(pyspin):
    cam = Camera()
    pyspin(cam)
    be = make_backend(dict(CFG, exposure_us=2_000_000, fps=30))
    be.open()
    w = " ".join(be.info()["warnings"])
    assert "longer than the frame interval" in w and "clamped" in w
    assert cam.nodes["ExposureTime"].value == 999240.0
    be.close()


def test_serial_selection_and_errors(pyspin):
    a, b = Camera(serial="111"), Camera(serial="222")
    pyspin(a, b)
    be = make_backend(dict(CFG, device="222"))
    be.open()
    assert be.info()["serial"] == "222" and b.streaming and not a.streaming
    be.close()
    sysobj = pyspin(Camera(serial="111"))
    with pytest.raises(RuntimeError, match="serial '999'"):
        make_backend(dict(CFG, device="999")).open()
    assert sysobj.released
    pyspin()
    with pytest.raises(RuntimeError, match="no camera"):
        make_backend(CFG).open()


def test_list_flir_cameras(pyspin):
    from mavric.video.flir import list_flir_cameras

    sysobj = pyspin(Camera(serial="111"), Camera(serial="222", model="Blackfly S"))
    assert list_flir_cameras() == [{"model": "Chameleon3 CM3-U3-13Y3M", "serial": "111"},
                                   {"model": "Blackfly S", "serial": "222"}]
    assert sysobj.released


def test_init_failure_releases_spinnaker(pyspin):
    """E.g. SpinView still has the camera open: the error surfaces and Spinnaker is released cleanly."""
    cam = Camera()
    cam.fail_init = True
    sysobj = pyspin(cam)
    with pytest.raises(SpinnakerException, match="in use"):
        make_backend(CFG).open()
    assert sysobj.released


def test_close_never_raises_and_reports_leaked_handle(pyspin):
    sysobj = pyspin(Camera())
    be = make_backend(CFG)
    be.open()
    leaked = be.cam  # simulate a handle kept elsewhere
    be.close()  # must not raise
    assert not sysobj.released
    assert any("ReleaseInstance" in w for w in be.info()["warnings"])
    del leaked


def test_missing_pyspin_gives_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "PySpin", None)  # makes `import PySpin` raise ImportError
    with pytest.raises(RuntimeError, match="Spinnaker SDK"):
        make_backend(CFG).open()


def test_led_pulse_warning():
    assert led_pulse_warning(30, 0.080) is None
    w = led_pulse_warning(13, 0.080)  # Chameleon3 CM3-U3-28S4 at full resolution
    assert w and "SYNC_PULSE_US >= 154000" in w


def test_camtest_on_fake_chameleon3(pyspin):
    cam = Camera(realtime=True)
    pyspin(cam)
    rep = camera_check(CFG, seconds=1.0)
    assert rep["frames"] >= 25
    assert rep["camera_dropped"] == 0
    assert rep["hw_clock"]["hw_fps"] == pytest.approx(30, rel=0.01)
    assert abs(rep["hw_clock"]["drift_ppm"]) < 2000  # sane units (was 1.25e11 with the 125 us increment)
    assert not any("pulse" in w for w in rep["warnings"])


def test_recording_with_fake_chameleon3(pyspin, tmp_path):
    """End to end through the controller: sidecar gets hardware ticks and camera counters."""
    import pandas as pd

    from mavric.acquire import AcquisitionController
    from mavric.config import RoiSet, TagMap, load_rig

    cam = Camera(realtime=True)
    pyspin(cam)
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    rig["cameras"] = [dict(CFG, line_status=True)]
    ctl = AcquisitionController(rig, TagMap(), RoiSet())
    try:
        ctl.connect_camera(rig["cameras"][0])
        sess = ctl.start_recording("flir", "fake camera")
        time.sleep(1.5)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    fr = pd.read_csv(sess.file("frames_cam0.csv"))
    assert len(fr) >= 35
    assert fr["hw_ticks"].notna().all() and fr["hw_counter"].diff().dropna().eq(1).all()
    meta_cam = sess.meta["cameras"][0]
    assert meta_cam["backend"] == "flir" and meta_cam["model"].startswith("Chameleon3")
    assert any("line status" in w for w in sess.meta.get("warnings", []))

    # analysis: camera clock fit in sane units, also for sessions saved with the old 125 us tick rate
    from mavric.analysis.io import load_frames
    from mavric.analysis.sync import fit_camera_clock, camera_tick_hz

    meta_cam["hw_tick_hz"] = 8000.0  # what the first version stored for a Chameleon3
    fit = fit_camera_clock(load_frames(sess), camera_tick_hz(meta_cam))
    assert abs(fit.drift_ppm) < 5000


def test_two_cameras_share_spinnaker(pyspin):
    """Closing one camera must not release Spinnaker under the other (one System per process)."""
    from mavric.video.flir import list_flir_cameras

    a, b = Camera(serial="111"), Camera(serial="222")
    sysobj = pyspin(a, b)
    be_a = make_backend(dict(CFG, id="cam0", device="111"))
    be_b = make_backend(dict(CFG, id="cam1", device="222"))
    be_a.open()
    be_b.open()
    assert a.streaming and b.streaming
    assert [c["serial"] for c in list_flir_cameras()] == ["111", "222"]  # listing while both stream
    assert not sysobj.released
    be_a.close()
    assert not sysobj.released and b.streaming
    b.queue = [b._make(0)]
    assert be_b.grab(0.1) is not None
    be_b.close()
    assert sysobj.released
    for be in (be_a, be_b):
        assert not [w for w in be.info()["warnings"] if w.startswith("close")]


def test_missing_frames_are_explained(pyspin):
    """No silent blank view: USB 2 links, a stuck TriggerMode, incomplete images and timeouts are reported."""
    nodes = chameleon3_nodes()
    nodes["TriggerMode"] = Node("enum", "On", ["Off", "On"], writable=False)
    nodes["DeviceLinkThroughputLimit"] = Node("int", 380_000_000, lo=1_000_000, hi=380_000_000)
    cam = Camera(nodes=nodes)
    cam.tl["DeviceCurrentSpeed"] = Node("enum", "HighSpeed", ["LowSpeed", "FullSpeed", "HighSpeed", "SuperSpeed"])
    pyspin(cam)
    be = make_backend(dict(CFG, link_limit_mb_s=150))
    be.open()
    info = be.info()
    assert info["link_speed"] == "HighSpeed" and any("not SuperSpeed" in w for w in info["warnings"])
    assert any("TriggerMode is still On" in w for w in info["warnings"])
    assert nodes["DeviceLinkThroughputLimit"].value == 150_000_000 and info["link_limit_mb_s"] == 150
    cam.queue = [Image(np.zeros((48, 64), np.uint8), 0, 1000, incomplete=True)]
    assert be.grab(0.1) is None and be.n_failed == 1 and be.last_problem.startswith("incomplete image")
    assert be.grab(0.01) is None and be.n_timeouts == 1 and be.last_problem.startswith("no image within")
    be.close()


def test_camtest_all_cameras_at_once(pyspin):
    from mavric.video.check import cameras_check

    pyspin(Camera(serial="111", realtime=True), Camera(serial="222", realtime=True))
    reps = cameras_check([dict(CFG, id="cam0", device="111"), dict(CFG, id="cam1", device="222")], seconds=1.0)
    assert [r["info"]["serial"] for r in reps] == ["111", "222"]
    assert all(r["frames"] >= 25 and r["timeouts"] == 0 and r["failed_frames"] == 0 for r in reps)


def test_status_explains_a_camera_without_frames(pyspin):
    from mavric.acquire import AcquisitionController, stall_text
    from mavric.config import RoiSet, TagMap, load_rig

    pyspin(Camera())  # connects, but every grab times out
    ctl = AcquisitionController(load_rig(), TagMap(), RoiSet())
    try:
        ctl.connect_camera(dict(CFG, id="cam1"))
        time.sleep(0.3)
        c = ctl.status()["cameras"]["cam1"]
    finally:
        ctl.shutdown()
    assert c["frames"] == 0 and c["timeouts"] >= 1
    text = stall_text(c, after_s=0.1)
    assert text.startswith("NO FRAMES") and "no image within" in text
    assert stall_text(c, after_s=60) is None


def test_invalid_chunk_data_turns_chunks_off_and_close_restores_the_camera(pyspin):
    """A Chameleon3 whose chunks Spinnaker can't parse: every image is IMAGE_CHUNK_DATA_INVALID until
    chunk mode is off. MAVRIC turns it off and keeps streaming, and never leaves chunk mode on behind it."""
    cam = Camera()
    pyspin(cam)
    be = make_backend(CFG)
    be.open()
    assert cam.nodes["ChunkModeActive"].value is True and be.info()["chunk_frame_counter"]
    bad = [Image(np.zeros((48, 64), np.uint8), 0, 1000 + k, incomplete=True, status=11) for k in range(3)]
    cam.queue = bad + [cam._make(3)]
    assert [be.grab(0.1) for _ in range(3)] == [None, None, None]
    assert "invalid chunk data" in be.last_problem
    assert cam.nodes["ChunkModeActive"].value is False and cam.streaming
    fr = be.grab(0.1)
    assert fr is not None and fr.hw_counter == 1003  # stream frame ID, no longer the chunk counter
    info = be.info()
    assert not info["chunk_frame_counter"] and any("chunk data invalid" in w for w in info["warnings"])
    be.close()

    cam2 = Camera()  # a healthy camera: chunk mode on while streaming, back off after close
    pyspin(cam2)
    be2 = make_backend(CFG)
    be2.open()
    assert cam2.nodes["ChunkModeActive"].value is True
    be2.close()
    assert cam2.nodes["ChunkModeActive"].value is False
