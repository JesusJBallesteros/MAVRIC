"""TTL outputs through an Arduino running firmware/MAVRIC_IO.

Every command carries an id; the board answers `<micros>,ACK,<id>` right after
switching the pin, so each action gets (PC send time, PC ack time, board micros)
for the log. Port "SIM" uses FakeIOBoard, a software copy of the firmware.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque

from ..core import now
from .devices import OutputDevice


class FakeIOBoard:
    """Software emulation of MAVRIC_IO (same protocol), for tests and demos without hardware."""

    def __init__(self, n_pins: int = 70):
        self.n_pins = n_pins
        self.pins: dict[int, int] = {}
        self.watchdog_ms = 0
        self.last_cmd = time.monotonic()
        self.tripped = False
        self._out: queue.Queue = queue.Queue()
        self._t0 = time.perf_counter()
        self._timers: list[threading.Timer] = []
        self.history: list[tuple[float, int, int]] = []  # (t, pin, value)
        self._lock = threading.Lock()
        self._closed = False
        self._emit("BOOT", 0)
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _us(self) -> int:
        return int((time.perf_counter() - self._t0) * 1e6) % (1 << 32)

    def _emit(self, kind: str, ident, extra: str = "") -> None:
        self._out.put(f"{self._us()},{kind},{ident}{extra}\r\n".encode())

    def _set(self, pin: int, v: int) -> None:
        with self._lock:
            self.pins[pin] = v
            self.history.append((time.perf_counter(), pin, v))

    def _watchdog(self):
        while not self._closed:
            time.sleep(0.01)
            if self.watchdog_ms and not self.tripped and (time.monotonic() - self.last_cmd) * 1000 > self.watchdog_ms:
                for p in list(self.pins):
                    self._set(p, 0)
                self.tripped = True
                self._emit("WDOG", 0)

    def write(self, data: bytes) -> int:
        for raw in data.decode().splitlines():
            parts = raw.split()
            if len(parts) < 2:
                continue
            ident, cmd, args = parts[0], parts[1], [int(a) for a in parts[2:4]]
            self.last_cmd = time.monotonic()
            self.tripped = False
            pin = args[0] if args else -1
            if cmd in "PST" and not 2 <= pin < self.n_pins:
                self._emit("ERR", ident, ",bad pin")
            elif cmd == "P":
                self._set(pin, 1)
                self._emit("ACK", ident)
                t = threading.Timer(args[1] / 1000, lambda p=pin, i=ident: (self._set(p, 0), self._emit("DONE", i)))
                t.daemon = True
                t.start()
                self._timers.append(t)
            elif cmd == "S":
                self._set(pin, args[1])
                self._emit("ACK", ident)
            elif cmd == "T":
                self._set(pin, 1 - self.pins.get(pin, 0))
                self._emit("ACK", ident)
            elif cmd == "A":
                for p in list(self.pins):
                    self._set(p, 0)
                self._emit("ACK", ident)
            elif cmd == "W":
                self.watchdog_ms = args[0] if args else 0
                self._emit("ACK", ident)
            elif cmd == "?":
                self._emit("INFO", f"MAVRIC_IO 1.0 pins={self.n_pins}")
            elif cmd != "H":
                self._emit("ERR", ident, ",unknown command")
        return len(data)

    def readline(self, timeout: float = 0.05) -> bytes | None:
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._closed = True
        for t in self._timers:
            t.cancel()


class _SerialTransport:
    def __init__(self, port: str, baud: int, reset_wait_s: float):
        import serial

        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=0.5)
        time.sleep(reset_wait_s)  # opening the port resets most Arduinos
        self.ser.reset_input_buffer()
        self.buf = b""

    def write(self, data: bytes) -> int:
        return self.ser.write(data)

    def readline(self, timeout: float = 0.05) -> bytes | None:
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                line, self.buf = self.buf[: i + 1], self.buf[i + 1 :]
                return line
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if not chunk:
                return None
            self.buf += chunk

    def close(self) -> None:
        self.ser.close()


class ArduinoIODevice(OutputDevice):
    type = "arduino_io"
    ack_timeout_s = 0.5

    def __init__(self, name: str, params: dict, transport=None):
        super().__init__(name, params)
        self.transport = transport
        self._ids = itertools.count(1)
        self._pending: dict[str, dict] = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self.held: set[int] = set()
        self.events: deque = deque(maxlen=500)  # asynchronous DONE / WDOG / BOOT lines
        self.on_event = None  # callback(kind, host_abs, device_us, ident) set by the engine
        self.last_device_us: int | None = None
        self.firmware = ""

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self.transport is None:
            port = str(self.params.get("port", ""))
            if port.upper() == "SIM":
                self.transport = FakeIOBoard()
            else:
                self.transport = _SerialTransport(port, int(self.params.get("baud", 115200)),
                                                  float(self.params.get("reset_wait_s", 2.0)))
        self._reader = threading.Thread(target=self._read_loop, daemon=True, name=f"io-{self.name}")
        self._reader.start()
        info = self._request("?", wait_for="INFO")
        if info is None or not any(k in info.get("text", "") for k in ("MAVRIC_IO", "VRFID_IO")):
            self.close()
            raise RuntimeError(f"no MAVRIC_IO firmware answered on {self.params.get('port')!r} "
                               "(flash firmware/MAVRIC_IO, check port and baud)")
        self.firmware = info["text"]
        wd = float(self.params.get("watchdog_s", 2.0) or 0)
        self._request("W", int(wd * 1000))
        if wd > 0:
            self._hb = threading.Thread(target=self._heartbeat, args=(wd / 3,), daemon=True)
            self._hb.start()
        self.opened = True

    def close(self) -> None:
        if self.transport is not None:
            try:
                self.safe_state()
                self._request("W", 0)
            except Exception:
                pass
        self._stop.set()
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.opened = False

    def safe_state(self) -> None:
        self.held.clear()
        if self.transport is not None and not self._stop.is_set():
            self._request("A")

    # ------------------------------------------------------------------ protocol
    def _write(self, text: str) -> None:
        with self._write_lock:
            self.transport.write((text + "\n").encode())

    def _request(self, cmd: str, *args, wait_for: str = "ACK") -> dict | None:
        ident = str(next(self._ids))
        rec = {"id": ident, "sent": now()}
        with self._cond:
            self._pending["INFO" if wait_for == "INFO" else ident] = rec
        self._write(" ".join([ident, cmd, *map(str, args)]))
        deadline = rec["sent"] + self.ack_timeout_s
        with self._cond:
            while "reply" not in rec:
                left = deadline - now()
                if left <= 0:
                    self._pending.pop("INFO" if wait_for == "INFO" else ident, None)
                    return None
                self._cond.wait(left)
        return rec

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.transport.readline(0.05)
            except Exception as e:
                self.error = f"read: {e}"
                time.sleep(0.2)
                continue
            if raw is None:
                continue
            t = now()
            line = raw.decode("ascii", errors="replace").strip()
            parts = line.split(",", 3)
            if len(parts) < 3:
                continue
            try:
                us = int(parts[0])
            except ValueError:
                continue
            kind, ident = parts[1], parts[2]
            if kind in ("ACK", "ERR", "INFO"):
                key = "INFO" if kind == "INFO" else ident
                with self._cond:
                    rec = self._pending.pop(key, None)
                    if rec is not None:
                        rec.update(reply=kind, ack=t, us=us, text=",".join(parts[2:]))
                        self._cond.notify_all()
            else:  # DONE / WDOG / BOOT
                self.events.append((kind, t, us, ident))
                if kind == "WDOG":
                    self.held.clear()
                if self.on_event:
                    try:
                        self.on_event(kind, t, us, ident)
                    except Exception:
                        pass

    def _heartbeat(self, period: float) -> None:
        while not self._stop.wait(period):
            try:
                self._write("0 H")
            except Exception as e:
                self.error = f"heartbeat: {e}"

    def _do(self, cmd: str, *args) -> str:
        rec = self._request(cmd, *args)
        if rec is None:
            raise TimeoutError(f"no reply from {self.name} to {cmd} {args}")
        if rec["reply"] == "ERR":
            raise RuntimeError(f"{self.name} refused {cmd} {args}: {rec.get('text', '')}")
        self.last_device_us = rec["us"]
        return f"{cmd} {' '.join(map(str, args))} ack {1e3 * (rec['ack'] - rec['sent']):.1f} ms"

    # ------------------------------------------------------------------ actions
    def execute(self, action: str, params: dict, ctx: dict) -> str:
        self.last_device_us = None
        pin = int(params.get("pin", -1))
        if action == "pulse":
            return self._do("P", pin, int(params.get("ms", 50)))
        if action == "hold":
            on = ctx.get("event") == "start"
            (self.held.add if on else self.held.discard)(pin)
            return self._do("S", pin, 1 if on else 0)
        if action == "set":
            return self._do("S", pin, 1 if int(params.get("value", 1)) else 0)
        if action == "toggle":
            return self._do("T", pin)
        raise ValueError(f"unknown action {action!r}")

    def test(self) -> str:
        return f"{self.firmware}; " + self._do("A")

    def info(self) -> dict:
        d = super().info()
        d.update(firmware=self.firmware, held=sorted(self.held))
        return d
