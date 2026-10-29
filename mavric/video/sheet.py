"""Printable ArUco stickers that turn a plain checkerboard into the ChArUco board of `calibration.board`.

    page 1      what to do, and a map: which sticker goes into which white square
    page 2...   the stickers at exact size, each with its id and an arrow to the top of the board

Everything is drawn in millimetres as vector shapes, so the print is exact at
100 % ("actual size"); a 50 mm bar on every page checks it. Stickers are cut
on the dashed line, with a white margin around the marker that matches the
white square around it.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from .calibration import BoardSpec  # noqa: E402

PAPER_MM = {"a4": (210.0, 297.0), "letter": (215.9, 279.4)}
INK = "#0b0b0b"
MUTED = "#6b6a66"
CUT = "#9a9993"
EPS = 0.02  # mm: black cells overlap this much, so no hairline shows between them


def square_name(col: int, row: int) -> str:
    """Column letter and row number, as on the map: (0, 0) -> A1."""
    return f"{chr(65 + col)}{row + 1}"


def sticker_layout(spec: BoardSpec) -> list[tuple[int, int, int]]:
    """(marker id, column, row) of every sticker, from OpenCV's board."""
    return sorted((i, c, r) for i, (c, r) in spec.marker_square.items())


def cut_size_mm(spec: BoardSpec) -> float:
    """The sticker: the marker plus a white margin, but at least 1 mm smaller than the square."""
    return min(spec.square_mm - 2.0, spec.marker_mm + 8.0)


def _page(paper: str):
    w, h = PAPER_MM[paper]
    fig = plt.figure(figsize=(w / 25.4, h / 25.4))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)  # y down, in mm, like the board
    ax.axis("off")
    return fig, ax, w, h


def _marker(ax, spec: BoardSpec, marker_id: int, x0: float, y0: float, side: float) -> None:
    """The marker's black cells, merged into runs per row, top-left corner at (x0, y0)."""
    cells = spec.marker_image(marker_id)
    n = len(cells)
    s = side / n
    for r in range(n):
        c = 0
        while c < n:
            if cells[r, c]:
                c1 = c
                while c1 < n and cells[r, c1]:
                    c1 += 1
                ax.add_patch(Rectangle((x0 + c * s - EPS, y0 + r * s - EPS), (c1 - c) * s + 2 * EPS, s + 2 * EPS,
                                       facecolor=INK, edgecolor="none", lw=0))
                c = c1
            else:
                c += 1


def _scale_bar(ax, x: float, y: float) -> None:
    ax.add_patch(Rectangle((x, y), 50, 2.2, facecolor=INK, edgecolor="none"))
    for k in range(6):
        ax.plot([x + 10 * k, x + 10 * k], [y - 1.2, y + 3.4], color=INK, lw=0.6)
    ax.text(x + 53, y + 1.6, "50 mm: measure it. If it is not exactly 50 mm, print again at 100 % (actual size).",
            fontsize=7.5, va="center", color=INK)


def _footer(ax, spec: BoardSpec, w: float, h: float, page: int, n_pages: int) -> None:
    ax.text(w / 2, h - 7, f"MAVRIC ChArUco stickers  ·  {spec.describe()}  ·  page {page}/{n_pages}", fontsize=6.5,
            ha="center", color=MUTED)


def make_sheet(spec: BoardSpec, out: str | Path, paper: str = "a4") -> Path:
    """Write the sticker sheet PDF; returns its path."""
    if spec.problems():
        raise ValueError("; ".join(spec.problems()))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    layout = sticker_layout(spec)
    cut = cut_size_mm(spec)
    pw, ph = PAPER_MM[paper]
    margin = 12.0
    pitch_x, pitch_y = cut + 6.0, cut + 9.0
    per_row = int((pw - 2 * margin + 6.0) // pitch_x)
    rows_per_page = int((ph - 30.0 - 14.0 + 9.0) // pitch_y)
    per_page = per_row * rows_per_page
    n_pages = 1 + -(-len(layout) // per_page)
    with PdfPages(out) as pdf:
        # ---------------------------------------------------------------- page 1: instructions and map
        fig, ax, w, h = _page(paper)
        ax.text(margin, 18, f"ArUco stickers for a {spec.squares_x} × {spec.squares_y} checkerboard", fontsize=15,
                fontweight="bold", color=INK)
        ax.text(margin, 25, f"{spec.square_mm:g} mm squares  ·  {spec.marker_mm:g} mm markers  ·  {spec.dictionary}"
                f"  ·  {len(layout)} stickers", fontsize=9, color=MUTED)
        steps = [
            f"1. Print pages 2-{n_pages} at 100 % (\"actual size\", not \"fit to page\"). Check the 50 mm bar.",
            f"2. Hold the board with {spec.squares_x} squares across, {spec.squares_y} down, and a BLACK square at the "
            "top left.",
            "    Mark that corner on the board's frame (\"top left\"): it fixes the board's orientation from now on.",
            f"3. Cut a sticker on its dashed line ({cut:g} mm) and stick it centred in its white square on the map,",
            "    with its ▲ arrow pointing to the top of the board. Then the next one: one sticker at a time.",
            "4. Measure the squares: 10 squares with calipers or a steel ruler, divided by 10. Write it in",
            "    config/rig.yaml under calibration: board: square_mm, and check squares and marker_mm there too.",
            "5. Check: hold the board in front of a camera (Acquire -> Calibrate cameras..., or "
            "mavric calibrate check).",
            f"    All {len(layout)} markers should be found and none shown in red (a red marker is in the wrong square "
            "or turned).",
        ]
        for k, s in enumerate(steps):
            ax.text(margin, 36 + 5.2 * k, s, fontsize=8.6, color=INK)
        s_map = min((w - 2 * margin - 20) / spec.squares_x, (h - 140 - 20) / spec.squares_y)
        bw, bh = s_map * spec.squares_x, s_map * spec.squares_y
        x0, y0 = (w - bw) / 2, 92 + (h - 92 - 22 - bh) / 2 + 4
        ids = {(c, r): i for i, c, r in layout}
        for r in range(spec.squares_y):
            for c in range(spec.squares_x):
                x, y = x0 + c * s_map, y0 + r * s_map
                if (c, r) in ids:
                    ax.add_patch(Rectangle((x, y), s_map, s_map, facecolor="white", edgecolor=CUT, lw=0.4))
                    ax.text(x + s_map / 2, y + s_map / 2 + 0.4, str(ids[(c, r)]), fontsize=10, fontweight="bold",
                            ha="center", va="center", color=INK)
                else:
                    ax.add_patch(Rectangle((x, y), s_map, s_map, facecolor="#2a2a2a", edgecolor="none"))
        for c in range(spec.squares_x):
            ax.text(x0 + (c + 0.5) * s_map, y0 - 2.5, chr(65 + c), fontsize=8, ha="center", color=MUTED)
        for r in range(spec.squares_y):
            ax.text(x0 - 2.5, y0 + (r + 0.5) * s_map, str(r + 1), fontsize=8, ha="right", va="center", color=MUTED)
        ax.text(x0 + bw / 2, y0 - 6, "▲  top of the board  ▲", fontsize=8, ha="center", color=INK)
        ax.annotate("top left: black", xy=(x0 + 1, y0 + 1), xytext=(x0 - 22, y0 - 8), fontsize=7.5, color=INK,
                    arrowprops={"arrowstyle": "->", "lw": 0.6, "color": INK})
        ax.text(margin, h - 14, "The map: numbers are sticker ids; dark squares get no sticker.", fontsize=7.5,
                color=MUTED)
        _footer(ax, spec, w, h, 1, n_pages)
        pdf.savefig(fig)
        plt.close(fig)

        # ---------------------------------------------------------------- stickers at exact size
        off = (cut - spec.marker_mm) / 2
        for p in range(n_pages - 1):
            fig, ax, w, h = _page(paper)
            _scale_bar(ax, margin, 12)
            chunk = layout[p * per_page:(p + 1) * per_page]
            gx = (w - (per_row * pitch_x - 6.0)) / 2
            for k, (i, c, r) in enumerate(chunk):
                x = gx + (k % per_row) * pitch_x
                y = 30.0 + (k // per_row) * pitch_y
                ax.text(x + cut / 2, y - 1.4, f"▲  {i}  ·  square {square_name(c, r)}  ▲", fontsize=7.5,
                        ha="center", color=INK)
                ax.add_patch(Rectangle((x, y), cut, cut, facecolor="none", edgecolor=CUT, lw=0.5,
                                       linestyle=(0, (3, 2))))
                _marker(ax, spec, i, x + off, y + off, spec.marker_mm)
            _footer(ax, spec, w, h, p + 2, n_pages)
            pdf.savefig(fig)
            plt.close(fig)
    return out


def default_sheet_path(spec: BoardSpec, folder: str | Path) -> Path:
    return Path(folder) / (f"charuco_stickers_{spec.squares_x}x{spec.squares_y}_{spec.square_mm:g}mm_"
                           f"{spec.marker_mm:g}mm.pdf")
