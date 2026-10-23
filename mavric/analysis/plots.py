"""Static report figures (PNG). Light surface, recessive chrome, thin marks.

Forms are chosen so colour carries as little as possible:
- where each pigeon is over time: small multiples per pigeon, y = perch,
  two series only (manual vs RFID)
- accuracy: dot plots per metric, one colour
- time budget, transitions, confusion: single-hue sequential heatmaps with values written in
"""

from __future__ import annotations

from pathlib import Path

import logging

import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
S_MANUAL = "#2a78d6"  # categorical slot 1
S_RFID = "#eb6834"  # categorical slot 2
BLUE_RAMP = ["#f0f5fc", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
SEQ = LinearSegmentedColormap.from_list("mavric_blue", BLUE_RAMP)

STYLE = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
    "font.size": 9,
    "text.color": INK,
    "axes.labelcolor": INK2,
    "axes.edgecolor": AXIS,
    "axes.linewidth": 0.8,
    "axes.titlesize": 10,
    "axes.titleweight": "semibold",
    "axes.titlecolor": INK,
    "axes.titlelocation": "left",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": False,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "grid.linestyle": "-",
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK2,
    "ytick.labelcolor": INK2,
    "legend.frameon": False,
    "legend.fontsize": 8,
}


def _save(fig, path: Path) -> Path:
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _time_unit(t_max: float) -> tuple[float, str]:
    return (60.0, "min") if t_max > 300 else (1.0, "s")


# --------------------------------------------------------------------------- figures


def timeline_by_pigeon(bouts, labels, rois, pigeons, coverage, out: Path, t_end: float | None = None) -> Path:
    """Small multiples per pigeon: perch occupancy over time, manual (wide) vs RFID (thin)."""
    n = max(1, len(pigeons))
    t_end = t_end or max(
        [float(bouts["stop_s"].max()) if len(bouts) else 0.0]
        + ([float(labels["stop_s"].max())] if labels is not None and len(labels) else [])
    )
    k, unit = _time_unit(t_end)
    if coverage and sum(b - a for a, b in coverage) >= 0.98 * t_end:
        coverage = []  # everything was labeled: no need to mark the span
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(n, 1, figsize=(10, 0.55 * len(rois) * n + 1.0), sharex=True, squeeze=False)
        for ax, p in zip(axes[:, 0], pigeons):
            ax.set_ylim(-0.6, len(rois) - 0.4)
            ax.set_yticks(range(len(rois)), rois)
            ax.invert_yaxis()
            ax.grid(axis="x")
            ax.set_axisbelow(True)
            for a, b in coverage:
                ax.axvspan(a / k, b / k, color="#f0efec", lw=0, zorder=0)
            if labels is not None and len(labels):
                g = labels[labels["pigeon"] == p]
                for roi, a, b in zip(g["roi"], g["start_s"], g["stop_s"]):
                    if roi in rois:
                        y = rois.index(roi)
                        ax.broken_barh([(a / k, (b - a) / k)], (y - 0.32, 0.64), color=S_MANUAL, alpha=0.35, lw=0)
            g = bouts[bouts["pigeon"] == p]
            for roi, a, b in zip(g["roi"], g["start_s"], g["stop_s"]):
                if roi in rois:
                    y = rois.index(roi)
                    ax.broken_barh([(a / k, (b - a) / k)], (y - 0.1, 0.2), color=S_RFID, lw=0)
            ax.set_title(p, fontsize=9, pad=3)
        axes[-1, 0].set_xlabel(f"Time ({unit})")
        axes[-1, 0].set_xlim(0, t_end / k)
        handles = [plt.Rectangle((0, 0), 1, 1, color=S_MANUAL, alpha=0.35, lw=0),
                   plt.Rectangle((0, 0), 1, 1, color=S_RFID, lw=0)]
        names = ["Manual label (ground truth)", "RFID bout"]
        if coverage:
            handles.append(plt.Rectangle((0, 0), 1, 1, color="#f0efec", lw=0))
            names.append("Labeled span")
        fig.legend(handles, names, loc="upper right", ncol=3, bbox_to_anchor=(1.0, 1.0))
        fig.suptitle("Place occupancy per subject", x=0.01, ha="left", fontsize=11, fontweight="semibold")
        fig.tight_layout(rect=(0, 0, 1, 0.97))
        return _save(fig, out / "timeline_by_subject.png")


def accuracy_dots(per_roi: pd.DataFrame, overall: dict, out: Path) -> Path:
    metrics = [
        ("presence_sensitivity", "Presence detected\n(sensitivity)"),
        ("presence_precision", "RFID presence correct\n(precision)"),
        ("id_accuracy_when_detected", "Correct ID when detected"),
    ]
    rows = per_roi["roi"].tolist() + ["All places"]
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 3, figsize=(10, 0.42 * len(rows) + 1.3), sharey=True)
        for ax, (m, title) in zip(axes, metrics):
            vals = per_roi[m].tolist() + [overall.get(m, np.nan)]
            y = np.arange(len(rows))
            ax.hlines(y, 0, 1.0, color=GRID, lw=0.6)
            ax.scatter(vals, y, s=48, color=S_MANUAL, edgecolor=SURFACE, linewidth=2, zorder=3)
            for yi, v in zip(y, vals):
                if np.isfinite(v):
                    ax.text(min(v, 1.0) + 0.02 if v < 0.9 else v - 0.02, yi - 0.28, f"{v:.0%}",
                            ha="left" if v < 0.9 else "right", va="center", fontsize=8, color=INK2)
            ax.set_xlim(0, 1.06)
            ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0], ["0", "25%", "50%", "75%", "100%"])
            ax.set_yticks(y, rows)
            ax.set_title(title, fontsize=9)
            ax.spines["left"].set_visible(False)
            ax.tick_params(axis="y", length=0)
        axes[0].invert_yaxis()
        fig.suptitle("RFID vs manual labels, per place", x=0.01, ha="left", fontsize=11, fontweight="semibold")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fig.subplots_adjust(wspace=0.18)
        return _save(fig, out / "accuracy_per_place.png")


def heatmap(df: pd.DataFrame, title: str, fmt, out_path: Path, xlabel: str = "", ylabel: str = "",
            note: str = "", color_df: pd.DataFrame | None = None) -> Path:
    """Sequential heatmap; values written in. `color_df` (e.g. row fractions) can drive the colour."""
    vals = df.to_numpy(dtype=float)
    cvals = vals if color_df is None else color_df.to_numpy(dtype=float)
    with plt.rc_context(STYLE):
        fig, ax = plt.subplots(figsize=(1.1 * df.shape[1] + 2.4, 0.5 * df.shape[0] + 1.4))
        vmax = np.nanmax(cvals) if np.isfinite(cvals).any() and np.nanmax(cvals) > 0 else 1
        ax.imshow(cvals, cmap=SEQ, vmin=0, vmax=vmax, aspect="auto")
        ax.set_xticks(range(df.shape[1]), [str(c) for c in df.columns])
        ax.set_yticks(range(df.shape[0]), [str(i) for i in df.index])
        ax.set_xticks(np.arange(-0.5, df.shape[1]), minor=True)
        ax.set_yticks(np.arange(-0.5, df.shape[0]), minor=True)
        ax.grid(which="minor", color=SURFACE, lw=2)
        ax.tick_params(which="both", length=0)
        for s in ax.spines.values():
            s.set_visible(False)
        for i in range(df.shape[0]):
            for j in range(df.shape[1]):
                v = vals[i, j]
                if np.isfinite(v):
                    dark = cvals[i, j] / vmax > 0.55
                    ax.text(j, i, fmt(v), ha="center", va="center", fontsize=8, color="white" if dark else INK)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        if note:
            fig.text(0.01, 0.0, note, fontsize=7.5, color=MUTED, ha="left", va="bottom")
        fig.tight_layout(rect=(0, 0.04 if note else 0, 1, 1))
        return _save(fig, out_path)


def latency_hist(events: pd.DataFrame, tol_s: float, out: Path) -> Path | None:
    man = events[(events["kind"] == "manual") & events["matched"]]
    if not len(man):
        return None
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(9, 2.8), sharey=True)
        for ax, col, title in zip(axes, ["onset_err_s", "offset_err_s"], ["Arrival: RFID start − manual start",
                                                                           "Departure: RFID stop − manual stop"]):
            v = man[col].dropna().to_numpy()
            lim = max(2 * tol_s, float(np.nanpercentile(np.abs(v), 98)) if len(v) else 1)
            edges = np.linspace(-lim, lim, 33)
            ax.hist(np.clip(v, -lim, lim), bins=edges, color=S_MANUAL, edgecolor=SURFACE, linewidth=1)
            ax.axvline(0, color=AXIS, lw=0.8)
            med = float(np.median(v)) if len(v) else np.nan
            ax.axvline(med, color=INK2, lw=1)
            ax.text(med, ax.get_ylim()[1] * 0.95, f" median {med:+.2f} s", color=INK2, fontsize=8, va="top")
            ax.axvspan(-tol_s, tol_s, color="#f0efec", lw=0, zorder=0)
            ax.set_title(title, fontsize=9)
            ax.set_xlabel("Seconds")
        axes[0].set_ylabel("Bouts")
        fig.suptitle(f"Bout timing error (shaded: ±{tol_s:g} s tolerance)", x=0.01, ha="left",
                     fontsize=11, fontweight="semibold")
        fig.tight_layout(rect=(0, 0, 1, 0.92))
        return _save(fig, out / "bout_timing_error.png")


def sync_diagnostics(P, out: Path) -> Path:
    raw = P.raw
    fits = P.sync["rfid_fits"]
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(1, 2, figsize=(10, 2.8))
        ax = axes[0]
        for seg, f in fits.items():
            g = raw[(raw["segment"] == seg) & raw["arduino_us"].notna()]
            lat = (g["host_t"].to_numpy() - f(g["arduino_us"].to_numpy() / 1e6)) * 1000
            ax.scatter(g["host_t"] / 60, lat, s=2, color=S_MANUAL, alpha=0.4, lw=0)
        ax.set_title("Serial latency after clock fit (Arduino → host)", fontsize=9)
        ax.set_xlabel("Session time (min)")
        ax.set_ylabel("ms")
        ax.grid(axis="y")
        ax.set_axisbelow(True)
        ax = axes[1]
        pairs = P.sync.get("led_pairs") or []
        rep = P.sync["report"].get("led", {})
        if pairs and rep.get("offset_s") is not None:
            lt = np.array([p[0] for p in pairs])
            st = np.array([p[1] for p in pairs])
            r = (lt - st - rep["offset_s"]) * 1000
            ax.scatter(lt / 60, r, s=18, color=S_MANUAL, edgecolor=SURFACE, linewidth=1)
            ax.axhline(0, color=AXIS, lw=0.8)
            ax.set_title(f"LED onset vs SYNC line, {len(pairs)}/{rep.get('n_sync')} pulses matched "
                         f"(offset {rep['offset_s'] * 1000:.1f} ms applied)", fontsize=9)
        else:
            ax.text(0.5, 0.5, "No LED/SYNC pulses matched\n(LED ROI missing or not visible)", ha="center",
                    va="center", color=MUTED, transform=ax.transAxes)
            ax.set_title("LED onset vs SYNC line", fontsize=9)
        ax.set_xlabel("Session time (min)")
        ax.set_ylabel("Residual (ms)")
        ax.grid(axis="y")
        ax.set_axisbelow(True)
        fig.tight_layout()
        return _save(fig, out / "sync_diagnostics.png")


def make_all(P, cmp, time_budget_df, transitions_df, labels, out: Path) -> list[Path]:
    out = Path(out)
    rois = P.rois
    pigeons = [p for p in P.session.tags.pigeons if
               (len(P.bouts) and (P.bouts["pigeon"] == p).any()) or
               (labels is not None and len(labels) and (labels["pigeon"] == p).any())]
    coverage = cmp.summary["coverage"] if cmp is not None else []
    t_end = float(P.frame_t[-1]) if P.frame_t is not None and len(P.frame_t) else None
    paths = [timeline_by_pigeon(P.bouts, labels, rois, pigeons, coverage, out, t_end), sync_diagnostics(P, out)]
    if len(time_budget_df):
        paths.append(heatmap(time_budget_df / 60.0, "Time at each place, RFID (minutes)", lambda v: f"{v:.1f}",
                             out / "time_budget_rfid.png", "Place", "Subject"))
    if transitions_df.to_numpy().sum() > 0:
        paths.append(heatmap(transitions_df, "Place-to-place moves, RFID (count)", lambda v: f"{v:.0f}",
                             out / "transitions_rfid.png", "To", "From"))
    if cmp is not None:
        paths.append(accuracy_dots(cmp.per_roi, cmp.summary["overall"], out))
        conf = cmp.confusion
        row_frac = conf.div(conf.sum(axis=1).replace(0, np.nan), axis=0)
        paths.append(heatmap(conf, "Who RFID reported vs who was there (seconds)", lambda v: f"{v:.0f}",
                             out / "confusion_seconds.png", "RFID", "Manual", color_df=row_frac,
                             note="Rows: manual label. Columns: RFID. Shade = share of the row; "
                                  "diagonal = agreement; EMPTY = nobody."))
        p = latency_hist(cmp.events, cmp.summary["tol_s"], out)
        if p:
            paths.append(p)
    return paths
