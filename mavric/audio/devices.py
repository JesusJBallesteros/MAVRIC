"""Finding audio devices through PortAudio (python-sounddevice).

The PyPI wheels of sounddevice ship a PortAudio built with ASIO, which is
enabled by setting SD_ENABLE_ASIO before the first import. A portaudio.dll
from conda-forge (no ASIO) or elsewhere on PATH would take precedence, so
sounddevice must come from pip (see environment.yml).
"""

from __future__ import annotations

import os

HOST_API_ALIASES = {
    "asio": "asio",
    "wasapi": "wasapi",
    "mme": "mme",
    "directsound": "directsound",
    "ds": "directsound",
    "wdm-ks": "wdm-ks",
    "wdmks": "wdm-ks",
    "core audio": "core audio",
    "alsa": "alsa",
    "jack": "jack",
}


def import_sounddevice():
    os.environ.setdefault("SD_ENABLE_ASIO", "1")
    try:
        import sounddevice as sd
    except (ImportError, OSError) as e:
        raise RuntimeError(f"sounddevice is not available ({e}). Install it with "
                           f"`pip install sounddevice soundfile` in the mavric environment.") from e
    return sd


def list_devices() -> list[dict]:
    sd = import_sounddevice()
    apis = sd.query_hostapis()
    out = []
    for i, d in enumerate(sd.query_devices()):
        out.append({"index": i, "name": d["name"], "host_api": apis[d["hostapi"]]["name"],
                    "inputs": int(d["max_input_channels"]), "outputs": int(d["max_output_channels"]),
                    "default_samplerate": float(d["default_samplerate"]),
                    "low_latency_s": (float(d["default_low_input_latency"]), float(d["default_low_output_latency"]))})
    return out


def _api_matches(api_name: str, wanted: str) -> bool:
    w = HOST_API_ALIASES.get(wanted.strip().lower(), wanted.strip().lower())
    return w in api_name.lower()


def find_device(name: str, host_api: str, need_in: int = 0, need_out: int = 0,
                devices: list[dict] | None = None) -> dict:
    """First device whose name contains `name` on `host_api` with enough channels."""
    devices = devices if devices is not None else list_devices()
    cands = [d for d in devices if name.lower() in d["name"].lower() and _api_matches(d["host_api"], host_api)]
    for d in cands:
        if d["inputs"] >= need_in and d["outputs"] >= need_out:
            return d
    have = "; ".join(f"{d['name']} [{d['host_api']}] in={d['inputs']} out={d['outputs']}" for d in cands) or "none"
    raise RuntimeError(f"No {host_api} device matching {name!r} with >= {need_in} inputs and >= {need_out} outputs "
                       f"(candidates: {have}). Is it connected and its driver installed? See `mavric audio devices`.")
