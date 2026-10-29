"""Sound events (AU3): audio access, proposals, emitter suggestions, files, exchange formats, review GUI."""

import os
import time

import numpy as np
import pandas as pd
import pytest

from mavric.analysis.audio_sync import AudioTimeMap
from mavric.audio.config import AudioConfig
from mavric.audio.detect import detect_events, measure_event, occupancy, suggest_emitters
from mavric.audio.events import (
    PLAYBACK_FILE,
    SoundEvent,
    emitters,
    export_events,
    import_events,
    load_events,
    load_types,
    merge_playback,
    playback_events,
    save_events,
    save_types,
)
from mavric.audio.recorder import AudioRecorder
from mavric.audio.source import AudioSource
from mavric.session import Session


@pytest.fixture(scope="module")
def sound_session(tmp_path_factory):
    from mavric.analysis.pipeline import prepare
    from mavric.sim import simulate_session

    sess = simulate_session(tmp_path_factory.mktemp("sound"), duration_s=90, seed=7, audio=True, write_labels=False)
    P = prepare(sess)
    calls = pd.read_csv(sess.file("ground_truth_calls.csv"))
    tr = sess.meta["sim"]["audio"]
    calls["t0"] = P.audio((calls["t"] - tr["start_s"]) * tr["rate"])
    calls["t1"] = P.audio((calls["t"] + calls["dur"] - tr["start_s"]) * tr["rate"])
    calls = calls[np.isfinite(calls["t0"]) & np.isfinite(calls["t1"])].reset_index(drop=True)
    return sess, P, calls


def test_audio_source_reads_across_files(tmp_path):
    sr = 8000
    cfg = AudioConfig(sample_rate=sr, inputs={1: "micA", 2: "micB"}, sync_channel="", split_s=0.5)
    rec = AudioRecorder(tmp_path, cfg, t0=0.0)
    ramp = (np.arange(3 * sr, dtype=np.float32) % 1000) / 2000
    x = np.stack([ramp, -ramp], axis=1)
    for k in range(0, len(x), 700):
        rec._write(k, x[k:k + 700], (k + 700) / sr, 0.0, "", [])
    rec._clock.close()
    rec._edges.close()
    if rec._sf is not None:
        rec._sf.close()
    assert len(rec.files) == 6
    sess = Session(tmp_path, {"audio": rec.info()})
    n = len(x)
    src = AudioSource(sess, AudioTimeMap(sr, [(0, n, np.array([0.0, n]), np.array([10.0, 10.0 + n / sr]))]))
    y = src.read_samples(3900, 4200)  # crosses the 4000-sample file boundary
    assert np.allclose(y, x[3900:4200], atol=1e-5)
    y, s0 = src.read(10.5, 10.6, [1])
    assert s0 == 4000 and np.allclose(y[:, 0], x[4000:4800, 1], atol=1e-5)
    assert src.sample_at([5.0, 99.0]).tolist() == [0.0, float(n)]  # clipped outside the recording
    src.close()


def test_envelope_and_spectrogram(sound_session):
    sess, P, calls = sound_session
    src = AudioSource(sess, P.audio)
    env = src.envelope()
    assert env["lo"].shape == env["hi"].shape and env["hi"].shape[1] == 2
    assert (env["hi"] >= env["lo"]).all()
    t = time.perf_counter()
    env2 = src.envelope()  # from the cache
    assert time.perf_counter() - t < 0.5 and np.array_equal(env["hi"], env2["hi"])
    c = calls.iloc[0]
    S, ta, tb, ftop = src.spectrogram(c.t0 - 1, c.t1 + 1, src.mic_idx[0], n_cols=400, fmax=4000)
    assert S.shape[1] == 400 and ta >= c.t0 - 1.05 and tb <= c.t1 + 1.05 and 3900 < ftop <= 4000
    f = np.linspace(0, ftop, S.shape[0])
    k = int(np.argmin(np.abs(f - c.f0)))
    assert S[k].max() > np.median(S) + 30  # the call's fundamental stands out
    src.close()


def test_proposals_find_the_calls(sound_session):
    sess, P, calls = sound_session
    src = AudioSource(sess, P.audio)
    props = detect_events(src)
    src.close()
    hits, onset = 0, []
    for c in calls.itertuples():
        m = props[(props["start_s"] < c.t1) & (props["stop_s"] > c.t0)]
        if len(m):
            hits += 1
            onset.append(m["start_s"].iloc[0] - c.t0)
    assert hits >= 0.95 * len(calls)
    assert abs(np.median(onset)) < 0.04
    false = sum(not ((calls["t0"] < p.stop_s) & (calls["t1"] > p.start_s)).any() for p in props.itertuples())
    assert false <= 2


def test_side_and_emitter_suggestions(sound_session):
    sess, P, calls = sound_session
    src = AudioSource(sess, P.audio)
    iso = [c for c in calls.itertuples()
           if not ((calls["t0"] < c.t1) & (calls["t1"] > c.t0) & (calls.index != c.Index)).any()]
    side = 0
    for c in iso:
        m = measure_event(src, c.t0, c.t1)
        near = "micA" if c.perch in (1, 3) else "micB"
        side += m["louder"] == near
    assert side >= 0.9 * len(iso)
    top2, n = 0, 0
    for c in calls.itertuples():
        perched = occupancy(P.bouts, c.t0, c.t1)
        if not perched:
            continue
        sug = suggest_emitters(measure_event(src, c.t0, c.t1), perched, src.mic_near)
        n += 1
        top2 += c.pigeon in [s[0] for s in sug[:2]]
        assert sug[-1][0] == "unknown"
    assert n >= 20 and top2 >= 0.9 * n
    played = [SoundEvent(1.0, 2.0, "playback", "system:spk1", source="playback", note="call_A.wav")]
    sug = suggest_emitters({"louder": None, "first": None}, [], {}, played)
    assert sug[0][0] == "system:spk1" and "call_A.wav" in sug[0][2]
    src.close()


def test_events_types_and_emitters_files(tmp_path):
    sess = Session(tmp_path, {})
    ev = [SoundEvent(2.0, 2.5, "coo", "S01", "micA", 300.0, 900.0, "sure", annotator="JB", note="clear"),
          SoundEvent(1.0, 1.2, "wing clap", "unknown")]
    save_events(sess, ev)
    back = load_events(sess)
    assert [(e.start_s, e.type, e.emitter) for e in back] == [(1.0, "wing clap", "unknown"), (2.0, "coo", "S01")]
    assert back[1].f_low_hz == 300.0 and np.isnan(back[0].f_low_hz) and back[1].note == "clear"
    p = tmp_path / "types.yaml"
    assert load_types(p)[0] == "call"  # defaults when missing
    save_types(p, ["coo", "alarm"])
    assert load_types(p) == ["coo", "alarm"]
    assert emitters(["S01", "S02"], ["spk1"]) == ["S01", "S02", "system:spk1", "unknown"]


def test_audacity_and_raven_round_trip(sound_session, tmp_path):
    sess, P, calls = sound_session
    ev = [SoundEvent(float(c.t0), float(c.t1), "coo", c.pigeon, "micA", 400.0, 2000.0, note="n")
          for c in calls.head(5).itertuples()]
    for fmt in ("audacity", "raven"):
        paths = export_events(sess, P.audio, ev, fmt, out_dir=tmp_path / fmt)
        assert len(paths) == 1
        back = import_events(sess, P.audio, paths[0], fmt)
        assert len(back) == 5
        for a, b in zip(ev, back):
            assert abs(a.start_s - b.start_s) < 1e-3 and abs(a.stop_s - b.stop_s) < 1e-3
            assert (b.type, b.emitter, b.note, b.source) == ("coo", a.emitter, "n", "import")
            assert b.f_low_hz == pytest.approx(400) and b.f_high_hz == pytest.approx(2000)
    raven = pd.read_csv(tmp_path / "raven" / "audio_000.selections.txt", sep="\t")
    assert {"Begin Time (s)", "End Time (s)", "Low Freq (Hz)", "Channel"} <= set(raven.columns)


def test_playback_log_becomes_events(sound_session):
    sess, P, _ = sound_session
    sr = sess.audio["sample_rate"]
    pd.DataFrame([{"speaker": "spk2", "sound": "call_A.wav", "sample": 48000 * 10, "n_samples": int(0.5 * sr)}]
                 ).to_csv(sess.file(PLAYBACK_FILE), index=False)
    try:
        played = playback_events(sess, P.audio)
        assert len(played) == 1 and played[0].emitter == "system:spk2" and played[0].note == "call_A.wav"
        assert played[0].start_s == pytest.approx(float(P.audio(np.array([480000.0]))[0]))
        assert played[0].duration_s == pytest.approx(0.5, abs=1e-3)
        merged, added = merge_playback([], played)
        merged, again = merge_playback(merged, played)
        assert added == 1 and again == 0 and len(merged) == 1
    finally:
        sess.file(PLAYBACK_FILE).unlink()


def test_sounds_cli(sound_session, capsys):
    from mavric.cli import main

    sess, _, _ = sound_session
    assert main(["sounds", "detect", str(sess.path)]) == 0
    assert len(pd.read_csv(sess.analysis_dir / "sound_proposals.csv")) > 10
    save_events(sess, [SoundEvent(5.0, 5.4, "coo", "S01")])
    try:
        assert main(["sounds", "export", str(sess.path)]) == 0
        lab = sess.analysis_dir / "audio_000_labels.txt"
        assert float(lab.read_text().split("	")[0]) == pytest.approx(5.0, abs=0.01)  # file time ~ session time
        assert main(["sounds", "import", str(sess.path), str(lab)]) == 0
        assert len(load_events(sess)) == 2
    finally:
        sess.file("sound_events.csv").unlink()


def test_review_window_sound_labeling(sound_session):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.gui.review_window import ReviewWindow

    sess, P, calls = sound_session
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ReviewWindow(sess, "JB")
    snd = win.sound
    assert snd is not None and snd.src.mics == ["micA", "micB"]
    t_end = time.time() + 5
    while snd.lanes.env is None and time.time() < t_end:  # the overview is computed in the background
        app.processEvents()
        time.sleep(0.02)
    assert snd.lanes.env is not None
    c = calls.iloc[2]
    win.plot.setXRange(c.t0 - 5, c.t0 + 15, padding=0)
    win.seek_time(c.t0)
    snd.mark_start()
    win.seek_time(c.t1)
    snd.set_type(0)
    snd.mark_end()
    e = snd.events[snd.selected]
    assert abs(e.start_s - c.t0) < 0.05 and e.type == snd.types[0] and e.annotator == "JB"
    snd.accept_suggestion()
    assert e.emitter == snd._sugg[0][0]
    snd.delete_selected()
    assert not snd.events
    win.seek_time(c.t0)
    snd.mark_start()
    win.seek_time(c.t1)
    snd.mark_end()
    win.save(quiet=True)
    assert len(load_events(sess)) == 1
    snd.close()
    win.dirty = snd.dirty = False
    win.close()
    sess.file("sound_events.csv").unlink()


def test_review_without_video(tmp_path):
    """An audio test without cameras: the start dialog offers audio only, and the window runs on its own clock."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.gui.review_window import CLOCK_HZ, ReviewWindow, StartDialog
    from mavric.gui.tips import missing_tips
    from mavric.session import COVERAGE_FILE, CURATION_FILE, LABELS_FILE
    from mavric.sim import simulate_session

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    sess = simulate_session(tmp_path, duration_s=30, seed=3, audio=True, write_labels=False)
    sess.meta["cameras"] = []  # as recorded without cameras
    sess.save_meta()
    sess = Session.open(sess.path)

    dlg = StartDialog(sess, "JB")
    assert not dlg.cb_video.isEnabled() and dlg.cb_audio.isEnabled()
    assert dlg.values() == ("JB", False, True)
    assert missing_tips(dlg) == []
    dlg.close()

    win = ReviewWindow(sess, "JB")  # video requested, but the session has none
    snd = win.sound
    assert win.src is None and win.video is None and win.roi_list is None and not win.labeling
    assert snd is not None and win.fps == CLOCK_HZ and win.t_end >= 29
    assert missing_tips(win) == []
    win.seek_time(10.0)
    assert win.t == pytest.approx(10.0, abs=1 / CLOCK_HZ)
    t0 = win.t
    win.step(1)
    assert win.t == pytest.approx(t0 + 1 / CLOCK_HZ)
    snd.mark_start()
    win.seek_time(10.5)
    snd.mark_end()
    win.save(quiet=True)
    assert len(load_events(sess)) == 1
    for f in (LABELS_FILE, COVERAGE_FILE, CURATION_FILE):  # place labels are off without video
        assert not sess.file(f).exists(), f
    snd.close()
    win.dirty = snd.dirty = False
    win.close()


def test_review_video_or_audio_only(sound_session):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.gui.review_window import ReviewWindow

    sess, P, calls = sound_session
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = ReviewWindow(sess, "JB", audio=False)
    assert win.sound is None and win.video is not None and win.labeling
    win.close()
    win = ReviewWindow(sess, "JB", video=False)
    assert win.sound is not None and win.video is None and not win.labeling
    win.sound.close()
    win.dirty = win.sound.dirty = False
    win.close()
