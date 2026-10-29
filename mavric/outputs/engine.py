"""ActionEngine: evaluates output rules on live events and drives the devices.

Inputs: RFID presence changes from the event bus (mavric.rfid.serial_reader.Presence).
Rule events:
  enter  a matching tag appears at one of the rule's antennas
  leave  a matching tag leaves one of the rule's antennas
  while  "start" when the first matching tag is present, "stop" when the last
         one has left (hold actions keep an output on in between)
Each rule has a refractory time, an optional delay and a max firings/minute.

Execution: one worker thread per device, so a slow device never delays another
one or the acquisition. Nothing is driven unless the engine is *armed*; while
disarmed the rules still evaluate and log as "dry". Disarming, stopping and
device errors put every device into its safe state (held outputs off).

Every decision is logged (memory ring for the GUI, and `actions.csv` in the
session while recording) with event time, send time, result and the device's
own timestamp where it reports one (I/O Arduino micros).
"""

from __future__ import annotations

import heapq
import json
import queue
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import TagMap
from ..core import CsvAppender, EventBus, now
from .config import HOLD_ACTIONS, OutputsConfig, Rule
from .devices import OutputDevice, make_device

ACTION_COLUMNS = ["t", "event_t", "rule", "event", "source", "antenna", "roi", "pigeon", "tag", "device", "action",
                  "params", "status", "result", "latency_ms", "device_us"]
ACTIONS_FILE = "actions.csv"


@dataclass
class _Job:
    rule: Rule
    event: str  # enter | leave | start | stop
    ctx: dict
    event_abs: float
    due_abs: float
    test: bool = False  # Test button: runs even when disarmed


@dataclass
class _RuleState:
    rule: Rule
    active: set  # {(antenna, tag)} for while-rules
    started: bool = False
    last_fire: float = -1e18
    fires: deque = None  # firing times for max_per_min
    count: int = 0
    suppressed: int = 0
    last_status: str = ""

    def __post_init__(self):
        if self.fires is None:
            self.fires = deque()


class ActionEngine:
    def __init__(self, cfg: OutputsConfig, tags: TagMap, antennas: dict[int, str], bus: EventBus | None = None,
                 devices: dict[str, OutputDevice] | None = None, resources: dict | None = None):
        """resources: live objects for devices that need them, e.g. {"player": callable -> audio Player}."""
        cfg.validate()
        self.cfg = cfg
        self.tags = tags
        self.antennas = antennas
        self.bus = bus
        self.devices: dict[str, OutputDevice] = devices or {n: make_device(n, d, resources)
                                                            for n, d in cfg.devices.items()}
        self.states = {r.name: _RuleState(r, set()) for r in cfg.rules}
        self.armed = False
        self.log: deque = deque(maxlen=500)
        self.device_errors: dict[str, str] = {}
        self._writer: CsvAppender | None = None
        self._t0 = 0.0
        self._session_name = ""
        self._lock = threading.RLock()
        self._timers: list = []  # heap of (due, seq, job)
        self._seq = 0
        self._wake = threading.Condition(self._lock)
        self._queues: dict[str, queue.Queue] = {}
        self._threads: list[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> dict[str, str]:
        """Open devices (errors are recorded, not raised) and start the threads."""
        for name, dev in self.devices.items():
            try:
                dev.open()
                if hasattr(dev, "on_event"):
                    dev.on_event = lambda kind, t, us, ident, n=name: self._device_event(n, kind, t, us, ident)
            except Exception as e:
                self.device_errors[name] = f"{type(e).__name__}: {e}"
        self._running = True
        for name in self.devices:
            q: queue.Queue = queue.Queue()
            self._queues[name] = q
            th = threading.Thread(target=self._worker, args=(name, q), daemon=True, name=f"out-{name}")
            th.start()
            self._threads.append(th)
        th = threading.Thread(target=self._scheduler, daemon=True, name="out-scheduler")
        th.start()
        self._threads.append(th)
        if self.bus:
            self.bus.subscribe("presence", self.on_presence)
        return dict(self.device_errors)

    def stop(self) -> None:
        if self.bus:
            self.bus.unsubscribe("presence", self.on_presence)
        self.disarm()
        with self._lock:
            self._running = False
            self._timers.clear()
            self._wake.notify_all()
        for q in self._queues.values():
            q.put(None)
        for th in self._threads:
            th.join(timeout=3)
        for name, dev in self.devices.items():
            try:
                dev.close()
            except Exception as e:
                self.device_errors[name] = f"close: {e}"
        self.detach_session()

    # ------------------------------------------------------------------ arming
    def arm(self, presence: dict[int, str] | None = None) -> None:
        """Start driving devices. Subjects already present start their while-rules now."""
        with self._lock:
            self.armed = True
            t = now()
            for st in self.states.values():
                st.active.clear()
                st.started = False
            for reader, tag in (presence or {}).items():
                if tag:
                    self._handle(reader, tag, "", t)
        self._note("armed")

    def disarm(self) -> None:
        with self._lock:
            was = self.armed
            self.armed = False
            self._timers.clear()
            for st in self.states.values():
                st.active.clear()
                st.started = False
        for name, dev in self.devices.items():
            if name in self.device_errors and not dev.opened:
                continue
            try:
                dev.safe_state()
            except Exception as e:
                self.device_errors[name] = f"safe_state: {e}"
        if was:
            self._note("disarmed: all held outputs released")

    # ------------------------------------------------------------------ session logging
    def attach_session(self, sess, t0_abs: float) -> None:
        with self._lock:
            self._t0 = t0_abs
            self._session_name = sess.path.name
            self._writer = CsvAppender(sess.file(ACTIONS_FILE), ACTION_COLUMNS)
        self.cfg.save(sess.file("outputs.yaml"))

    def detach_session(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None
            self._t0 = 0.0
            self._session_name = ""

    # ------------------------------------------------------------------ events
    def on_presence(self, p) -> None:
        with self._lock:
            if p.prev_tag:
                self._handle(p.reader, "", p.prev_tag, p.host_abs)
            if p.tag:
                self._handle(p.reader, p.tag, "", p.host_abs)

    def _matches(self, r: Rule, reader: int, tag: str) -> bool:
        if not r.enabled or r.source != "rfid":
            return False
        if r.antennas is not None and reader not in r.antennas:
            return False
        if r.who is None:
            return True
        return tag.upper() in {w.upper() for w in r.who} or self.tags.name(tag) in r.who

    def _handle(self, reader: int, tag: str, prev_tag: str, t: float) -> None:
        """One tag arriving (tag) or leaving (prev_tag) at one antenna."""
        arriving = bool(tag)
        who_tag = tag or prev_tag
        for st in self.states.values():
            r = st.rule
            if not self._matches(r, reader, who_tag):
                continue
            ctx = self._ctx(r, reader, who_tag, t)
            if r.event == "enter" and arriving:
                self._fire(st, "enter", ctx, t)
            elif r.event == "leave" and not arriving:
                self._fire(st, "leave", ctx, t)
            elif r.event == "while":
                if arriving:
                    st.active.add((reader, who_tag))
                    if not st.started:
                        self._fire(st, "start", ctx, t)
                else:
                    st.active.discard((reader, who_tag))
                    if not st.active and st.started:
                        self._fire(st, "stop", ctx, t, force=True)

    def _ctx(self, r: Rule, reader: int, tag: str, t: float) -> dict:
        ctx = {"rule": r.name, "antenna": reader, "roi": self.antennas.get(reader, str(reader)), "tag": tag,
                "subject": self.tags.name(tag) if tag else "", "t": t - self._t0 if self._t0 else t,
                "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "session": self._session_name}
        ctx["pigeon"] = ctx["subject"]  # {pigeon}: the template name before 0.8
        return ctx

    def _fire(self, st: _RuleState, event: str, ctx: dict, t: float, force: bool = False) -> None:
        r = st.rule
        if not force:
            while st.fires and t - st.fires[0] > 60:
                st.fires.popleft()
            reason = None
            if t - st.last_fire < r.refractory_s:
                reason = f"refractory ({r.refractory_s:g} s)"
            elif r.max_per_min and len(st.fires) >= r.max_per_min:
                reason = f"max {r.max_per_min}/min"
            if reason:
                st.suppressed += 1
                self._log(t, None, r, event, ctx, "suppressed", reason)
                return
            st.last_fire = t
            st.fires.append(t)
        if event == "start":
            st.started = True
        elif event == "stop":
            st.started = False
        st.count += 1
        ctx = {**ctx, "event": event}
        job = _Job(r, event, ctx, t, t + r.delay_s)
        if not self.armed:
            self._log(t, None, r, event, ctx, "dry", "not armed")
            st.last_status = "dry"
            return
        if r.delay_s > 0:
            self._seq += 1
            heapq.heappush(self._timers, (job.due_abs, self._seq, job))
            self._wake.notify_all()
        else:
            self._dispatch(job)

    def _dispatch(self, job: _Job) -> None:
        q = self._queues.get(job.rule.device)
        if q is None:
            self._log(job.event_abs, None, job.rule, job.event, job.ctx, "error", "device not started")
            return
        q.put(job)

    def _scheduler(self) -> None:
        with self._lock:
            while self._running:
                if not self._timers:
                    self._wake.wait(0.5)
                    continue
                due = self._timers[0][0]
                left = due - now()
                if left > 0:
                    self._wake.wait(min(left, 0.5))
                    continue
                _, _, job = heapq.heappop(self._timers)
                if self.armed or job.test:
                    self._dispatch(job)

    def _worker(self, name: str, q: queue.Queue) -> None:
        dev = self.devices[name]
        while True:
            job = q.get()
            if job is None:
                return
            r = job.rule
            sent = now()
            if name in self.device_errors and not dev.opened:
                self._log(job.event_abs, sent, r, job.event, job.ctx, "error", self.device_errors[name])
                continue
            try:
                result = dev.execute(r.action, r.params, job.ctx)
                status = "ok"
            except Exception as e:
                result, status = f"{type(e).__name__}: {e}", "error"
            self.states[r.name].last_status = status
            self._log(job.event_abs, sent, r, job.event, job.ctx, status, result,
                      getattr(dev, "last_device_us", None))

    # ------------------------------------------------------------------ manual tests
    def test_device(self, name: str) -> str:
        return self.devices[name].test()

    def fire_now(self, rule_name: str, event: str | None = None) -> None:
        """Run a rule's action once (Test button), even when disarmed; bypasses refractory."""
        st = self.states[rule_name]
        r = st.rule
        ev = event or ("start" if r.event == "while" else r.event)
        t = now()
        ctx = {**self._ctx(r, (r.antennas or [1])[0], "TEST", t), "pigeon": (r.who or ["TEST"])[0], "event": ev}
        self._dispatch(_Job(r, ev, ctx, t, t, test=True))
        if r.action in HOLD_ACTIONS and ev == "start":  # release a held output again after 1 s
            stop_ctx = {**ctx, "event": "stop"}
            with self._lock:
                self._seq += 1
                heapq.heappush(self._timers, (t + 1.0, self._seq, _Job(r, "stop", stop_ctx, t, t + 1.0, test=True)))
                self._wake.notify_all()

    # ------------------------------------------------------------------ logging / status
    def _log(self, event_abs, sent_abs, r: Rule, event, ctx, status, result, device_us=None) -> None:
        t = sent_abs if sent_abs is not None else event_abs
        lat = (sent_abs - event_abs) * 1e3 if sent_abs is not None else None
        row = {
            "t": t - self._t0 if self._t0 else t, "event_t": event_abs - self._t0 if self._t0 else event_abs,
            "rule": r.name, "event": event, "source": r.source, "antenna": ctx.get("antenna", ""),
            "roi": ctx.get("roi", ""), "pigeon": ctx.get("pigeon", ""), "tag": ctx.get("tag", ""),
            "device": r.device, "action": r.action, "params": json.dumps(r.params, separators=(",", ":")),
            "status": status, "result": result, "latency_ms": "" if lat is None else f"{lat:.2f}",
            "device_us": "" if device_us is None else device_us,
        }
        self.log.append(row)
        with self._lock:
            if self._writer is not None:
                self._writer.write([row[c] if isinstance(row[c], str) else
                                    (f"{row[c]:.6f}" if isinstance(row[c], float) else row[c]) for c in ACTION_COLUMNS])
        if self.bus:
            self.bus.publish("action", row)

    def _device_event(self, name, kind, t, us, ident) -> None:
        row = {"t": t - self._t0 if self._t0 else t, "event_t": t - self._t0 if self._t0 else t, "rule": "",
               "event": kind.lower(), "source": "device", "antenna": "", "roi": "", "pigeon": "", "tag": "",
               "device": name, "action": "", "params": "", "status": "info" if kind != "WDOG" else "warning",
               "result": f"{kind} id={ident}", "latency_ms": "", "device_us": us}
        self.log.append(row)
        with self._lock:
            if self._writer is not None:
                self._writer.write([row[c] if isinstance(row[c], str) else
                                    (f"{row[c]:.6f}" if isinstance(row[c], float) else row[c]) for c in ACTION_COLUMNS])

    def log_manual(self, device: str, action: str, params: dict, result: str, status: str = "ok") -> None:
        """An action taken by hand (the soundboard), logged like a rule's: event and source "manual"."""
        t = now()
        rel = t - self._t0 if self._t0 else t
        row = {"t": rel, "event_t": rel, "rule": "", "event": "manual", "source": "manual", "antenna": "", "roi": "",
               "pigeon": "", "tag": "", "device": device, "action": action,
               "params": json.dumps(params, separators=(",", ":")), "status": status, "result": result,
               "latency_ms": "", "device_us": ""}
        self.log.append(row)
        with self._lock:
            if self._writer is not None:
                self._writer.write([row[c] if isinstance(row[c], str) else
                                    (f"{row[c]:.6f}" if isinstance(row[c], float) else row[c]) for c in ACTION_COLUMNS])
        if self.bus:
            self.bus.publish("action", row)

    def _note(self, text: str) -> None:
        t = now()
        self.log.append({"t": t - self._t0 if self._t0 else t, "rule": "", "event": "note", "device": "",
                         "status": "info", "result": text})

    def status(self) -> dict:
        return {
            "armed": self.armed,
            "devices": {n: {"opened": d.opened, "error": self.device_errors.get(n) or d.error}
                        for n, d in self.devices.items()},
            "rules": {n: {"enabled": st.rule.enabled, "count": st.count, "suppressed": st.suppressed,
                          "active": st.started, "last": st.last_status, "desc": st.rule.describe()}
                      for n, st in self.states.items()},
        }
