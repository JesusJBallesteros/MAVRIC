"""Shared runtime pieces: monotonic clock, event bus, crash-safe CSV appender."""

from __future__ import annotations

import csv
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


def now() -> float:
    """Host monotonic time in seconds (QueryPerformanceCounter on Windows).

    Every frame and every serial line is stamped with this clock, so all
    streams of one acquisition share a single time base.
    """
    return time.perf_counter()


def clock_anchor() -> dict:
    """Pair the monotonic clock with wall-clock UTC (taken back to back)."""
    t_perf = time.perf_counter()
    t_utc_ns = time.time_ns()
    t_perf2 = time.perf_counter()
    return {"perf_s": (t_perf + t_perf2) / 2, "utc_ns": t_utc_ns}


class EventBus:
    """Minimal thread-safe publish/subscribe.

    Callbacks run in the publisher's thread; GUI subscribers must hop to their
    own thread (e.g. via a Qt signal).
    Topics used: "presence" (RFID presence changes), "rfid_line", "status".
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, topic: str, cb: Callable[[Any], None]) -> None:
        with self._lock:
            self._subs[topic].append(cb)

    def unsubscribe(self, topic: str, cb: Callable[[Any], None]) -> None:
        with self._lock:
            if cb in self._subs[topic]:
                self._subs[topic].remove(cb)

    def publish(self, topic: str, payload: Any) -> None:
        with self._lock:
            subs = list(self._subs[topic])
        for cb in subs:
            try:
                cb(payload)
            except Exception as e:  # a faulty subscriber must not kill acquisition
                print(f"[EventBus] subscriber error on {topic!r}: {e}")


class CsvAppender:
    """Append rows to a CSV and flush at least every `flush_s` seconds.

    Rows are on disk within ~1 s, so a crash or power cut loses almost nothing.
    """

    def __init__(self, path: str | Path, columns: list[str], flush_s: float = 1.0):
        self.path = Path(path)
        self.columns = columns
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(columns)
        self._flush_s = flush_s
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self.n_rows = 0

    def write(self, row: list | tuple) -> None:
        with self._lock:
            if self._f.closed:
                return
            self._w.writerow(row)
            self.n_rows += 1
            t = time.monotonic()
            if t - self._last_flush >= self._flush_s:
                self._f.flush()
                self._last_flush = t

    def close(self) -> None:
        with self._lock:
            if not self._f.closed:
                self._f.flush()
                self._f.close()
