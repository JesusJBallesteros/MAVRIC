"""MultiRFID Arduino serial protocol.

Firmware lines (CRLF terminated, 500000 baud):

    <micros>,<TAG12HEX>,<reader>   tag present at reader 1..4 in this sampling round
    <micros>,NONE,0                round with no tag at any reader (8 Hz heartbeat)
    <micros>,SYNC,0                sync LED rising edge (MAVRIC firmware)
    <micros>,BOOT,0                firmware (re)started (MAVRIC firmware)

All tag lines of one sampling round share the same <micros> value.
<micros> is the Arduino's 32-bit micros() counter: it wraps every 2**32 us
(~71.6 min) and restarts from ~0 whenever the board resets (port open).
"""

from __future__ import annotations

from dataclasses import dataclass

WRAP = 1 << 32  # micros() period

KIND_TAG = "TAG"
KIND_NONE = "NONE"
KIND_SYNC = "SYNC"
KIND_BOOT = "BOOT"
KIND_BAD = "BAD"

_SPECIAL = {"NONE": KIND_NONE, "SYNC": KIND_SYNC, "BOOT": KIND_BOOT}


@dataclass(slots=True)
class RfidLine:
    kind: str
    micros: int | None = None  # raw 32-bit firmware timestamp
    tag: str = ""
    reader: int = 0
    checksum_ok: bool | None = None  # None when not applicable
    raw: str = ""


def em4100_checksum_ok(tag: str) -> bool:
    """ID-xxLA ASCII frame: 10 hex data chars + 2 hex checksum (XOR of the 5 data bytes)."""
    if len(tag) != 12:
        return False
    try:
        b = bytes.fromhex(tag)
    except ValueError:
        return False
    x = 0
    for v in b[:5]:
        x ^= v
    return x == b[5]


def parse_line(line: str | bytes) -> RfidLine:
    """Parse one firmware line. Never raises; malformed input gives kind=BAD."""
    if isinstance(line, bytes):
        line = line.decode("ascii", errors="replace")
    s = line.strip()
    parts = s.split(",")
    if len(parts) != 3:
        return RfidLine(KIND_BAD, raw=s)
    ts, tag, reader = (p.strip() for p in parts)
    try:
        micros = int(ts)
        rd = int(reader)
    except ValueError:
        return RfidLine(KIND_BAD, raw=s)
    if not 0 <= micros < WRAP:
        return RfidLine(KIND_BAD, raw=s)
    tag_u = tag.upper()
    if tag_u in _SPECIAL:
        return RfidLine(_SPECIAL[tag_u], micros=micros, tag=tag_u, reader=rd)
    if not 1 <= rd <= 4 or not tag_u:
        return RfidLine(KIND_BAD, micros=micros, tag=tag_u, reader=rd, raw=s)
    return RfidLine(KIND_TAG, micros=micros, tag=tag_u, reader=rd, checksum_ok=em4100_checksum_ok(tag_u))


class MicrosUnwrapper:
    """Turn the 32-bit micros() stream into a monotonic 64-bit count, per segment.

    A backwards step is a *wrap* when the previous value was near 2**32 and the
    new one near 0; any other backwards step (or a BOOT line) is a board *reset*,
    which starts a new segment because the two clocks no longer share an origin.
    """

    def __init__(self, wrap_margin_us: int = 60_000_000):
        self.margin = wrap_margin_us
        self.segment = 0
        self._offset = 0
        self._last: int | None = None

    def reset(self) -> None:
        self.segment += 1
        self._offset = 0
        self._last = None

    def __call__(self, micros: int, boot: bool = False) -> tuple[int, int]:
        """Return (unwrapped_us, segment)."""
        if boot and self._last is not None:
            self.reset()
        if self._last is not None and micros < self._last:
            if self._last > WRAP - self.margin and micros < self.margin:
                self._offset += WRAP
            elif self._last - micros > 1_000_000:  # >1 s backwards: board reset
                self.reset()
            # small backwards jitter is impossible from one micros() source; ignore
        self._last = micros
        return micros + self._offset, self.segment


RAW_COLUMNS = [
    "host_t",  # s since session start (host perf_counter), stamped on line arrival
    "segment",  # increments on Arduino reset
    "micros",  # raw firmware timestamp
    "arduino_us",  # unwrapped within segment
    "kind",
    "tag",
    "reader",
    "checksum_ok",
    "raw",
]
