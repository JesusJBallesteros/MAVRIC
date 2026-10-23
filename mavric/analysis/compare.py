"""RFID vs human ground truth, per ROI and time bin, plus event-level matching."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import EMPTY, MULTI


def _bins(coverage: list[tuple[float, float]], bin_s: float) -> np.ndarray:
    centers = [np.arange(a + bin_s / 2, b, bin_s) for a, b in coverage]
    return np.concatenate(centers) if centers else np.array([])


def occupancy(intervals: pd.DataFrame, rois: list[str], centers: np.ndarray) -> dict[str, np.ndarray]:
    """Occupant of each ROI at each bin centre: pigeon name, EMPTY or MULTI."""
    occ = {r: np.full(len(centers), EMPTY, dtype=object) for r in rois}
    if intervals is None or len(intervals) == 0:
        return occ
    for r in rois:
        g = intervals[intervals["roi"] == r]
        lab = occ[r]
        for pig, a, b in zip(g["pigeon"], g["start_s"], g["stop_s"]):
            i0, i1 = np.searchsorted(centers, [a, b])
            seg = lab[i0:i1]
            other = (seg != EMPTY) & (seg != pig)
            seg[seg == EMPTY] = pig
            seg[other] = MULTI
    return occ


def cohen_kappa(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) == 0:
        return float("nan")
    cats = np.union1d(a, b)
    ia = np.searchsorted(cats, a)
    ib = np.searchsorted(cats, b)
    cm = np.zeros((len(cats), len(cats)))
    np.add.at(cm, (ia, ib), 1)
    n = cm.sum()
    po = np.trace(cm) / n
    pe = (cm.sum(0) * cm.sum(1)).sum() / n**2
    return float((po - pe) / (1 - pe)) if pe < 1 else 1.0


def _rate(num: float, den: float) -> float:
    return float(num / den) if den else float("nan")


@dataclass
class Comparison:
    bins: pd.DataFrame
    per_roi: pd.DataFrame
    per_pigeon: pd.DataFrame
    confusion: pd.DataFrame
    events: pd.DataFrame
    summary: dict = field(default_factory=dict)


def compare(
    manual: pd.DataFrame,
    rfid_bouts: pd.DataFrame,
    rois: list[str],
    coverage: list[tuple[float, float]],
    bin_s: float = 0.5,
    tol_s: float = 1.0,
) -> Comparison:
    centers = _bins(coverage, bin_s)
    man = occupancy(manual, rois, centers)
    rf = occupancy(rfid_bouts, rois, centers)

    # long table of bins
    parts = []
    for r in rois:
        m, f = man[r], rf[r]
        cat = np.where(
            m != EMPTY,
            np.where(f == EMPTY, "FN", np.where(f == m, "TP_same", "TP_diff")),
            np.where(f == EMPTY, "TN", "FP"),
        )
        parts.append(pd.DataFrame({"t": centers, "roi": r, "manual": m, "rfid": f, "category": cat}))
    bins = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["t", "roi", "manual", "rfid", "category"])
    bins["agree"] = bins["manual"] == bins["rfid"]

    # FP split: crosstalk (that pigeon is truly at another ROI in that bin) vs phantom
    where = {}  # (bin index, pigeon) -> manual ROI
    for r in rois:
        for i in np.flatnonzero(man[r] != EMPTY):
            where[(i, man[r][i])] = r
    n_bins = len(centers)
    fp_kind = np.full(len(bins), "", dtype=object)
    for k in np.flatnonzero((bins["category"] == "FP").to_numpy()):
        i = k % n_bins if n_bins else 0
        fp_kind[k] = "crosstalk" if (i, bins["rfid"].iat[k]) in where else "phantom"
    bins["fp_kind"] = fp_kind

    def metrics(g: pd.DataFrame) -> dict:
        c = g["category"].value_counts()
        tp_s, tp_d, fn, fp, tn = (int(c.get(k, 0)) for k in ("TP_same", "TP_diff", "FN", "FP", "TN"))
        tp = tp_s + tp_d
        return {
            "bins": int(len(g)),
            "occupied_s": (tp + fn) * bin_s,
            "presence_sensitivity": _rate(tp, tp + fn),
            "presence_precision": _rate(tp, tp + fp),
            "presence_specificity": _rate(tn, tn + fp),
            "id_accuracy_when_detected": _rate(tp_s, tp),
            "overall_agreement": _rate(tp_s + tn, len(g)),
            "kappa": cohen_kappa(g["manual"].to_numpy(str), g["rfid"].to_numpy(str)),
            "fp_crosstalk_s": float((g["fp_kind"] == "crosstalk").sum() * bin_s),
            "fp_phantom_s": float((g["fp_kind"] == "phantom").sum() * bin_s),
            "multi_flag_bins": int(((g["manual"] == MULTI) | (g["rfid"] == MULTI)).sum()),
        }

    per_roi = pd.DataFrame([{"roi": r, **metrics(bins[bins["roi"] == r])} for r in rois])
    overall = metrics(bins) if len(bins) else {}

    # per pigeon: of the time a pigeon is truly on a perch, how much does RFID put it there?
    pig_rows = []
    pigeons = sorted((set(bins["manual"]) | set(bins["rfid"])) - {EMPTY, MULTI, ""})
    for p in pigeons:
        truth = bins["manual"] == p
        said = bins["rfid"] == p
        pig_rows.append({
            "pigeon": p,
            "perched_s": float(truth.sum() * bin_s),
            "detected_same_roi": _rate((truth & said).sum(), truth.sum()),
            "rfid_s": float(said.sum() * bin_s),
            "rfid_precision": _rate((truth & said).sum(), said.sum()),
        })
    per_pigeon = pd.DataFrame(pig_rows)

    cats = sorted(set(bins["manual"]) | set(bins["rfid"]), key=lambda x: (x in (EMPTY, MULTI), x))
    confusion = pd.crosstab(
        pd.Categorical(bins["manual"], cats), pd.Categorical(bins["rfid"], cats), rownames=["manual"], colnames=["rfid"],
        dropna=False,
    ) * bin_s

    events = match_events(manual, rfid_bouts, coverage, tol_s)
    ev_sum = {}
    if len(events):
        man_ev = events[events["kind"] == "manual"]
        rf_ev = events[events["kind"] == "rfid"]
        ev_sum = {
            "manual_bouts": int(len(man_ev)),
            "manual_bouts_detected": int(man_ev["matched"].sum()),
            "event_recall": _rate(man_ev["matched"].sum(), len(man_ev)),
            "rfid_bouts": int(len(rf_ev)),
            "event_precision": _rate(rf_ev["matched"].sum(), len(rf_ev)),
            "onset_latency_med_s": float(man_ev["onset_err_s"].median()) if man_ev["matched"].any() else None,
            "offset_latency_med_s": float(man_ev["offset_err_s"].median()) if man_ev["matched"].any() else None,
            "onset_within_tol": _rate((man_ev["onset_err_s"].abs() <= tol_s).sum(), man_ev["matched"].sum()),
            "fragments_per_bout_mean": float(man_ev.loc[man_ev["matched"], "n_rfid_fragments"].mean())
            if man_ev["matched"].any() else None,
        }
    summary = {
        "bin_s": bin_s,
        "tol_s": tol_s,
        "coverage": coverage,
        "coverage_s": float(sum(b - a for a, b in coverage)),
        "overall": overall,
        "events": ev_sum,
    }
    return Comparison(bins, per_roi, per_pigeon, confusion, events, summary)


def match_events(manual: pd.DataFrame, rfid: pd.DataFrame, coverage: list[tuple[float, float]], tol_s: float) -> pd.DataFrame:
    """Bout-level matching per (pigeon, roi): overlap or onset within tol counts as detected."""
    if manual is None or len(manual) == 0:
        return pd.DataFrame()

    def in_cov(df):
        if df is None or len(df) == 0:
            return df
        keep = np.zeros(len(df), bool)
        for a, b in coverage:
            keep |= (df["stop_s"].to_numpy() > a) & (df["start_s"].to_numpy() < b)
        return df[keep]

    man = in_cov(manual).reset_index(drop=True)
    rf = in_cov(rfid).reset_index(drop=True) if rfid is not None else pd.DataFrame(columns=manual.columns)
    # Each RFID bout (fragment) is assigned to at most one manual bout of the same
    # pigeon and ROI: the one it overlaps most, else the nearest within tol_s.
    owner = np.full(len(rf), -1)
    for i, r in rf.iterrows():
        cand = man[(man["pigeon"] == r["pigeon"]) & (man["roi"] == r["roi"])]
        if not len(cand):
            continue
        ov = np.minimum(cand["stop_s"], r["stop_s"]) - np.maximum(cand["start_s"], r["start_s"])
        if ov.max() > 0:
            owner[i] = int(ov.idxmax())
            continue
        gap = -ov  # distance between the intervals
        if gap.min() <= tol_s:
            owner[i] = int(gap.idxmin())
    rows = []
    rf_matched = owner >= 0
    for j, m in man.iterrows():
        ov = rf[owner == j]
        matched = len(ov) > 0
        rows.append({
            "kind": "manual", "pigeon": m["pigeon"], "roi": m["roi"], "start_s": m["start_s"], "stop_s": m["stop_s"],
            "matched": matched, "n_rfid_fragments": int(len(ov)),
            "onset_err_s": float(ov["start_s"].min() - m["start_s"]) if matched else np.nan,
            "offset_err_s": float(ov["stop_s"].max() - m["stop_s"]) if matched else np.nan,
        })
    for i, r in rf.iterrows():
        rows.append({"kind": "rfid", "pigeon": r["pigeon"], "roi": r["roi"], "start_s": r["start_s"],
                     "stop_s": r["stop_s"], "matched": bool(rf_matched[i]), "n_rfid_fragments": 1,
                     "onset_err_s": np.nan, "offset_err_s": np.nan})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- descriptive stats (RFID only)


def time_budget(intervals: pd.DataFrame, rois: list[str]) -> pd.DataFrame:
    """Seconds per pigeon per ROI (rows: pigeon, cols: ROIs)."""
    if intervals is None or len(intervals) == 0:
        return pd.DataFrame(columns=rois)
    d = intervals.assign(dur=intervals["stop_s"] - intervals["start_s"])
    tb = d.pivot_table(index="pigeon", columns="roi", values="dur", aggfunc="sum", fill_value=0.0)
    return tb.reindex(columns=rois, fill_value=0.0)


def transitions(intervals: pd.DataFrame, rois: list[str], merge_gap_s: float = 2.0) -> pd.DataFrame:
    """Counts of perch-to-perch moves (from ROI row to ROI col), all pigeons pooled."""
    tm = pd.DataFrame(0, index=rois, columns=rois)
    if intervals is None or len(intervals) == 0:
        return tm
    for _, g in intervals.sort_values("start_s").groupby("pigeon"):
        seq = []
        last_stop = -np.inf
        for roi, a, b in zip(g["roi"], g["start_s"], g["stop_s"]):
            if seq and roi == seq[-1] and a - last_stop <= merge_gap_s:
                last_stop = b
                continue
            seq.append(roi)
            last_stop = b
        for x, y in zip(seq[:-1], seq[1:]):
            if x in tm.index and y in tm.columns and x != y:
                tm.loc[x, y] += 1
    return tm
