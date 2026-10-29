"""Audio analysis (AU5): sounds linked to RFID places, call rates, answers to played sounds, render, timeline."""

import av
import numpy as np
import pandas as pd
import pytest

from mavric.analysis.sounds import (
    ANY_PLACE,
    NOT_DETECTED,
    Occupancy,
    call_rates,
    link_events,
    playback_responses,
)


def _bouts(rows):
    return pd.DataFrame(rows, columns=["pigeon", "roi", "start_s", "stop_s"])


def _events(rows):
    return pd.DataFrame(rows, columns=["start_s", "stop_s", "type", "emitter"]).assign(
        channel="", confidence="", source="manual", annotator="", note="")


BOUTS = _bouts([["S01", "A", 0.0, 50.0], ["S01", "B", 60.0, 100.0], ["S02", "B", 0.0, 100.0]])


def test_occupancy_place_is_where_the_subject_spent_most_of_the_sound():
    occ = Occupancy(BOUTS)
    assert occ.place("S01", 10, 11) == "A"
    assert occ.place("S01", 49.5, 61.0) == "B"  # 0.5 s at A, 1 s at B
    assert occ.place("S01", 52, 53) == ""
    assert occ.place("S09", 10, 11) == ""
    assert occ.present(10.0) == [("S01", "A"), ("S02", "B")]
    spur = BOUTS.assign(curated=["valid", "spurious", "valid"])
    assert Occupancy(spur).place("S01", 70, 71) == ""  # bouts marked spurious in review do not count


def test_link_and_call_rates():
    ev = _events([[10, 10.5, "call", "S01"], [20, 20.5, "coo", "S01"], [55, 55.5, "call", "S01"],
                  [70, 70.5, "call", "S01"], [30, 30.2, "call", "unknown"], [40, 40.4, "playback", "system:spk1"]])
    linked = link_events(ev, BOUTS, None, ["S01", "S02"])
    by_t = linked.set_index("start_s")
    assert by_t.loc[10, "place_rfid"] == "A" and by_t.loc[55, "place_rfid"] == "" and by_t.loc[70, "place_rfid"] == "B"
    assert by_t.loc[30, "emitter_kind"] == "unknown" and by_t.loc[40, "emitter_kind"] == "system"
    assert by_t.loc[40, "place_rfid"] == ""  # only subjects have a place
    assert by_t.loc[10, "present_rfid"] == "S01@A; S02@B"
    # reviewed: 0-80 s only; the call at 70 s counts, nothing after 80 s does
    r = call_rates(linked, BOUTS, [(0.0, 80.0)], ["S01", "S02"], ["A", "B"]).set_index(["subject", "place", "type"])
    assert r.loc[("S01", "A", "(all)"), "n"] == 2 and r.loc[("S01", "A", "(all)"), "time_s"] == 50
    assert r.loc[("S01", "A", "(all)"), "per_min"] == pytest.approx(2 / 50 * 60)
    assert r.loc[("S01", "B", "(all)"), "time_s"] == 20 and r.loc[("S01", "B", "(all)"), "n"] == 1
    assert r.loc[("S01", NOT_DETECTED, "(all)"), "time_s"] == 10 and r.loc[("S01", NOT_DETECTED, "(all)"), "n"] == 1
    assert r.loc[("S01", ANY_PLACE, "(all)"), "n"] == 4 and r.loc[("S01", ANY_PLACE, "(all)"), "time_s"] == 80
    assert r.loc[("S01", "A", "coo"), "n"] == 1
    assert r.loc[("S02", ANY_PLACE, "(all)"), "n"] == 0


def test_playback_responses_windows():
    ev = _events([[10, 10.4, "playback", "system:spk1"], [12, 12.5, "call", "S02"], [13, 13.5, "call", "S01"],
                  [14, 14.5, "call", "S02"], [8, 8.5, "call", "S01"], [16, 16.4, "playback", "system:spk2"],
                  [17, 17.5, "call", "unknown"], [40, 40.5, "call", "S01"]])
    linked = link_events(ev, BOUTS, None, ["S01", "S02"])
    resp, by = playback_responses(linked, BOUTS, [(0.0, 30.0)], ["S01", "S02"], window_s=10.0)
    a, b = resp.iloc[0], resp.iloc[1]
    assert a["window_s"] == pytest.approx(6.0)  # cut at the next played sound
    assert a["n_after"] == 3 and a["n_before"] == 1
    assert a["first_latency_s"] == pytest.approx(2.0) and a["first_emitter"] == "S02"
    assert a["responders"] == "S01; S02" and a["reviewed_frac"] == 1.0
    assert a["present_rfid"] == "S01@A; S02@B"
    assert b["n_before"] == 3  # the window before starts where the first played sound ended (10.4 s)
    assert b["n_after"] == 1 and b["first_emitter"] == "unknown" and b["responders"] == ""
    resp, _ = playback_responses(linked, BOUTS, [(0.0, 20.0)], ["S01", "S02"], window_s=10.0)
    assert resp["reviewed_frac"].iloc[1] == pytest.approx((20 - 10.4) / (26 - 10.4))
    s01 = by[(by["t"] == 10) & (by["subject"] == "S01")].iloc[0]
    assert s01["first_latency_s"] == pytest.approx(3.0) and s01["place_rfid"] == "A" and s01["n_after"] == 1


@pytest.fixture(scope="module")
def played_session(tmp_path_factory):
    from mavric.sim import simulate_session

    return simulate_session(tmp_path_factory.mktemp("au5"), duration_s=150, seed=3, audio=True, playback_every_s=15,
                            sound_labels=True, label_noise_s=0.05)


def test_analysis_of_a_simulated_session(played_session):
    from mavric.analysis.pipeline import run_analysis

    sess = played_session
    s = run_analysis(sess, plots=True)
    out = sess.analysis_dir
    for f in ("sound_events_linked.csv", "call_rates.csv", "playback_responses.csv", "playback_by_subject.csv",
              "sound_ethogram.png", "call_rates.png", "playback_response.png"):
        assert (out / f).exists(), f
    se = s["sound_events"]
    log = pd.read_csv(sess.file("audio_playback.csv"))
    assert se["playback"]["n_played"] == len(log) and se["coverage_assumed"] is False
    calls = pd.read_csv(sess.file("ground_truth_calls.csv"))
    assert se["n"] == len(calls) + len(log)
    # every call happened on a perch: RFID places the caller there (allowing for RFID's own misses)
    linked = pd.read_csv(out / "sound_events_linked.csv", keep_default_na=False)
    subj = linked[linked["emitter_kind"] == "subject"].reset_index(drop=True)
    names = sess.antennas
    truth = calls.sort_values("t").reset_index(drop=True)
    agree = (subj["place_label"] == truth["perch"].map(names)).mean()
    assert agree > 0.95
    assert (subj["place_rfid"] == truth["perch"].map(names)).mean() > 0.8
    # played sounds land on the session timeline where the simulator played them
    resp = pd.read_csv(out / "playback_responses.csv")
    tr = sess.meta["sim"]["audio"]
    d = resp["t"] - (log["sample"] / tr["rate"] + tr["start_s"])
    assert d.std() < 1e-4  # exact up to one offset: the session clock is the simulated world's within the sync
    assert abs(d.mean()) < 0.02  # offset, a few ms
    # the simulated answers come 0.6 s or more after the onset: the answered share and latencies reflect them
    by = pd.read_csv(out / "playback_by_subject.csv")
    answers = truth[truth["response"] == 1]
    for t0 in resp["t"]:
        for p in sess.tags.pigeons:
            a = answers[(answers["pigeon"] == p) & (answers["t"] > t0) & (answers["t"] < t0 + 10)]
            got = by[(np.isclose(by["t"], t0)) & (by["subject"] == p)]["first_latency_s"].iloc[0]
            if len(a):
                assert got <= a["t"].min() - t0 + 1e-3  # the first answer can only be earlier (a spontaneous call)
    rates = pd.read_csv(out / "call_rates.csv")
    allt = rates[rates["type"] == "(all)"]
    for p, g in allt.groupby("subject"):
        g = g.set_index("place")
        assert g.drop(index=ANY_PLACE)["n"].sum() == g.loc[ANY_PLACE, "n"]
        assert g.drop(index=ANY_PLACE)["time_s"].sum() >= g.loc[ANY_PLACE, "time_s"] - 1e-6


def test_render_carries_the_sound_in_step_with_the_picture(played_session):
    from mavric.analysis.pipeline import prepare
    from mavric.analysis.render import render_annotated
    from mavric.audio.source import AudioSource

    sess = played_session
    P = prepare(sess)
    out = render_annotated(P, None, out_path=sess.analysis_dir / "clip.mp4", t_range=(40.0, 52.0))
    with av.open(str(out)) as c:
        vs, as_ = c.streams.video[0], c.streams.audio[0]
        assert len(as_.layout.channels) == 2 and as_.rate == 48000
        assert vs.height > 480  # picture plus the spectrogram strip
        n_frames = sum(1 for _ in c.decode(video=0))
    with av.open(str(out)) as c:
        x = np.concatenate([f.to_ndarray() for f in c.decode(audio=0)], axis=1).T
    assert abs(len(x) / 48000 - n_frames / 30.0) < 0.05
    # the track is the session audio seen through the frames' times: cross-correlate with the source
    i0 = int(np.searchsorted(P.frame_t, 40.0))
    t_mid = float(P.frame_t[i0 + 150])  # frame 150 of the clip = 5 s into the video
    src = AudioSource(sess, P.audio)
    ref, _ = src.read(t_mid - 1, t_mid + 1, [src.mic_idx[0]])
    src.close()
    seg = x[int(4 * 48000): int(6 * 48000), 0]
    ref = ref[:, 0] / np.abs(ref).max()
    seg = seg / np.abs(seg).max()
    n = len(seg) + len(ref)
    cc = np.fft.irfft(np.fft.rfft(seg - seg.mean(), n) * np.conj(np.fft.rfft(ref - ref.mean(), n)), n)
    lag = int(np.argmax(cc))
    lag_s = (lag - n if lag > n // 2 else lag) / 48000  # > 0: the track is late
    assert abs(lag_s) < 0.002  # measured: within 0.1 ms (the decoder drops the AAC encoder's priming)


def test_timeline_export(played_session):
    import soundfile as sf

    from mavric.analysis.labels_io import load_labels
    from mavric.analysis.pipeline import prepare
    from mavric.analysis.timeline import export_timeline

    sess = played_session
    P = prepare(sess)
    out = export_timeline(P, load_labels(sess))
    tl = pd.read_csv(out / "timeline.csv", keep_default_na=False)
    assert set(tl["track"]) == {"place_rfid", "place_label", "sound", "played"}
    info = sf.info(str(out / "audio_cam0.wav"))
    assert info.channels == 2 and abs(info.duration - len(P.frame_t) / 30.0) < 0.05
    # clock seconds = frame index / fps: a frame's own time maps to its index
    k = 1000
    from mavric.analysis.render import video_timeline

    tau, t = video_timeline(P.frame_t, 0, len(P.frame_t), 30.0)
    assert np.interp(P.frame_t[k], t, tau) == pytest.approx(k / 30.0)
    lines = (out / "sound_events.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == (tl["track"] == "sound").sum() and "\t" in lines[0]
    assert (out / "README.txt").exists()
