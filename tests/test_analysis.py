import numpy as np
import pandas as pd
import pytest

from mavric.analysis.bouts import build_bouts
from mavric.analysis.compare import compare, occupancy, transitions
from mavric.analysis.labels_io import changes_to_intervals, intervals_to_changes, merge_spans
from mavric.analysis.sync import fit_lower_envelope, match_pulses
from mavric.config import EMPTY
from mavric.rfid.serial_reader import PresenceTracker


def test_lower_envelope_recovers_drift_and_offset():
    rng = np.random.default_rng(0)
    dev = np.arange(0, 3600, 0.125)
    host = 12.5 + dev * (1 - 145e-6) + 0.002 + rng.exponential(0.004, len(dev))
    host[::500] += 0.3  # catch-up bursts
    f = fit_lower_envelope(dev, host)
    assert f.drift_ppm == pytest.approx(145, abs=1)
    assert f(dev[0]) == pytest.approx(12.5 + 0.002, abs=0.002)
    assert f.latency_med_s < 0.01


def test_match_pulses_with_missing_and_extra():
    rng = np.random.default_rng(1)
    sync = np.cumsum(rng.uniform(2, 6, 60))
    led = sync + 0.012 + rng.uniform(-0.016, 0.016, len(sync))
    led = np.delete(led, [3, 17, 40])  # LED occluded
    led = np.sort(np.r_[led, [5.55, 101.3]])  # reflections
    m = match_pulses(led, sync, fps=30)
    assert m["n_matched"] == 57
    assert m["offset_s"] == pytest.approx(0.012, abs=0.005)
    assert m["resid_p95_abs_s"] < 0.02


def test_build_bouts_gap_and_period():
    t = np.r_[np.arange(10, 12, 0.125), np.arange(12.4, 13, 0.125), np.arange(20, 21, 0.125)]
    s = pd.DataFrame({"t": t, "antenna": 1, "tag": "A", "checksum_ok": True, "pigeon": "S01", "roi": "PerchA"})
    b = build_bouts(s, period_s=0.125, gap_s=0.5)
    assert len(b) == 2  # 0.4 s hole merged, 7 s hole splits
    assert b.loc[0, "start_s"] == pytest.approx(10)
    assert b.loc[0, "stop_s"] == pytest.approx(t[t < 13][-1] + 0.125)
    bad = s.assign(checksum_ok=False)
    assert len(build_bouts(bad)) == 0


def test_occupancy_multi():
    iv = pd.DataFrame({"roi": ["A", "A"], "pigeon": ["p1", "p2"], "start_s": [0, 1], "stop_s": [2, 3]})
    occ = occupancy(iv, ["A"], np.array([0.5, 1.5, 2.5, 3.5]))
    assert list(occ["A"]) == ["p1", "MULTI", "p2", EMPTY]


def test_compare_known_numbers():
    man = pd.DataFrame({"annotator": "x", "roi": ["A", "B"], "pigeon": ["p1", "p2"],
                        "start_s": [0.0, 10.0], "stop_s": [10.0, 20.0], "flag": ""})
    rf = pd.DataFrame({"pigeon": ["p1", "p1", "p2"], "tag": "", "antenna": [1, 2, 2], "roi": ["A", "B", "B"],
                       "start_s": [2.0, 4.0, 10.0], "stop_s": [10.0, 5.0, 15.0], "duration_s": 0, "n_reads": 1})
    c = compare(man, rf, ["A", "B"], [(0.0, 20.0)], bin_s=1.0, tol_s=1.0)
    a = c.per_roi.set_index("roi")
    # A: truth p1 0-10; RFID p1 2-10 -> sensitivity 8/10, precision 1
    assert a.loc["A", "presence_sensitivity"] == pytest.approx(0.8)
    assert a.loc["A", "presence_precision"] == pytest.approx(1.0)
    # B: truth p2 10-20; RFID p2 10-15 (5 TP) + p1 at 4-5 (1 FP, crosstalk: p1 truly on A)
    assert a.loc["B", "presence_sensitivity"] == pytest.approx(0.5)
    assert a.loc["B", "presence_precision"] == pytest.approx(5 / 6)
    assert a.loc["B", "fp_crosstalk_s"] == pytest.approx(1.0)
    assert c.summary["overall"]["id_accuracy_when_detected"] == pytest.approx(1.0)
    ev = c.summary["events"]
    assert ev["manual_bouts"] == 2 and ev["manual_bouts_detected"] == 2
    assert ev["onset_latency_med_s"] == pytest.approx(1.0)  # (2 + 0) / 2


def test_changes_roundtrip_and_spans():
    ch = {"A": [(1.0, "p1"), (3.0, EMPTY), (5.0, "p2")], "B": []}
    iv = changes_to_intervals(ch, 10.0, "me")
    assert iv[["roi", "pigeon", "start_s", "stop_s"]].values.tolist() == [["A", "p1", 1.0, 3.0], ["A", "p2", 5.0, 10.0]]
    back = intervals_to_changes(iv, ["A", "B"])
    assert back["A"] == [(1.0, "p1"), (3.0, EMPTY), (5.0, "p2"), (10.0, EMPTY)]
    assert merge_spans([(5, 7), (0, 2), (1, 3), (7, 8)]) == [(0, 3), (5, 8)]


def test_transitions():
    iv = pd.DataFrame({"pigeon": ["p"] * 4, "roi": ["A", "A", "B", "A"],
                       "start_s": [0, 1.5, 10, 20], "stop_s": [1, 3, 12, 22]})
    tm = transitions(iv, ["A", "B"])
    assert tm.loc["A", "B"] == 1 and tm.loc["B", "A"] == 1 and tm.loc["A", "A"] == 0


def test_presence_tracker_debounce():
    pt = PresenceTracker(4, off_rounds=2)
    ev = []
    seq = [(1, "TAG", 1, "X"), (2, "TAG", 1, "X"), (3, "NONE", 0, ""), (4, "TAG", 1, "X"),
           (5, "NONE", 0, ""), (6, "NONE", 0, ""), (7, "NONE", 0, "")]
    for k, kind, r, tag in seq:
        ev += pt.feed(k, kind, r, tag, float(k))
    assert [(e.reader, e.tag, e.prev_tag) for e in ev] == [(1, "X", ""), (1, "", "X")]
    assert ev[1].host_abs == 6.0  # second missing round
