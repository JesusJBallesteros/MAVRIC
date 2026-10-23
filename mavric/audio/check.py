"""Bench check of the audio interface: levels, clipping, sync pulses, clock, optional speaker tones."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from ..core import now
from .config import AudioConfig
from .edges import pulse_onsets
from .stream import AudioStream, MemorySink, db


def tone(freq: float, dur_s: float, sample_rate: float, level_dbfs: float, fade_s: float = 0.005) -> np.ndarray:
    t = np.arange(int(dur_s * sample_rate)) / sample_rate
    y = np.sin(2 * np.pi * freq * t) * 10 ** (level_dbfs / 20)
    nf = int(fade_s * sample_rate)
    if nf:
        ramp = np.sin(np.linspace(0, np.pi / 2, nf)) ** 2
        y[:nf] *= ramp
        y[-nf:] *= ramp[::-1]
    return y.astype(np.float32)


def tone_response(x: np.ndarray, start: int, sr: float, freq: float = 1000.0, dur_s: float = 0.1,
                  win_s: float = 0.4, fade_s: float = 0.005) -> dict:
    """Level of a `dur_s` tone at `freq` in a mic channel after it was played at sample `start`, and its delay.

    Narrow-band (10 ms window, ~100 Hz wide) and required to last about
    `dur_s`, so calls or noise with energy near `freq` do not count.
    """
    a, b = max(0, start - int(0.3 * sr)), min(len(x), start + int(win_s * sr))
    seg = x[a:b].astype(np.float64)
    t = np.arange(a, b) / sr
    z = seg * np.exp(-2j * np.pi * freq * t)
    n = int(0.01 * sr)
    env = np.abs(np.convolve(z, np.ones(n) / n, mode="full")[: len(z)]) * 2
    i0 = start - a
    bg = float(np.median(env[: max(i0 - n, 1)])) + 1e-9
    after = env[i0:]
    if not len(after):
        return {"heard": False}
    pk = float(after.max())
    above = after > pk / 2
    width = above.sum() / sr
    heard = pk > 10 * bg and 0.6 * dur_s <= width <= 1.6 * dur_s
    out = {"heard": bool(heard), "level_dbfs": db(pk), "above_background_db": db(pk) - db(bg)}
    if heard:
        j = int(np.argmax(above))
        # minus the envelope's own lag (half the window) and the tone's fade-in up to half amplitude
        out["delay_ms"] = ((j - n / 2) / sr - fade_s / 2) * 1e3
    return out


def audio_check(cfg: AudioConfig, seconds: float = 10.0, tones: bool = False, tone_db: float = -30.0,
                world=None, save_path: str | Path | None = None) -> dict:
    sr = cfg.sample_rate
    st = AudioStream(cfg, world=world)
    st.open()
    sink = MemorySink()
    plan = []
    if tones and cfg.n_out_open:
        plan = [(1.0 + 1.0 * i, ch, name) for i, (ch, name) in enumerate(sorted(cfg.speakers.items()))]
        seconds = max(seconds, plan[-1][0] + 1.5)
    try:
        info = st.info()
        st.attach(sink)
        t0 = now()
        done = set()
        while now() - t0 < seconds:
            for at, ch, name in plan:
                if name not in done and now() - t0 >= at:
                    st.play(tone(1000.0, 0.1, sr, tone_db), name, name=name)
                    done.add(name)
            time.sleep(0.02)
        st.detach()
        played = list(st.mixer.played)
        flags = dict(st.flag_counts)
    finally:
        st.close()
    x = sink.data()
    rep: dict = {"info": info, "seconds": len(x) / sr, "flags": flags, "channels": [], "warnings": []}
    w = rep["warnings"]
    if not len(x):
        w.append("no audio received")
        return rep
    for j, name in enumerate(cfg.names):
        c = x[:, j]
        ch = {"name": name, "input": cfg.in_channels[j], "peak_dbfs": db(np.abs(c).max()),
              "rms_dbfs": db(np.sqrt(np.mean(c.astype(np.float64) ** 2))), "clipped": int((np.abs(c) >= 0.999).sum()),
              "dc": float(c.mean())}
        rep["channels"].append(ch)
        if ch["peak_dbfs"] < -80:
            hint = " (NT5 on a mic input: phantom power 48 V on? cable?)" if j != cfg.sync_index else ""
            w.append(f"{name} (input {ch['input']}): no signal{hint}")
        if ch["clipped"]:
            w.append(f"{name} (input {ch['input']}): {ch['clipped']} clipped samples: lower the gain")

    if cfg.sync_index is not None:
        e = np.array(sink.edges, float).reshape(-1, 3)
        on, pol = pulse_onsets(e, sr, cfg.pulse_s)
        s = {"edges": int(len(e)), "pulses": int(len(on)), "polarity": pol}
        if len(on) >= 2:
            iv = np.diff(on) / sr
            s["interval_s"] = {"min": float(iv.min()), "median": float(np.median(iv)), "max": float(iv.max())}
        if len(e) >= 2 and len(on):
            idx = np.searchsorted(e[:, 0], on)
            widths = [(e[i + 1, 0] - e[i, 0]) / sr for i in idx if i + 1 < len(e)]
            if widths:
                s["width_ms"] = float(np.median(widths) * 1e3)
        if len(on):
            s["step_dbfs"] = db(np.median(np.abs(e[:, 2])))
        rep["sync"] = s
        if not len(e):
            w.append(f"sync ({cfg.sync_channel}, input {cfg.in_channels[cfg.sync_index]}): no pulses. Is the RFID "
                     f"Arduino running (it pulses every 2-6 s) and the D22 cable on this input?")
        elif not len(on):
            w.append(f"sync: {len(e)} edges but no {cfg.pulse_s * 1e3:.0f} ms pulses: check sync.led_pulse_s "
                     f"and the cable")
        elif len(x) / sr > 8 and len(on) < (len(x) / sr) / 7:
            w.append(f"sync: only {len(on)} pulses in {len(x) / sr:.0f} s (expected one every 2-6 s)")

    if len(sink.clock) >= 20 and len(x) / sr >= 4:
        from ..analysis.sync import fit_lower_envelope

        c = np.array([(k, h) for k, h, _ in sink.clock], float)
        f = fit_lower_envelope(c[:, 0] / sr, c[:, 1])
        rep["clock"] = {"drift_ppm": f.drift_ppm, "delivery_ms_med": f.latency_med_s * 1e3,
                        "delivery_ms_p95": f.latency_p95_s * 1e3}
        if abs(f.drift_ppm) > 500:
            w.append(f"audio clock runs {f.drift_ppm:+.0f} ppm off the PC clock: is the device really at {sr} Hz?")
    if flags.get("in_over"):
        w.append(f"{flags['in_over']} input overflow(s): samples were lost. Raise audio.block or latency.")

    if plan:
        k_first = sink.k_first or 0
        rep["tones"] = []
        for k0, out_ch, name in played:
            start = k0 - k_first
            r = {"speaker": name, "output": out_ch + 1, "at_s": start / sr, "mics": {}}
            for j in cfg.mic_indices:
                r["mics"][cfg.names[j]] = tone_response(x[:, j], start, sr)
            rep["tones"].append(r)
            if not any(m["heard"] for m in r["mics"].values()):
                w.append(f"tone on {name} (output {out_ch + 1}) not heard by the microphones: speaker powered "
                         f"and connected? volume?")
    if save_path is not None:
        import soundfile as sf

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(save_path), x, sr, subtype=cfg.subtype if cfg.format == "FLAC" else "PCM_24")
        rep["saved"] = str(save_path)
    return rep
