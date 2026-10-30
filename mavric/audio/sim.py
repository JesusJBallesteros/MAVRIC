"""Simulated audio for the simulated cage: two microphones, the sync pulse, pigeon calls, played stimuli.

micA sits by perches 1 and 3, micB by perches 2 and 4: a call is louder and
earlier on the near microphone. The sync channel carries the RFID Arduino's
pulses as an AC-coupled line input records them. The audio interface's
sample clock drifts against the world clock like a real crystal.

Offline sessions can also play stimuli on the speakers (odd speakers stand on
micA's side); perched pigeons answer some of them with a call.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..core import now
from .config import AudioConfig

NEAR = {"micA": (1, 3), "micB": (2, 4)}
HIDDEN_LATENCY = 29  # samples: speaker -> microphone delay the simulated driver does not report (3D cage only)


@dataclass
class SimAudioModel:
    drift_ppm: float = 25.0  # interface clock vs world clock
    latency_s: float = 0.006  # minimum delay from block end to delivery
    jitter_s: float = 0.002  # extra delivery delay, exponential mean (offline sessions)
    sync_amp: float = 0.13  # sync pulse step (0.45 V into a line input)
    ac_tau_s: float = 0.02  # AC coupling of the line input
    edge_w_samples: float = 0.8  # edge smoothing (converter anti-alias filter)
    noise_dbfs: float = -66.0
    call_rate_hz: float = 0.2  # calls per second per perched pigeon
    call_dbfs: float = -20.0  # call peak on the near microphone
    far_gain: float = 0.35
    far_delay_s: float = 0.0012
    playback_every_s: float = 0.0  # offline sessions: a stimulus about this often (0 = none)
    stim_dur_s: float = 0.4
    stim_dbfs: float = -24.0  # stimulus peak on the near microphone
    air_s: float = 0.003  # speaker -> near microphone
    response_p: float = 0.6  # chance that a perched pigeon answers a stimulus
    response_min_s: float = 0.6  # answer latency: minimum + exponential
    response_tau_s: float = 1.2


@dataclass
class Call:
    t: float
    dur: float
    f0: float
    pigeon: str
    perch: int
    response: bool = False  # an answer to a played stimulus


@dataclass
class Stimulus:
    t: float  # world time the sound leaves the speaker
    dur: float
    speaker: int  # output number (1-based)
    sound: str = "sim_call"


def stimuli(duration_s: float, model: SimAudioModel | None = None, n_speakers: int = 4, seed: int = 0,
            start_s: float = 0.0) -> list[Stimulus]:
    """Stimuli about every `playback_every_s` on a random speaker (none when it is 0)."""
    m = model or SimAudioModel()
    if m.playback_every_s <= 0 or n_speakers < 1:
        return []
    rng = np.random.default_rng(seed + 4321)
    out = []
    t = start_s + 5.0 + rng.uniform(0, m.playback_every_s)
    while t < duration_s - 5.0:
        out.append(Stimulus(float(t), m.stim_dur_s, int(rng.integers(1, n_speakers + 1))))
        t += m.playback_every_s * rng.uniform(0.6, 1.4)
    return out


def call_events(world, model: SimAudioModel | None = None, seed: int = 7,
                stim: list[Stimulus] | None = None) -> list[Call]:
    """Calls of perched pigeons (a Poisson process per visit), plus answers to stimuli, sorted by time."""
    m = model or SimAudioModel()
    rng = np.random.default_rng(world.seed + seed)
    calls = []
    for v in world.visits:
        t = v.start + rng.exponential(1 / m.call_rate_hz)
        while t < v.stop - 0.3:
            dur = float(rng.uniform(0.3, 0.9))
            calls.append(Call(float(t), min(dur, v.stop - t), float(rng.uniform(450, 700)), v.pigeon, v.perch))
            t += dur + rng.exponential(1 / m.call_rate_hz)
    rng = np.random.default_rng(world.seed + seed + 1)  # answers: own stream, so the calls above stay the same
    for s in stim or []:
        for v in world.visits:
            if v.start <= s.t < v.stop and rng.random() < m.response_p:
                t = s.t + m.response_min_s + rng.exponential(m.response_tau_s)
                if t < v.stop - 0.3:
                    dur = float(rng.uniform(0.3, 0.9))
                    calls.append(Call(float(t), min(dur, v.stop - t), float(rng.uniform(450, 700)), v.pigeon,
                                      v.perch, response=True))
    calls.sort(key=lambda c: c.t)
    return calls


class AudioSynth:
    """Renders the input channels at arbitrary world times."""

    def __init__(self, world, cfg: AudioConfig, model: SimAudioModel | None = None, seed: int = 0,
                 stim: list[Stimulus] | None = None):
        self.world = world
        self.cfg = cfg
        self.m = model or SimAudioModel()
        self.stim = list(stim or [])
        self.calls = call_events(world, self.m, stim=self.stim)
        self._starts = [c.t for c in self.calls]
        self.rng = np.random.default_rng(seed + 1234)
        self.noise = 10 ** (self.m.noise_dbfs / 20)
        self.amp = 10 ** (self.m.call_dbfs / 20)

    def render(self, t: np.ndarray) -> np.ndarray:
        out = np.zeros((len(t), len(self.cfg.names)), np.float32)
        for j, name in enumerate(self.cfg.names):
            if j == self.cfg.sync_index:
                out[:, j] = self._sync(t)
            else:
                out[:, j] = self._mic(t, name)
        return out

    def _sync(self, t: np.ndarray) -> np.ndarray:
        e = self.world.sync_edges
        if not len(e):
            return np.zeros(len(t), np.float32)
        w = self.m.edge_w_samples / self.cfg.sample_rate
        i = np.searchsorted(e, t + 6 * w) - 1  # the last pulse (earlier ones have decayed)
        rise = e[np.clip(i, 0, None)]

        def step(dt):  # smoothed step, then the AC coupling's exponential sag
            return 0.5 * (1 + np.tanh(dt / w)) * np.exp(-np.clip(dt, 0, None) / self.m.ac_tau_s)

        y = step(t - rise) - step(t - rise - self.cfg.pulse_s)
        y[i < 0] = 0
        return (self.m.sync_amp * y + self.rng.normal(0, 1e-5, len(t))).astype(np.float32)

    def _mic(self, t: np.ndarray, name: str) -> np.ndarray:
        y = self.rng.normal(0, self.noise, len(t))
        near = NEAR.get(name, ())
        t0, t1 = float(t[0]), float(t[-1])
        i = bisect.bisect_left(self._starts, t0 - 1.0)
        while i < len(self.calls) and self.calls[i].t <= t1:
            c = self.calls[i]
            i += 1
            far = c.perch not in near
            delay = self.m.far_delay_s if far else 0.0
            u = (t - c.t - delay) / c.dur
            m = (u >= 0) & (u < 1)
            if not m.any():
                continue
            tt = t[m] - c.t - delay
            env = np.sin(np.pi * u[m]) ** 2 * (0.7 + 0.3 * np.sin(2 * np.pi * 6 * tt))
            tone = (np.sin(2 * np.pi * c.f0 * tt) + 0.5 * np.sin(4 * np.pi * c.f0 * tt)
                    + 0.25 * np.sin(6 * np.pi * c.f0 * tt)) / 1.75
            y[m] += self.amp * (self.m.far_gain if far else 1.0) * env * tone
        side = 1 if name == "micA" else 0  # odd speaker numbers stand on micA's side
        for s in self.stim:
            if s.t > t1 or s.t + s.dur + 0.01 < t0:
                continue
            far = s.speaker % 2 != side
            tt = t - s.t - self.m.air_s - (self.m.far_delay_s if far else 0.0)
            m = (tt >= 0) & (tt < s.dur)
            if not m.any():
                continue
            u = tt[m] / s.dur
            ph = 2 * np.pi * (900.0 * tt[m] + 250.0 * tt[m] ** 2 / s.dur)  # sweep 900 -> 1400 Hz, unlike the coos
            amp = 10 ** (self.m.stim_dbfs / 20) * (self.m.far_gain if far else 1.0)
            y[m] += amp * np.sin(np.pi * u) ** 2 * (np.sin(ph) + 0.3 * np.sin(2 * ph)) / 1.3
        return y.astype(np.float32)


class SimAudioBackend:
    """Real-time stand-in for the audio interface, paced by the simulated world's clock."""

    def __init__(self, cfg: AudioConfig, world, model: SimAudioModel | None = None):
        if world is None:
            from ..sim import SimWorld

            world = SimWorld.random(duration_s=3600, seed=0)
        self.cfg = cfg
        self.world = world
        self.m = model or SimAudioModel()
        self.synth = AudioSynth(world, cfg, self.m)
        self._stop = threading.Event()
        self._th: threading.Thread | None = None

    def open(self, handler) -> None:
        self.handler = handler
        self.t_abs0 = self.world.ensure_started()
        self.t_start = now() - self.t_abs0  # world time of sample 0
        self._th = threading.Thread(target=self._run, daemon=True, name="audio-sim")
        self._th.start()

    def paths(self) -> dict | None:
        """With a 3D cage whose devices are placed (SimScene3D.devices): every speaker -> microphone path as
        (delay samples, gain), the direct sound and its floor reflection, by distance and the microphone's cardioid
        pickup. The delay includes HIDDEN_LATENCY, which the driver does not report (like converter latency)."""
        scene = getattr(self.world, "scene", None)
        if scene is None or not hasattr(scene, "devices"):
            return None
        from .geometry import cardioid_db

        devs = scene.devices()
        c, sr = 343.0, self.cfg.sample_rate  # mm per ms
        out = {}
        for o in range(self.cfg.n_out_open):
            spk = devs.get(self.cfg.speakers.get(o + 1, ""))
            for j in self.cfg.mic_indices:
                mic = devs.get(self.cfg.names[j])
                if spk is None or mic is None:
                    return None
                image = spk.position * np.array([1.0, 1.0, -1.0])  # the speaker mirrored in the floor
                out[(o, j)] = [(2 * self.cfg.block + HIDDEN_LATENCY + np.linalg.norm(p - mic.position) / c * sr / 1e3,
                                g * 0.3 * 1000.0 / np.linalg.norm(p - mic.position) * 10 ** (cardioid_db(mic, p) / 20))
                               for p, g in ((spk.position, 1.0), (image, 0.45))]
        return out

    def _run(self) -> None:
        c = self.cfg
        rate = c.sample_rate * (1 + self.m.drift_ppm * 1e-6)  # samples per world second
        # speakers -> microphones: output buffered one block, input one block, plus 3 ms of air (+1.2 ms far side);
        # in a 3D cage with placed devices, by distance instead (paths)
        n_out = c.n_out_open
        hist = np.zeros((4 * c.block + 1024, max(n_out, 1)), np.float32)
        delay = 2 * c.block + int(0.003 * c.sample_rate)
        far = int(self.m.far_delay_s * c.sample_rate)
        paths = self.paths() if n_out else None
        k = 0
        while not self._stop.is_set():
            n = c.block
            t_end = self.t_start + (k + n) / rate
            due = self.t_abs0 + t_end + self.m.latency_s
            while not self._stop.is_set():
                d = due - now()
                if d <= 0:
                    break
                time.sleep(min(d, 0.02))
            if self._stop.is_set():
                return
            t = self.t_start + (k + np.arange(n)) / rate
            x = self.synth.render(t)
            if paths is not None:
                for (o, j), taps in paths.items():
                    for d, g in taps:  # fractional delay: linear interpolation
                        i0 = np.floor(k + np.arange(n) - d)
                        u = (k + np.arange(n) - d - i0).astype(np.float32)
                        i0 = i0.astype(np.int64)
                        x[:, j] += g * ((1 - u) * hist[i0 % len(hist), o] + u * hist[(i0 + 1) % len(hist), o])
            elif n_out:
                for j in c.mic_indices:
                    side = 1 if c.names[j] == "micA" else 0  # odd speaker numbers stand on micA's side
                    for o in range(n_out):
                        near = (o + 1) % 2 == side
                        idx = (k + np.arange(n) - delay - (0 if near else far)) % len(hist)
                        x[:, j] += (0.3 if near else 0.1) * hist[idx, o]
            out = np.zeros((n, n_out), np.float32) if n_out else None
            self.handler(x, out, n, now(), 0.0, "")
            if n_out:
                hist[(k + np.arange(n)) % len(hist)] = out
            k += n

    def info(self) -> dict:
        return {"device": "simulated", "host_api": "sim", "latency_s": [self.m.latency_s, self.m.latency_s],
                "sample_rate": float(self.cfg.sample_rate)}

    def round_trip_samples(self) -> int:
        """Output -> input delay of the simulated interface (one block out, one block in; no air)."""
        return 2 * self.cfg.block if self.cfg.n_out_open else 0

    def close(self) -> None:
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=2)


def write_sim_audio(sess, world, cfg: AudioConfig, duration_s: float, model: SimAudioModel | None = None,
                    start_s: float = 0.0, seed: int = 0) -> dict:
    """Record simulated audio into an offline session, through the same recorder as live acquisition.

    Sample 0 is captured at world time `start_s`; host times (session clock =
    world clock here) are block ends plus the delivery delay. Stimuli
    (`model.playback_every_s`) go to audio_playback.csv like the mixer's log.
    """
    import pandas as pd

    from .edges import EdgeDetector
    from .recorder import PLAYBACK_COLUMNS, PLAYBACK_FILE, AudioRecorder

    m = model or SimAudioModel()
    rng = np.random.default_rng(seed + 99)
    stim = stimuli(duration_s, m, n_speakers=len(cfg.speakers), seed=seed, start_s=start_s)
    synth = AudioSynth(world, cfg, m, seed, stim)
    rec = AudioRecorder(sess.path, cfg, t0=0.0, stream_info={"device": "simulated", "host_api": "sim"})
    det = EdgeDetector(cfg.sample_rate) if cfg.sync_index is not None else None
    rate = cfg.sample_rate * (1 + m.drift_ppm * 1e-6)
    n_total = int((duration_s - start_s) * rate)
    for k in range(0, n_total - cfg.block + 1, cfg.block):
        t = start_s + (k + np.arange(cfg.block)) / rate
        x = synth.render(t)
        host = start_s + (k + cfg.block) / rate + m.latency_s + rng.exponential(m.jitter_s)
        edges = det.process(x[:, cfg.sync_index], k) if det is not None else []
        rec._write(k, x, host, 0.0, "", edges)  # synchronous: no writer thread needed offline
    rec._flush_clock()
    if rec._sf is not None:
        rec._sf.close()
        rec._sf = None
    rec._clock.close()
    rec._edges.close()
    calls = [{"t": c.t, "dur": c.dur, "f0": c.f0, "pigeon": c.pigeon, "perch": c.perch, "response": int(c.response)}
             for c in synth.calls if start_s <= c.t < duration_s]
    pd.DataFrame(calls, columns=["t", "dur", "f0", "pigeon", "perch", "response"]).to_csv(
        sess.file("ground_truth_calls.csv"), index=False)
    if stim:
        names = [cfg.speakers[k] for k in sorted(cfg.speakers)]
        pd.DataFrame([[int(round((s.t - start_s) * rate)), int(round(s.dur * cfg.sample_rate)), names[s.speaker - 1],
                       s.sound, "0.00", 0, 0, "rule", 0] for s in stim],
                     columns=PLAYBACK_COLUMNS).to_csv(sess.file(PLAYBACK_FILE), index=False)
    sess.add_audio({**rec.info(), "stats": rec.stats()})
    return {"drift_ppm": m.drift_ppm, "start_s": start_s, "latency_s": m.latency_s, "rate": rate}
