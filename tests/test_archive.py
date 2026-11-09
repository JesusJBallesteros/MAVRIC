"""Record on a fast drive, keep on a big one: finished sessions moved to the archive folder, every file checked."""

import hashlib
import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def make_session(folder, finished=True, mb=3):
    """A session folder: session.json, two raw chunks, a frame table and a calibration snapshot."""
    folder.mkdir(parents=True)
    meta = {"name": folder.name, "stop_utc": "2026-11-09T10:01:00+00:00" if finished else None}
    (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    for k in range(2):
        (folder / f"cam0_raw_{k:03d}.mkv").write_bytes(os.urandom(mb << 20))
    (folder / "frames_cam0.csv").write_text("frame_idx\n0\n1\n", encoding="utf-8")
    (folder / "calibration").mkdir()
    (folder / "calibration" / "reference_cam0.png").write_bytes(b"png")
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_a_session_moves_and_every_file_is_checked(tmp_path):
    from mavric.archive import ARCHIVE_FILE, ArchiveError, archive_session, state

    rec, arch = tmp_path / "ssd", tmp_path / "hdd"
    src = rec / "20261109_100000_a"
    content = make_session(src)
    assert state(src, arch) == "waiting" and state(src, None) == "no archive"
    seen = []
    r = archive_session(src, arch, progress=seen.append)
    dst = arch / src.name
    assert r["local"] == "deleted" and r["copied"] == r["files"] == len(content) and not src.exists()
    assert {p.relative_to(dst).as_posix(): p.read_bytes() for p in dst.rglob("*")
            if p.is_file() and p.name != ARCHIVE_FILE} == content
    man = json.loads((dst / ARCHIVE_FILE).read_text(encoding="utf-8"))
    assert man["complete"] and set(man["files"]) == set(content)
    assert all(e["sha256"] == hashlib.sha256(content[rel]).hexdigest() for rel, e in man["files"].items())
    assert seen[-1]["done"] == seen[-1]["total"] == 2 * sum(map(len, content.values()))  # written, then read back
    assert not (arch / (src.name + ".partial")).exists()

    crashed = rec / "20261109_110000_b"
    make_session(crashed, finished=False)
    assert state(crashed, arch) == "unfinished"
    with pytest.raises(ArchiveError, match="--unfinished"):
        archive_session(crashed, arch)
    assert archive_session(crashed, arch, unfinished=True)["local"] == "deleted"


def test_a_stopped_copy_resumes_and_a_changed_file_moves_again(tmp_path):
    from mavric.archive import ArchiveStopped, archive_session, interrupted, state

    rec, arch = tmp_path / "ssd", tmp_path / "hdd"
    src = rec / "20261109_100000_a"
    content = make_session(src)
    stop = threading.Event()

    def progress(info):  # quit the acquire window during the second file
        if info["file"] != sorted(content)[0]:
            stop.set()

    with pytest.raises(ArchiveStopped):
        archive_session(src, arch, progress=progress, stop=stop)
    assert state(src, arch) == "partly copied" and interrupted(rec, arch) == [src]
    assert src.exists() and not (arch / src.name).exists()
    r = archive_session(src, arch, keep_local=True)
    assert r["copied"] == len(content) - 1  # the file checked before the stop is not copied again
    assert state(src, arch) == "archived (local copy kept)" and interrupted(rec, arch) == []

    (src / "frames_cam0.csv").write_text("frame_idx\n0\n1\n2\n", encoding="utf-8")  # analysed here meanwhile
    assert state(src, arch) == "changed since archived"
    r = archive_session(src, arch)
    assert r["copied"] == 1 and r["local"] == "deleted" and not src.exists()
    assert (arch / src.name / "frames_cam0.csv").read_text(encoding="utf-8").endswith("2\n")


def test_files_changed_after_the_copy_stay(tmp_path):
    from mavric.archive import archive_session, delete_local, _read_manifest

    src = tmp_path / "ssd" / "20261109_100000_a"
    make_session(src)
    archive_session(src, tmp_path / "hdd", keep_local=True)
    time.sleep(0.05)
    (src / "frames_cam0.csv").write_text("changed\n", encoding="utf-8")
    kept = delete_local(src, _read_manifest(tmp_path / "hdd" / src.name))
    assert kept == ["frames_cam0.csv"]
    assert sorted(p.name for p in src.iterdir()) == ["frames_cam0.csv", "session.json"]  # still a session


def test_the_archive_refuses_what_it_cannot_do(tmp_path, monkeypatch):
    import mavric.archive as A

    rec = tmp_path / "ssd"
    src = rec / "20261109_100000_a"
    make_session(src)
    for bad in (rec, src / "inside"):
        with pytest.raises(A.ArchiveError, match="recording folder"):
            A.archive_session(src, bad)
    with pytest.raises(A.ArchiveError, match="not a session"):
        A.archive_session(tmp_path, tmp_path / "hdd")
    monkeypatch.setattr(A.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 << 20})())
    with pytest.raises(A.ArchiveError, match="GB free"):
        A.archive_session(src, tmp_path / "hdd")
    assert len(list(src.rglob("*.*"))) == 5  # nothing deleted


def test_the_archiver_takes_every_session(tmp_path):
    from mavric.archive import Archiver

    arc = Archiver()
    names = [f"20261109_1000{k:02d}_s" for k in range(8)]
    for k, n in enumerate(names):  # submitted while the worker is finishing the one before, or idle
        make_session(tmp_path / "ssd" / n, mb=0)
        assert arc.submit(tmp_path / "ssd" / n, tmp_path / "hdd")
        time.sleep(0.002 * (k % 3))
    assert arc.wait(30) and not arc.busy
    assert sorted(r["session"] for r in arc.results if not r.get("error")) == names
    assert sorted(p.name for p in (tmp_path / "hdd").iterdir()) == names


def test_storage_settings_override_the_rig(example_rig):
    from mavric.config import load_rig
    from mavric.video.settings import VideoSettings

    rig = load_rig()
    assert rig["paths"]["archive_dir"] == "" and rig["archive"] == {"after_stop": True, "keep_local": False}
    VideoSettings(storage={"sessions_dir": "D:/rec", "archive_dir": "archive", "keep_local": True}).save(
        example_rig / "config" / "video.yaml")
    rig = load_rig()
    assert rig["paths"]["sessions_dir"].replace("\\", "/").endswith("D:/rec")
    assert rig["paths"]["archive_dir"] == str(example_rig / "archive")  # relative: to the rig folder
    assert rig["archive"]["keep_local"] is True and rig["archive"]["after_stop"] is True


def test_a_session_moves_after_stop(tmp_path):
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.storage_dialog import StorageDialog
    from mavric.video.settings import VideoSettings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rec, arch = tmp_path / "ssd", tmp_path / "hdd"
    rig["paths"] = {**rig["paths"], "sessions_dir": str(rec), "outputs_file": str(tmp_path / "o.yaml"),
                    "archive_dir": str(tmp_path / "rig_archive")}
    rec.mkdir()
    ctl = simulation_controller(rig, seed=1)
    assert ctl.archive_settings["dir"] == ""  # simulated sessions never go to the rig's archive by themselves
    win = AcquireWindow(ctl, sim=True)
    dlg = StorageDialog(win)
    dlg.arch_edit.setText(str(rec / "sub"))
    dlg.refresh()
    assert not dlg.b_all.isEnabled() and "inside the recording folder" in dlg.arch_info.text()
    dlg.arch_edit.setText(str(arch))
    dlg.save()
    assert VideoSettings.load(rec / "_sim_video.yaml").storage["archive_dir"] == str(arch)
    assert ctl.archive_settings == {"dir": str(arch), "after_stop": True, "keep_local": False}
    try:
        ctl.connect_rfid(ctl._sim_source)
        ctl.connect_camera(ctl.rig["cameras"][0])
        sess = ctl.start_recording("arch", "test")
        time.sleep(1.5)
        sess = ctl.stop_recording()
        assert ctl.archiver.wait(60)
        last = ctl.archiver.results[-1]
        assert not last.get("error") and last["local"] == "deleted", last
        assert not sess.path.exists() and (arch / sess.path.name / "archive.json").is_file()
        from mavric.session import Session

        moved = Session.open(arch / sess.path.name)  # opens where it landed
        assert moved.meta["stop_utc"] and moved.cameras[0]["id"] == "cam0"
        win._saved_path = str(sess.path)
        win._archive_seen = 0
        win.update_archive_label()
        assert "moved to" in win.archive_label.text() and str(arch) in win.rec_label.text()
        dlg.refresh()
        assert dlg.list.topLevelItemCount() == 0
    finally:
        ctl.shutdown()
        win.close()
        app.processEvents()


def test_mavric_archive_lists_and_moves(example_rig, tmp_path, capsys):
    from mavric.cli import main

    rec = example_rig / "sessions"
    make_session(rec / "20261109_100000_a")
    make_session(rec / "20261109_110000_b", finished=False)
    assert main(["archive", "--to", str(tmp_path / "hdd")]) == 0
    out = capsys.readouterr().out
    assert "20261109_100000_a" in out and "waiting" in out and "unfinished" in out
    assert main(["archive", "--all", "--to", str(tmp_path / "hdd")]) == 0
    out = capsys.readouterr().out
    assert "removed from the recording folder" in out
    assert not (rec / "20261109_100000_a").exists() and (rec / "20261109_110000_b").exists()
    assert main(["archive", "--all"]) == 2  # no archive folder set in the rig
