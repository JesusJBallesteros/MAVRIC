"""Audio settings from rig.yaml (`audio:` section)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AudioConfig:
    device: str = "MOTU"
    host_api: str = "ASIO"
    sample_rate: int = 48000
    block: int = 1024
    latency: str | float = "high"
    inputs: dict[int, str] = field(default_factory=lambda: {1: "micA", 2: "micB", 3: "sync"})
    sync_channel: str = "sync"
    speakers: dict[int, str] = field(default_factory=dict)
    format: str = "FLAC"
    subtype: str = "PCM_24"
    split_s: float = 3600.0
    max_level_dbfs: float = -6.0
    display_fmax_hz: float = 10000.0
    pulse_s: float = 0.080  # sync pulse width (rig sync.led_pulse_s)
    enabled: bool = False
    mic_near: dict[str, list[int]] = field(default_factory=dict)  # mic -> antennas next to it

    @classmethod
    def from_rig(cls, rig: dict) -> "AudioConfig":
        a = dict(rig.get("audio") or {})
        lat = a.get("latency", "high")
        try:
            lat = float(lat)
        except (TypeError, ValueError):
            lat = str(lat)
        return cls(
            device=str(a.get("device", "MOTU")),
            host_api=str(a.get("host_api", "ASIO")),
            sample_rate=int(a.get("sample_rate", 48000)),
            block=int(a.get("block", 1024)),
            latency=lat,
            inputs={int(k): str(v) for k, v in (a.get("inputs") or {}).items()},
            sync_channel=str(a.get("sync_channel") or ""),
            speakers={int(k): str(v) for k, v in (a.get("speakers") or {}).items()},
            format=str(a.get("format", "FLAC")).upper(),
            subtype=str(a.get("subtype", "PCM_24")),
            split_s=float(a.get("split_s", 3600)),
            max_level_dbfs=float(a.get("max_level_dbfs", -6.0)),
            display_fmax_hz=float(a.get("display_fmax_hz", 10000)),
            pulse_s=float((rig.get("sync") or {}).get("led_pulse_s", 0.08)),
            enabled=bool(a.get("enabled", False)),
            mic_near={str(k): [int(x) for x in (v if isinstance(v, list) else [v])]
                      for k, v in (a.get("mic_near") or {}).items()},
        )

    # ------------------------------------------------------------------ channel layout
    @property
    def in_channels(self) -> list[int]:
        """Device input channels (1-based) in recording order."""
        return sorted(self.inputs)

    @property
    def names(self) -> list[str]:
        return [self.inputs[c] for c in self.in_channels]

    @property
    def n_in_open(self) -> int:
        """Input channels to open on the device (1..highest used)."""
        return max(self.inputs) if self.inputs else 0

    @property
    def n_out_open(self) -> int:
        return max(self.speakers) if self.speakers else 0

    @property
    def sync_index(self) -> int | None:
        """Column of the sync channel in the recorded data."""
        return self.names.index(self.sync_channel) if self.sync_channel in self.names else None

    @property
    def mic_indices(self) -> list[int]:
        return [i for i, n in enumerate(self.names) if i != self.sync_index]

    def speaker_index(self, speaker: str | int) -> int:
        """Output column (0-based) of a speaker given by name or 1-based channel."""
        if isinstance(speaker, int) or str(speaker).isdigit():
            ch = int(speaker)
        else:
            ch = next((c for c, n in self.speakers.items() if n == speaker), 0)
        if ch not in self.speakers:
            raise KeyError(f"No speaker {speaker!r} (have {self.speakers})")
        return ch - 1

    @property
    def is_sim(self) -> bool:
        return self.host_api.lower() == "sim"

    def describe(self) -> str:
        return f"{self.device} ({self.host_api}) {self.sample_rate / 1000:g} kHz"
