"""Offline pipeline: sync -> bouts -> (labels) -> comparison -> tables, plots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ..session import CURATION_FILE, Session
from .bouts import build_bouts, stream_health, tag_samples
from .compare import compare, time_budget, transitions
from .io import load_frames, load_rfid_raw
from .labels_io import load_coverage, load_labels
from .sync import synchronise


@dataclass
class Prepared:
    """Synchronised session data, shared by analysis, review GUI and renderer."""

    session: Session
    raw: pd.DataFrame
    frames: pd.DataFrame | None
    frame_t: np.ndarray | None  # event time of each video frame (index = frame_idx)
    samples: pd.DataFrame  # tag reads with t
    bouts: pd.DataFrame
    sync: dict
    health: dict
    audio: object | None = None  # AudioTimeMap: audio sample -> session time

    @property
    def rois(self) -> list[str]:
        return [r.name for r in self.session.rois.perches] or list(self.session.antennas.values())


def prepare(sess: Session, gap_s: float = 0.5, min_dur_s: float = 0.0, cam_id: str | None = None) -> Prepared:
    raw = load_rfid_raw(sess)
    frames, cam = None, None
    if sess.cameras:
        cam = sess.camera(cam_id)
        frames = load_frames(sess, cam_id)
    sy = synchronise(raw, frames, cam)
    tags = sess.tags
    samples = tag_samples(raw, sy["rfid_t"], tags, sess.antennas)
    bouts = build_bouts(samples, period_s=sess.round_period_s, gap_s=gap_s, min_dur_s=min_dur_s)
    bouts = apply_curation(sess, bouts)
    health = stream_health(raw, sy["rfid_t"], sess.round_period_s)
    health["n_unknown_tag_reads"] = int((samples["pigeon"] == "UNKNOWN").sum())
    health["unknown_tags"] = sorted(samples.loc[samples["pigeon"] == "UNKNOWN", "tag"].unique().tolist())
    audio_map = None
    if sess.audio:
        from .audio_sync import synchronise_audio

        audio_map, sy["report"]["audio"] = synchronise_audio(sess, raw, sy["rfid_fits"], sy["offset_s"])
    return Prepared(sess, raw, frames, sy["frame_t"], samples, bouts, sy, health, audio_map)


def apply_curation(sess: Session, bouts: pd.DataFrame) -> pd.DataFrame:
    """Mark bouts the annotator flagged as spurious in the review GUI (column `curated`)."""
    bouts = bouts.copy()
    bouts["curated"] = "unreviewed"
    p = sess.file(CURATION_FILE)
    if not p.exists() or not len(bouts):
        return bouts
    cur = pd.read_csv(p)
    for _, c in cur.iterrows():
        m = (bouts["antenna"] == c["antenna"]) & (bouts["tag"] == c["tag"]) & \
            (bouts["start_s"] < c["t"]) & (bouts["stop_s"] > c["t"])
        bouts.loc[m, "curated"] = c["verdict"]
    return bouts


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, float) and np.isnan(o):
        return None
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def run_analysis(
    sess: Session | str | Path,
    bin_s: float = 0.5,
    gap_s: float = 0.5,
    tol_s: float = 1.0,
    plots: bool = True,
    use_curated: bool = False,
) -> dict:
    """Run everything and write `analysis/`. Returns the summary dict."""
    if not isinstance(sess, Session):
        sess = Session.open(sess)
    P = prepare(sess, gap_s=gap_s)
    out = sess.analysis_dir
    rois = P.rois

    P.samples.to_csv(out / "rfid_samples.csv", index=False)
    P.bouts.to_csv(out / "rfid_bouts.csv", index=False)
    if P.frame_t is not None:
        pd.DataFrame({"frame_idx": P.frames["frame_idx"], "t": P.frame_t}).to_csv(out / "frame_times.csv", index=False)
    if P.audio is not None:
        P.audio.knots().to_csv(out / "audio_time_knots.csv", index=False)
    with open(out / "sync_report.json", "w") as f:
        json.dump(_jsonable({**P.sync["report"], "stream_health": P.health}), f, indent=2)

    rfid_used = P.bouts
    if use_curated and "curated" in rfid_used:
        rfid_used = rfid_used[rfid_used["curated"] != "spurious"]

    summary: dict = {
        "session": sess.path.name,
        "duration_s": sess.meta.get("duration_s"),
        "sync": P.sync["report"],
        "stream_health": {k: v for k, v in P.health.items() if k != "heartbeat_gaps"},
        "rfid_time_budget_s": time_budget(rfid_used, rois).round(2).to_dict(orient="index"),
        "params": {"bin_s": bin_s, "gap_s": gap_s, "tol_s": tol_s, "use_curated": use_curated},
    }
    tb = time_budget(rfid_used, rois)
    tb.to_csv(out / "time_budget_rfid.csv")

    actions_file = sess.file("actions.csv")
    if actions_file.exists():
        acts = pd.read_csv(actions_file)
        rules = acts[acts["rule"].notna() & (acts["rule"].astype(str) != "")]
        if len(rules):
            lat = pd.to_numeric(rules["latency_ms"], errors="coerce")
            summ = rules.assign(latency_ms=lat).groupby("rule").agg(
                fired=("status", lambda s: int((s == "ok").sum())),
                errors=("status", lambda s: int((s == "error").sum())),
                dry=("status", lambda s: int((s == "dry").sum())),
                suppressed=("status", lambda s: int((s == "suppressed").sum())),
                latency_med_ms=("latency_ms", "median"),
                latency_p95_ms=("latency_ms", lambda x: x.quantile(0.95)),
                first_t=("t", "min"), last_t=("t", "max"),
            )
            summ.to_csv(out / "actions_summary.csv")
            summary["actions"] = summ.reset_index().to_dict(orient="records")
        wd = acts[acts["event"].astype(str) == "wdog"]
        if len(wd):
            summary.setdefault("warnings", []).append(f"I/O watchdog fired {len(wd)} time(s): outputs dropped")

    from ..audio.events import load_events, load_sound_coverage

    sound = load_events(sess) if sess.audio else []
    if sound:
        from ..audio.events import events_to_df

        sdf = events_to_df(sound)
        sdf["duration_s"] = sdf["stop_s"] - sdf["start_s"]
        sdf.groupby(["type", "emitter"]).agg(n=("start_s", "size"), total_s=("duration_s", "sum")).to_csv(
            out / "sound_events_summary.csv")
        summary["sound_events"] = {
            "n": int(len(sdf)), "reviewed_s": float(sum(b - a for a, b in load_sound_coverage(sess))),
            "by_type": sdf["type"].value_counts().to_dict(), "by_emitter": sdf["emitter"].value_counts().to_dict(),
        }

    if sess.clips:
        from .clips import clip_table

        fits = {}
        if P.sync.get("cam_fit") is not None and sess.cameras:
            fits[sess.camera()["id"]] = P.sync["cam_fit"]
        ct = clip_table(sess, P.bouts, fits)
        ct.to_csv(out / "clips.csv", index=False)
        summary["clips"] = {
            "n": int(len(ct)),
            "total_s": float((ct["last_frame_t"] - ct["first_frame_t"]).sum()),
            "by_rule": ct.groupby("rule").size().to_dict(),
            "linked_to_rfid_bout": int(ct["bout_start_s"].notna().sum()),
            "covering_whole_bout": int(ct["covers_bout"].sum()),
        }
    tr = transitions(rfid_used, rois)
    tr.to_csv(out / "transitions_rfid.csv")

    labels = load_labels(sess)
    coverage = load_coverage(sess)
    cmp = None
    if labels is not None and len(labels):
        if not coverage:
            coverage = [(float(labels["start_s"].min()), float(labels["stop_s"].max()))]
        cmp = compare(labels, rfid_used, rois, coverage, bin_s=bin_s, tol_s=tol_s)
        cmp.bins.to_csv(out / "comparison_bins.csv", index=False)
        cmp.per_roi.to_csv(out / "accuracy_per_roi.csv", index=False)
        cmp.per_pigeon.to_csv(out / "accuracy_per_pigeon.csv", index=False)
        cmp.confusion.to_csv(out / "confusion_seconds.csv")
        cmp.events.to_csv(out / "event_matching.csv", index=False)
        time_budget(labels, rois).to_csv(out / "time_budget_manual.csv")
        summary["comparison"] = cmp.summary
        summary["accuracy_per_roi"] = cmp.per_roi.to_dict(orient="records")
        summary["accuracy_per_pigeon"] = cmp.per_pigeon.to_dict(orient="records")

    summary = _jsonable(summary)
    with open(out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    if plots:
        from . import plots as pl

        pl.make_all(P, cmp, tb, tr, labels, out)
    return summary
