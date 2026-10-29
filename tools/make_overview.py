"""Draw the README overview figure from simulated data (no hardware, no real recordings).

    python tools/make_overview.py            # writes docs/images/overview.svg

The spectrogram and the sound events come from a short simulated session; everything
else is drawn as vector graphics.
"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "overview.svg"
sys.path.insert(0, str(ROOT))

W, H = 1200, 632
FONT = "Segoe UI, Helvetica Neue, Helvetica, Arial, sans-serif"
MONO = "Consolas, Menlo, monospace"
INK, GREY, LIGHT, CARD = "#1d1d1b", "#5f5e5a", "#b9b8b4", "#f6f6f4"
BLUE, ORANGE, GREEN, YELLOW, PINK = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"
SENSE, ACT, AFTER = BLUE, ORANGE, GREEN

# 24x24 line icons, drawn white on a coloured badge
ICONS = {
    "rfid": '<circle cx="7" cy="17" r="2.2" fill="#fff" stroke="none"/><path d="M7 11a6 6 0 0 1 6 6M7 6a11 11 0 0 1 11 11"/>',
    "camera": '<rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="M8 7l1.5-3h5L16 7"/><circle cx="12" cy="13.5" r="3.5"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>',
    "sync": '<path d="M2 17h4V7h4v10h5V7h3v10h4"/>',
    "speaker": '<path d="M4 9.5h3.5L12 5v14l-4.5-4.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "ttl": '<path d="M2 17h5V7h5v10h3"/><path d="M15 12h7M19 9l3 3-3 3"/>',
    "code": '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13.5 5l-3 14"/>',
    "network": '<circle cx="5" cy="12" r="2.2"/><circle cx="19" cy="6" r="2.2"/><circle cx="19" cy="18" r="2.2"/>'
               '<path d="M7 11l10-4M7 13l10 4"/>',
    "folder": '<path d="M3 7.5V18a1.5 1.5 0 0 0 1.5 1.5h15A1.5 1.5 0 0 0 21 18V9.5A1.5 1.5 0 0 0 19.5 8H12l-2-3H4.5'
              'A1.5 1.5 0 0 0 3 6.5z"/>',
    "eye": '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "chart": '<path d="M4 20V4M4 20h16"/><path d="M8 16v-4M12 16V8M16 16v-6"/>',
    "film": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 5v14M17 5v14M3 9.5h4M3 14.5h4M17 9.5h4'
            'M17 14.5h4"/>',
}


def icon(name: str, cx: float, cy: float, scale: float = 1.0, color: str = "#fff", width: float = 2.0) -> str:
    return (f'<g transform="translate({cx - 12 * scale:.1f},{cy - 12 * scale:.1f}) scale({scale})" fill="none" '
            f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">'
            f'{ICONS[name]}</g>')


def text(x, y, s, size=14, color=INK, weight="normal", anchor="start", family=FONT, extra="") -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}"{extra}>{s}</text>')


def tile(x, y, w, h, badge, name, title, sub) -> str:
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{CARD}"/>'
            f'<circle cx="{x + 38}" cy="{y + h / 2}" r="22" fill="{badge}"/>{icon(name, x + 38, y + h / 2)}'
            + text(x + 72, y + h / 2 - 3, title, 17, weight="600")
            + text(x + 72, y + h / 2 + 17, sub, 14, GREY))


def arrow(d, color, dashed=False, width=2.0) -> str:
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    mid = "arrO" if color == ORANGE else "arrG"
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash} marker-end="url(#{mid})"/>'


def spectrogram_jpeg(sess: Path, t0: float, t1: float, fmax: float, px_w: int, px_h: int) -> str:
    """Base64 JPEG of micA's spectrogram between t0 and t1 (the review window's colours)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import soundfile as sf
    from matplotlib.colors import LinearSegmentedColormap

    from mavric.gui.audio_widgets import SPEC_STOPS

    x, sr = sf.read(sess / "audio_000.flac", always_2d=True)
    sig = x[int(t0 * sr):int(t1 * sr), 0]
    n, hop = 1024, 256
    win = np.hanning(n)
    frames = np.lib.stride_tricks.sliding_window_view(sig, n)[::hop] * win
    spec = 20 * np.log10(np.abs(np.fft.rfft(frames, axis=1)).T + 1e-9)
    spec = spec[: int(fmax / (sr / n)) + 1]
    lo, hi = np.percentile(spec, 50), spec.max()
    cmap = LinearSegmentedColormap.from_list("mavric", SPEC_STOPS)
    fig = plt.figure(figsize=(px_w / 100, px_h / 100), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.imshow(spec, origin="lower", aspect="auto", cmap=cmap, vmin=lo, vmax=hi, interpolation="bilinear")
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="jpg", dpi=100, pil_kwargs={"quality": 85})
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def simulated_session() -> tuple[Path, list[dict], float]:
    from mavric.sim import simulate_session

    tmp = Path(tempfile.mkdtemp(prefix="mavric_overview_"))
    sess = simulate_session(tmp, duration_s=60, seed=4, audio=True, write_labels=False).path
    import pandas as pd

    calls = pd.read_csv(sess / "ground_truth_calls.csv")
    calls = calls.sort_values("t")

    def window(t):
        w = calls[(calls.t >= t) & (calls.t + calls.dur <= t + 6)]
        gaps = (w.t.values[1:] - (w.t + w.dur).values[:-1]) if len(w) > 1 else np.array([1.0])
        return w if gaps.min() > 0.15 else w.iloc[:0]

    # the 6 s window with the most calls that don't overlap
    best = max(np.arange(1.0, 54.0, 0.25), key=lambda t: len(window(t)))
    return sess, window(best).to_dict("records"), float(best)


def live_view(x0, y0) -> str:
    """The acquire live view: four places, subjects, the sync LED."""
    out = [f'<rect x="{x0}" y="{y0}" width="400" height="150" rx="8" fill="#2f2f2e"/>',
           f'<rect x="{x0 + 10}" y="{y0 + 8}" width="380" height="134" rx="4" fill="#474745"/>']
    places = [(x0 + 22, y0 + 30, "Feeder", "S04", YELLOW), (x0 + 258, y0 + 30, "Perch", None, None),
              (x0 + 22, y0 + 92, "Nest box", "S02", ORANGE), (x0 + 258, y0 + 92, "Door", "S01", BLUE)]
    for x, y, name, who, col in places:
        stroke, sw = (col, 2.5) if col else (LIGHT, 1.5)
        out.append(f'<rect x="{x}" y="{y}" width="120" height="44" fill="none" stroke="{stroke}" stroke-width="{sw}"/>')
        out.append(f'<rect x="{x + 10}" y="{y + 30}" width="100" height="9" fill="#7a5233"/>')
        if who:
            out.append(f'<ellipse cx="{x + 60}" cy="{y + 18}" rx="15" ry="10.5" fill="{col}" stroke="{INK}"/>')
        label = f"{name} · {who}" if who else name
        lw = 7.0 * len(label) + 10
        out.append(f'<rect x="{x}" y="{y - 15}" width="{lw:.0f}" height="15" fill="{INK}" opacity="0.85"/>')
        out.append(text(x + 5, y - 4, label, 11, "#fff"))
    out.append(f'<ellipse cx="{x0 + 200}" cy="{y0 + 66}" rx="15" ry="10.5" fill="{GREEN}" stroke="{INK}"/>')
    out.append(f'<ellipse cx="{x0 + 176}" cy="{y0 + 108}" rx="15" ry="10.5" fill="{PINK}" stroke="{INK}"/>')
    lx, ly = x0 + 200, y0 + 24
    out.append(f'<circle cx="{lx}" cy="{ly}" r="8" fill="#ffd25a" opacity="0.35"/>'
               f'<circle cx="{lx}" cy="{ly}" r="3.5" fill="#ffd25a"/>'
               f'<rect x="{lx - 9}" y="{ly - 9}" width="18" height="18" fill="none" stroke="{YELLOW}" stroke-width="2"/>')
    return "".join(out)


def streams(x0, y0) -> str:
    """Four streams on one clock, with the shared sync pulses."""
    xa, xb = x0 + 54, x0 + 400
    rows = {"RFID": y0, "video": y0 + 21, "audio": y0 + 42, "sync": y0 + 63}
    out = [text(x0, y + 4, name, 12.5, GREY) for name, y in rows.items()]
    for a, b, col in ((xa, xa + 74, YELLOW), (xa + 88, xa + 186, ORANGE), (xa + 200, xb, BLUE)):
        out.append(f'<rect x="{a}" y="{rows["RFID"] - 5}" width="{b - a}" height="10" rx="2" fill="{col}"/>')
    pulses = [xa + 52, xa + 158, xa + 286]
    for x in np.arange(xa, xb, 5.0):
        lit = any(p - 4 <= x <= p + 6 for p in pulses)
        out.append(f'<path d="M{x:.1f} {rows["video"] - 6}v12" stroke="{YELLOW if lit else LIGHT}" '
                   f'stroke-width="{2 if lit else 1}"/>')
    rng = np.random.default_rng(3)
    xs = np.arange(xa, xb, 1.5)
    env = 1.2 + 6.5 * (np.exp(-((xs - xa - 118) / 14) ** 2) + np.exp(-((xs - xa - 240) / 9) ** 2)
                       + 0.8 * np.exp(-((xs - xa - 318) / 11) ** 2))
    ys = rows["audio"] + env * np.sin(xs * 1.9) * rng.uniform(0.4, 1.0, xs.size)
    out.append('<path d="M' + " L".join(f"{x:.1f} {y:.1f}" for x, y in zip(xs, ys))
               + f'" fill="none" stroke="{BLUE}" stroke-width="1.1"/>')
    base, top = rows["sync"] + 6, rows["sync"] - 6
    d = f"M{xa} {base}" + "".join(f" H{p - 4} V{top} H{p + 6} V{base}" for p in pulses) + f" H{xb}"
    out.append(f'<path d="{d}" fill="none" stroke="{YELLOW}" stroke-width="2"/>')
    for p in pulses:
        out.append(f'<path d="M{p + 1} {rows["RFID"] - 10}V{base + 4}" stroke="{YELLOW}" stroke-width="1.3" '
                   f'stroke-dasharray="3 3" opacity="0.9"/>')
    return "".join(out)


def review_card(x, y, w, sess, calls, t0, colors) -> str:
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="180" rx="12" fill="{CARD}"/>',
           f'<circle cx="{x + 30}" cy="{y + 30}" r="18" fill="{AFTER}"/>{icon("eye", x + 30, y + 30, 0.8)}',
           text(x + 58, y + 27, "Review", 17, weight="600"), text(x + 58, y + 45, "mavric review", 13, BLUE, "600",
                                                                  family=MONO)]
    px, pw = x + 16, w - 32
    lanes = [[(0.00, 0.34, colors["S03"]), (0.42, 1.00, colors["S05"])],
             [(0.00, 0.58, colors["S04"]), (0.66, 0.88, colors["S01"])]]
    for i, lane in enumerate(lanes):
        ly = y + 58 + 12 * i
        out.append(f'<rect x="{px}" y="{ly}" width="{pw}" height="8" fill="#e9e8e4"/>')
        for a, b, col in lane:
            out.append(f'<rect x="{px + a * pw:.1f}" y="{ly}" width="{(b - a) * pw:.1f}" height="8" fill="{col}"/>')
    sy, sh, fmax = y + 86, 70, 3000.0
    jpg = spectrogram_jpeg(sess, t0, t0 + 6, fmax, int(pw * 1.5), int(sh * 1.5))
    out.append(f'<image x="{px}" y="{sy}" width="{pw}" height="{sh}" preserveAspectRatio="none" '
               f'href="data:image/jpeg;base64,{jpg}"/>')
    for c in calls:
        a = px + (c["t"] - t0) / 6 * pw
        bw = c["dur"] / 6 * pw
        f_hi = min(0.96 * fmax, 4.3 * c["f0"])
        top = sy + sh * (1 - f_hi / fmax)
        col = colors.get(c["pigeon"], GREY)
        out.append(f'<rect x="{a:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{sy + sh - top - 1:.1f}" '
                   f'fill="{col}" fill-opacity="0.12" stroke="{col}" stroke-width="1.6"/>')
    out.append(f'<rect x="{px}" y="{sy}" width="{pw}" height="{sh}" fill="none" stroke="{LIGHT}"/>')
    out.append(text(x + w / 2, y + 172, "blind place labels · sound events", 13, GREY, anchor="middle"))
    return "".join(out)


def analyze_card(x, y, w) -> str:
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="180" rx="12" fill="{CARD}"/>',
           f'<circle cx="{x + 30}" cy="{y + 30}" r="18" fill="{AFTER}"/>{icon("chart", x + 30, y + 30, 0.8)}',
           text(x + 58, y + 27, "Analyze", 17, weight="600"),
           text(x + 58, y + 45, "mavric analyze", 13, BLUE, "600", family=MONO),
           f'<rect x="{x + w - 92}" y="{y + 64}" width="76" height="22" rx="11" fill="#e3f5ec" stroke="{GREEN}"/>',
           text(x + w - 54, y + 79, "sync PASS", 12, "#11784f", "700", "middle"),
           text(x + 16, y + 80, "RFID vs labels", 12.5, GREY)]
    base, hmax = y + 152, 52
    for i, (name, v) in enumerate((("Feeder", 0.97), ("Perch", 0.93), ("Nest", 0.99), ("Door", 0.95))):
        bx = x + 22 + i * 52
        bh = v * hmax
        out.append(f'<rect x="{bx}" y="{base - bh:.1f}" width="30" height="{bh:.1f}" rx="3" fill="{BLUE}"/>')
        out.append(text(bx + 15, base - bh - 4, f"{v:.0%}", 11, INK, anchor="middle"))
        out.append(text(bx + 15, base + 15, name, 11.5, GREY, anchor="middle"))
    out.append(f'<path d="M{x + 14} {base}H{x + w - 14}" stroke="{LIGHT}"/>')
    return "".join(out)


def main() -> None:
    sess, calls, t0 = simulated_session()
    import pandas as pd

    tags = pd.read_csv(sess / "tags.csv")
    colors = dict(zip(tags.subject, tags.color))

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
         "<title>MAVRIC: RFID, video, audio and the sync pulse recorded on one clock; outputs on RFID events; "
         "review, analysis and compression afterwards</title>",
         '<defs>'
         f'<marker id="arrO" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
         f'<path d="M0 0 L10 5 L0 10 z" fill="{ORANGE}"/></marker>'
         f'<marker id="arrG" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
         f'<path d="M0 0 L10 5 L0 10 z" fill="{GREY}"/></marker>'
         '</defs>',
         f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="18" fill="#ffffff" stroke="#e4e3df"/>']

    # sense
    p.append(text(28, 42, "SENSE", 13, SENSE, "700", extra=' letter-spacing="1.5"')
             + text(88, 42, "who is where, what was seen and heard", 13, GREY))
    sense = [("rfid", "RFID readers", "who is at each place"), ("camera", "Cameras", "FLIR, Basler, webcams"),
             ("mic", "Microphones", "multichannel audio interface"), ("sync", "Sync pulse", "into video, audio and Intan")]
    for i, (ic, title, sub) in enumerate(sense):
        y = 58 + i * 84
        p.append(tile(24, y, 310, 74, SENSE, ic, title, sub))
        p.append(arrow(f"M336 {y + 37}H380", GREY))

    # acquire hub
    hx, hy, hw, hh = 384, 58, 432, 326
    p.append(f'<rect x="{hx}" y="{hy}" width="{hw}" height="{hh}" rx="16" fill="#f1f6fd" stroke="{BLUE}" '
             f'stroke-width="2"/>')
    p.append(text(hx + 18, hy + 30, "mavric acquire", 16, BLUE, "700", family=MONO))
    p.append(text(hx + hw - 18, hy + 30, "live view · ROIs · recording", 13, GREY, anchor="end"))
    p.append(live_view(hx + 16, hy + 44))
    p.append(streams(hx + 18, hy + 216))
    p.append(text(hx + 18, hy + hh - 14, "One session clock; shared sync pulses check every stream.", 13, GREY))

    # act
    p.append(text(866, 42, "ACT", 13, ACT, "700", extra=' letter-spacing="1.5"')
             + text(904, 42, "when a subject arrives, stays or leaves", 13, GREY))
    act = [("speaker", "Speakers", "sounds from your library"), ("ttl", "TTL and Intan", "pulses from an I/O Arduino"),
           ("code", "MATLAB, programs", "run code, start programs"), ("network", "Messages, clips", "UDP/TCP, camera clips")]
    for i, (ic, title, sub) in enumerate(act):
        y = 58 + i * 84
        p.append(arrow(f"M818 {y + 37}H862", ORANGE, dashed=True))
        p.append(tile(866, y, 310, 74, ACT, ic, title, sub))

    # afterwards
    by = 434
    p.append(arrow(f"M{hx + 40} {hy + hh + 2}V{by - 22}H150V{by - 2}", GREY))
    p.append(text(170, by - 28, "one folder per session", 13, GREY))
    p.append(f'<rect x="24" y="{by}" width="246" height="180" rx="12" fill="{CARD}"/>'
             f'<circle cx="54" cy="{by + 30}" r="18" fill="{AFTER}"/>{icon("folder", 54, by + 30, 0.8)}'
             + text(82, by + 27, "Session folder", 17, weight="600")
             + text(82, by + 45, "sessions/<date>_<name>", 12.5, GREY, family=MONO))
    for i, f in enumerate(("session.json", "rfid_raw.csv", "cam0_raw_000.mkv", "frames_cam0.csv", "audio_000.flac",
                           "audio_playback.csv", "actions.csv")):
        p.append(text(40, by + 74 + i * 15.5, f, 12.5, INK, family=MONO))
    p.append(arrow(f"M272 {by + 90}H296", GREY))
    p.append(review_card(300, by, 360, sess, calls, t0, colors))
    p.append(arrow(f"M662 {by + 90}H686", GREY))
    p.append(analyze_card(690, by, 240))
    p.append(arrow(f"M932 {by + 90}H956", GREY))
    cx = 960
    p.append(f'<rect x="{cx}" y="{by}" width="216" height="180" rx="12" fill="{CARD}"/>'
             f'<circle cx="{cx + 30}" cy="{by + 30}" r="18" fill="{AFTER}"/>{icon("film", cx + 30, by + 30, 0.8)}'
             + text(cx + 58, by + 27, "Compress", 17, weight="600")
             + text(cx + 58, by + 45, "mavric compress", 13, BLUE, "600", family=MONO)
             + icon("film", cx + 108, by + 98, 2.0, GREEN, 1.4)
             + text(cx + 108, by + 150, "H.264, every frame kept,", 13, GREY, anchor="middle")
             + text(cx + 108, by + 167, "ready for DeepLabCut", 13, GREY, anchor="middle"))
    p.append("</svg>")
    OUT.write_text("\n".join(p) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} kB)")


if __name__ == "__main__":
    main()
