"""Name, version, build (git commit), author and licence, for the About window and `mavric --version`."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from . import __version__

NAME = "MAVRIC"
FULL_NAME = "Multimodal Audio-Video-RFID Interaction and Control"
DESCRIPTION = ("A behavioural suite: synchronized video, audio and RFID identification of individual animals, "
               "with event-driven outputs for task control and interaction")
AUTHOR = "Jesus J. Ballesteros"
AFFILIATION = "Neural Basis of Learning Lab, Institute of Cognitive Neuroscience (IKN), Ruhr-Universität Bochum"
COPYRIGHT_YEAR = "2026"
LICENSE = "MIT"
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(SOURCE_ROOT), *args], capture_output=True, text=True, timeout=3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _git_dir_commit() -> str | None:
    """Commit hash from .git without the git program (lab PCs often lack it)."""
    g = SOURCE_ROOT / ".git"
    try:
        head = (g / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(" ", 1)[1]
        p = g / ref
        if p.exists():
            return p.read_text().strip()
        packed = g / "packed-refs"
        for line in packed.read_text().splitlines() if packed.exists() else []:
            if line.endswith(" " + ref):
                return line.split(" ")[0]
    except OSError:
        return None
    return None


def build_info() -> dict:
    commit = _git("rev-parse", "HEAD") or _git_dir_commit()
    info = {"version": __version__, "commit": commit[:7] if commit else None, "commit_full": commit,
            "date": _git("log", "-1", "--format=%cd", "--date=short") if commit else None,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"), "modified": None}
    status = _git("status", "--porcelain", "--untracked-files=no")
    if status is not None:
        info["modified"] = bool(status)
    return info


def version_string() -> str:
    b = build_info()
    s = f"{NAME} {b['version']}"
    if b["commit"]:
        s += f" (commit {b['commit']}" + (f", {b['date']}" if b["date"] else "") + \
             (", with local changes" if b["modified"] else "") + ")"
    return s


def environment() -> str:
    parts = [f"Python {platform.python_version()}", platform.platform(terse=True)]
    try:
        from PySide6 import QtCore

        parts.append(f"Qt {QtCore.qVersion()}")
    except Exception:
        pass
    return " · ".join(parts)
