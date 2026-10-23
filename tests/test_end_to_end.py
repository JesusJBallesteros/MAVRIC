"""Simulated sessions with known ground truth must be recovered by the pipeline."""

import threading
import time

import pandas as pd
import pytest

from mavric.analysis.labels_io import import_boris, load_labels
from mavric.analysis.pipeline import prepare, run_analysis
from mavric.rfid.protocol import WRAP
from mavric.rfid.serial_reader import RfidReaderThread
from mavric.sim import RfidModel, simulate_session


@pytest.fixture(scope="module")
def sim_session(tmp_path_factory):
    out = tmp_path_factory.mktemp("sessions")
    model = RfidModel(start_micros=WRAP - 30_000_000)  # micros() wraps 28 s into the recording
    return simulate_session(out, duration_s=150, seed=11, label_noise_s=0.1, rfid_model=model)


def test_pipeline_recovers_truth(sim_session):
    s = run_analysis(sim_session, plots=True)
    segs = s["sync"]["rfid_segments"]
    assert len(segs) == 1, "micros wrap must not split the stream"
    assert segs["0"]["drift_ppm"] == pytest.approx(145, abs=10)
    cam = s["sync"]["camera"]
    assert cam["fit"]["drift_ppm"] == pytest.approx(-20, abs=10)
    assert cam["camera_dropped"] >= 0
    led = s["sync"]["led"]
    assert led["validated"] and led["pass"]
    assert led["n_matched"] >= 0.9 * led["n_sync"]
    o = s["comparison"]["overall"]
    assert o["presence_sensitivity"] > 0.85
    assert o["presence_precision"] > 0.9
    assert o["id_accuracy_when_detected"] > 0.98
    for png in ("timeline_by_subject", "accuracy_per_place", "confusion_seconds", "sync_diagnostics"):
        assert (sim_session.analysis_dir / f"{png}.png").exists()


def test_render_short_clip(sim_session):
    from mavric.analysis.render import render_annotated

    P = prepare(sim_session)
    p = render_annotated(P, load_labels(sim_session), out_path=sim_session.analysis_dir / "clip.mp4",
                         t_range=(10, 14))
    import cv2

    cap = cv2.VideoCapture(str(p))
    assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == pytest.approx(120, abs=3)


def test_boris_import(sim_session, tmp_path):
    P = prepare(sim_session)
    gt = pd.read_csv(sim_session.file("ground_truth.csv"))
    fps = sim_session.camera()["fps"]
    # BORIS times are media times (frame_idx / fps); convert our truth to media time
    import numpy as np

    media = lambda t: np.interp(t, P.frame_t, np.arange(len(P.frame_t))) / fps  # noqa: E731
    boris = pd.DataFrame({
        "Observation id": "obs1", "Subject": gt["pigeon"], "Behavior": gt["roi"].str.lower(),
        "Behavior type": "STATE", "Start (s)": media(gt["start_s"]), "Stop (s)": media(gt["stop_s"]),
        "Media duration (s)": len(P.frame_t) / fps,
    })
    f = tmp_path / "boris.tsv"
    boris.to_csv(f, sep="\t", index=False)
    lab, cov = import_boris(sim_session, f, P.frame_t)
    assert len(lab) == len(gt)
    assert (lab["start_s"] - gt["start_s"]).abs().max() < 0.05
    assert cov[0][1] == pytest.approx(P.frame_t[-1], abs=0.1)


class _FlakySource:
    """Delivers some lines then dies like an unplugged USB cable."""

    def __init__(self, n):
        self.n = n
        self.i = 0

    def open(self):
        pass

    def readline(self):
        self.i += 1
        if self.i > self.n:
            raise OSError("device disconnected")
        time.sleep(0.001)
        return f"{self.i * 125000},NONE,0\r\n".encode()

    def close(self):
        pass


def test_rfid_log_survives_disconnect(tmp_path):
    th = RfidReaderThread(_FlakySource(50))
    th.attach_writer(tmp_path / "rfid_raw.csv", 0.0)
    th.start()
    th.join(5)
    assert th.error and "disconnected" in th.error
    df = pd.read_csv(tmp_path / "rfid_raw.csv")
    assert len(df) == 50


def test_headless_realtime_acquisition(tmp_path):
    """Short real-time run: synthetic camera + simulated Arduino through the controller."""
    from mavric.acquire import simulation_controller
    from mavric.config import load_rig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    ctl = simulation_controller(rig, seed=3)
    try:
        ctl.connect_rfid(ctl._sim_source)
        ctl.connect_camera(ctl.rig["cameras"][0])
        sess = ctl.start_recording("rt", "test")
        time.sleep(4)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    st = sess.meta["stats"]
    assert st["cameras"]["cam0"]["frames"] >= 100
    assert st["cameras"]["cam0"]["recorder_dropped"] == 0
    assert st["rfid_lines"] >= 25
    raw = pd.read_csv(sess.file("rfid_raw.csv"))
    fr = pd.read_csv(sess.file("frames_cam0.csv"))
    assert raw["host_t"].min() >= 0 and fr["host_t"].min() >= 0
    assert abs(raw["host_t"].max() - fr["host_t"].max()) < 0.5  # same clock, same span
    assert threading.active_count() < 10
