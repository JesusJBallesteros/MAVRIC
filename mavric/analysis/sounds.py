"""Sound events on the RFID timeline: who was where, call rates, answers to played sounds.

    sound_events_linked.csv  every sound event (the labeled ones and every played sound) with the emitter's place
                             (RFID, and the manual place label when there is one) and who RFID detected where
    call_rates.csv           subject x place x type: events, time at the place, events per minute
    playback_responses.csv   one row per played sound: sounds before and after it, the first answer and its latency
    playback_by_subject.csv  played sound x subject: where the subject was, its answers, its first latency

Times are seconds on the session timeline. Rates count only the time reviewed
for sounds (sound_coverage.csv): an unreviewed span has unknown events, not
zero events. A sound's place is where its emitter spent most of the sound.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..audio.events import SYSTEM, UNKNOWN, events_to_df, load_events, merge_playback, playback_events
from ..session import Session
from .labels_io import merge_spans

NOT_DETECTED = "(not detected)"
ALL = "(all)"
ANY_PLACE = "(any place)"

LINKED_COLUMNS = ["start_s", "stop_s", "duration_s", "type", "emitter", "emitter_kind", "place_rfid", "place_label",
                  "present_rfid", "channel", "confidence", "source", "annotator", "note"]
RATE_COLUMNS = ["subject", "place", "type", "n", "time_s", "per_min"]
RESPONSE_COLUMNS = ["t", "t_stop", "speaker", "sound", "window_s", "reviewed_frac", "n_before", "n_after",
                    "rate_before_per_min", "rate_after_per_min", "first_latency_s", "first_emitter", "responders",
                    "present_rfid"]
BY_SUBJECT_COLUMNS = ["t", "speaker", "sound", "subject", "place_rfid", "reviewed_frac", "n_after", "first_latency_s"]


def sound_table(sess: Session, tmap) -> pd.DataFrame:
    """The labeled sound events plus every played sound that is not in the table yet."""
    events = load_events(sess)
    if tmap is not None:
        events, _ = merge_playback(events, playback_events(sess, tmap))
    return events_to_df(events)


def coverage_or_all(sess: Session, tmap) -> tuple[list[tuple[float, float]], bool]:
    """Reviewed spans (sound_coverage.csv); without that file, the whole recording (second value: assumed)."""
    from ..audio.events import load_sound_coverage

    cov = load_sound_coverage(sess)
    if cov:
        return merge_spans(cov), False
    if tmap is None or not sess.audio:
        return [], True
    n = sum(int(f["n_samples"]) for f in sess.audio["files"])
    a, b = (float(x) for x in tmap(np.array([0.0, float(n)])))
    return ([(a, b)] if np.isfinite(a) and np.isfinite(b) and b > a else []), True


# --------------------------------------------------------------------------- interval helpers


def _total(spans) -> float:
    return float(sum(b - a for a, b in spans))


def _intersect(a, b) -> list[tuple[float, float]]:
    """Intersection of two lists of merged, sorted spans."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _covered(spans, t0: float, t1: float) -> float:
    """Share of [t0, t1] inside `spans`."""
    return _total(_intersect(spans, [(t0, t1)])) / (t1 - t0) if t1 > t0 else 0.0


def _in_spans(t: np.ndarray, spans) -> np.ndarray:
    m = np.zeros(len(t), bool)
    for a, b in spans:
        m |= (t >= a) & (t < b)
    return m


class Occupancy:
    """Where each subject was, from an interval table (pigeon, roi, start_s, stop_s): RFID bouts or labels."""

    def __init__(self, intervals: pd.DataFrame | None):
        self.by_subject: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        if intervals is None or not len(intervals):
            return
        d = intervals
        if "curated" in d:
            d = d[d["curated"] != "spurious"]
        for subj, g in d.sort_values("start_s").groupby("pigeon"):
            self.by_subject[str(subj)] = (g["start_s"].to_numpy(float), g["stop_s"].to_numpy(float),
                                          g["roi"].to_numpy(str))

    def place(self, subject: str, t0: float, t1: float) -> str:
        """The place where `subject` spent most of [t0, t1]; '' when it was at none."""
        d = self.by_subject.get(subject)
        if d is None:
            return ""
        a, b, r = d
        t1 = max(t1, t0 + 1e-3)
        ov = np.minimum(b, t1) - np.maximum(a, t0)
        m = ov > 0
        if not m.any():
            return ""
        s = pd.Series(ov[m]).groupby(r[m]).sum()
        return str(s.idxmax())

    def present(self, t: float) -> list[tuple[str, str]]:
        """(subject, place) of everyone at a place at time t."""
        out = []
        for subj, (a, b, r) in self.by_subject.items():
            m = (a <= t) & (b > t)
            out += [(subj, p) for p in sorted(set(r[m]))]
        return sorted(out)

    def spans(self, subject: str, place: str | None = None) -> list[tuple[float, float]]:
        d = self.by_subject.get(subject)
        if d is None:
            return []
        a, b, r = d
        m = np.ones(len(a), bool) if place is None else (r == place)
        return merge_spans(list(zip(a[m], b[m])))


def _kind(emitter: str, subjects: list[str]) -> str:
    if emitter.startswith(f"{SYSTEM}:"):
        return "system"
    return "subject" if emitter in subjects else UNKNOWN


def _who(present: list[tuple[str, str]]) -> str:
    return "; ".join(f"{s}@{p}" for s, p in present)


# --------------------------------------------------------------------------- tables


def link_events(events: pd.DataFrame, bouts: pd.DataFrame | None, labels: pd.DataFrame | None,
                subjects: list[str]) -> pd.DataFrame:
    """Each sound event with its emitter's place (RFID and manual label) and who RFID detected where."""
    if events is None or not len(events):
        return pd.DataFrame(columns=LINKED_COLUMNS)
    rf, lab = Occupancy(bouts), Occupancy(labels)
    df = events.copy()
    df["duration_s"] = df["stop_s"] - df["start_s"]
    df["emitter_kind"] = [_kind(str(e), subjects) for e in df["emitter"]]
    subj = df["emitter_kind"] == "subject"
    df["place_rfid"] = [rf.place(e, a, b) if s else "" for e, a, b, s in
                        zip(df["emitter"], df["start_s"], df["stop_s"], subj)]
    df["place_label"] = [lab.place(e, a, b) if s else "" for e, a, b, s in
                         zip(df["emitter"], df["start_s"], df["stop_s"], subj)]
    df["present_rfid"] = [_who(rf.present(a)) for a in df["start_s"]]
    return df[LINKED_COLUMNS].sort_values("start_s").reset_index(drop=True)


def call_rates(linked: pd.DataFrame, bouts: pd.DataFrame | None, coverage, subjects: list[str],
               places: list[str]) -> pd.DataFrame:
    """Events per minute of each subject at each place (RFID), over the reviewed time only.

    `(not detected)` is the reviewed time when RFID had the subject at no place;
    `(any place)` is all of the subject's events over all reviewed time.
    """
    cov = merge_spans(coverage)
    total = _total(cov)
    if not total:
        return pd.DataFrame(columns=RATE_COLUMNS)
    rf = Occupancy(bouts)
    ev = linked[(linked["emitter_kind"] == "subject")] if len(linked) else linked
    if len(ev):
        ev = ev[_in_spans(ev["start_s"].to_numpy(float), cov)]
    types = [ALL] + sorted(ev["type"].unique().tolist()) if len(ev) else [ALL]
    rows = []
    for s in subjects:
        es = ev[ev["emitter"] == s] if len(ev) else ev
        at = {p: _total(_intersect(rf.spans(s, p), cov)) for p in places}
        at[NOT_DETECTED] = total - _total(_intersect(rf.spans(s), cov))
        at[ANY_PLACE] = total
        for typ in types:
            et = es if typ == ALL else es[es["type"] == typ]
            for p, secs in at.items():
                if p == ANY_PLACE:
                    n = len(et)
                elif p == NOT_DETECTED:
                    n = int((et["place_rfid"] == "").sum()) if len(et) else 0
                else:
                    n = int((et["place_rfid"] == p).sum()) if len(et) else 0
                rows.append([s, p, typ, n, secs, n / secs * 60 if secs > 0 else np.nan])
    return pd.DataFrame(rows, columns=RATE_COLUMNS)


def playback_responses(linked: pd.DataFrame, bouts: pd.DataFrame | None, coverage, subjects: list[str],
                       window_s: float = 10.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sounds before and after each played sound, and who answered how fast.

    An answer is a sound by a subject (or an unknown emitter) that starts after
    the played sound's onset, within `window_s` and before the next played
    sound. The comparison window before the onset has the same length, and
    stops at the previous played sound's end.
    """
    if linked is None or not len(linked):
        return pd.DataFrame(columns=RESPONSE_COLUMNS), pd.DataFrame(columns=BY_SUBJECT_COLUMNS)
    cov = merge_spans(coverage)
    stim = linked[linked["emitter_kind"] == "system"].sort_values("start_s").reset_index(drop=True)
    ans = linked[linked["emitter_kind"] != "system"]
    a_t = ans["start_s"].to_numpy(float)
    a_em = ans["emitter"].to_numpy(str)
    rf = Occupancy(bouts)
    rows, by_subj = [], []
    for k, s in stim.iterrows():
        t, t_stop = float(s["start_s"]), float(s["stop_s"])
        nxt = float(stim["start_s"].iloc[k + 1]) if k + 1 < len(stim) else np.inf
        prev = float(stim["stop_s"].iloc[:k].max()) if k else -np.inf
        a1 = min(t + window_s, nxt)
        b0 = max(t - window_s, prev)
        after = (a_t > t) & (a_t <= a1)
        before = (a_t >= b0) & (a_t < t)
        reviewed = _covered(cov, b0, a1)
        lat = a_t[after] - t
        first = int(np.argmin(lat)) if len(lat) else -1
        present = rf.present(t)
        speaker = str(s["emitter"]).split(":", 1)[1]
        rows.append([t, t_stop, speaker, s["note"], a1 - t, reviewed, int(before.sum()), int(after.sum()),
                     before.sum() / (t - b0) * 60 if t > b0 else np.nan,
                     after.sum() / (a1 - t) * 60 if a1 > t else np.nan,
                     float(lat[first]) if first >= 0 else np.nan, a_em[after][first] if first >= 0 else "",
                     "; ".join(sorted(set(a_em[after]) & set(subjects))), _who(present)])
        where = dict(present)
        for subj in subjects:
            m = after & (a_em == subj)
            by_subj.append([t, speaker, s["note"], subj, where.get(subj, ""), reviewed, int(m.sum()),
                            float((a_t[m] - t).min()) if m.any() else np.nan])
    return pd.DataFrame(rows, columns=RESPONSE_COLUMNS), pd.DataFrame(by_subj, columns=BY_SUBJECT_COLUMNS)


def summarize(linked: pd.DataFrame, rates: pd.DataFrame, responses: pd.DataFrame, by_subject: pd.DataFrame,
              coverage, assumed: bool, window_s: float) -> dict:
    out: dict = {
        "n": int(len(linked)),
        "reviewed_s": _total(merge_spans(coverage)),
        "coverage_assumed": bool(assumed),
        "by_type": linked["type"].value_counts().to_dict() if len(linked) else {},
        "by_emitter": linked["emitter"].value_counts().to_dict() if len(linked) else {},
    }
    subj = linked[linked["emitter_kind"] == "subject"] if len(linked) else linked
    if len(subj):
        out["subject_events_with_rfid_place"] = float((subj["place_rfid"] != "").mean())
    if len(rates):
        r = rates[(rates["type"] == ALL) & (rates["place"] == ANY_PLACE)]
        out["per_min_by_subject"] = dict(zip(r["subject"], r["per_min"].round(3)))
    if len(responses):
        ok = responses[responses["reviewed_frac"] >= 0.99]
        pb = {"n_played": int(len(responses)), "n_reviewed": int(len(ok)), "window_s": window_s}
        if len(ok):
            pb["answered"] = float(ok["first_latency_s"].notna().mean())
            pb["first_latency_med_s"] = float(ok["first_latency_s"].median()) if ok["first_latency_s"].notna().any() \
                else None
            pb["rate_before_per_min"] = float(ok["rate_before_per_min"].mean())
            pb["rate_after_per_min"] = float(ok["rate_after_per_min"].mean())
            bs = by_subject[by_subject["reviewed_frac"] >= 0.99]
            lat = bs.groupby("subject")["first_latency_s"]
            pb["by_subject"] = {s: {"answered": int(g.notna().sum()), "of": int(len(g)),
                                    "latency_med_s": float(g.median()) if g.notna().any() else None}
                                for s, g in lat}
        out["playback"] = pb
    return out


def analyze_sounds(sess: Session, tmap, bouts: pd.DataFrame | None, labels: pd.DataFrame | None, places: list[str],
                   out_dir, window_s: float = 10.0) -> dict | None:
    """Write the sound tables to `out_dir`. Returns the summary and the tables; None without sound events."""
    events = sound_table(sess, tmap)
    if not len(events):
        return None
    subjects = sess.tags.pigeons
    linked = link_events(events, bouts, labels, subjects)
    cov, assumed = coverage_or_all(sess, tmap)
    rates = call_rates(linked, bouts, cov, subjects, places)
    resp, by_subj = playback_responses(linked, bouts, cov, subjects, window_s)
    linked.to_csv(out_dir / "sound_events_linked.csv", index=False)
    if len(rates):
        rates.to_csv(out_dir / "call_rates.csv", index=False)
    if len(resp):
        resp.to_csv(out_dir / "playback_responses.csv", index=False)
        by_subj.to_csv(out_dir / "playback_by_subject.csv", index=False)
    d = linked.assign(duration_s=linked["stop_s"] - linked["start_s"])
    d.groupby(["type", "emitter"]).agg(n=("start_s", "size"), total_s=("duration_s", "sum")).to_csv(
        out_dir / "sound_events_summary.csv")
    return {"summary": summarize(linked, rates, resp, by_subj, cov, assumed, window_s), "linked": linked,
            "rates": rates, "responses": resp, "by_subject": by_subj, "coverage": cov, "window_s": window_s}
