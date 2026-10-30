"""Simulated cage: pigeons visiting 4 RFID perches, a sync LED, RFID and camera streams.

Used for (1) the real-time synthetic camera and simulated Arduino, so the
acquire GUI can be exercised without hardware, and (2) generating complete
offline sessions with known ground truth, so the analysis pipeline can be
verified end to end (sync recovery, bout building, accuracy metrics).
"""

from __future__ import annotations

import bisect
import math
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pandas as pd

from .config import CATEGORICAL, Roi, RoiSet, TagMap, load_rig
from .core import CsvAppender, now
from .rfid.protocol import RAW_COLUMNS, WRAP, MicrosUnwrapper, parse_line
from .session import COVERAGE_FILE, LABELS_FILE, RFID_FILE, Session
from .video.recorder import FRAME_COLUMNS, VideoWriter

IMAGE_SIZE = (640, 480)
PERCH_RECTS = {  # antenna -> (x0, y0, x1, y1) of the perch bar
    1: (60, 110, 200, 126),
    2: (440, 110, 580, 126),
    3: (60, 330, 200, 346),
    4: (440, 330, 580, 346),
}
LED_RECT = (600, 16, 620, 36)
FLOOR = (220, 160, 420, 440)  # free-moving area
PERCH_Z_MM = {1: 320.0, 2: 320.0, 3: 160.0, 4: 160.0}  # 3D scene (SimScene3D): height of each bar's top, mm
LED_Z_MM = 300.0
BIRD_MM = (100.0, 70.0, 45.0)  # 3D scene: body length, width, height of the body's centre above what it stands on
PLAN_MM_PER_PX = 1.25  # 3D scene: the plan picture laid on the floor
PLAN_CENTRE_MM = (156.0, -195.0)  # where the simulated rig's cameras look
SYNC_PULSE_S = 0.080


@dataclass
class Visit:
    pigeon: str
    perch: int
    start: float
    stop: float


@dataclass
class SimWorld:
    pigeons: list[str]
    colors: dict[str, str]
    visits: list[Visit]
    sync_edges: np.ndarray
    duration_s: float
    image_size: tuple[int, int] = IMAGE_SIZE
    seed: int = 0
    t0_abs: float | None = None  # host monotonic time of world t=0 (real-time mode)
    _phase: dict = field(default_factory=dict)
    scene: "SimScene3D | None" = None  # several synthetic cameras: the cage in 3D, seen from each

    # ------------------------------------------------------------------ generation
    @classmethod
    def random(
        cls,
        pigeons: list[str] | None = None,
        colors: dict[str, str] | None = None,
        duration_s: float = 600.0,
        seed: int = 0,
        mean_dwell_s: float = 20.0,
        mean_away_s: float = 15.0,
    ) -> "SimWorld":
        rng = np.random.default_rng(seed)
        pigeons = pigeons or ["S01", "S02", "S03", "S04", "S05"]
        colors = colors or {p: c for p, c in zip(pigeons, CATEGORICAL)}
        busy: dict[int, list[tuple[float, float]]] = {k: [] for k in PERCH_RECTS}
        visits: list[Visit] = []

        def free(perch, a, b):
            return all(b + 0.5 <= s or a >= e + 0.5 for s, e in busy[perch])

        for p in pigeons:
            t = float(rng.exponential(mean_away_s / 2))
            while t < duration_s:
                dwell = float(np.clip(rng.exponential(mean_dwell_s), 1.0, 5 * mean_dwell_s))
                stop = min(t + dwell, duration_s)
                for perch in rng.permutation(list(PERCH_RECTS)):
                    if free(int(perch), t, stop):
                        busy[int(perch)].append((t, stop))
                        visits.append(Visit(p, int(perch), t, stop))
                        break
                t = stop + float(np.clip(rng.exponential(mean_away_s), 1.0, None))
        visits.sort(key=lambda v: v.start)
        # sync pulses: random 2-6 s rising-edge intervals, like the firmware
        edges, t = [], float(rng.uniform(0.5, 2.0))
        while t < duration_s:
            edges.append(t)
            t += float(rng.uniform(2.0, 6.0))
        return cls(pigeons, colors, visits, np.array(edges), duration_s, seed=seed)

    def ensure_started(self) -> float:
        if self.t0_abs is None:
            self.t0_abs = now()
        return self.t0_abs

    # ------------------------------------------------------------------ queries
    def _index(self) -> dict:
        if "_by" not in self._phase:
            by: dict = {}
            for v in self.visits:  # visits of one pigeon never overlap
                by.setdefault(v.pigeon, []).append(v)
            self._phase["_by"] = {p: (vs, [v.start for v in vs]) for p, vs in by.items()}
        return self._phase["_by"]

    def _near_visit(self, pigeon: str, t: float, pad: float = 0.0) -> Visit | None:
        vs, starts = self._index().get(pigeon, ([], []))
        i = bisect.bisect_right(starts, t + pad) - 1
        for j in (i, i + 1):
            if 0 <= j < len(vs) and vs[j].start - pad <= t < vs[j].stop + pad:
                return vs[j]
        return None

    def visit_at(self, pigeon: str, t: float) -> Visit | None:
        return self._near_visit(pigeon, t)

    def occupant(self, perch: int, t: float) -> str | None:
        for p in self.pigeons:
            v = self._near_visit(p, t)
            if v is not None and v.perch == perch:
                return p
        return None

    def led_on(self, t: float) -> bool:
        i = np.searchsorted(self.sync_edges, t, side="right") - 1
        return bool(i >= 0 and t - self.sync_edges[i] < SYNC_PULSE_S)

    def _floor_pos(self, pigeon: str, t: float) -> tuple[float, float]:
        if pigeon not in self._phase:
            r = np.random.default_rng(zlib.crc32(pigeon.encode()) + self.seed)
            self._phase[pigeon] = r.uniform(0, 2 * math.pi, 4), r.uniform(0.05, 0.25, 4)
        ph, fr = self._phase[pigeon]
        x0, y0, x1, y1 = FLOOR
        u = 0.5 + 0.25 * (math.sin(fr[0] * t + ph[0]) + math.sin(fr[1] * t + ph[1]))
        v = 0.5 + 0.25 * (math.sin(fr[2] * t + ph[2]) + math.sin(fr[3] * t + ph[3]))
        return x0 + u * (x1 - x0), y0 + v * (y1 - y0)

    @staticmethod
    def _perch_pos(perch: int, pigeon: str, t: float) -> tuple[float, float]:
        x0, y0, x1, _ = PERCH_RECTS[perch]
        return (x0 + x1) / 2 + 6 * math.sin(0.7 * t + len(pigeon)), y0 - 16

    def height(self, pigeon: str, t: float) -> float:
        """Height (mm) of what the pigeon stands on: its perch, the floor, or in between while flying."""
        fly = 0.4
        v = self._near_visit(pigeon, t, pad=fly)
        if v is None:
            return 0.0
        a = (v.start - t) / fly if t < v.start else (t - v.stop) / fly if t >= v.stop else 0.0
        return (1 - a) * PERCH_Z_MM[v.perch]

    def position(self, pigeon: str, t: float) -> tuple[float, float]:
        fly = 0.4  # s to fly between floor and perch
        v = self._near_visit(pigeon, t, pad=fly)
        if v is not None:
            pp = self._perch_pos(v.perch, pigeon, t)
            if t < v.start:
                a = (v.start - t) / fly
            elif t >= v.stop:
                a = (t - v.stop) / fly
            else:
                return pp
            fp = self._floor_pos(pigeon, t)
            return pp[0] + a * (fp[0] - pp[0]), pp[1] + a * (fp[1] - pp[1])
        return self._floor_pos(pigeon, t)

    # ------------------------------------------------------------------ rendering
    def render(self, t: float, noise: bool = True, cam: str | None = None) -> np.ndarray:
        """A plan of the cage; with a 3D scene attached, the cage seen by camera `cam` from its place."""
        if self.scene is not None and cam is not None and cam in self.scene.cams:
            return self.scene.render(t, cam, noise)
        w, h = self.image_size
        img = np.full((h, w, 3), 70, np.uint8)
        cv2.rectangle(img, (20, 20), (w - 20, h - 20), (95, 95, 95), -1)
        for x0, y0, x1, y1 in PERCH_RECTS.values():
            cv2.rectangle(img, (x0, y0), (x1, y1), (40, 70, 110), -1)
        for p in self.pigeons:
            x, y = self.position(p, t)
            c = self.colors.get(p, "#999999").lstrip("#")
            bgr = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
            cv2.ellipse(img, (int(x), int(y)), (20, 14), 0, 0, 360, bgr, -1)
            cv2.ellipse(img, (int(x), int(y)), (20, 14), 0, 0, 360, (20, 20, 20), 1)
        x0, y0, x1, y1 = LED_RECT
        cv2.rectangle(img, (x0, y0), (x1, y1), (255, 255, 255) if self.led_on(t) else (45, 45, 45), -1)
        if noise:
            rng = np.random.default_rng(int(t * 1000) & 0xFFFFFFFF)
            img = cv2.add(img, rng.integers(0, 6, img.shape, dtype=np.uint8))
        return img

    def attach_rig(self, rig, roi_camera: str) -> "SimScene3D":
        """Give the cage a 3D shape seen by the simulated cameras of `rig` (a SimulatedRig); see SimScene3D."""
        self.scene = SimScene3D(self, rig, roi_camera)
        return self.scene

    def roi_set(self, antennas: dict[int, str]) -> RoiSet:
        if self.scene is not None:
            return self.scene.roi_set(antennas)
        rois = []
        for k, (x0, y0, x1, y1) in PERCH_RECTS.items():
            pts = [(x0 - 10, y0 - 40), (x1 + 10, y0 - 40), (x1 + 10, y1 + 8), (x0 - 10, y1 + 8)]
            rois.append(Roi(antennas.get(k, f"Place{k}"), pts, antenna=k, kind="perch"))
        x0, y0, x1, y1 = LED_RECT
        rois.append(Roi("SYNC", [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], antenna=None, kind="sync"))
        return RoiSet(rois, self.image_size)

    def ground_truth(self, antennas: dict[int, str]) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {"annotator": "ground_truth", "roi": antennas[v.perch], "pigeon": v.pigeon,
                 "start_s": v.start, "stop_s": v.stop, "flag": ""}
                for v in self.visits
            ]
        )




class SimScene3D:
    """The simulated cage in 3D, seen by several synthetic cameras (a SimulatedRig), each from its own place.

    The cage is the plan of the one-camera simulation laid on the floor
    (PLAN_MM_PER_PX, centred where the rig's cameras look), with the perch bars
    at PERCH_Z_MM, the sync LED on a post at LED_Z_MM and the pigeons standing on
    the floor or on a bar. Its default ROIs are the places' surfaces as the ROI
    camera sees them; placed at the true heights, they sit on the perches in
    every camera.
    """

    def __init__(self, world: SimWorld, rig, roi_camera: str):
        from .video.places import CameraModel

        self.world, self.rig, self.roi_camera = world, rig, roi_camera
        self.cams = {cid: CameraModel(cid, lens.K, np.asarray(lens.dist, float).ravel(), rig.T[cid], tuple(lens.size))
                     for cid, lens in rig.lenses.items()}
        w, h = world.image_size
        self.floor = self.plan([(20, 20), (w - 20, 20), (w - 20, h - 20), (20, h - 20)], 0.0)
        self.perches = {k: self.plan([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], PERCH_Z_MM[k])
                        for k, (x0, y0, x1, y1) in PERCH_RECTS.items()}
        x0, y0, x1, y1 = LED_RECT
        self.led = self.plan([((x0 + x1) / 2, (y0 + y1) / 2)], LED_Z_MM)[0]

    def plan(self, uv, z: float) -> np.ndarray:
        """World points (mm) of plan points (pixels of the one-camera picture) at height z."""
        w, h = self.world.image_size
        uv = np.asarray(uv, float).reshape(-1, 2)
        return np.column_stack([PLAN_CENTRE_MM[0] + (uv[:, 0] - w / 2) * PLAN_MM_PER_PX,
                                PLAN_CENTRE_MM[1] + (h / 2 - uv[:, 1]) * PLAN_MM_PER_PX, np.full(len(uv), z)])

    @staticmethod
    def truth() -> dict:
        """The true heights (mm) of the places' surfaces by antenna, and of the sync LED ("SYNC")."""
        return {**PERCH_Z_MM, "SYNC": LED_Z_MM}

    def devices(self) -> dict:
        """Where the simulated microphones and speakers really are: micA and micB above the cage's long midline at a
        quarter and three quarters of its length, each facing a different side; a speaker beyond each corner,
        aimed at the middle. Mostly outside the cameras' views."""
        from .audio.geometry import Device, aim_at

        (x0, y0), (x1, y1) = self.floor.min(axis=0)[:2], self.floor.max(axis=0)[:2]
        mid = np.array([(x0 + x1) / 2, (y0 + y1) / 2, 200.0])
        out = {}
        for name, (x, y) in zip(("spk1", "spk2", "spk3", "spk4"),
                                ((x0 - 120, y1 + 120), (x1 + 120, y1 + 120), (x0 - 120, y0 - 120), (x1 + 120, y0 - 120))):
            p = np.array([x, y, 300.0])
            out[name] = Device(name, "speaker", p, aim_at(p, mid))
        for name, u, az in (("micA", 0.25, -90.0), ("micB", 0.75, 90.0)):
            out[name] = Device(name, "mic", np.array([x0 + u * (x1 - x0), mid[1], 650.0]), (az, -35.0))
        return out

    def surface(self, perch: int) -> np.ndarray:
        """A place's surface: its bar with a margin, at the bar's height."""
        x0, y0, x1, y1 = PERCH_RECTS[perch]
        return self.plan([(x0 - 10, y0 - 14), (x1 + 10, y0 - 14), (x1 + 10, y1 + 14), (x0 - 10, y1 + 14)],
                         PERCH_Z_MM[perch])

    def roi_set(self, antennas: dict[int, str]) -> RoiSet:
        """The places' surfaces and the LED's box as the ROI camera sees them (not yet placed in 3D)."""
        cam = self.cams[self.roi_camera]
        rois = []
        for k in PERCH_RECTS:
            p, _ = cam.project(self.surface(k))
            rois.append(Roi(antennas.get(k, f"Place{k}"), [(round(float(x), 1), round(float(y), 1)) for x, y in p],
                            antenna=k, kind="perch"))
        p, _ = cam.project([self.led])
        r = 2.5 * float(cam.K[0, 0]) * 12 / float(cam.to_camera(self.led)[0, 2])
        x, y = float(p[0, 0]), float(p[0, 1])
        rois.append(Roi("SYNC", [(x - r, y - r), (x + r, y - r), (x + r, y + r), (x - r, y + r)], kind="sync"))
        return RoiSet(rois, cam.size)

    def bird(self, pigeon: str, t: float) -> np.ndarray:
        """The pigeon's body centre in the world (mm)."""
        x, y = self.world.position(pigeon, t)
        v = self.world.visit_at(pigeon, t)
        if v is not None:  # sitting on the bar
            y = (PERCH_RECTS[v.perch][1] + PERCH_RECTS[v.perch][3]) / 2
        return self.plan([(x, y)], self.world.height(pigeon, t) + BIRD_MM[2])[0]

    @staticmethod
    def _px(cam, X) -> tuple[np.ndarray, np.ndarray]:
        Xc = cam.to_camera(X)
        front = Xc[:, 2] > 10.0
        p, _ = cv2.projectPoints(np.where(front[:, None], Xc, 1.0).reshape(-1, 1, 3), np.zeros(3), np.zeros(3), cam.K,
                                 cam.dist)
        return p.reshape(-1, 2), front

    def _poly(self, img, cam, X, color, edge=None, step: int = 10) -> None:
        P = np.asarray(X, float)
        dense = np.array([P[i] + u * (P[(i + 1) % len(P)] - P[i]) for i in range(len(P))
                          for u in np.linspace(0, 1, step, endpoint=False)])
        p, ok = self._px(cam, dense)
        if ok.sum() < 3:
            return
        pts = np.round(p[ok] * 16).astype(np.int32)
        cv2.fillPoly(img, [pts], color, cv2.LINE_AA, shift=4)
        if edge is not None:
            cv2.polylines(img, [pts], True, edge, 1, cv2.LINE_AA, shift=4)

    def _line(self, img, cam, X, color, width: int = 1) -> None:
        p, ok = self._px(cam, X)
        if ok.all():
            cv2.polylines(img, [np.round(p).astype(np.int32)], False, color, width, cv2.LINE_AA)

    def render(self, t: float, cid: str, noise: bool = True) -> np.ndarray:
        cam = self.cams[cid]
        w, h = cam.size
        img = np.full((h, w, 3), 70, np.uint8)
        self._poly(img, cam, self.floor, (95, 95, 95), step=20)
        (x0, y0), (x1, y1) = self.floor.min(axis=0)[:2], self.floor.max(axis=0)[:2]
        for gx in np.arange(np.ceil(x0 / 100) * 100, x1, 100):  # a floor grid, for depth
            self._line(img, cam, [(gx, y, 0.0) for y in np.linspace(y0, y1, 30)], (86, 86, 86))
        for gy in np.arange(np.ceil(y0 / 100) * 100, y1, 100):
            self._line(img, cam, [(x, gy, 0.0) for x in np.linspace(x0, x1, 30)], (86, 86, 86))
        items = [(top.mean(axis=0), ("perch", top)) for top in self.perches.values()]
        items += [(X, ("bird", pg, X)) for pg in self.world.pigeons for X in [self.bird(pg, t)]]
        items.append((self.led, ("led",)))
        f = float(cam.K[0, 0])
        for depth, it in sorted(((float(cam.to_camera(X)[0, 2]), it) for X, it in items), key=lambda d: -d[0]):
            if depth <= 10:
                continue
            if it[0] == "perch":
                top = it[1]
                bottom = top - np.array([0.0, 0.0, 22.0])
                for end in ((bottom[0] + bottom[3]) / 2, (bottom[1] + bottom[2]) / 2):  # posts down to the floor
                    self._line(img, cam, [end, (end[0], end[1], 0.0)], (40, 52, 66), max(1, int(round(f * 10 / depth))))
                for a, b in ((0, 1), (1, 2), (2, 3), (3, 0)):  # the bar's sides, then its top
                    self._poly(img, cam, [top[a], top[b], bottom[b], bottom[a]], (30, 52, 82))
                self._poly(img, cam, top, (40, 70, 110), edge=(25, 40, 60))
            elif it[0] == "bird":
                _, pg, X = it
                p, ok = self._px(cam, X)
                if not ok[0]:
                    continue
                c = self.world.colors.get(pg, "#999999").lstrip("#")
                bgr = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
                ax = (max(2, int(f * BIRD_MM[0] / 2 / depth)), max(2, int(f * BIRD_MM[1] / 2 / depth)))
                ctr = (int(round(p[0, 0])), int(round(p[0, 1])))
                cv2.ellipse(img, ctr, ax, 0, 0, 360, bgr, -1, cv2.LINE_AA)
                cv2.ellipse(img, ctr, ax, 0, 0, 360, (20, 20, 20), 1, cv2.LINE_AA)
            else:
                self._line(img, cam, [self.led - (0, 0, 25.0), (self.led[0], self.led[1], 0.0)], (40, 40, 40),
                           max(1, int(round(f * 8 / depth))))
                p, ok = self._px(cam, self.led)
                if not ok[0]:
                    continue
                r = max(2, int(round(f * 12 / depth)))
                c = np.round(p[0]).astype(int)
                cv2.rectangle(img, (int(c[0] - 2 * r), int(c[1] - 2 * r)), (int(c[0] + 2 * r), int(c[1] + 2 * r)),
                              (30, 30, 30), -1)
                cv2.rectangle(img, (int(c[0] - r), int(c[1] - r)), (int(c[0] + r), int(c[1] + r)),
                              (255, 255, 255) if self.world.led_on(t) else (45, 45, 45), -1)
        if noise:
            rng = np.random.default_rng((int(t * 1000) + zlib.crc32(cid.encode())) & 0xFFFFFFFF)
            img = cv2.add(img, rng.integers(0, 6, img.shape, dtype=np.uint8))
        return img


# --------------------------------------------------------------------------- RFID model


@dataclass
class RfidModel:
    drift_ppm: float = 145.0  # Arduino resonator error
    boot_offset_s: float = -2.0  # Arduino booted this long before session t=0
    start_micros: int = 0  # micros() at boot (set near 2**32 to exercise a wrap)
    onset_latency_s: tuple[float, float] = (0.10, 0.50)  # landing -> first read
    offset_latency_s: tuple[float, float] = (0.0, 0.10)  # leaving -> last read
    p_dropout: float = 0.15  # chance a visit has a read gap
    dropout_s: tuple[float, float] = (0.5, 3.0)
    p_missed_visit: float = 0.03  # visit never read (tag position)
    p_crosstalk: float = 0.04  # visit also read briefly at a neighbouring perch
    p_corrupt: float = 2e-4  # per tag line, checksum corruption
    latency_ms: tuple[float, float] = (2.0, 3.0)  # serial latency: base + exponential mean
    round_period_s: float = 0.125


def simulate_rfid(world: SimWorld, tags: TagMap, model: RfidModel | None = None, seed: int = 1):
    """Return (lines, truth): lines = list of (host_t, line_bytes) sorted by host_t."""
    m = model or RfidModel()
    rng = np.random.default_rng(seed)
    pig2tag = {p: t for t, p in tags.ids.items()}
    k_drift = 1 + m.drift_ppm * 1e-6
    neighbours = {1: 2, 2: 1, 3: 4, 4: 3}

    # per-visit read window, dropouts, crosstalk
    windows: list[tuple[int, str, float, float]] = []  # (perch, tag, start, stop)
    for v in world.visits:
        if rng.random() < m.p_missed_visit:
            continue
        a = v.start + rng.uniform(*m.onset_latency_s)
        b = v.stop + rng.uniform(*m.offset_latency_s)
        if a >= b:
            continue
        tag = pig2tag[v.pigeon]
        if rng.random() < m.p_dropout and b - a > 4:
            g0 = rng.uniform(a + 1, b - 1)
            g1 = min(b, g0 + rng.uniform(*m.dropout_s))
            windows += [(v.perch, tag, a, g0), (v.perch, tag, g1, b)]
        else:
            windows.append((v.perch, tag, a, b))
        if rng.random() < m.p_crosstalk:
            c0 = rng.uniform(a, max(a, b - 1))
            windows.append((neighbours[v.perch], tag, c0, c0 + rng.uniform(0.3, 1.0)))

    def micros_at(t_true: float) -> int:
        return int(round(m.start_micros + (t_true - m.boot_offset_s) * 1e6 * k_drift)) % WRAP

    def lat() -> float:
        return (m.latency_ms[0] + rng.exponential(m.latency_ms[1])) / 1000

    lines: list[tuple[float, bytes]] = []
    t_boot = m.boot_offset_s
    lines.append((t_boot + lat(), f"{micros_at(t_boot)},BOOT,0\r\n".encode()))
    period_true = m.round_period_s / k_drift
    t, last_host = t_boot + period_true, -1e9
    windows.sort(key=lambda w: w[2])
    wi, active = 0, []
    while t < world.duration_s:
        us = micros_at(t)
        while wi < len(windows) and windows[wi][2] <= t:
            active.append(windows[wi])
            wi += 1
        active = [w for w in active if w[3] > t]
        present = {}
        for perch, tag, _a, _b in active:
            if perch not in present:  # one tag per reader (no anti-collision)
                present[perch] = tag
        host = max(t + lat(), last_host + 1e-5)
        if present:
            for perch in sorted(present):
                tag = present[perch]
                if rng.random() < m.p_corrupt:
                    tag = tag[:-1] + ("0" if tag[-1] != "0" else "1")
                lines.append((host, f"{us},{tag},{perch}\r\n".encode()))
                host += 0.0003
        else:
            lines.append((host, f"{us},NONE,0\r\n".encode()))
        last_host = host
        t += period_true
    for e in world.sync_edges:
        lines.append((e + lat(), f"{micros_at(e)},SYNC,0\r\n".encode()))
    lines.sort(key=lambda x: x[0])
    truth = {"drift_ppm": m.drift_ppm, "windows": windows}
    return lines, truth


class SimulatedArduino:
    """Real-time LineSource replaying simulate_rfid() against the world clock."""

    def __init__(self, world: SimWorld, tags: TagMap, model: RfidModel | None = None):
        self.world, self.tags, self.model = world, tags, model or RfidModel(boot_offset_s=0.0)
        self.port = "SIM"

    def open(self) -> None:
        self.lines, _ = simulate_rfid(self.world, self.tags, self.model)
        self.lines = [x for x in self.lines if x[0] >= 0]
        self.t0 = self.world.ensure_started()
        self.i = 0

    def readline(self) -> bytes | None:
        if self.i >= len(self.lines):
            time.sleep(0.2)
            return None
        t_rel, line = self.lines[self.i]
        delay = self.t0 + t_rel - now()
        if delay > 0.01:
            time.sleep(0.01)  # short idle timeouts, like the real serial source
            return None
        if delay > 0:
            time.sleep(delay)
        self.i += 1
        return line

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- offline session


def simulate_session(
    out_dir: str | Path,
    duration_s: float = 300.0,
    fps: float = 30.0,
    seed: int = 0,
    name: str = "sim",
    rfid_model: RfidModel | None = None,
    cam_drift_ppm: float = -20.0,
    label_noise_s: float = 0.0,
    label_coverage_s: float | None = None,
    write_labels: bool = True,
    rig_path: str | Path | None = None,
    audio: bool = False,
    audio_model=None,
    audio_sync: bool = True,
    playback_every_s: float = 0.0,
    sound_labels: bool = False,
) -> Session:
    """Write a complete synthetic session (video, frame sidecar, RFID log, labels; optionally audio).

    With audio, `playback_every_s` plays a stimulus about that often (logged in
    audio_playback.csv), and `sound_labels` writes the true calls as reviewed
    sound events, as if an annotator had labeled the whole session.
    """
    rng = np.random.default_rng(seed + 100)
    rig = load_rig(rig_path)
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "device": "", "fps": fps}]
    tags = TagMap.load(rig["paths"]["tags_file"])
    if not tags.ids:
        tags = TagMap({"0A000000010B": "S01", "0A0000000208": "S02"}, {})
    world = SimWorld.random(tags.pigeons, {p: tags.color(p) for p in tags.pigeons}, duration_s, seed)
    antennas = rig["antennas"]
    rois = world.roi_set(antennas)
    anchor = {"perf_s": 0.0, "utc_ns": time.time_ns()}
    sess = Session.create(out_dir, name, rig, tags, rois, anchor, notes=f"simulated, seed={seed}")

    # RFID
    lines, truth = simulate_rfid(world, tags, rfid_model, seed=seed + 1)
    unwrap = MicrosUnwrapper()
    w = CsvAppender(sess.file(RFID_FILE), RAW_COLUMNS, flush_s=60)
    for host_t, raw in lines:
        if host_t < 0:
            continue  # before recording started
        ln = parse_line(raw)
        us, seg = unwrap(ln.micros, boot=ln.kind == "BOOT")
        w.write([f"{host_t:.6f}", seg, ln.micros, us, ln.kind, ln.tag, ln.reader,
                 "" if ln.checksum_ok is None else int(ln.checksum_ok), ln.raw])
    w.close()

    # Video + frame sidecar (camera clock drifts against host; host stamps jitter)
    k_cam = 1 + cam_drift_ppm * 1e-6
    vw = VideoWriter(sess.file("cam0.mp4"), fps, IMAGE_SIZE, rig["video"])
    fw = CsvAppender(sess.file("frames_cam0.csv"), FRAME_COLUMNS, flush_s=60)
    x0, y0, x1, y1 = LED_RECT
    n = int(duration_s * fps)
    idx = 0
    for k in range(n):
        t_exp = k / fps / k_cam  # true time of exposure (camera clock runs k_cam fast)
        if rng.random() < 0.002:
            continue  # camera-side drop: counter gap
        img = world.render(t_exp)
        host_t = t_exp + 0.004 + rng.exponential(0.003) + (0.03 if rng.random() < 0.01 else 0)
        vw.write(img)
        led = float(img[y0:y1, x0:x1].mean())
        fw.write([idx, idx, f"{host_t:.6f}", int(round(k / fps * 1e9)), k, int(world.led_on(t_exp)), f"{led:.2f}"])
        idx += 1
    vw.close()
    fw.close()
    sess.add_camera({"id": "cam0", "backend": "synthetic", "fps": fps, "width": IMAGE_SIZE[0],
                     "height": IMAGE_SIZE[1], "hw_tick_hz": 1e9, "video_file": "cam0.mp4",
                     "frames_file": "frames_cam0.csv", "codec": vw.codec})

    # Ground truth and (noisy) manual labels
    gt = world.ground_truth(antennas)
    gt.to_csv(sess.file("ground_truth.csv"), index=False)
    if write_labels:
        cov_end = min(duration_s, label_coverage_s or duration_s)
        lab = gt[gt["start_s"] < cov_end].copy()
        lab["stop_s"] = lab["stop_s"].clip(upper=cov_end)
        if label_noise_s > 0:
            lab["start_s"] += rng.normal(0, label_noise_s, len(lab))
            lab["stop_s"] += rng.normal(0, label_noise_s, len(lab))
        lab["annotator"] = "sim"
        lab.to_csv(sess.file(LABELS_FILE), index=False)
        pd.DataFrame([{"annotator": "sim", "start_s": 0.0, "stop_s": cov_end}]).to_csv(
            sess.file(COVERAGE_FILE), index=False)
    audio_truth = None
    if audio:
        from .audio.config import AudioConfig
        from .audio.sim import write_sim_audio

        acfg = AudioConfig.from_rig(rig)
        acfg.host_api = "sim"
        acfg.mic_near = {"micA": [1, 3], "micB": [2, 4]}  # the simulated microphone placement
        if not audio_sync:
            acfg.sync_channel = ""
        model = audio_model
        if playback_every_s > 0:
            from dataclasses import replace

            from .audio.sim import SimAudioModel

            model = replace(audio_model or SimAudioModel(), playback_every_s=playback_every_s)
        audio_truth = write_sim_audio(sess, world, acfg, duration_s, model, seed=seed)
        if sound_labels:
            from .audio.events import SoundEvent, save_events, save_sound_coverage

            calls = pd.read_csv(sess.file("ground_truth_calls.csv"))
            save_events(sess, [SoundEvent(float(c.t), float(c.t + c.dur), "call", str(c.pigeon), annotator="sim")
                               for c in calls.itertuples()])
            save_sound_coverage(sess, [(0.0, duration_s)], "sim")
    sess.meta["sim"] = {"seed": seed, "rfid_drift_ppm": truth["drift_ppm"], "cam_drift_ppm": cam_drift_ppm,
                        "n_visits": len(world.visits), "n_sync": len(world.sync_edges), "audio": audio_truth}
    sess.finalize(duration_s, {"frames": idx, "rfid_lines": len(lines)})
    return sess

