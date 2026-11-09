"""Move finished sessions from the recording drive to an archive folder.

Recording wants a fast drive (an SSD); keeping recordings wants a big one (a hard disk, a NAS, a server share).
With an archive folder set (rig.yaml `paths.archive_dir`, or Settings → Storage… in the acquire window), each
session is moved there after Stop, in the background:

1. every file is copied to ``<archive>/<session>.partial/``, its SHA-256 taken while reading it;
2. the copy is read back and its SHA-256 compared; a file that changed while it was copied is copied again;
3. ``archive.json`` lists every file with its size, time stamp and SHA-256; the folder is renamed to
   ``<archive>/<session>/``;
4. only then are the files deleted from the recording folder (unless `keep_local`). A file that changed after it
   was copied stays, with session.json, and moves the next time.

A copy that stops half-way (the acquire window closed, a power cut, the archive drive full) keeps what is
checked: the next Stop, Settings → Storage… or ``mavric archive`` resumes it. Sessions open anywhere: session.json
names its files relative to its own folder.
"""

from __future__ import annotations

import collections
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

ARCHIVE_FILE = "archive.json"
PARTIAL = ".partial"
BLOCK = 8 << 20  # bytes per read
SPARE_BYTES = 1 << 30  # free space kept on the archive drive


class ArchiveError(RuntimeError):
    pass


class ArchiveStopped(ArchiveError):
    pass


# --------------------------------------------------------------------------- one session


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _skip(name: str) -> bool:
    """Temporary files: ours, session.json's, and mavric compress's <name>.part.mp4."""
    return name == ARCHIVE_FILE or name.endswith((".tmp", ".part")) or ".part." in name


def _scan(folder: Path) -> dict[str, os.stat_result]:
    out = {}
    for d, _dirs, files in os.walk(folder):
        for n in files:
            if not _skip(n):
                p = Path(d) / n
                out[p.relative_to(folder).as_posix()] = p.stat()
    return out


def _same(entry: dict | None, st: os.stat_result) -> bool:
    return bool(entry) and entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns


def _read_manifest(folder: Path) -> dict | None:
    try:
        with open(folder / ARCHIVE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_manifest(folder: Path, man: dict) -> None:
    tmp = folder / (ARCHIVE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    os.replace(tmp, folder / ARCHIVE_FILE)


def _sha256(path: Path, stop: threading.Event | None, tick) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(BLOCK):
            if stop is not None and stop.is_set():
                raise ArchiveStopped("stopped")
            h.update(b)
            tick(len(b))
    return h.hexdigest()


def _copy_checked(src: Path, dst: Path, stop: threading.Event | None, tick) -> str:
    """Copy via <dst>.part, read the copy back, and compare SHA-256s; returns the SHA-256."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while b := fi.read(BLOCK):
                if stop is not None and stop.is_set():
                    raise ArchiveStopped("stopped")
                h.update(b)
                fo.write(b)
                tick(len(b))
            fo.flush()
            os.fsync(fo.fileno())
        sha = h.hexdigest()
        if _sha256(tmp, stop, tick) != sha:
            raise ArchiveError(f"{dst.name}: the copy reads back different from the original")
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
        return sha
    finally:
        tmp.unlink(missing_ok=True)


def destination(session: str | Path, archive_root: str | Path) -> Path:
    return Path(archive_root).expanduser() / Path(session).name


def _check(src: Path, root: Path, unfinished: bool) -> dict:
    try:
        with open(src / "session.json", encoding="utf-8") as f:
            meta = json.load(f)
    except OSError:
        raise ArchiveError(f"{src}: not a session folder (no session.json)") from None
    except ValueError as e:
        raise ArchiveError(f"{src}: session.json is unreadable ({e})") from None
    if not meta.get("stop_utc") and not unfinished:
        raise ArchiveError(f"{src.name}: still recording, or it stopped without a stop time (a crash); "
                           "mavric archive --unfinished moves it anyway")
    if root == src or root.is_relative_to(src) or root == src.parent:
        raise ArchiveError(f"the archive folder {root} is the recording folder, or inside the session")
    return meta


def archive_session(src: str | Path, archive_root: str | Path, keep_local: bool = False, progress=None,
                    stop: threading.Event | None = None, unfinished: bool = False) -> dict:
    """Copy a session folder to <archive_root>/<name>/, check every file, then delete it from the recording folder.

    `progress(info)` gets {"phase": "copy", "file", "done", "total"} in bytes of work (each copied file counts twice:
    written, then read back). Raises ArchiveError (nothing deleted) or ArchiveStopped (resumable)."""
    src = Path(src).expanduser().resolve()
    root = Path(archive_root).expanduser().resolve()
    _check(src, root, unfinished)
    final, partial = root / src.name, root / (src.name + PARTIAL)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ArchiveError(f"cannot create the archive folder {root}: {e}") from None
    work = final if final.is_dir() else partial
    work.mkdir(exist_ok=True)
    man = _read_manifest(work) or {}
    man.update(session=src.name, mavric=__version__, complete=False)
    man.setdefault("started_utc", _utc())
    man.setdefault("files", {})
    man.setdefault("moved_from", [])
    if str(src) not in man["moved_from"]:
        man["moved_from"].append(str(src))
    t0 = time.perf_counter()
    done = total = 0
    copied, n_bytes, changing = [], 0, []

    def tick(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress({"phase": "copy", "file": cur, "done": done, "total": total})

    for _ in range(3):  # files that change while they are copied get copied again
        files = _scan(src)
        todo = {rel: st for rel, st in files.items()
                if not (_same(man["files"].get(rel), st) and (work / rel).is_file()
                        and (work / rel).stat().st_size == st.st_size)}
        if not todo:
            break
        need = sum(st.st_size for st in todo.values())
        free = shutil.disk_usage(root).free
        if need + SPARE_BYTES > free:
            raise ArchiveError(f"the archive drive has {free / 1e9:.1f} GB free; {src.name} needs {need / 1e9:.1f} GB")
        total += 2 * need
        changing = []
        for rel in sorted(todo, key=lambda r: (r == "session.json", r)):  # session.json last
            cur, st = rel, todo[rel]
            try:
                sha = _copy_checked(src / rel, work / rel, stop, tick)
            except FileNotFoundError:  # deleted since the scan
                continue
            except OSError as e:
                raise ArchiveError(f"{rel}: {e}") from None
            after = (src / rel).stat()
            if after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                changing.append(rel)  # written to while it was copied: again in the next pass
                continue
            man["files"][rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha}
            copied.append(rel)
            n_bytes += st.st_size
            _write_manifest(work, man)
        if not changing:
            break
    man.update(complete=True, finished_utc=_utc(), files=dict(sorted(man["files"].items())))
    _write_manifest(work, man)
    if work is partial:
        for attempt in range(20):  # Windows: a virus scanner or indexer may hold a file in it for a moment
            try:
                os.replace(partial, final)
                break
            except PermissionError:
                if attempt == 19:
                    raise ArchiveError(f"cannot rename {partial.name} to {final.name}: in use") from None
                time.sleep(0.25)
    kept = [] if keep_local else delete_local(src, man)
    return {"session": src.name, "to": str(final), "files": len(man["files"]), "copied": len(copied),
            "bytes": n_bytes, "seconds": time.perf_counter() - t0, "local": "kept" if keep_local else
            ("partly kept" if kept else "deleted"), "kept": kept, "changing": changing}


def delete_local(src: Path, man: dict) -> list[str]:
    """Delete the recording folder's files that the archive holds unchanged; session.json goes last, and only when
    nothing else is left. Returns what stays (changed since the copy, open in another program, or new)."""
    kept = []
    for rel, e in man["files"].items():
        if rel == "session.json":
            continue
        p = src / rel
        try:
            if _same(e, p.stat()):
                p.unlink()
            else:
                kept.append(rel)
        except FileNotFoundError:
            pass
        except OSError:
            kept.append(rel)
    for d, _dirs, _files in sorted(os.walk(src), key=lambda w: -len(w[0])):  # empty folders, deepest first
        if Path(d) != src:
            try:
                Path(d).rmdir()
            except OSError:
                pass
    left = [rel for rel in _scan(src) if rel != "session.json"]
    kept = sorted(set(kept) | set(left))
    if kept:
        return kept
    js = src / "session.json"
    try:
        if not js.exists() or _same(man["files"].get("session.json"), js.stat()):
            js.unlink(missing_ok=True)
            for p in src.iterdir():  # leftovers of ours (session.json.tmp, *.part)
                if p.is_file() and _skip(p.name):
                    p.unlink()
            src.rmdir()
            return []
    except OSError:
        pass
    return ["session.json"]


# --------------------------------------------------------------------------- what is where


def session_size(folder: Path) -> int:
    return sum(st.st_size for st in _scan(folder).values())


def state(src: Path, archive_root: str | Path | None) -> str:
    """unfinished | waiting | partly copied | archived (local copy kept) | changed since archived | no archive"""
    meta = {}
    try:
        with open(src / "session.json", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        pass
    if not meta.get("stop_utc"):
        return "unfinished"
    if not archive_root:
        return "no archive"
    root = Path(archive_root).expanduser()
    man = _read_manifest(root / src.name)
    if man and man.get("complete"):
        files = _scan(src)
        return ("archived (local copy kept)" if all(_same(man["files"].get(r), st) for r, st in files.items())
                else "changed since archived")
    if (root / (src.name + PARTIAL)).is_dir():
        return "partly copied"
    return "waiting"


def local_sessions(sessions_dir: str | Path) -> list[Path]:
    d = Path(sessions_dir).expanduser()
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_dir() and (p / "session.json").is_file())


def interrupted(sessions_dir: str | Path, archive_root: str | Path) -> list[Path]:
    """Sessions of the recording folder whose copy to the archive stopped half-way."""
    root = Path(archive_root).expanduser()
    return [p for p in local_sessions(sessions_dir) if (root / (p.name + PARTIAL)).is_dir()]


# --------------------------------------------------------------------------- in the background


def _background_io() -> None:
    """Low disk and memory priority for this thread (Windows), so the recording's writes go first. The CPU priority
    stays: a low-priority thread holding Python's lock would hold up the recorder threads."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), 0x00010000)  # THREAD_MODE_BACKGROUND_BEGIN
    except Exception:
        pass


class Archiver:
    """Moves sessions one after the other in a background thread."""

    def __init__(self):
        self._q: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self._running = False  # a worker is taking jobs; set and cleared under the lock, so no job is left behind
        self.current: dict | None = None
        self.results: list[dict] = []  # newest last: archive_session()'s result, or {"session", "error"}

    def submit(self, src: str | Path, archive_root: str | Path, keep_local: bool = False,
               unfinished: bool = False) -> bool:
        """Queue a session; False when it is queued or moving already."""
        src = Path(src)
        with self._lock:
            names = [j[0].name for j in self._q] + ([self.current["session"]] if self.current else [])
            if src.name in names:
                return False
            self._q.append((src, Path(archive_root), keep_local, unfinished))
            self._stop.clear()
            if not self._running:
                self._running = True
                self._th = threading.Thread(target=self._run, daemon=True, name="archiver")
                self._th.start()
        return True

    @property
    def busy(self) -> bool:
        return self._running

    def status(self) -> dict:
        with self._lock:
            return {"busy": self.busy, "current": dict(self.current) if self.current else None,
                    "queued": [j[0].name for j in self._q], "last": dict(self.results[-1]) if self.results else None,
                    "n_results": len(self.results)}

    def stop(self, timeout: float | None = 10.0) -> None:
        """Stop after the block being copied; the half-done session resumes later."""
        with self._lock:
            self._q.clear()
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        if self._th is not None:
            self._th.join(timeout)
        return not self.busy

    def _run(self) -> None:
        _background_io()
        while True:
            with self._lock:
                if self._stop.is_set() or not self._q:
                    self._running = False
                    self.current = None
                    return
                src, root, keep, unfinished = self._q.popleft()
                self.current = {"session": src.name, "to": str(destination(src, root)), "done": 0, "total": 0,
                                "file": ""}

            def progress(info, cur=self.current):
                cur.update(done=info["done"], total=info["total"], file=info["file"])

            try:
                res = archive_session(src, root, keep, progress, self._stop, unfinished)
            except ArchiveStopped:
                res = {"session": src.name, "to": str(destination(src, root)), "stopped": True,
                       "error": "stopped; it resumes at the next Stop or with mavric archive"}
            except Exception as e:  # nothing deleted: the session stays in the recording folder
                res = {"session": src.name, "to": str(destination(src, root)), "error": str(e)}
            with self._lock:
                self.results.append(res)
                self.current = None
