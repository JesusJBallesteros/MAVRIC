"""Raw (lossless, chunked) recording, frame-accurate reading, compression, video settings."""

import os
import time

import cv2
import numpy as np
import pandas as pd
import pytest

from mavric.video.compress import SUFFIX, compress_parts, compress_path, ffmpeg_command
from mavric.video.reader import VideoReader
from mavric.video.recorder import LosslessChunkWriter


def numbered_frames(n, w=320, h=240, color=False, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        img = np.full((h, w), 50, np.uint8)
        cv2.putText(img, f"{k:03d}", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 3, 255, 6)
        img = cv2.add(img, rng.integers(0, 20, img.shape, dtype=np.uint8))
        if color:
            img = np.dstack([img, 255 - img, img // 2])
        out.append(img)
    return out


@pytest.mark.parametrize("codec,color", [("ffv1", False), ("ffv1", True), ("rawvideo", False), ("rawvideo", True)])
def test_lossless_chunks_are_bit_exact(tmp_path, codec, color):
    frames = numbered_frames(90, color=color)
    seen = []
    w = LosslessChunkWriter(tmp_path / "cam0", 30.0, (320, 240), codec, chunk_mb=0.8, on_chunk=seen.append)
    for f in frames:
        w.write(f)
    w.close()
    assert len(w.files) >= 3 and seen[-1] == w.files
    assert [f["first_frame"] for f in w.files] == list(np.cumsum([0] + [f["n_frames"] for f in w.files[:-1]]))
    assert sum(f["n_frames"] for f in w.files) == 90
    assert w.files[0]["file"] == f"cam0_raw_000.{'mkv' if codec == 'ffv1' else 'avi'}"
    r = VideoReader([(tmp_path / f["file"], f["first_frame"], f["n_frames"]) for f in w.files])
    assert r.n == 90
    for i in (0, 1, 2, 45, 3, 89, 30, 31, 60, 10):  # random access across chunk boundaries
        img = r.get(i)
        got = img if color else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        assert np.array_equal(got, frames[i]), i
    r.close()


def test_frame_size_change_starts_a_new_chunk(tmp_path):
    w = LosslessChunkWriter(tmp_path / "c", 30.0, (320, 240), "ffv1", chunk_mb=100)
    for f in numbered_frames(5):
        w.write(f)
    for f in numbered_frames(5, w=160, h=120):
        w.write(f)
    w.close()
    assert [f["n_frames"] for f in w.files] == [5, 5]


def test_compress_keeps_every_frame_and_seeks(tmp_path):
    frames = numbered_frames(120)
    w = LosslessChunkWriter(tmp_path / "cam0", 30.0, (320, 240), "ffv1", chunk_mb=0.8)
    for f in frames:
        w.write(f)
    w.close()
    parts = [tmp_path / f["file"] for f in w.files]
    res = compress_parts(parts, tmp_path / "cam0_compressed.mp4", 30.0, expected_frames=120)
    assert res["frames"] == 120 and (res["width"], res["height"]) == (320, 240) and not res["geometry_changed"]
    cap = cv2.VideoCapture(str(tmp_path / "cam0_compressed.mp4"))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == 120 and cap.get(cv2.CAP_PROP_FPS) == pytest.approx(30)
    for i in (0, 37, 100, 119):  # DeepLabCut-style seeking lands on the right frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, img = cap.read()
        diffs = [np.abs(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(int) - f).mean() for f in frames]
        assert int(np.argmin(diffs)) == i and min(diffs) < 4
    cap.release()
    small = compress_parts(parts, tmp_path / "half.mp4", 30.0, {"downscale": 2, "crf": 23})
    assert (small["width"], small["height"]) == (160, 120) and small["geometry_changed"]
    with pytest.raises(RuntimeError, match="expected 121"):
        compress_parts(parts, tmp_path / "bad.mp4", 30.0, expected_frames=121)
    assert not (tmp_path / "bad.mp4").exists() and not list(tmp_path.glob("*.part*"))


def test_compress_folder_names_and_skips(tmp_path):
    w = LosslessChunkWriter(tmp_path / "cam1", 30.0, (320, 240), "ffv1", chunk_mb=0.5)
    for f in numbered_frames(40):
        w.write(f)
    w.close()
    other = LosslessChunkWriter(tmp_path / "loose", 30.0, (320, 240), "rawvideo", chunk_mb=100)
    for f in numbered_frames(10):
        other.write(f)
    other.close()
    res = compress_path(tmp_path)
    assert sorted(r["file"] for r in res) == [f"cam1{SUFFIX}.mp4", f"loose{SUFFIX}.mp4"]
    assert {r["file"]: r["frames"] for r in res} == {f"cam1{SUFFIX}.mp4": 40, f"loose{SUFFIX}.mp4": 10}
    again = compress_path(tmp_path)
    assert all(r.get("skipped") == "exists" for r in again)  # and *_compressed files are never inputs
    cmd = ffmpeg_command([tmp_path / "a.mkv", tmp_path / "b.mkv"], tmp_path / "o.mp4", 30.0, {"downscale": 2})
    for part in ("-f concat", "-c:v libx264", "-crf 18", "-g 30", "-sc_threshold 0", "-pix_fmt yuv420p",
                 "scale=trunc(iw/2/2)*2"):
        assert part in cmd


def test_raw_session_record_review_compress(tmp_path):
    from mavric.acquire import simulation_controller
    from mavric.analysis.pipeline import prepare
    from mavric.analysis.render import render_annotated
    from mavric.config import load_rig
    from mavric.session import Session
    from mavric.video.compress import compress_session
    from mavric.video.reader import open_session_video, video_parts

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    rig["video"] = {**rig["video"], "record_format": "raw", "raw_codec": "ffv1", "raw_chunk_mb": 5}
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30},
                      {"id": "cam1", "backend": "synthetic", "fps": 30, "record": "triggered"}]
    rig["triggers"] = [{"name": "any", "camera": "cam1", "antennas": "any", "pre_s": 0.5, "post_s": 0.5}]
    ctl = simulation_controller(rig, seed=4)
    try:
        ctl.connect_rfid(ctl._sim_source)
        for c in rig["cameras"]:
            ctl.connect_camera(c)
        time.sleep(1.0)
        sess = ctl.start_recording("raw", "test")
        time.sleep(6)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    sess = Session.open(sess.path)
    cam = sess.camera()
    n = len(pd.read_csv(sess.file("frames_cam0.csv")))
    assert cam["record_format"] == "raw" and len(cam["video_files"]) >= 2
    assert sum(f["n_frames"] for f in cam["video_files"]) == n
    reader = open_session_video(sess)
    assert reader.n == n and reader.get(n - 1) is not None
    reader.close()
    P = prepare(sess)
    out = render_annotated(P, out_path=sess.analysis_dir / "r.mp4", t_range=(1.0, 2.0))
    assert out.exists()
    assert sess.clips and sess.clips[0]["record_format"] == "raw"

    res = compress_session(sess, {"crf": 23, "preset": "veryfast"})
    names = sorted(r["file"] for r in res)
    assert "cam0_compressed.mp4" in names and any(x.startswith("clips/") and SUFFIX in x for x in names)
    sess = Session.open(sess.path)
    cam = sess.camera()
    assert cam["compressed_file"] == "cam0_compressed.mp4" and cam["compressed"]["frames"] == n
    assert video_parts(sess, cam, prefer="compressed")[0][0].name == "cam0_compressed.mp4"
    assert video_parts(sess, cam)[0][0].name == "cam0_raw_000.mkv"  # raw stays the default for review
    assert sess.clips[0]["compressed_file"].endswith(f"{SUFFIX}.mp4")
    assert all(r.get("skipped") for r in compress_session(sess))  # nothing redone without --overwrite


def test_video_settings_file_overrides_rig(tmp_path):
    from mavric.config import load_rig
    from mavric.video.settings import VideoSettings, params_for, video_cfg

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "rig.yaml").write_text("cameras:\n  - {id: cam0, backend: flir, fps: 30, exposure_us: 8000}\n"
                                  "video: {crf: 20}\n", encoding="utf-8")
    vs = VideoSettings({"cam0": {"fps": 60, "exposure_us": 4000, "width": 640}},
                       {"record_format": "h264", "raw_chunk_mb": 500, "compress": {"crf": 23}})
    vs.save(cfg / "video.yaml")
    rig = load_rig(cfg / "rig.yaml")
    cam = rig["cameras"][0]
    assert (cam["fps"], cam["exposure_us"], cam["width"]) == (60, 4000, 640)
    v = video_cfg(rig)
    assert v["record_format"] == "h264" and v["raw_chunk_mb"] == 500 and v["crf"] == 20
    assert v["compress"]["crf"] == 23 and v["compress"]["gop"] == 30  # defaults fill the rest
    keys = {p.key for p in params_for("flir")}
    assert {"fps", "exposure_us", "gain_db", "black_level", "gamma", "width", "height", "offset_x"} <= keys
    assert {p.key for p in params_for("flir") if p.live} == {"exposure_us", "gain_db", "black_level", "gamma",
                                                            "white_balance"}


def test_flir_roi_brightness_gamma_and_live_changes(monkeypatch):
    import sys

    from test_flir import CFG, Camera, Node, chameleon3_nodes, fake_pyspin

    from mavric.video.backends import make_backend

    nm = chameleon3_nodes()
    nm["OffsetX"] = Node("int", 0, lo=0, hi=1272)
    nm["OffsetY"] = Node("int", 0, lo=0, hi=1016)
    nm["BlackLevel"] = Node("float", 0.0, lo=0.0, hi=12.5)
    nm["Gamma"] = Node("float", 1.0, lo=0.5, hi=4.0)
    cam = Camera(nodes=nm)
    monkeypatch.setitem(sys.modules, "PySpin", fake_pyspin([cam]))
    be = make_backend(dict(CFG, width=640, height=480, offset_x=100, offset_y=60, black_level=2.0, gamma=0.8))
    be.open()
    n = cam.nodes
    assert (n["Width"].value, n["Height"].value, n["OffsetX"].value, n["OffsetY"].value) == (640, 480, 100, 60)
    assert n["OffsetX"].writes == [0, 100] and n["OffsetY"].writes == [0, 60]  # cleared first, so the size can grow
    assert be.size == (640, 480)
    assert n["BlackLevel"].value == 2.0 and n["GammaEnabled"].value is True and n["Gamma"].value == 0.8
    done = be.apply_live({"exposure_us": 4000.0, "gain_db": 3.0, "black_level": 1.5, "gamma": 0.0, "fps": 60})
    assert done == {"exposure_us", "gain_db", "black_level", "gamma"}  # fps needs a reconnect
    assert n["ExposureTime"].value == 4000.0 and n["Gain"].value == 3.0 and n["GammaEnabled"].value is False
    be.close()
    # the camera keeps the 640x480 region while powered: asking for full size (0) must restore it
    full = make_backend(dict(CFG, width=0, height=0))
    full.open()
    assert (n["Width"].value, n["Height"].value, n["OffsetX"].value, n["OffsetY"].value) == (1280, 1024, 0, 0)
    assert full.size == (1280, 1024)
    full.close()
    again = make_backend(dict(CFG))  # no region keys at all: full size too
    n["Width"].value, n["Height"].value = 320, 240
    again.open()
    assert again.size == (1280, 1024)
    again.close()


def test_video_dialogs(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.video_dialogs import CompressDialog, VideoSettingsDialog
    from mavric.video.settings import VideoSettings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml"),
                    "video_settings_file": str(tmp_path / "video.yaml")}
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    dlg = VideoSettingsDialog(win)
    assert dlg.cam_list.count() == len(ctl.rig["cameras"])
    dlg._getters["fps"]  # the synthetic camera exposes its frame rate
    dlg.chunk.setValue(750)
    dlg.c_widgets.crf.setValue(21)
    dlg.save()
    vs = VideoSettings.load(tmp_path / "video.yaml")
    assert vs.video["raw_chunk_mb"] == 750 and vs.video["compress"]["crf"] == 21
    assert "cam0" in vs.cameras and ctl.rig["video"]["raw_chunk_mb"] == 750
    w = LosslessChunkWriter(tmp_path / "x", 30.0, (320, 240), "ffv1")
    for f in numbered_frames(5):
        w.write(f)
    w.close()
    cd = CompressDialog(win, tmp_path, {"crf": 22})
    assert cd.opts.values()["crf"] == 22 and "_compressed.mp4" in cd.log.toPlainText()
    dlg.close()
    cd.close()
    win.close()
    app.processEvents()
