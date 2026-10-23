"""8 Hz RFID samples -> presence bouts per (pigeon, antenna)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import UNKNOWN, TagMap
from ..rfid.protocol import KIND_BAD, KIND_NONE, KIND_TAG

BOUT_COLUMNS = ["pigeon", "tag", "antenna", "roi", "start_s", "stop_s", "duration_s", "n_reads"]


def tag_samples(raw: pd.DataFrame, t: np.ndarray, tags: TagMap, antennas: dict[int, str]) -> pd.DataFrame:
    """Tag reads with event time, pigeon and ROI name."""
    m = (raw["kind"] == KIND_TAG).to_numpy()
    s = pd.DataFrame(
        {
            "t": t[m],
            "antenna": raw.loc[m, "reader"].to_numpy(int),
            "tag": raw.loc[m, "tag"].to_numpy(str),
            "checksum_ok": raw.loc[m, "checksum_ok"].fillna(0).astype(int).to_numpy(bool),
        }
    )
    s["pigeon"] = [tags.name(x) for x in s["tag"]]
    s["roi"] = s["antenna"].map(antennas).fillna("?")
    return s


def build_bouts(
    samples: pd.DataFrame,
    period_s: float = 0.125,
    gap_s: float = 0.5,
    min_dur_s: float = 0.0,
    keep_bad: bool = False,
    keep_unknown: bool = False,
) -> pd.DataFrame:
    """Merge consecutive reads of one tag at one antenna into bouts.

    A read at time t stands for presence during its sampling round [t, t+period),
    so a bout runs from its first read to last read + one period. An empty hole
    longer than `gap_s` between two presence windows starts a new bout.
    """
    s = samples
    if not keep_bad:
        s = s[s["checksum_ok"]]
    if not keep_unknown:
        s = s[s["pigeon"] != UNKNOWN]
    rows = []
    for (ant, tag), g in s.groupby(["antenna", "tag"], sort=False):
        tt = np.sort(g["t"].to_numpy())
        if len(tt) == 0:
            continue
        brk = np.flatnonzero(np.diff(tt) - period_s > gap_s + 1e-9)
        starts = np.r_[0, brk + 1]
        stops = np.r_[brk, len(tt) - 1]
        pig, roi = g["pigeon"].iloc[0], g["roi"].iloc[0]
        for a, b in zip(starts, stops):
            t0, t1 = tt[a], tt[b] + period_s
            if t1 - t0 >= min_dur_s:
                rows.append([pig, tag, int(ant), roi, t0, t1, t1 - t0, int(b - a + 1)])
    df = pd.DataFrame(rows, columns=BOUT_COLUMNS)
    return df.sort_values(["start_s", "antenna"]).reset_index(drop=True)


def stream_health(raw: pd.DataFrame, t: np.ndarray, period_s: float = 0.125) -> dict:
    """Heartbeat gaps (dropped lines / disconnects), bad lines, unknown tags, checksum failures."""
    rounds = raw["kind"].isin([KIND_TAG, KIND_NONE]).to_numpy()
    key = raw.loc[rounds, "segment"].astype(str) + ":" + raw.loc[rounds, "arduino_us"].astype(str)
    first = ~key.duplicated().to_numpy()
    rt = np.sort(t[rounds][first])
    gaps = []
    if len(rt) > 1:
        d = np.diff(rt)
        for i in np.flatnonzero(d > 2.5 * period_s):
            gaps.append({"start_s": float(rt[i]), "stop_s": float(rt[i + 1]), "missing_rounds": int(round(d[i] / period_s)) - 1})
    tagrows = raw[raw["kind"] == KIND_TAG]
    return {
        "n_lines": int(len(raw)),
        "n_rounds": int(len(rt)),
        "round_period_measured_s": float(np.median(np.diff(rt))) if len(rt) > 1 else None,
        "n_bad_lines": int((raw["kind"] == KIND_BAD).sum()),
        "n_tag_reads": int(len(tagrows)),
        "n_checksum_fail": int((tagrows["checksum_ok"].fillna(0) == 0).sum()),
        "n_segments": int(raw["segment"].nunique()),
        "heartbeat_gaps": gaps,
        "missing_rounds_total": int(sum(g["missing_rounds"] for g in gaps)),
    }
