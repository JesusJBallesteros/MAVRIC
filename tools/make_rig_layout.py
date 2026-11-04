"""Draw the reference rig in its room: the cage, the cameras, microphones, speakers and perches, and the PC cart.

    python tools/make_rig_layout.py            # writes docs/images/rig_layout.svg

A simple 3D scene (metres; x along the filmed wall, y into the cage, z up) seen in perspective, drawn back to
front. Positions are illustrative, not measured.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images" / "rig_layout.svg"

W, H = 1200, 820
FONT = "Segoe UI, Helvetica Neue, Helvetica, Arial, sans-serif"
INK, GREY, LIGHT = "#1d1d1b", "#5f5e5a", "#b9b8b4"
BLUE, ORANGE, GREEN, PURPLE, PINK, RED = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e87ba4", "#e34948"
WOOD, BODY = "#a0784c", "#3d3c39"

EYE = np.array([4.6, -6.4, 3.4])
TARGET = np.array([0.7, 1.3, 0.85])
ROOM = (-2.0, 2.9, -0.7, 3.0, 2.8)  # x0, x1, y0, y1, height
PERCH_X = (0.4, 0.8, 1.2, 1.6)  # along the filmed wall (y = 2), 0.4 m apart
PERCH_Z, PERCH_LEN = 1.05, 0.28
MIC_Z = PERCH_Z + 0.2  # head height of a bird on a perch
MICS = {"micA": 0.1, "micB": 1.9}  # x, at the ends of the row of perches, on the same line
CAMS = {"cam0": (0.65, 0.1, 1.8), "cam1": (1.35, 0.1, 1.8)}
VIEWS = {"cam0": (0.0, 1.45), "cam1": (0.55, 2.0)}  # x range each camera covers on the filmed wall (z 0.25-1.9)


class View:
    """Perspective from EYE towards TARGET, scaled to fit the canvas."""

    def __init__(self, extent: np.ndarray):
        f = TARGET - EYE
        self.f = f / np.linalg.norm(f)
        r = np.cross(self.f, [0, 0, 1.0])
        self.r = r / np.linalg.norm(r)
        self.u = np.cross(self.r, self.f)
        self.s, self.o = 1.0, np.zeros(2)
        p = self.raw(extent)
        lo, hi = p.min(0), p.max(0)
        self.s = min((W - 60) / (hi[0] - lo[0]), (H - 110) / (hi[1] - lo[1]))
        self.o = np.array([30, 70]) - lo * self.s

    def raw(self, pts) -> np.ndarray:
        d = np.atleast_2d(np.asarray(pts, float)) - EYE
        z = d @ self.f
        return np.c_[d @ self.r / z, -(d @ self.u) / z]

    def __call__(self, pts) -> np.ndarray:
        return self.raw(pts) * self.s + self.o


def pts(xy) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)


def poly(v, corners, fill="none", stroke="none", width=1.0, opacity=1.0, extra="") -> str:
    return (f'<polygon points="{pts(v(corners))}" fill="{fill}" stroke="{stroke}" stroke-width="{width}" '
            f'fill-opacity="{opacity}" stroke-linejoin="round"{extra}/>')


def line(v, a, b, color=GREY, width=1.0, opacity=1.0, dash="", cap="round") -> str:
    (x1, y1), (x2, y2) = v([a, b])
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{width}" '
            f'stroke-opacity="{opacity}" stroke-linecap="{cap}"{d}/>')


def text(x, y, s, size=13, color=INK, weight="normal", anchor="start") -> str:
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}">{s}</text>')


def label(v, at, tx, ty, lines, color=INK, anchor="start") -> str:
    """Leader lines from the 3D points `at` to a short text block at the canvas point `tx`, `ty`."""
    out = []
    for p in np.atleast_2d(at):
        (x, y), = v([p])
        out += [f'<path d="M{x:.1f} {y:.1f} L{tx:.1f} {ty:.1f}" stroke="{GREY}" stroke-width="1" fill="none"/>',
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{GREY}"/>']
    tx += -4 if anchor == "end" else 4
    for i, s in enumerate(lines):
        out.append(text(tx, ty + 4 + i * 16, s, 13 if i == 0 else 12, color if i == 0 else GREY,
                        "600" if i == 0 else "normal", anchor))
    return "\n".join(out)


def box(v, lo, hi, color, edge="#1d1d1b") -> str:
    """A shaded box; only the faces turned towards the eye."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    faces = [((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),  # y0
             ((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)),  # y1
             ((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)),  # x0
             ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),  # x1
             ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))]  # top
    normals = [(0, -1, 0), (0, 1, 0), (-1, 0, 0), (1, 0, 0), (0, 0, 1)]
    shade = [0.0, 0.0, -0.18, -0.18, 0.22]
    c = np.array([(lo[i] + hi[i]) / 2 for i in range(3)])
    out = []
    for face, n, k in zip(faces, normals, shade):
        if np.dot(n, EYE - c) > 0:
            out.append(poly(v, face, tint(color, k), edge, 0.6))
    return "\n".join(out)


def tint(color: str, k: float) -> str:
    rgb = np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], float)
    rgb = rgb + (255 - rgb) * k if k > 0 else rgb * (1 + k)
    return "#" + "".join(f"{int(round(c)):02x}" for c in np.clip(rgb, 0, 255))


def mesh(v, origin, ua, ub, la, lb, step, color, width, opacity) -> str:
    """Wire mesh on the rectangle origin + a*ua + b*ub, 0 <= a <= la, 0 <= b <= lb."""
    o, ua, ub = (np.asarray(x, float) for x in (origin, ua, ub))
    out = [line(v, o + a * ua, o + a * ua + lb * ub, color, width, opacity, cap="butt")
           for a in np.arange(step, la - step / 2, step)]
    out += [line(v, o + b * ub, o + b * ub + la * ua, color, width, opacity, cap="butt")
            for b in np.arange(step, lb - step / 2, step)]
    return "\n".join(out)


def main() -> None:
    rx0, rx1, ry0, ry1, rh = ROOM
    room = np.array([(x, y, z) for x in (rx0, rx1) for y in (ry0, ry1) for z in (0.0, rh)])
    v = View(room)
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
         "<title>The reference rig: a 2 × 2 × 2 m cage in a room with the PC cart beside it; two cameras film the "
         "wire-mesh wall with four perches; a microphone at each end of the row of perches; a speaker in each top "
         "corner</title>",
         f'<rect width="{W}" height="{H}" fill="#fcfcfb"/>']

    # the room: floor, back and right walls
    s.append(poly(v, [(rx0, ry0, 0), (rx1, ry0, 0), (rx1, ry1, 0), (rx0, ry1, 0)], "#efede8"))
    s.append(poly(v, [(rx0, ry1, 0), (rx1, ry1, 0), (rx1, ry1, rh), (rx0, ry1, rh)], "#f6f5f1", LIGHT, 1))
    s.append(poly(v, [(rx0, ry0, 0), (rx0, ry1, 0), (rx0, ry1, rh), (rx0, ry0, rh)], "#f1f0eb", LIGHT, 1))

    # the cage: floor, then its far walls (the filmed wall at y = 2, the side at x = 2)
    s.append(poly(v, [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)], "#e4e2dc", GREY, 1))
    s.append(poly(v, [(0, 2, 0), (2, 2, 0), (2, 2, 2), (0, 2, 2)], "#e9e7e1", "none"))
    s.append(mesh(v, (0, 2, 0), (1, 0, 0), (0, 0, 1), 2, 2, 0.1, "#8f8d87", 0.7, 0.9))
    s.append(mesh(v, (0, 0, 0), (0, 1, 0), (0, 0, 1), 2, 2, 0.2, "#8f8d87", 0.6, 0.45))

    # on the filmed wall: what the cameras cover, the sync LED, the perches with their readers, the microphones
    for cid, (xa, xb) in VIEWS.items():
        s.append(poly(v, [(xa, 1.995, 0.25), (xb, 1.995, 0.25), (xb, 1.995, 1.9), (xa, 1.995, 1.9)], GREEN,
                      GREEN, 1.2, 0.07, ' stroke-dasharray="6 4"'))
    led = (1.0, 1.98, 1.72)
    s.append(f'<circle cx="{v([led])[0, 0]:.1f}" cy="{v([led])[0, 1]:.1f}" r="5" fill="{RED}" stroke="#7a1f1e"/>')
    for x in PERCH_X:
        s.append(box(v, (x - 0.05, 1.86, PERCH_Z - 0.07), (x + 0.05, 1.98, PERCH_Z - 0.02), BLUE, "#1b4f8f"))
        s.append(line(v, (x, 2.0, PERCH_Z), (x, 2.0 - PERCH_LEN, PERCH_Z), "#5c4329", 9.5, cap="round"))
        s.append(line(v, (x, 2.0, PERCH_Z), (x, 2.0 - PERCH_LEN, PERCH_Z), WOOD, 7, cap="round"))
    for name, x in MICS.items():
        inward = 1 if x < 1 else -1
        s.append(line(v, (x, 2.0, MIC_Z), (x, 1.86, MIC_Z), GREY, 2))
        s.append(line(v, (x, 1.86, MIC_Z), (x + inward * 0.14, 1.86, MIC_Z), BODY, 7, cap="butt"))
        s.append(line(v, (x + inward * 0.14, 1.86, MIC_Z), (x + inward * 0.16, 1.86, MIC_Z), "#8f8d87", 7,
                      cap="butt"))

    # inside, back to front: the speakers in the top corners, the cameras' views and the cameras
    spk = {"spk1": (0.03, 0.03), "spk2": (1.83, 0.03), "spk3": (0.03, 1.83), "spk4": (1.83, 1.83)}
    items = []
    for n, (x, y) in spk.items():
        items.append(((x + 0.07, y + 0.07, 1.86), box(v, (x, y, 1.76), (x + 0.14, y + 0.14, 1.96), BODY)))
    for cid, (cx, cy, cz) in CAMS.items():
        xa, xb = VIEWS[cid]
        rays = [line(v, (cx, cy + 0.06, cz), c, GREEN, 1, 0.6, "5 4")
                for c in ((xa, 2, 0.25), (xb, 2, 0.25), (xb, 2, 1.9), (xa, 2, 1.9))]
        items.append(((cx, 1.0, 1.0), "\n".join(rays)))
        body = box(v, (cx - 0.04, cy - 0.06, cz - 0.04), (cx + 0.04, cy + 0.06, cz + 0.04), BODY)
        lens = line(v, (cx, cy + 0.06, cz), (cx, cy + 0.1, cz - 0.015), GREEN, 6, cap="butt")
        items.append(((cx, cy, cz), body + "\n" + lens))
    for _, svg in sorted(items, key=lambda it: -np.linalg.norm(np.asarray(it[0]) - EYE)):
        s.append(svg)

    # the near walls (x = 2, the front and the roof) as light mesh, and the frame
    s.append(mesh(v, (2, 0, 0), (0, 1, 0), (0, 0, 1), 2, 2, 0.2, "#8f8d87", 0.6, 0.22))
    s.append(mesh(v, (0, 0, 0), (1, 0, 0), (0, 0, 1), 2, 2, 0.2, "#8f8d87", 0.6, 0.16))
    s.append(mesh(v, (0, 0, 2), (1, 0, 0), (0, 1, 0), 2, 2, 0.2, "#8f8d87", 0.6, 0.16))
    for a, b in [((0, 2, 0), (0, 2, 2)), ((2, 2, 0), (2, 2, 2)), ((0, 2, 2), (2, 2, 2)), ((0, 0, 2), (0, 2, 2)),
                 ((2, 0, 2), (2, 2, 2)), ((0, 0, 0), (0, 0, 2)), ((2, 0, 0), (2, 0, 2)), ((0, 0, 2), (2, 0, 2))]:
        s.append(line(v, a, b, GREY, 3))

    # the PC cart beside the cage, and the cables to it
    cx0, cx1, cy0, cy1 = -1.6, -0.8, 0.3, 1.0
    s.append(f'<path d="M{pts(v([(-0.85, 0.85, 0.02), (-0.4, 1.05, 0.02), (0.0, 1.1, 0.02)]))}" '
             f'stroke="#898781" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    for x, y in ((cx0, cy1), (cx1, cy1), (cx0, cy0), (cx1, cy0)):
        s.append(line(v, (x, y, 0.06), (x, y, 0.9), "#6b6a66", 3))
        (px, py), = v([(x, y, 0.03)])
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{BODY}"/>')
    for z in (0.22, 0.9):
        s.append(box(v, (cx0, cy0, z - 0.03), (cx1, cy1, z), "#c9c7c0", "#6b6a66"))
    s.append(box(v, (-1.5, 0.4, 0.22), (-1.28, 0.9, 0.66), BODY))  # PC
    s.append(box(v, (-1.15, 0.75, 0.22), (-0.97, 0.9, 0.28), BLUE, "#1b4f8f"))  # Arduino MEGA
    s.append(box(v, (-1.15, 0.4, 0.22), (-0.87, 0.58, 0.29), "#52514e"))  # MOTU M4
    s.append(box(v, (-1.45, 0.82, 0.9), (-0.95, 0.86, 1.25), BODY))  # monitor
    s.append(box(v, (-1.4, 0.38, 0.9), (-1.0, 0.53, 0.92), "#6b6a66"))  # keyboard

    # dimensions
    def dim(a, b, off, txt, dx=0, dy=0):
        a, b, off = (np.asarray(x, float) for x in (a, b, off))
        out = [line(v, a + off, b + off, GREY, 1), line(v, a, a + off * 1.3, GREY, 0.8),
               line(v, b, b + off * 1.3, GREY, 0.8)]
        (mx, my), = v([(a + b) / 2 + off])
        out.append(text(mx + dx, my + dy, txt, 12, GREY, anchor="middle"))
        return "\n".join(out)

    s.append(dim((0, 0, 0), (2, 0, 0), (0, -0.25, 0), "2 m", dy=16))
    s.append(dim((2, 0, 0), (2, 2, 0), (0.25, 0, 0), "2 m", dx=18, dy=8))
    s.append(dim((2, 2, 0), (2, 2, 2), (0.2, 0, 0), "2 m", dx=18))

    # labels
    s.append(label(v, (1.6, 1.75, PERCH_Z), 960, 560, ["4 perches on the filmed wall",
                                                        "0.4 m apart; an RFID reader under each"]))
    s.append(label(v, [(0.24, 1.86, MIC_Z), (1.76, 1.86, MIC_Z)], 960, 342,
                   ["micA and micB", "at the two ends of the row of perches,",
                    "on one line: each perch reaches them",
                    "with its own time difference"], PURPLE))
    s.append(label(v, (1.9, 1.9, 1.96), 960, 200, ["speakers in the 4 top corners", "spk1 … spk4"], PINK))
    s.append(label(v, [CAMS["cam0"], CAMS["cam1"]], 400, 210, ["cam0 and cam1", "film the perch wall together",
                                                                 "(D46 frame trigger)"], GREEN, "end"))
    s.append(label(v, led, 790, 140, ["sync LED", "in view of both cameras"], RED))
    s.append(label(v, (-1.2, 0.84, 1.25), 250, 320, ["PC cart", "PC · Arduino MEGA · MOTU M4"], INK, "end"))
    s.append(label(v, (-0.4, 1.05, 0.02), 420, 690, ["cables to the cage", "USB · readers · mics · speakers"],
                   INK, "end"))

    s.append(text(30, 38, "The reference rig in its room", 17, INK, "600"))
    s.append(text(30, 58, "A 2 × 2 × 2 m wire-mesh cage; the wall with the perches faces the cameras "
                          "(dashed green: what each camera sees).", 13, GREY))
    s.append("</svg>")
    OUT.write_text("\n".join(s) + "\n", encoding="utf-8")
    print("wrote", OUT.relative_to(ROOT))


if __name__ == "__main__":
    main()
