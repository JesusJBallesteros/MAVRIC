"""Event-driven outputs: rule semantics, devices (against local fakes) and the I/O firmware protocol."""

import socket
import sys
import threading
import time
import types

import pandas as pd
import pytest

from mavric.config import RoiSet, TagMap
from mavric.core import now
from mavric.outputs.arduino_io import ArduinoIODevice, FakeIOBoard
from mavric.outputs.config import OutputsConfig, Rule, render
from mavric.outputs.devices import CommandDevice, IntanRhxDevice, MatlabDevice, SimDevice, TcpDevice, UdpDevice
from mavric.outputs.engine import ActionEngine
from mavric.rfid.serial_reader import Presence, PresenceTracker

TAGS = TagMap({"0A000000010B": "S01", "0A0000000208": "S02"}, {})
S01, S02 = "0A000000010B", "0A0000000208"
ANT = {1: "PerchA", 2: "PerchB", 3: "PerchC", 4: "PerchD"}


def wait_for(cond, timeout=2.0):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def engine(rules, armed=True):
    cfg = OutputsConfig({"sim": {"type": "sim"}}, rules)
    eng = ActionEngine(cfg, TAGS, ANT, devices={"sim": SimDevice("sim", {})})
    eng.start()
    if armed:
        eng.arm()
    return eng, eng.devices["sim"]


def arrive(eng, reader, tag, t=None):
    eng.on_presence(Presence(reader, tag, "", t or now()))


def leave(eng, reader, tag, t=None):
    eng.on_presence(Presence(reader, "", tag, t or now()))


# --------------------------------------------------------------------------- basics


def test_render_templates():
    ctx = {"event": "enter", "roi": "PerchB", "pigeon": "S01", "t": 12.3456}
    assert render("{event} {roi} {pigeon} {t:.2f}", ctx) == "enter PerchB S01 12.35"
    assert render("{unknown} {roi}", ctx) == "{unknown} PerchB"  # unknown fields stay
    assert render("{t:.2f}", {}) == "{t:.2f}"  # bad format falls back to the template


def test_config_roundtrip_and_validation(tmp_path):
    cfg = OutputsConfig({"io": {"type": "arduino_io", "port": "SIM"}},
                        [Rule("r1", "io", "pulse", {"pin": 30, "ms": 20}, antennas=[2], who=["S01"])])
    cfg.save(tmp_path / "o.yaml")
    back = OutputsConfig.load(tmp_path / "o.yaml")
    assert back.devices == cfg.devices and back.rules[0] == cfg.rules[0]
    with pytest.raises(ValueError, match="unknown device"):
        OutputsConfig({}, [Rule("r", "nope", "pulse")]).validate()
    with pytest.raises(ValueError, match="no action"):
        OutputsConfig({"io": {"type": "arduino_io"}}, [Rule("r", "io", "send")]).validate()
    warns = OutputsConfig({"io": {"type": "arduino_io"}}, [Rule("r", "io", "hold", event="enter")]).validate()
    assert warns and "while" in warns[0]


# --------------------------------------------------------------------------- rule semantics


def test_enter_leave_filters_by_antenna_and_pigeon():
    eng, sim = engine([Rule("in", "sim", "mark", {"text": "{event} {roi} {pigeon}"}, antennas=[2], who=["S01"]),
                       Rule("out", "sim", "mark", {"text": "{event} {roi}"}, event="leave")])
    arrive(eng, 1, S01)  # wrong antenna for "in"
    arrive(eng, 2, S02)  # wrong pigeon for "in"
    arrive(eng, 2, S01)  # fires "in"
    leave(eng, 2, S01)  # fires "out"
    assert wait_for(lambda: len(sim.calls) == 2)
    assert [c[2]["event"] for c in sim.calls] == ["enter", "leave"]
    assert sim.calls[0][2]["pigeon"] == "S01" and sim.calls[0][2]["roi"] == "PerchB"
    eng.stop()


def test_while_rule_holds_until_last_bird_leaves():
    eng, sim = engine([Rule("hold", "sim", "hold", event="while", antennas=[1, 2])])
    arrive(eng, 1, S01)
    arrive(eng, 2, S02)  # already active: no second start
    leave(eng, 1, S01)  # S02 still present
    assert wait_for(lambda: len(sim.calls) == 1) and sim.held
    leave(eng, 2, S02)
    assert wait_for(lambda: len(sim.calls) == 2)
    assert [c[2]["event"] for c in sim.calls] == ["start", "stop"] and not sim.held
    eng.stop()


def test_refractory_max_rate_and_delay():
    eng, sim = engine([Rule("ref", "sim", "mark", refractory_s=10), Rule("rate", "sim", "mark", max_per_min=2),
                       Rule("late", "sim", "mark", delay_s=0.3, antennas=[4])])
    for _ in range(3):
        arrive(eng, 1, S01)
        leave(eng, 1, S01)
    assert wait_for(lambda: len(sim.calls) == 3)  # ref fired once, rate twice
    st = eng.status()["rules"]
    assert (st["ref"]["count"], st["ref"]["suppressed"]) == (1, 2)
    assert (st["rate"]["count"], st["rate"]["suppressed"]) == (2, 1)
    t0 = time.time()
    arrive(eng, 4, S02)  # ref (refractory) and rate (max/min) are suppressed again; "late" fires after 0.3 s
    assert wait_for(lambda: len(sim.calls) == 4)
    assert time.time() - t0 >= 0.25 and sim.calls[-1][2]["roi"] == "PerchD"
    st = eng.status()["rules"]
    assert (st["ref"]["suppressed"], st["rate"]["suppressed"]) == (3, 2)
    eng.stop()


def test_disarmed_is_dry_and_arm_seeds_present_birds():
    eng, sim = engine([Rule("hold", "sim", "hold", event="while")], armed=False)
    arrive(eng, 1, S01)
    time.sleep(0.1)
    assert sim.calls == [] and eng.log[-1]["status"] == "dry"
    eng.arm(presence={1: S01, 2: ""})  # bird already on perch 1 when arming
    assert wait_for(lambda: sim.held)
    eng.disarm()
    assert not sim.held  # safe state released the hold
    eng.stop()


def test_test_button_runs_while_disarmed_and_releases_hold():
    eng, sim = engine([Rule("hold", "sim", "hold", event="while")], armed=False)
    eng.fire_now("hold")
    assert wait_for(lambda: sim.held)
    assert wait_for(lambda: not sim.held, timeout=2.5)  # auto stop after 1 s
    eng.stop()


def test_session_actions_csv(tmp_path):
    from mavric.config import load_rig
    from mavric.session import Session

    anchor = {"perf_s": now(), "utc_ns": time.time_ns()}
    sess = Session.create(tmp_path, "act", load_rig(), TAGS, RoiSet(), anchor)
    eng, sim = engine([Rule("in", "sim", "mark", {"text": "{event} {pigeon}"})])
    eng.attach_session(sess, anchor["perf_s"])
    arrive(eng, 3, S02)
    assert wait_for(lambda: any(r.get("status") == "ok" for r in eng.log))
    eng.detach_session()
    eng.stop()
    df = pd.read_csv(sess.file("actions.csv"))
    ok = df[df["status"] == "ok"].iloc[0]
    assert ok["rule"] == "in" and ok["pigeon"] == "S02" and ok["roi"] == "PerchC"
    assert ok["t"] >= ok["event_t"] >= 0 and ok["latency_ms"] < 500
    assert (sess.path / "outputs.yaml").exists()


# --------------------------------------------------------------------------- I/O Arduino


def io_device(**params):
    board = FakeIOBoard()
    dev = ArduinoIODevice("io", {"port": "SIM", "watchdog_s": 0, **params}, transport=board)
    dev.open()
    return dev, board


def test_io_pulse_set_toggle_and_errors():
    dev, board = io_device()
    assert "MAVRIC_IO" in dev.firmware
    res = dev.execute("pulse", {"pin": 30, "ms": 40}, {})
    assert res.startswith("P 30 40 ack") and isinstance(dev.last_device_us, int)
    assert board.pins[30] == 1
    assert wait_for(lambda: board.pins[30] == 0) and wait_for(lambda: any(e[0] == "DONE" for e in dev.events))
    dev.execute("set", {"pin": 31, "value": 1}, {})
    dev.execute("toggle", {"pin": 31}, {})
    assert board.pins[31] == 0
    dev.execute("hold", {"pin": 32}, {"event": "start"})
    assert board.pins[32] == 1 and dev.held == {32}
    dev.safe_state()
    assert board.pins[32] == 0 and not dev.held
    with pytest.raises(RuntimeError, match="refused"):
        dev.execute("pulse", {"pin": 1, "ms": 5}, {})  # serial pin
    dev.close()


def test_io_watchdog_drops_outputs_when_pc_goes_silent():
    board = FakeIOBoard()
    board.write(b"1 S 30 1\n2 W 100\n")
    assert board.pins[30] == 1
    assert wait_for(lambda: board.pins[30] == 0, timeout=1.0)
    lines = []
    while (ln := board.readline(0.01)) is not None:
        lines.append(ln.decode())
    assert any(",WDOG," in ln for ln in lines)
    board.close()


def test_io_heartbeat_keeps_outputs_on():
    dev, board = io_device(watchdog_s=0.15)
    dev.execute("hold", {"pin": 33}, {"event": "start"})
    time.sleep(0.5)  # heartbeats every 50 ms
    assert board.pins[33] == 1 and not board.tripped
    dev.close()
    assert board.pins[33] == 0


def test_io_open_fails_without_firmware():
    class Mute(FakeIOBoard):
        def write(self, data):
            return len(data)

    dev = ArduinoIODevice("io", {"port": "COMX", "watchdog_s": 0}, transport=Mute())
    dev.ack_timeout_s = 0.1
    with pytest.raises(RuntimeError, match="no MAVRIC_IO firmware"):
        dev.open()


# --------------------------------------------------------------------------- network / programs


def test_udp_and_tcp_messages():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    udp = UdpDevice("u", {"host": "127.0.0.1", "port": rx.getsockname()[1]})
    udp.open()
    udp.execute("send", {"text": "{event} {roi} {pigeon}"}, {"event": "enter", "roi": "PerchA", "pigeon": "S01"})
    assert rx.recv(100) == b"enter PerchA S01"
    udp.close()
    rx.close()

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def serve():
        c, _ = srv.accept()
        got.append(c.recv(100))
        c.close()

    th = threading.Thread(target=serve)
    th.start()
    tcp = TcpDevice("t", {"host": "127.0.0.1", "port": srv.getsockname()[1]})
    tcp.open()
    tcp.execute("hold", {"start_text": "start {roi}", "stop_text": "stop"}, {"event": "start", "roi": "PerchC"})
    th.join(2)
    assert got == [b"start PerchC\n"]
    tcp.close()
    srv.close()


def test_intan_rhx_commands():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    received = []

    def rhx():
        c, _ = srv.accept()
        c.settimeout(2)
        while True:
            try:
                data = c.recv(200)
            except OSError:
                break
            if not data:
                break
            received.append(data.decode())
            if b"get runmode" in data:
                c.sendall(b"Return: RunMode Record")
        c.close()

    th = threading.Thread(target=rhx, daemon=True)
    th.start()
    dev = IntanRhxDevice("intan", {"host": "127.0.0.1", "port": srv.getsockname()[1]})
    dev.open()
    dev.execute("hold_record", {}, {"event": "start"})
    assert dev.test() == "Return: RunMode Record"
    dev.execute("hold_record", {}, {"event": "stop"})
    dev.close()
    th.join(2)
    joined = "".join(received)
    assert "set runmode record" in joined and "get runmode" in joined and "set runmode stop" in joined
    assert "\n" not in joined  # RHX commands are sent without newlines
    srv.close()


def test_command_device_runs_program_with_event_args(tmp_path):
    out = tmp_path / "args.txt"
    script = tmp_path / "write_args.py"
    script.write_text("import sys, pathlib; pathlib.Path(sys.argv[1]).write_text(' '.join(sys.argv[2:]))")
    dev = CommandDevice("cmd", {"program": sys.executable, "cwd": str(tmp_path)})
    dev.open()
    assert dev.test().startswith("found")
    res = dev.execute("run", {"args": f'"{script}" "{out}" {{event}} {{pigeon}}'}, {"event": "enter", "pigeon": "S01"})
    assert res.startswith("started pid")
    assert wait_for(lambda: out.exists() and out.read_text() == "enter S01", timeout=10)


def test_matlab_device_with_fake_engine(monkeypatch):
    calls = []

    class Eng:
        def eval(self, code, nargout=0):
            calls.append(code)

        def cd(self, path, nargout=0):
            calls.append(f"cd {path}")

    engine_mod = types.ModuleType("matlab.engine")
    engine_mod.find_matlab = lambda: ("mavric",)
    engine_mod.connect_matlab = lambda name: Eng()
    matlab_mod = types.ModuleType("matlab")
    matlab_mod.engine = engine_mod
    monkeypatch.setitem(sys.modules, "matlab", matlab_mod)
    monkeypatch.setitem(sys.modules, "matlab.engine", engine_mod)
    dev = MatlabDevice("m", {"session": "mavric"})
    dev.open()
    dev.execute("call", {"function": "reward", "args": "'{pigeon}', {t}"}, {"pigeon": "S03", "t": 4.5})
    dev.execute("eval", {"code": "disp('{roi}')"}, {"roi": "PerchD"})
    assert calls == ["reward('S03', 4.5);", "disp('PerchD')"]
    with pytest.raises(RuntimeError, match="shareEngine"):
        MatlabDevice("m2", {"session": "other"}).open()


# --------------------------------------------------------------------------- faster RFID events


def test_presence_round_closes_on_idle():
    pt = PresenceTracker(4, off_rounds=2)
    assert pt.feed(1, "TAG", 2, S01, 10.000) == []
    assert pt.idle(10.002) == []  # still within the round's burst
    ev = pt.idle(10.006)  # 5 ms idle: round closed now, not 125 ms later
    assert [(e.reader, e.tag) for e in ev] == [(2, S01)]
    assert pt.feed(2, "TAG", 2, S01, 10.125) == []  # next round, same state
    assert pt.idle(10.140) == []


# --------------------------------------------------------------------------- end to end


def test_simulated_cage_drives_rules(tmp_path):
    """Real-time simulated cage -> presence events -> rules -> I/O emulator; logged in the session."""
    from mavric.acquire import simulation_controller
    from mavric.config import load_rig

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml")}
    OutputsConfig({"io": {"type": "arduino_io", "port": "SIM", "watchdog_s": 1.0}},
                  [Rule("any_perch", "io", "hold", {"pin": 30}, event="while"),
                   Rule("arrivals", "io", "pulse", {"pin": 31, "ms": 20})]).save(tmp_path / "outputs.yaml")
    ctl = simulation_controller(rig, seed=4)
    try:
        ctl.connect_rfid(ctl._sim_source)
        errors = ctl.start_outputs()
        assert errors == {}
        ctl.outputs.arm(ctl.presence())
        sess = ctl.start_recording("outputs", "test")
        time.sleep(8)
        sess = ctl.stop_recording()
    finally:
        ctl.shutdown()
    df = pd.read_csv(sess.file("actions.csv"))
    ok = df[df["status"] == "ok"]
    assert (ok["rule"] == "arrivals").sum() >= 1 and (ok["rule"] == "any_perch").sum() >= 1
    assert ok["device_us"].notna().all()
    assert ok["latency_ms"].max() < 200
