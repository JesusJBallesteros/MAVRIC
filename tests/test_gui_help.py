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


def test_acquire_panels_menus_and_saved_arrangement(app, tmp_path):
    from pathlib import Path

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import PANELS, AcquireWindow
    from mavric.gui.common import HANDLE_RADIUS

    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    layout = Path(rig["paths"]["gui_layout_file"])
    assert layout.parent.name == "config" and not layout.exists()
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    menus = {a.text(): [b.text() for b in a.menu().actions() if b.text()] for a in win.menuBar().actions()}
    assert menus["Settings"] == ["Video settings…", "Calibrate cameras…", "Outputs…"]
    assert menus["Help"] == ["The basics", "Manual (README)", "About MAVRIC"]
    assert [PANELS[n][0] for n in win.panels] == menus["Panels"][:len(PANELS)]
    assert not win.panels["audio"].toggleViewAction().isEnabled()  # until audio connects
    win.show()
    app.processEvents()
    assert win.panels["rois"].windowTitle() == "ROIs and LED" and win.cb_edit.text() == "Edit ROIs"
    rois = win.panels["rois"]
    full = rois.height()
    rois.set_collapsed(True)
    app.processEvents()
    assert rois.height() < 40 < full and not win.cb_edit.isVisible()
    assert rois.width() == win.panels["devices"].width()  # a collapsed panel keeps the column's width
    win.close()  # saves the arrangement for this rig
    assert layout.is_file()
    win = AcquireWindow(simulation_controller(rig, seed=1), sim=True)
    assert win.panels["rois"].collapsed and not win.panels["devices"].collapsed
    win.arrange_panels()
    assert not any(p.collapsed for p in win.panels.values())
    win._new_roi(1)
    win._new_roi(None)
    poly = win.video.rois[win.antennas[1]].item
    a, b = (poly.handles[i]["item"].pos() for i in (0, 1))
    poly.segmentClicked(poly.segments[0], pos=(a + b) / 2)  # a corner added on an edge
    assert len(poly.handles) == 5
    for g in win.video.rois.values():  # corners easy to grab
        assert all(h["item"].radius == HANDLE_RADIUS for h in g.item.handles)
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
