"""Output devices. Each executes named actions with already-rendered parameters.

Every device runs in its own worker thread (see engine.py), so a slow device
(MATLAB, a network peer) never delays another one or the acquisition.
execute() returns a short result text (or raises); the engine logs both.
"""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import threading
from pathlib import Path

from .config import render


class OutputDevice:
    type = "base"

    def __init__(self, name: str, params: dict):
        self.name = name
        self.params = params
        self.error: str | None = None
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.safe_state()
        self.opened = False

    def safe_state(self) -> None:
        """Drive every held output to its idle state (called on disarm, stop and errors)."""

    def execute(self, action: str, params: dict, ctx: dict) -> str:
        raise NotImplementedError

    def test(self) -> str:
        return "ok"

    def info(self) -> dict:
        return {"name": self.name, "type": self.type, "opened": self.opened, "error": self.error}


class SimDevice(OutputDevice):
    """Logs actions only. Used for dry runs, the simulator and tests."""

    type = "sim"

    def __init__(self, name, params):
        super().__init__(name, params)
        self.calls: list[tuple[str, dict, dict]] = []
        self.held = False
        self._lock = threading.Lock()

    def execute(self, action, params, ctx):
        with self._lock:
            self.calls.append((action, dict(params), dict(ctx)))
            if action == "hold":
                self.held = ctx.get("event") == "start"
        return f"{action} {render(params.get('text', ''), ctx)}".strip()

    def safe_state(self):
        self.held = False


class UdpDevice(OutputDevice):
    type = "udp"

    def open(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = (str(self.params.get("host", "127.0.0.1")), int(self.params.get("port", 9000)))
        self.opened = True

    def _send(self, text: str) -> str:
        self.sock.sendto(text.encode("utf-8"), self.addr)
        return f"sent {text!r} to {self.addr[0]}:{self.addr[1]}"

    def execute(self, action, params, ctx):
        if action == "send":
            return self._send(render(params.get("text", ""), ctx))
        if action == "hold":
            key = "start_text" if ctx.get("event") == "start" else "stop_text"
            return self._send(render(params.get(key, ""), ctx))
        raise ValueError(f"unknown action {action!r}")

    def test(self):
        return self._send("mavric ping")

    def close(self):
        if getattr(self, "sock", None):
            self.sock.close()
        self.opened = False


class TcpDevice(OutputDevice):
    """Persistent TCP client; reconnects once per send if the peer went away."""

    type = "tcp"
    timeout_s = 2.0

    def open(self):
        self.addr = (str(self.params.get("host", "127.0.0.1")), int(self.params.get("port", 9001)))
        self.sock = None
        self._connect()
        self.opened = True

    def _connect(self):
        self.sock = socket.create_connection(self.addr, timeout=self.timeout_s)

    def _send(self, text: str) -> str:
        data = text + ("\n" if self.params.get("newline", True) else "")
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                self.sock.sendall(data.encode("utf-8"))
                return f"sent {text!r}"
            except OSError:
                self.sock = None
                if attempt == 2:
                    raise
        return ""

    def execute(self, action, params, ctx):
        if action == "send":
            return self._send(render(params.get("text", ""), ctx))
        if action == "hold":
            key = "start_text" if ctx.get("event") == "start" else "stop_text"
            return self._send(render(params.get(key, ""), ctx))
        raise ValueError(f"unknown action {action!r}")

    def test(self):
        return self._send("mavric ping")

    def close(self):
        if getattr(self, "sock", None):
            try:
                self.sock.close()
            except OSError:
                pass
        self.opened = False


class IntanRhxDevice(TcpDevice):
    """Intan RHX remote TCP control (command port, default 5000).

    In RHX: Network > Remote TCP Control > Commands: host/port, Connect. RHX is
    the server; commands are plain ASCII ("set runmode record", "get runmode").
    """

    type = "intan_rhx"
    MODES = {"record": "set runmode record", "run": "set runmode run", "stop": "set runmode stop"}
    min_gap_s = 0.05  # RHX parses one command per packet; keep packets apart (Intan's examples sleep too)

    def _send(self, text: str) -> str:
        import time

        wait = getattr(self, "_last_send", 0.0) + self.min_gap_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return super()._send(text)
        finally:
            self._last_send = time.monotonic()

    def open(self):
        self.params = {**self.params, "newline": False}
        self.addr = (str(self.params.get("host", "127.0.0.1")), int(self.params.get("port", 5000)))
        self.sock = None
        self._connect()
        self.opened = True

    def query(self, command: str = "get runmode", timeout_s: float = 1.0) -> str:
        self._send(command)
        self.sock.settimeout(timeout_s)
        try:
            return self.sock.recv(4096).decode("utf-8", errors="replace").strip()
        finally:
            self.sock.settimeout(self.timeout_s)

    def execute(self, action, params, ctx):
        if action in self.MODES:
            return self._send(self.MODES[action])
        if action == "hold_record":
            return self._send(self.MODES["record" if ctx.get("event") == "start" else "stop"])
        if action == "send":
            return self._send(render(params.get("text", ""), ctx))
        raise ValueError(f"unknown action {action!r}")

    def test(self):
        return self.query("get runmode")


class MatlabDevice(OutputDevice):
    """MATLAB through the Engine API.

    In MATLAB run `matlab.engine.shareEngine('mavric')` once, so actions execute
    in your open MATLAB session and workspace; with an empty session name a
    new (invisible) MATLAB is started instead (~10-20 s, done at open()).
    """

    type = "matlab"

    def open(self):
        try:
            import matlab.engine  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "MATLAB Engine for Python is not installed in this environment. With the mavric env active: "
                "pip install \"<matlabroot>\\extern\\engines\\python\" (R2026a supports Python 3.12)."
            ) from e
        import matlab.engine

        session = str(self.params.get("session", "") or "")
        if session:
            names = matlab.engine.find_matlab()
            if session not in names:
                raise RuntimeError(f"no shared MATLAB session {session!r} (found {list(names)}). "
                                   f"In MATLAB run: matlab.engine.shareEngine('{session}')")
            self.eng = matlab.engine.connect_matlab(session)
        else:
            self.eng = matlab.engine.start_matlab()
        cwd = str(self.params.get("cwd", "") or "")
        if cwd:
            self.eng.cd(cwd, nargout=0)
        self.opened = True

    def execute(self, action, params, ctx):
        if action == "eval":
            code = render(params.get("code", ""), ctx)
        elif action == "call":
            code = f"{params.get('function', '')}({render(params.get('args', ''), ctx)});"
        elif action == "run":
            code = f"run('{render(params.get('script', ''), ctx)}');"
        else:
            raise ValueError(f"unknown action {action!r}")
        self.eng.eval(code, nargout=0)
        return f"eval {code!r}"

    def test(self):
        self.eng.eval("disp('MAVRIC test')", nargout=0)
        return "MATLAB responded"

    def close(self):
        eng = getattr(self, "eng", None)
        if eng is not None and not self.params.get("session"):
            try:
                eng.quit()
            except Exception:
                pass
        self.eng = None
        self.opened = False


class CommandDevice(OutputDevice):
    """Start a program per event (non-blocking). Arguments accept {placeholders}."""

    type = "command"

    def open(self):
        prog = str(self.params.get("program", "") or "")
        if not prog:
            raise ValueError("no program configured")
        self.opened = True

    def _start(self, args: str) -> str:
        prog = str(self.params["program"])
        argv = [prog] + shlex.split(args, posix=os.name != "nt")
        argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a for a in argv]
        cwd = str(self.params.get("cwd", "") or "") or None
        if cwd and not Path(cwd).is_dir():
            raise FileNotFoundError(f"working folder {cwd} does not exist")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
        return f"started pid {proc.pid}: {' '.join(argv)}"

    def execute(self, action, params, ctx):
        if action != "run":
            raise ValueError(f"unknown action {action!r}")
        return self._start(render(params.get("args", ""), ctx))

    def test(self):
        """Check the program can be found; running it is left to a rule's Test button."""
        import shutil

        prog = str(self.params["program"])
        found = shutil.which(prog) or (prog if Path(prog).exists() else None)
        if not found:
            raise FileNotFoundError(f"program {prog!r} not found")
        return f"found {found}"


class AudioOutputDevice(OutputDevice):
    """Plays library sounds on the speakers of the acquisition's audio stream.

    `get_player()` returns the live mavric.audio.player.Player, or None while
    audio is not connected (actions then fail with a clear message). hold_play
    loops its sound from a while-rule's start until its stop; disarming fades
    out every sound.
    """

    type = "audio"

    def __init__(self, name, params, get_player=None):
        super().__init__(name, params)
        self.get_player = get_player or (lambda: None)
        self._held: dict[str, list[int]] = {}  # rule -> voices of its hold_play
        self._lock = threading.Lock()

    def _player(self):
        p = self.get_player()
        if p is None:
            raise RuntimeError("audio is not connected (Connect audio; speakers in rig.yaml audio.speakers)")
        return p

    def _play(self, p, params, ctx, loop: bool):
        rule = ctx.get("rule", "")
        return p.play(render(params.get("sound", ""), ctx), render(str(params.get("speakers", "all")), ctx),
                      float(params.get("gain_db", 0.0)), loop, f"rule:{rule}" if rule else "outputs")

    def execute(self, action, params, ctx):
        p = self._player()
        if action == "play":
            return "play " + self._play(p, params, ctx, bool(params.get("loop", False)))[1]
        if action == "stop":
            return p.stop(render(str(params.get("speakers", "all")), ctx))
        if action == "hold_play":
            rule = ctx.get("rule", "")
            with self._lock:
                old = self._held.pop(rule, [])
            stopped = p.stop(voices=old) if old else "nothing playing"
            if ctx.get("event") == "start":
                ids, text = self._play(p, params, ctx, True)
                with self._lock:
                    self._held[rule] = ids
                return "hold_play " + text
            return stopped
        raise ValueError(f"unknown action {action!r}")

    def safe_state(self):
        with self._lock:
            self._held.clear()
        p = self.get_player()
        if p is not None:
            p.stop_all()

    def test(self):
        p = self._player()
        from ..audio.library import TEST_TONE

        _ids, text = p.play(TEST_TONE, "all", 0.0, source="test")
        return f"played {text}; {len(p.library.sounds)} sound(s) in {p.library.folder}"


def make_device(name: str, cfg: dict, resources: dict | None = None) -> OutputDevice:
    """resources: shared live objects some devices need ({"player": callable returning the audio Player})."""
    from .arduino_io import ArduinoIODevice

    kinds = {"sim": SimDevice, "udp": UdpDevice, "tcp": TcpDevice, "intan_rhx": IntanRhxDevice,
             "matlab": MatlabDevice, "command": CommandDevice, "arduino_io": ArduinoIODevice}
    dtype = cfg.get("type")
    params = {k: v for k, v in cfg.items() if k != "type"}
    if dtype == "audio":
        return AudioOutputDevice(name, params, (resources or {}).get("player"))
    if dtype not in kinds:
        raise ValueError(f"unknown device type {dtype!r}")
    return kinds[dtype](name, params)
