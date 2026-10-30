"""Regenerate the README screenshots from simulated data (no hardware, no real recordings).

    python tools/make_screenshots.py            # writes docs/images/*.png

Windows are rendered offscreen; numbered callouts are drawn on the controls
the README describes.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
ACCENT = QtGui.QColor("#eb6834")


def pump(app, seconds: float) -> None:
    t_end = time.time() + seconds
    while time.time() < t_end:
        app.processEvents()
        time.sleep(0.02)


def annotate(window: QtWidgets.QWidget, items: list[tuple[QtWidgets.QWidget, int]], path: Path,
             scale: float = 1.0) -> None:
    """Grab `window` and draw numbered callouts on the given widgets."""
    pm = window.grab()
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    font = QtGui.QFont("Segoe UI", 11)
    font.setBold(True)
    p.setFont(font)
    for w, n in items:
        if w is None or not w.isVisible():
            continue
        tl = w.mapTo(window, QtCore.QPoint(0, 0))
        r = QtCore.QRect(tl, w.size()).adjusted(1, 1, -1, -1)
        p.setPen(QtGui.QPen(ACCENT, 2.5))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 6, 6)
        c = QtCore.QRect(r.right() - 22, r.top() - 4, 26, 26)  # top-right: keeps group titles readable
        p.setBrush(ACCENT)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(c)
        p.setPen(QtGui.QColor("white"))
        p.drawText(c, QtCore.Qt.AlignmentFlag.AlignCenter, str(n))
    p.end()
    if scale != 1.0:
        pm = pm.scaledToWidth(int(pm.width() * scale), QtCore.Qt.TransformationMode.SmoothTransformation)
    pm.save(str(path))
    print("wrote", path.relative_to(ROOT))


def neutral_paths(widget, tmp: Path) -> None:
    """Show config paths as a user would see them (config/...), not the temporary folder."""
    for lab in widget.findChildren(QtWidgets.QLabel):
        t = lab.text()
        if str(tmp) in t:
            for name in ("outputs.yaml", "video2.yaml", "video.yaml"):
                t = t.replace(str(tmp / name), "config/" + name.replace("video2", "video"))
            lab.setText(t.replace(str(tmp), "config"))


def group(window, title: str):
    return next((g for g in window.findChildren(QtWidgets.QGroupBox) if g.title() == title), None)


def button(window, text: str):
    return next((b for b in window.findChildren(QtWidgets.QAbstractButton) if b.text() == text), None)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    font = QtGui.QFont("Segoe UI", 9)
    font.setFamilies(["Segoe UI", "Segoe UI Symbol", "Segoe UI Emoji"])  # ✔ ⚡ ▶ need the symbol fonts
    app.setFont(font)
    OUT.mkdir(parents=True, exist_ok=True)
    (ROOT / "build").mkdir(exist_ok=True)  # inside the repo (git-ignored): no user paths in the pictures
    tmp = Path(tempfile.mkdtemp(prefix="screens_", dir=ROOT / "build"))
    shutil.copytree(ROOT / "config" / "examples", tmp / "config")  # the example rig, not the lab's own
    rig_file = tmp / "config" / "rig.yaml"
    os.environ["MAVRIC_ROOT"] = str(tmp)  # windows that call load_rig() themselves see the example rig too
    import numpy as np
    import soundfile as sf

    sr, rng = 48000, np.random.default_rng(1)  # a small demo stimulus library for the soundboard
    (tmp / "sounds" / "calls").mkdir(parents=True)
    t = np.arange(int(0.6 * sr)) / sr
    for name, f0 in (("calls/call_A", 620.0), ("calls/call_B", 480.0)):
        x = sum(np.sin(2 * np.pi * k * f0 * t) / k for k in (1, 2, 3)) * np.hanning(t.size) * 0.25
        sf.write(tmp / "sounds" / f"{name}.wav", x, sr)
    sf.write(tmp / "sounds" / "noise_burst.wav", rng.normal(0, 0.05, sr // 2), sr)

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import GRID, AcquireWindow
    from mavric.gui.outputs_dialog import OutputsDialog
    from mavric.gui.video_dialogs import VideoSettingsDialog
    from mavric.outputs.config import OutputsConfig, Rule

    # ------------------------------------------------------------------ acquire (simulated cage)
    rig = load_rig(rig_file)
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp / "sessions"), "outputs_file": str(tmp / "outputs.yaml"),
                    "video_settings_file": str(tmp / "video.yaml")}
    rig["cameras"] = [{"id": "cam0", "backend": "synthetic", "fps": 30},
                      {"id": "cam1", "backend": "synthetic", "fps": 30, "record": "triggered"}]
    OutputsConfig(
        {"io": {"type": "arduino_io", "port": "SIM", "baud": 115200, "watchdog_s": 2.0, "reset_wait_s": 0.0}},
        [Rule("ttl_placeB", "io", "hold", {"pin": 30}, antennas=[2], event="while"),
         Rule("led_arrivals", "io", "pulse", {"pin": 31, "ms": 200}, event="enter", refractory_s=2.0)],
        [{"name": "placeB_clip", "camera": "cam1", "antennas": [2], "match": "any", "pre_s": 3.0, "post_s": 5.0,
          "max_s": 300.0}]).save(tmp / "outputs.yaml")
    (tmp / "sessions").mkdir()
    ctl = simulation_controller(rig, seed=5)
    win = AcquireWindow(ctl, sim=True)
    win.resize(1500, 1600)
    win.show()
    ctl.connect_rfid(ctl._sim_source)
    for c in rig["cameras"]:
        ctl.connect_camera(c)
    ctl.connect_audio()
    win._build_audio_views()
    win._refresh_views(select="cam0")
    win.name_edit.setText("demo")
    win.sb_sound.setCurrentIndex(max(win.sb_sound.findData("calls/call_A"), 0))
    win.sb_speaker.setCurrentIndex(min(2, win.sb_speaker.count() - 1))
    win.sb_gain.setValue(-6.0)
    pump(app, 8)
    annotate(win, [(group(win, "Devices"), 1), (group(win, "Audio inputs"), 2), (group(win, "Soundboard"), 3),
                   (group(win, "Place ROIs (one per antenna) and sync LED"), 4), (group(win, "Live RFID"), 5),
                   (group(win, "Recording"), 6),
                   (group(win, "Outputs and camera clips (TTL, Intan, MATLAB, sounds, messages, clips)"), 7),
                   (win.video, 8), (win.spec, 9), (button(win, "Help"), 10)],
             OUT / "acquire.png", scale=0.8)

    dlg = OutputsDialog(win, ctl)
    dlg.resize(1000, 380)
    neutral_paths(dlg, tmp)
    tabs = dlg.findChild(QtWidgets.QTabWidget)
    for i, name in ((1, "outputs_rules.png"), (2, "outputs_clips.png")):
        tabs.setCurrentIndex(i)
        dlg.show()
        pump(app, 0.3)
        dlg.grab().save(str(OUT / name))
        print("wrote docs/images/" + name)
    dlg.close()

    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.video.calibration import calibrate_lens

    cd = CalibrationDialog(win)  # the synthetic camera shows a simulated board moved by hand
    cd.resize(1280, 760)
    cd.show()
    pump(app, 26)
    cd._calibrated(calibrate_lens(cd.collector.views, cd.spec, cd.cid))
    pump(app, 0.6)
    annotate(cd, [(cd.view, 1), (group(cd, "Board"), 2), (group(cd, "Views"), 3), (group(cd, "Lens"), 4)],
             OUT / "calibrate.png", scale=0.8)
    cd._save()
    cd.cam_combo.setCurrentIndex(cd.cam_combo.findData("cam1"))  # the second camera's lens, off screen
    pump(app, 26)
    cd._calibrated(calibrate_lens(cd.collector.views, cd.spec, cd.cid))
    cd._save()
    from mavric.video.extrinsics import solve_rig

    cd.resize(1400, 820)
    cd.tabs.setCurrentIndex(1)
    pump(app, 30)
    cd._set_origin()
    cd._solved(solve_rig(cd.spec, cd.snaps.snapshots, cd._lenses(), "cam0", cd.origin))
    pump(app, 0.8)
    annotate(cd, [(cd.pos_views["cam0"], 1), (group(cd, "Snapshots"), 2), (group(cd, "World origin"), 3),
                  (group(cd, "Positions"), 4), (cd.pos_plot, 5)], OUT / "calibrate_positions.png", scale=0.75)
    cd._save_positions()
    cd.tabs.setCurrentIndex(2)  # places in 3D: the true heights, then P1 being marked in two cameras
    pl = cd.places
    pl._use_truth()
    pl.table.selectRow(0)
    pump(app, 1.0)
    pl._toggle_mark()
    X = ctl.world.scene.perches[1][0]  # a corner of P1's bar, as cam0 sees it
    p0, _ = pl.cams["cam0"].project([X])
    pl._clicked("cam0", float(p0[0, 0]), float(p0[0, 1]))
    pump(app, 0.8)
    annotate(cd, [(pl.views["cam0"], 1), (pl.views["cam1"], 2), (group(cd, "Places"), 3)],
             OUT / "calibrate_places.png", scale=0.75)
    pl._end_mark()
    pl._save()
    cd.tabs.setCurrentIndex(3)  # microphones and speakers: the true speakers, microphones fitted by sound
    cd.resize(1400, 1180)  # tall enough for the speaker level table
    dv = cd.devices
    for n, d in ctl.world.scene.devices().items():
        if d.kind == "speaker":
            dv.devices[n] = d
    dv._set_all(dv.devices)
    from mavric.audio.ranging import measure

    dv._measured(measure(ctl.audio, repeats=2, gap_s=0.25))
    dv._fit()
    dv._apply_fit()
    pump(app, 0.8)
    annotate(cd, [(dv.space, 1), (group(cd, "Microphones and speakers (mm, degrees)"), 2),
                  (group(cd, "Check with sound"), 3), (group(cd, "Places as the microphones hear them"), 4),
                  (group(cd, "Speaker level at each place"), 5)],
             OUT / "calibrate_devices.png", scale=0.75)
    dv._save()
    cd.close()
    win.view_combo.setCurrentIndex(win.view_combo.findData(GRID))  # every camera with the places it sees
    win.resize(1500, 760)
    pump(app, 1.5)
    win.grid.grab().save(str(OUT / "acquire_grid.png"))
    print("wrote docs/images/acquire_grid.png")
    win._refresh_views(select="cam1")  # cam1 bumped: Position warns, the alignment overlay shows how to put it back
    pump(app, 0.5)
    ctl.take_reference("cam0")
    ctl.take_reference("cam1")
    ctl.world.moved["cam1"] = (22.0, -9.0, 1.2, 0.0)
    pump(app, 0.5)
    for _ in range(3):
        win._update_positions(background=False)
    win.b_align.setChecked(True)
    pump(app, 2.0)
    annotate(win, [(win.video, 1), (group(win, "Devices"), 2)], OUT / "acquire_alignment.png", scale=0.6)
    win.close()
    ctl.shutdown()

    # video settings: two (unconnected) FLIR cameras with placeholder serials
    rig2 = load_rig(rig_file)
    rig2["paths"] = {**rig2["paths"], "outputs_file": str(tmp / "o2.yaml"),
                     "video_settings_file": str(tmp / "video2.yaml")}
    rig2["cameras"] = [{"id": "cam0", "backend": "flir", "device": "12345678", "fps": 30, "exposure_us": 8000},
                       {"id": "cam1", "backend": "flir", "device": "12345679", "fps": 30, "record": "triggered"}]
    from mavric.acquire import AcquisitionController
    from mavric.config import RoiSet, TagMap

    w2 = AcquireWindow(AcquisitionController(rig2, TagMap(), RoiSet()))
    vs = VideoSettingsDialog(w2)
    vs.resize(820, 700)
    neutral_paths(vs, tmp)
    vs.show()
    pump(app, 0.3)
    annotate(vs, [(vs.form_box, 1), (vs.b_apply, 2), (vs.fmt.parentWidget(), 3), (vs.c_widgets.parentWidget(), 4)],
             OUT / "video_settings.png", scale=0.85)
    vs.close()
    w2.close()

    # ------------------------------------------------------------------ review (simulated session)
    from mavric.analysis.pipeline import run_analysis
    from mavric.audio.detect import detect_events
    from mavric.gui.review_window import ReviewWindow
    from mavric.sim import simulate_session

    sess = simulate_session(tmp / "sessions", duration_s=240, seed=7, audio=True, places_3d=True, label_noise_s=0.15,
                            label_coverage_s=150, name="demo", rig_path=rig_file)
    win = ReviewWindow(sess, "demo")
    win.resize(1600, 1380)
    win.show()
    win.rb_cur.setChecked(True)
    pump(app, 2.0)
    snd = win.sound
    snd.proposals = snd._without_labeled(detect_events(snd.src))
    t0 = 40.0
    for r in snd.proposals[(snd.proposals["start_s"] > t0) & (snd.proposals["start_s"] < t0 + 18)].itertuples():
        win.seek_time(r.start_s + 0.01)
        snd.set_type(0)
        snd.accept_proposal()
        snd.accept_suggestion()
    win.plot.setXRange(t0 - 2, t0 + 22, padding=0)
    win.seek_time(t0 + 6)
    pump(app, 1.0)
    annotate(win, [(win.video, 1), (win.plot, 2), (snd.lanes.env_plot, 3), (snd.lanes.spec_plots[0], 4),
                   (group(win, "Mode"), 5), (group(win, "Place (Up/Down)"), 6), (group(win, "Subject keys"), 7),
                   (group(win, "Playback"), 8), (group(win, "Session"), 9), (snd.panel, 10)],
             OUT / "review.png", scale=0.75)
    snd.close()
    win.dirty = snd.dirty = False
    win.close()

    # ------------------------------------------------------------------ analysis plots
    run_analysis(sess, plots=True)
    for name in ("timeline_by_subject", "accuracy_per_place", "sync_diagnostics"):
        src = sess.analysis_dir / f"{name}.png"
        if src.exists():
            shutil.copy(src, OUT / f"analysis_{name}.png")
            print(f"wrote docs/images/analysis_{name}.png")

    # ------------------------------------------------------------------ sound analysis and the annotated video
    import cv2

    from mavric.analysis.pipeline import prepare
    from mavric.analysis.render import render_annotated

    snd = simulate_session(tmp / "sessions", duration_s=240, seed=7, audio=True, playback_every_s=20,
                           sound_labels=True, label_noise_s=0.15, name="sounds", rig_path=rig_file)
    run_analysis(snd, plots=True)
    for name in ("sound_ethogram", "playback_response"):
        shutil.copy(snd.analysis_dir / f"{name}.png", OUT / f"analysis_{name}.png")
        print(f"wrote docs/images/analysis_{name}.png")
    clip = render_annotated(prepare(snd), None, out_path=snd.analysis_dir / "clip.mp4", t_range=(50.0, 58.0))
    cap = cv2.VideoCapture(str(clip))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 138)  # just after the sound played at 54.3 s
    ok, img = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(str(OUT / "render_frame.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 88])  # noisy picture: JPEG
        print("wrote docs/images/render_frame.jpg")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
