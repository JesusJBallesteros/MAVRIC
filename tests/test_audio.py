"""Audio: sync edge detection, stream pieces, recorder, simulated sessions and live acquisition."""

import time

import numpy as np
import pandas as pd
import pytest

from mavric.audio.config import AudioConfig
from mavric.audio.devices import find_device
from mavric.audio.edges import EdgeDetector, detect_edges, pulse_onsets
from mavric.audio.recorder import AudioRecorder
from mavric.audio.stream import AudioRing, Mixer

SR = 48000


def pulse_train(onsets_s, width_s=0.08, amp=0.13, tau=0.02, n=None, invert=False, seed=0):
    """AC-coupled pulses with smoothed edges at exact (sub-sample) times."""
    n = n or int((max(onsets_s) + 1) * SR)
    t = np.arange(n) / SR
    w = 0.8 / SR
    y = np.zeros(n)
    for e in onsets_s:
        for start, sgn in ((e, 1), (e + width_s, -1)):
            dt = t - start
            y += sgn * 0.5 * (1 + np.tanh(dt / w)) * np.exp(-np.clip(dt, 0, None) / tau)
    y *= -amp if invert else amp
    return (y + np.random.default_rng(seed).normal(0, 1e-4, n)).astype(np.float32)


# --------------------------------------------------------------------------- sync edges


@pytest.mark.parametrize("invert", [False, True])
def test_edges_subsample_streaming(invert):
    onsets = np.array([0.5003, 2.71234, 5.9, 8.123456])
    x = pulse_train(onsets, invert=invert)
    det = EdgeDetector(SR)
    edges, k = [], 0
    rng = np.random.default_rng(1)
    while k < len(x):  # odd block sizes: edges straddle block boundaries
        b = int(rng.integers(100, 1500))
        edges += det.process(x[k: k + b], k)
        k += b
    e = np.array(edges)
    assert len(e) == 2 * len(onsets)
    on, pol = pulse_onsets(e, SR, 0.08)
    assert pol == (-1 if invert else 1)
    assert len(on) == len(onsets)
    err = on / SR - onsets
    assert np.abs(err).max() < 2e-6  # < 0.1 sample


def test_edges_ignore_clicks_and_dc_coupling():
    x = pulse_train([1.0, 3.5], tau=1e9)  # DC-coupled input: flat top
    x[int(2.2 * SR)] += 0.3  # one-sample click is not a step
    e = detect_edges(x, SR)
    assert len(e) == 4
    on, _ = pulse_onsets(e, SR, 0.08)
    assert np.allclose(on / SR, [1.0, 3.5], atol=5e-6)


def test_pulse_onsets_need_the_right_width():
    e = detect_edges(pulse_train([1.0, 3.0], width_s=0.3), SR)
    assert len(e) == 4
    assert len(pulse_onsets(e, SR, 0.08)[0]) == 0


# --------------------------------------------------------------------------- config, devices, stream pieces


def test_config_from_rig_and_channel_maps(tmp_path):
    from mavric.config import load_rig

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "rig.yaml").write_text(
        "audio:\n  inputs: {1: micA, 2: micB}\n  speakers: {3: left, 4: right}\n", encoding="utf-8")
    rig = load_rig(tmp_path / "config" / "rig.yaml")
    cfg = AudioConfig.from_rig(rig)
    assert cfg.inputs == {1: "micA", 2: "micB"}, "user's inputs replace the defaults (no stray sync channel)"
    assert cfg.sync_index is None and cfg.mic_indices == [0, 1]
    assert cfg.n_out_open == 4 and cfg.speaker_index("right") == 3 and cfg.speaker_index(3) == 2
    with pytest.raises(KeyError):
        cfg.speaker_index("spk1")
    d = AudioConfig.from_rig(load_rig(tmp_path / "missing.yaml"))
    assert d.names == ["micA", "micB", "sync"] and d.sync_index == 2 and d.n_out_open == 4


def test_find_device():
    devs = [
        {"index": 0, "name": "Realtek ASIO", "host_api": "ASIO", "inputs": 2, "outputs": 2},
        {"index": 1, "name": "MOTU M Series", "host_api": "ASIO", "inputs": 4, "outputs": 4},
        {"index": 2, "name": "In 1-2 (MOTU M Series)", "host_api": "Windows WASAPI", "inputs": 2, "outputs": 0},
    ]
    assert find_device("MOTU", "ASIO", 3, 4, devs)["index"] == 1
    assert find_device("motu", "wasapi", 2, 0, devs)["index"] == 2
    with pytest.raises(RuntimeError, match="MOTU M Series"):
        find_device("MOTU", "WASAPI", 3, 0, devs)


def test_ring_and_mixer():
    r = AudioRing(2, 100)
    for k in range(0, 250, 30):
        r.write(np.full((30, 2), k, np.float32))
    x, c = r.since(200)
    assert c == 270 and len(x) == 70 and x[0, 0] == 180 and x[-1, 0] == 240
    x, _ = r.since(0)
    assert len(x) == 100  # at most the ring size

    m = Mixer(2, max_level_dbfs=-6)
    out = np.zeros((64, 2), np.float32)
    m.play(np.ones(100), 1, gain=0.25, name="a")
    m.render(out, 1000)
    assert np.all(out[:, 1] == 0.25) and np.all(out[:, 0] == 0)
    m.render(out, 1064)
    assert np.all(out[:36, 1] == 0.25) and np.all(out[36:, 1] == 0)
    assert [(e["kind"], e["sample"], e["channel"], e["name"]) for e in m.log] == [("start", 1000, 1, "a"),
                                                                                 ("end", 1100, 1, "a")]
    m.play(np.ones(1000), 0, gain=4.0)
    m.render(out, 1128)
    assert out[:, 0].max() == pytest.approx(10 ** (-6 / 20), rel=1e-6)  # limiter
    m.stop()  # fades out over 10 ms (480 samples)
    for i in range(8):
        m.render(out, 1192 + 64 * i)
    assert not m.busy
    m.render(out, 1704)
    assert not out.any()
    with pytest.raises(ValueError):
        m.play(np.ones(10), 2)


# --------------------------------------------------------------------------- recorder


def test_recorder_splits_files_and_logs_clock(tmp_path):
    import soundfile as sf

    cfg = AudioConfig(inputs={1: "micA", 2: "sync"}, sync_channel="sync", split_s=1.0)
    rec = AudioRecorder(tmp_path, cfg, t0=100.0)
    rec.start()
    blk = 1000
    k0 = 5000  # stream was running before the recording started
    for i in range(130):  # 2.708 s
        x = np.full((blk, 2), i / 1000, np.float32)
        flags = "in_over" if i == 60 else ""
        edges = [(k0 + i * blk + 10.5, 1, 0.1)] if i in (0, 70) else []
        rec.submit(k0 + i * blk, x, 100.0 + (i + 1) * blk / SR + 0.004, 0.0, flags, edges)
    st = rec.finish()
    assert st["samples"] == 130 * blk and st["files"] == 3
    assert [f["n_samples"] for f in rec.files] == [48000, 48000, 34000]
    assert [f["first_sample"] for f in rec.files] == [0, 48000, 96000]
    total = np.concatenate([sf.read(tmp_path / f["file"], dtype="float32")[0] for f in rec.files])
    assert total.shape == (130 * blk, 2)
    assert total[48000, 0] == pytest.approx(48 / 1000, abs=1e-5)  # sample-exact continuation (24 bit)
    clk = pd.read_csv(tmp_path / "audio_clock.csv", keep_default_na=False)
    assert 20 <= len(clk) <= 40  # ~10 rows/s plus the flagged one
    assert (clk["flags"] == "in_over").sum() == 1
    assert clk["host_t"].iloc[0] == pytest.approx((clk["sample"].iloc[0]) / SR + 0.004, abs=1e-5)
    e = pd.read_csv(tmp_path / "audio_edges.csv")
    assert list(e["sample"]) == [10.5, 70 * blk + 10.5]
    info = rec.info()
    assert info["channels"] == ["micA", "sync"] and info["sync_channel"] == "sync"


def test_segments_split_at_overflows():
    from mavric.analysis.audio_sync import _segments

    clock = pd.DataFrame({"sample": [1024, 2048, 9216, 10240], "n": [1024] * 4, "flags": ["", "", "in_over", ""]})
    assert _segments(clock, 20000) == [(0, 8192), (8192, 20000)]


# --------------------------------------------------------------------------- offline sessions + analysis


@pytest.fixture(scope="module")
def audio_session(tmp_path_factory):
    from mavric.sim import simulate_session

    return simulate_session(tmp_path_factory.mktemp("audio"), duration_s=60, seed=5, audio=True, write_labels=False)


def _expected_times(sess, P, tc):
    """Session time of world times `tc`, through the RFID SYNC lines (linear in world time)."""
    from mavric.config import TagMap, load_rig
    from mavric.rfid.protocol import KIND_SYNC
    from mavric.sim import SimWorld

    tags = TagMap.load(load_rig()["paths"]["tags_file"])
    w = SimWorld.random(tags.pigeons, {p: tags.color(p) for p in tags.pigeons}, sess.meta["duration_s"],
                        sess.meta["sim"]["seed"])
    sync_t = np.sort(P.sync["rfid_t"][(P.raw["kind"] == KIND_SYNC).to_numpy()])
    e = w.sync_edges[: len(sync_t)]
    b1, b0 = np.polyfit(e, sync_t, 1)
    return b0 + b1 * np.asarray(tc)


def test_audio_sync_recovers_truth(audio_session):
    from mavric.analysis.pipeline import prepare, run_analysis

    s = run_analysis(audio_session, plots=False)
    au = s["sync"]["audio"]
    assert au["method"] == "sync_pulses" and au["validated"] and au["pass"]
    assert au["pulses"]["n_matched"] == au["pulses"]["n_sync"] >= 10
    assert au["pulses"]["drift_ppm"] == pytest.approx(25, abs=5)
    assert (audio_session.analysis_dir / "audio_time_knots.csv").exists()
    P = prepare(audio_session)
    truth = audio_session.meta["sim"]["audio"]
    calls = pd.read_csv(audio_session.file("ground_truth_calls.csv"))
    k = (calls["t"].to_numpy() - truth["start_s"]) * truth["rate"]
    err = P.audio(k) - _expected_times(audio_session, P, calls["t"])
    assert np.abs(err).max() < 5e-5  # audio events land on the RFID timeline within 50 us
    assert np.allclose(P.audio.sample_at(P.audio(k)), k, atol=1e-3)


def test_audio_without_sync_channel_uses_host_clock(tmp_path):
    from mavric.analysis.pipeline import prepare
    from mavric.sim import simulate_session

    sess = simulate_session(tmp_path, duration_s=40, seed=6, audio=True, audio_sync=False, write_labels=False)
    P = prepare(sess)
    au = P.sync["report"]["audio"]
    assert au["method"] == "host_clock" and not au["validated"]
    assert au["host_drift_ppm"] == pytest.approx(25, abs=15)
    truth = sess.meta["sim"]["audio"]
    calls = pd.read_csv(sess.file("ground_truth_calls.csv"))
    k = (calls["t"].to_numpy() - truth["start_s"]) * truth["rate"]
    err = P.audio(k) - _expected_times(sess, P, calls["t"])
    assert np.abs(err).max() < 0.01  # host clock alone: within a few ms


def test_missing_edges_file_is_redetected(audio_session):
    from mavric.analysis.audio_sync import load_audio_edges

    logged = pd.read_csv(audio_session.file("audio_edges.csv"))
    redone = load_audio_edges(audio_session, redetect=True)
    assert len(redone) == len(logged)
    assert np.abs(redone["sample"].to_numpy() - logged["sample"].to_numpy()).max() < 0.01


# --------------------------------------------------------------------------- live (simulated interface)


def test_live_recording_through_controller(tmp_path):
    from mavric.acquire import simulation_controller
    from mavric.config import load_rig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    ctl = simulation_controller(rig, seed=4)
    try:
        ctl.connect_rfid(ctl._sim_source)
        ctl.connect_audio()
        time.sleep(0.5)
        sess = ctl.start_recording("audio", "test")
        time.sleep(3)
        st = ctl.status()["audio"]
        assert st["recorded_s"] > 2 and not st["error"]
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    a = sess.meta["audio"]
    assert a["channels"] == ["micA", "micB", "sync"] and a["files"][0]["file"] == "audio_000.flac"
    assert sess.meta["stats"]["audio"]["duration_s"] == pytest.approx(3.0, abs=0.4)
    clk = pd.read_csv(sess.file("audio_clock.csv"))
    assert len(clk) >= 20 and clk["host_t"].min() >= 0
    assert abs(clk["host_t"].max() - sess.meta["duration_s"]) < 0.3  # same clock as the other streams


def test_audio_check_hears_each_speaker():
    from mavric.audio.check import audio_check
    from mavric.sim import SimWorld

    quiet = SimWorld([], {}, [], np.arange(0.5, 3600, 3.0), 3600)  # no birds calling, pulses every 3 s
    cfg = AudioConfig(host_api="sim", speakers={1: "spk1", 2: "spk2"}, block=512)
    rep = audio_check(cfg, seconds=4, tones=True, world=quiet)
    assert not rep["warnings"], rep["warnings"]
    assert rep["sync"]["pulses"] >= 1 and rep["sync"]["width_ms"] == pytest.approx(80, abs=0.5)
    expected_ms = (2 * 512 + int(0.003 * SR)) / SR * 1e3
    for tn in rep["tones"]:
        near = "micA" if tn["speaker"] == "spk1" else "micB"
        assert tn["mics"][near]["heard"]
        assert tn["mics"][near]["delay_ms"] == pytest.approx(expected_ms, abs=0.5)
