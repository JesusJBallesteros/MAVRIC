"""Serial acquisition of MultiRFID lines with host-clock stamping on arrival."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from ..core import CsvAppender, EventBus, now
from .protocol import KIND_BAD, KIND_BOOT, KIND_NONE, KIND_TAG, RAW_COLUMNS, MicrosUnwrapper, parse_line

ARDUINO_VIDS = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4}  # Arduino, Arduino.org, CH340, FTDI, CP210x


class LineSource(Protocol):
    def open(self) -> None: ...
    def readline(self) -> bytes | None: ...  # None on timeout
    def close(self) -> None: ...


def list_ports() -> list[dict]:
    from serial.tools import list_ports as lp

    return [
        {"device": p.device, "description": p.description, "vid": p.vid, "pid": p.pid, "serial": p.serial_number}
        for p in lp.comports()
    ]


def find_arduino_port() -> str | None:
    for p in list_ports():
        if p["vid"] in ARDUINO_VIDS:
            return p["device"]
    return None


class SerialLineSource:
    """pyserial line source. Opening the port resets the MEGA (DTR), so wait, then flush."""

    def __init__(self, port: str, baud: int = 500000, reset_wait_s: float = 2.0, timeout_s: float = 0.005):
        self.port = port
        self.baud = baud
        self.reset_wait_s = reset_wait_s
        self.timeout_s = timeout_s  # short: an idle line lets the presence tracker close a round at once
        self._ser = None
        self._buf = b""

    def open(self) -> None:
        import serial

        port = self.port
        if port in ("", "auto", None):
            port = find_arduino_port()
            if port is None:
                raise RuntimeError("No Arduino-like serial port found")
            self.port = port
        self._ser = serial.Serial(port, self.baud, timeout=self.timeout_s)
        time.sleep(self.reset_wait_s)
        self._ser.reset_input_buffer()  # flush AFTER the reset wait (MultiRFID flushed before)

    def readline(self) -> bytes | None:
        """A complete line, or None after `timeout_s` without one (partial lines are kept)."""
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
                return line
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if not chunk:
                return None
            self._buf += chunk

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None


@dataclass(slots=True)
class Presence:
    """Presence change at one antenna. tag == "" means the antenna became empty."""

    reader: int
    tag: str
    prev_tag: str
    host_abs: float  # host monotonic seconds of the round that showed the change


class PresenceTracker:
    """Collapse 8 Hz sampling rounds into presence changes per antenna.

    All lines of one round share the firmware timestamp and arrive back to back
    (< 1 ms at 500000 baud). A round is closed when a line with a new timestamp
    arrives or, sooner, when the line stream has been idle for `idle_s` (see
    idle()), so presence changes are known ~5 ms after the round's lines
    instead of one round (125 ms) later. A tag is dropped only after it is
    missing for `off_rounds` consecutive rounds (debounce for the live display;
    offline analysis builds its own bouts from the raw lines).
    """

    def __init__(self, n_readers: int = 4, off_rounds: int = 2):
        self.state = {r: "" for r in range(1, n_readers + 1)}
        self._missing = {r: 0 for r in self.state}
        self.off_rounds = off_rounds
        self._round_key = None
        self._round_tags: dict[int, str] = {}
        self._round_t = 0.0
        self._round_closed = True
        self._last_line_t = 0.0

    def feed(self, key, kind: str, reader: int, tag: str, host_abs: float) -> list[Presence]:
        out: list[Presence] = []
        if key != self._round_key:
            if not self._round_closed:
                out = self._close_round()
            self._round_key = key
            self._round_tags = {}
            self._round_t = host_abs
            self._round_closed = False
        if kind == KIND_TAG and reader in self.state:
            self._round_tags[reader] = tag  # (a line arriving after an idle close counts next round)
        self._last_line_t = host_abs
        return out

    def idle(self, now_abs: float, idle_s: float = 0.005) -> list[Presence]:
        """Close the open round once no line has arrived for `idle_s`."""
        if self._round_closed or now_abs - self._last_line_t < idle_s:
            return []
        return self._close_round()

    def _close_round(self) -> list[Presence]:
        self._round_closed = True
        out = []
        for r, cur in self.state.items():
            new = self._round_tags.get(r, "")
            if new == cur:
                self._missing[r] = 0
                continue
            if new == "":
                self._missing[r] += 1
                if self._missing[r] < self.off_rounds:
                    continue
            self._missing[r] = 0
            self.state[r] = new
            out.append(Presence(r, new, cur, self._round_t))
        return out


class RfidReaderThread(threading.Thread):
    """Reads lines, stamps host time on arrival, parses, logs and publishes.

    Runs for as long as the device is connected (preview and recording); a CSV
    writer is attached only while recording.
    """

    def __init__(self, source: LineSource, bus: EventBus | None = None, n_readers: int = 4):
        super().__init__(daemon=True, name="rfid-reader")
        self.source = source
        self.bus = bus
        self.unwrap = MicrosUnwrapper()
        self.presence = PresenceTracker(n_readers)
        self.recent: deque = deque(maxlen=200)
        self._stop_evt = threading.Event()
        self._writer_lock = threading.Lock()
        self._writer: CsvAppender | None = None
        self._t0 = 0.0
        self.n_lines = 0
        self.n_bad = 0
        self.last_line_abs: float | None = None
        self.error: str | None = None
        self._rate_win: deque = deque(maxlen=64)

    # recording control -------------------------------------------------------
    def attach_writer(self, path, session_t0_abs: float) -> None:
        with self._writer_lock:
            self._t0 = session_t0_abs
            self._writer = CsvAppender(path, RAW_COLUMNS)

    def detach_writer(self) -> int:
        with self._writer_lock:
            n = 0
            if self._writer is not None:
                n = self._writer.n_rows
                self._writer.close()
                self._writer = None
            return n

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def line_rate(self) -> float:
        if len(self._rate_win) < 2:
            return 0.0
        dt = self._rate_win[-1] - self._rate_win[0]
        return (len(self._rate_win) - 1) / dt if dt > 0 else 0.0

    # main loop ---------------------------------------------------------------
    def run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                raw = self.source.readline()
                t = now()  # stamp on arrival, before parsing
                if raw is None:
                    for p in self.presence.idle(t):
                        if self.bus:
                            self.bus.publish("presence", p)
                    continue
                self._process_line(raw, t)
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            if self.bus:
                self.bus.publish("status", {"source": "rfid", "error": self.error})
        finally:
            self.detach_writer()
            try:
                self.source.close()
            except Exception:
                pass

    def _process_line(self, raw: bytes, t: float) -> None:
        ln = parse_line(raw)
        self.n_lines += 1
        self.last_line_abs = t
        self._rate_win.append(t)
        if ln.kind == KIND_BAD:
            self.n_bad += 1
            us, seg = (None, self.unwrap.segment)
        else:
            us, seg = self.unwrap(ln.micros, boot=ln.kind == KIND_BOOT)
        with self._writer_lock:
            if self._writer is not None:
                self._writer.write(
                    [
                        f"{t - self._t0:.6f}",
                        seg,
                        "" if ln.micros is None else ln.micros,
                        "" if us is None else us,
                        ln.kind,
                        ln.tag,
                        ln.reader,
                        "" if ln.checksum_ok is None else int(ln.checksum_ok),
                        ln.raw,
                    ]
                )
        self.recent.append((t, ln))
        if ln.kind in (KIND_TAG, KIND_NONE):
            for p in self.presence.feed((seg, us), ln.kind, ln.reader, ln.tag, t):
                if self.bus:
                    self.bus.publish("presence", p)
        if self.bus:
            self.bus.publish("rfid_line", (t, ln))
