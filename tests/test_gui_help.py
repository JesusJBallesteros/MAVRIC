"""Every control has a hover tip; Help, README and About open; version info is complete."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_version_and_build_info():
    from mavric import __version__
    from mavric.about import LICENSE, build_info, version_string

    import tomllib

    from mavric.about import SOURCE_ROOT

    project = tomllib.loads((SOURCE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    b = build_info()
    assert b["version"] == __version__ == project["version"] and LICENSE == "MIT"
    assert version_string().startswith(f"MAVRIC {__version__}")
    if b["commit"]:
        assert len(b["commit"]) == 7


def test_acquire_and_dialogs_have_tips(app, tmp_path):
    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.outputs_dialog import ClipEditor, DeviceEditor, OutputsDialog, RuleEditor
    from mavric.gui.tips import missing_tips
    from mavric.gui.video_dialogs import CompressDialog, VideoSettingsDialog
    from mavric.outputs.config import Rule
    from mavric.triggers import TriggerRule

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml"),
                    "video_settings_file": str(tmp_path / "v.yaml")}
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    assert missing_tips(win) == []
    dialogs = [
        OutputsDialog(win, ctl), VideoSettingsDialog(win), CompressDialog(win, tmp_path, {}),
        DeviceEditor(win, "io", {"type": "arduino_io"}),
        DeviceEditor(win, "spk", {"type": "audio"}),
        RuleEditor(win, Rule("r", "sim", "mark", event="enter"), {"sim": {"type": "sim"}}, ctl.rig["antennas"],
                   ctl.tags),
        RuleEditor(win, Rule("p", "spk", "play", event="enter"), {"spk": {"type": "audio"}}, ctl.rig["antennas"],
                   ctl.tags, choices={"sound": ["test_tone"], "speakers": ["all", "spk1"]}),
        ClipEditor(win, TriggerRule("c", "cam0"), ctl.rig["cameras"], ctl.rig["antennas"], ctl.tags),
    ]
    for d in dialogs:
        assert missing_tips(d) == [], type(d).__name__
        d.close()
    win.close()


def test_review_has_tips_and_help(app, tmp_path):
    from mavric.gui.help_dialogs import AboutDialog, HelpDialog, ReadmeViewer
    from mavric.gui.review_window import ReviewWindow
    from mavric.gui.tips import missing_tips
    from mavric.sim import simulate_session

    sess = simulate_session(tmp_path, duration_s=30, seed=2, audio=True, write_labels=False)
    win = ReviewWindow(sess, "T")
    assert win.sound is not None
    assert missing_tips(win) == []
    for ctx in ("acquire", "review"):
        h = HelpDialog(win, ctx)
        assert missing_tips(h) == []
        h.close()
    r = ReadmeViewer(win)
    a = AboutDialog(win)
    assert missing_tips(r) == [] and missing_tips(a) == []
    from PySide6 import QtWidgets

    about_text = " ".join(lbl.text() for lbl in a.findChildren(QtWidgets.QLabel))
    from mavric import __version__

    assert __version__ in about_text and "MIT" in about_text and "Ballesteros" in about_text
    win.sound.close()
    win.dirty = win.sound.dirty = False
    win.close()
