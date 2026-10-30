"""Microphone and speaker positions in the world frame of the camera calibration (mm, floor origin, z up).

The devices need not be seen by any camera: they are placed by number or by
dragging them in the top and side views of the space, and checked by sound
(mavric.audio.ranging). Saved in calibration.yaml under `devices:`, next to the
cameras, so a session's copy of the calibration holds them too.

Directions: azimuth in the floor plane from +x towards +y, elevation up from
the horizontal, in degrees. The microphones are cardioids (Rode NT5): their
pickup falls off away from their axis, 0.5 (1 + cos θ).

A call from a place reaches the two microphones at slightly different times
and levels (its fingerprint, `place_fingerprints`); `place_fit` grades how
well a sound heard in a session fits each place, for the emitter suggestions
in review. `speaker_levels` gives each speaker's level at each place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

KINDS = ("mic", "speaker")
CARDIOID_FLOOR_DB = -25.0  # rear rejection of a real cardioid (an ideal one would be -inf)
HEAD_MM = 60.0  # a sound from a place: this far above its surface (a bird's head)
SD_DT_MS = 0.1  # a measured arrival-time difference: its spread on top of the place's own (a few samples)
SD_DL_DB = 3.0  # a measured level difference: its spread (echoes, the direction the bird faces)


def speed_of_sound(temp_c: float = 20.0) -> float:
    """Speed of sound in air, mm per ms (= m/s)."""
    return 331.3 * float(np.sqrt(1.0 + temp_c / 273.15))


def direction(az_deg: float, el_deg: float) -> np.ndarray:
    a, e = np.radians(az_deg), np.radians(el_deg)
    return np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])


def aim_at(position, target) -> tuple[float, float]:
    """(azimuth, elevation) in degrees of the direction from `position` to `target`."""
    d = np.asarray(target, float) - np.asarray(position, float)
    az = float(np.degrees(np.arctan2(d[1], d[0])))
    el = float(np.degrees(np.arctan2(d[2], np.hypot(d[0], d[1]))))
    return round(az, 1), round(el, 1)


@dataclass
class Device:
    name: str
    kind: str  # "mic" | "speaker"
    position: np.ndarray  # world, mm
    aim: tuple[float, float] = (0.0, 0.0)  # azimuth, elevation (degrees)
    how: str = "typed"  # typed | sound (fitted to the speaker-to-microphone sounds)
    sd_mm: float | None = None  # after a sound fit: its uncertainty
    # measured by sound: a microphone's recording gain relative to the first microphone, a speaker's output relative
    # to the first speaker (dB)
    level_db: float | None = None

    @property
    def axis(self) -> np.ndarray:
        return direction(*self.aim)

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "position_mm": [round(float(v), 1) for v in self.position],
             "aim_deg": [round(float(self.aim[0]), 1), round(float(self.aim[1]), 1)], "placed": self.how}
        if self.sd_mm is not None:
            d["sd_mm"] = round(float(self.sd_mm), 1)
        if self.level_db is not None:
            d["level_db"] = round(float(self.level_db), 1)
        return d

    @classmethod
    def from_dict(cls, name: str, d: dict) -> Device:
        aim = d.get("aim_deg") or [0.0, 0.0]
        sd, lv = d.get("sd_mm"), d.get("level_db")
        return cls(name, str(d.get("kind", "mic")), np.array(d["position_mm"], float), (float(aim[0]), float(aim[1])),
                   str(d.get("placed", "typed")), None if sd is None else float(sd), None if lv is None else float(lv))


def cardioid_db(mic: Device, source) -> float:
    """Pickup of a cardioid microphone for a source at `source`, relative to on-axis (dB)."""
    d = np.asarray(source, float) - mic.position
    n = np.linalg.norm(d)
    if n < 1e-6:
        return 0.0
    g = 0.5 * (1.0 + float(mic.axis @ d) / n)
    return max(20 * np.log10(max(g, 1e-6)), CARDIOID_FLOOR_DB)


def arrival(mic: Device, source, c_mm_per_ms: float = 343.0) -> tuple[float, float]:
    """(delay ms, level dB re 1 m on axis) of a sound from `source` at the microphone."""
    d = float(np.linalg.norm(np.asarray(source, float) - mic.position))
    return d / c_mm_per_ms, -20 * np.log10(max(d, 1.0) / 1000.0) + cardioid_db(mic, source)


def fingerprints(mics: list[Device], sources: dict[str, np.ndarray], c_mm_per_ms: float = 343.0) -> dict:
    """How the two microphones hear a sound from each source (e.g. each place): {name: (dt ms, dL dB)}, second
    microphone minus first: later and quieter on the second one is positive dt, negative dL. A measured difference
    in the microphones' recording gains (level_db) is included."""
    a, b = mics[:2]
    gain = (b.level_db or 0.0) - (a.level_db or 0.0)
    out = {}
    for name, x in sources.items():
        ta, la = arrival(a, x, c_mm_per_ms)
        tb, lb = arrival(b, x, c_mm_per_ms)
        out[name] = (tb - ta, lb - la + gain)
    return out


def place_fingerprints(mics: list[Device], rs, c_mm_per_ms: float = 343.0) -> dict:
    """{place: (dt ms, dL dB, sd dt, sd dL)} for a call from each place placed in 3D: from the middle of the place,
    and how much it varies over the place's outline (a bird at one end of a long perch)."""
    out = {}
    for r in rs.perches:
        if not r.points3d:
            continue
        P = np.asarray(r.points3d, float) + np.array([0.0, 0.0, HEAD_MM])
        pts = {"mid": P.mean(axis=0), **{f"c{i}": p for i, p in enumerate(P)}}
        fp = np.array(list(fingerprints(mics, pts, c_mm_per_ms).values()))
        spread = (fp.max(axis=0) - fp.min(axis=0)) / 4  # about the sd of a point spread over the outline
        out[r.name] = (float(fp[0, 0]), float(fp[0, 1]), float(spread[0]), float(spread[1]))
    return out


def place_fit(fp: dict, dl_db: float | None = None, dt_ms=(), sd_dt: float = SD_DT_MS,
              sd_dl: float = SD_DL_DB) -> dict[str, float]:
    """How well a sound fits each place of `place_fingerprints`, 0 to 1 (1: exactly as expected), from its level
    difference and arrival-time difference between the two microphones (second minus first). `dt_ms` lists the
    candidate time differences: a tonal call correlates again one pitch period away, so the one nearest to each
    place's counts. Without either measurement every place fits."""
    dts = [float(v) for v in (dt_ms or [])]
    out = {}
    for name, (pdt, pdl, sdt, sdl) in fp.items():
        chi2 = 0.0
        if dl_db is not None:
            chi2 += (dl_db - pdl) ** 2 / (sd_dl ** 2 + sdl ** 2)
        if dts:
            chi2 += min((v - pdt) ** 2 for v in dts) / (sd_dt ** 2 + sdt ** 2)
        out[name] = float(np.exp(-0.5 * chi2))
    return out


def speaker_levels(speakers: list[Device], sources: dict[str, np.ndarray]) -> dict[str, dict[str, tuple]]:
    """{speaker: {place: (distance mm, off-axis degrees, level dB)}}: the level at each place relative to 1 m in
    front of the first speaker, from the distance (and each speaker's measured output, level_db). The speakers'
    own directivity and the room's echoes are not included: the off-axis angle shows where the first matters."""
    out = {}
    for s in speakers:
        row = {}
        for name, x in sources.items():
            d = np.asarray(x, float) - s.position
            n = float(np.linalg.norm(d))
            ang = float(np.degrees(np.arccos(np.clip(s.axis @ d / max(n, 1e-6), -1.0, 1.0))))
            row[name] = (n, ang, -20 * np.log10(max(n, 1.0) / 1000.0) + (s.level_db or 0.0))
        out[s.name] = row
    return out


def confusable(fp: dict, dt_ms: float = 0.25, dl_db: float = 3.0) -> list[tuple[str, str]]:
    """Pairs of sources the two microphones can hardly tell apart (both differences within the tolerances)."""
    names = list(fp)
    out = []
    for i, p in enumerate(names):
        for q in names[i + 1:]:
            if abs(fp[p][0] - fp[q][0]) < dt_ms and abs(fp[p][1] - fp[q][1]) < dl_db:
                out.append((p, q))
    return out


def place_sources(rs) -> dict[str, np.ndarray]:
    """A sound source for each place placed in 3D: a bird's head above the middle of its surface."""
    out = {}
    for r in rs.perches:
        if r.points3d:
            out[r.name] = np.asarray(r.points3d, float).mean(axis=0) + np.array([0.0, 0.0, HEAD_MM])
    return out


def suggested_layout(sources: dict[str, np.ndarray], mics: list[str], speakers: list[str],
                     margin_mm: float = 200.0) -> dict[str, Device]:
    """A starting layout around the places: speakers at the corners, aimed at the middle; two microphones above the
    long midline at a quarter and three quarters of its length, each facing a different side. Drag or type the real
    positions afterwards."""
    P = np.array(list(sources.values())) if sources else np.zeros((1, 3))
    lo, hi = P.min(axis=0) - [margin_mm, margin_mm, 0], P.max(axis=0) + [margin_mm, margin_mm, 0]
    mid = (lo + hi) / 2
    z_spk = float(P[:, 2].mean())
    z_mic = float(P[:, 2].max() + 250.0)
    out: dict[str, Device] = {}
    corners = [(lo[0], hi[1]), (hi[0], hi[1]), (lo[0], lo[1]), (hi[0], lo[1])]
    for k, name in enumerate(speakers):
        x, y = corners[k % 4]
        pos = np.array([x, y, z_spk])
        out[name] = Device(name, "speaker", pos, aim_at(pos, mid))
    long_x = (hi[0] - lo[0]) >= (hi[1] - lo[1])
    for k, name in enumerate(mics):
        u = (0.25, 0.75)[k % 2]
        side = -1 if k % 2 == 0 else 1
        if long_x:
            pos = np.array([lo[0] + u * (hi[0] - lo[0]), mid[1], z_mic])
            aim = (90.0 * side, -35.0)
        else:
            pos = np.array([mid[0], lo[1] + u * (hi[1] - lo[1]), z_mic])
            aim = (90.0 + 90.0 * side, -35.0)
        out[name] = Device(name, "mic", pos, aim)
    return out


# --------------------------------------------------------------------------- files


def session_places(sess, mics: list[str]) -> tuple[dict, float, str]:
    """For a session's first two microphones: the fingerprints of its places and the largest possible arrival-time
    difference (their distance / c, ms), from the session's copies of calibration.yaml (the devices) and rois.json
    (the places placed in 3D); or ({}, 0, why not)."""
    from ..session import CALIBRATION_FILE

    if len(mics) < 2:
        return {}, 0.0, "one microphone"
    devs, _ = load_devices(sess.file(CALIBRATION_FILE))
    missing = [m for m in mics[:2] if m not in devs]
    if missing:
        return {}, 0.0, f"no position for {', '.join(missing)} in the session's calibration"
    temp = float(((sess.meta.get("rig") or {}).get("audio") or {}).get("temperature_c", 20.0))
    c = speed_of_sound(temp)
    a, b = devs[mics[0]], devs[mics[1]]
    fp = place_fingerprints([a, b], sess.rois, c)
    if not fp:
        return {}, 0.0, "the session's places are not placed in 3D"
    return fp, float(np.linalg.norm(b.position - a.position)) / c, ""


def load_devices(path: str | Path) -> tuple[dict[str, Device], dict]:
    """The devices in calibration.yaml and their `devices_meta:` (world date, last sound check)."""
    p = Path(path)
    if not p.exists():
        return {}, {}
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    devs = {str(k): Device.from_dict(str(k), v) for k, v in (d.get("devices") or {}).items() if v}
    return devs, dict(d.get("devices_meta") or {})


def save_devices(path: str | Path, devices: dict[str, Device], meta: dict | None = None) -> Path:
    """Write the devices into calibration.yaml (everything else in it is kept)."""
    from ..video.calibration import HEADER

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}
    world = d.get("world") or {}
    d["devices"] = {n: dev.as_dict() for n, dev in devices.items()}
    d["devices_meta"] = {"date": dt.datetime.now().isoformat(timespec="seconds"), "world_date": world.get("date"),
                         "note": "positions in the world of the camera calibration (mm); aim: azimuth from +x towards "
                                 "+y, elevation up from the horizontal (degrees)", **(meta or {})}
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(HEADER)
        yaml.safe_dump(d, f, sort_keys=False, default_flow_style=None, allow_unicode=True, width=120)
    tmp.replace(p)
    return p
