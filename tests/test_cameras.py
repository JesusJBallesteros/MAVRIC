"""A third camera: added in the GUI (saved in video.yaml), connected in order, shown, calibrated with the others."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_added_cameras_follow_rig_yaml(tmp_path):
    from mavric.config import load_rig
    from mavric.video.settings import VideoSettings, new_camera

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "rig.yaml").write_text(
        "cameras:\n"
        "  - {id: cam0, backend: flir, device: '111', fps: 30, exposure_us: 6000, frame_trigger: true}\n"
        "  - {id: cam1, backend: flir, device: '222', fps: 30, record: triggered}\n", encoding="utf-8")
    rig = load_rig(cfg / "rig.yaml")
    cam = new_camera(rig["cameras"], "flir", "333", frame_trigger=True)
    assert cam["id"] == "cam2" and cam["exposure_us"] == 6000 and cam["frame_trigger"] is True
    assert new_camera([{"id": "cam0"}, {"id": "cam2"}])["id"] == "cam1"  # the first free id
    dup = {"id": "cam1", "backend": "flir", "device": "999"}  # rig.yaml wins over an added camera of the same id
    VideoSettings({"cam2": {"exposure_us": 3000}}, {}, [cam, dup]).save(cfg / "video.yaml")
    rig = load_rig(cfg / "rig.yaml")
    assert [c["id"] for c in rig["cameras"]] == ["cam0", "cam1", "cam2"]
    assert rig["cameras"][1]["device"] == "222"
    assert rig["cameras"][2]["device"] == "333" and rig["cameras"][2]["exposure_us"] == 3000
    assert VideoSettings.load(cfg / "video.yaml").added_ids == ["cam2", "cam1"]


def test_grid_columns_fit_three_cameras():
    from mavric.gui.camera_grid import grid_columns

    assert grid_columns(1, 1000, 800) == 1
    assert grid_columns(2, 1200, 500) == 2
    assert grid_columns(3, 1100, 850) == 2  # two above one
    assert grid_columns(3, 1600, 420) == 3  # a wide, low area: side by side
    assert grid_columns(4, 1000, 800) == 2


def test_third_camera_in_the_gui(tmp_path):
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.config import load_rig
    from mavric.gui.acquire_window import GRID, AcquireWindow
    from mavric.gui.calibration_dialog import CalibrationDialog
    from mavric.gui.video_dialogs import AddCameraDialog, VideoSettingsDialog
    from mavric.video.calibration import save_lens
    from mavric.video.extrinsics import load_poses, solve_rig
    from mavric.video.settings import VideoSettings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "o.yaml")}
    assert [c["id"] for c in simulation_controller(rig, n_cameras=3).rig["cameras"]] == ["cam0", "cam1", "cam2"]
    rig["cameras"] = [{"id": c, "backend": "synthetic", "fps": 30} for c in ("cam0", "cam1")]  # a two-camera rig
    ctl = simulation_controller(rig, seed=1)
    win = AcquireWindow(ctl, sim=True)
    sim_video = tmp_path / "_sim_video.yaml"
    assert ctl.rig["paths"]["video_settings_file"] == str(sim_video)  # never the rig's own config/video.yaml

    # add a third camera from Video settings
    vd = VideoSettingsDialog(win)
    add = AddCameraDialog(vd, ctl.rig["cameras"], sim=True)
    cam = add.camera()
    assert cam["id"] == "cam2" and cam["backend"] == "synthetic"
    add.close()
    vd.add(cam)
    assert [c["id"] for c in ctl.rig["cameras"]] == ["cam0", "cam1", "cam2"]
    assert win.cam_combo.count() == 3 and "○ cam2" in win.cams_label.text()
    assert VideoSettings.load(sim_video).added_ids == ["cam2"]
    vd.cam_list.setCurrentRow(2)
    assert vd.b_remove.isEnabled()  # added here: removable
    vd.cam_list.setCurrentRow(0)
    assert not vd.b_remove.isEnabled()  # from the rig: not
    vd.close()

    # the next start of the simulation has it too, seen in 3D like the others
    ctl.shutdown()
    win.close()
    ctl = simulation_controller(rig, seed=1)
    assert [c["id"] for c in ctl.rig["cameras"]] == ["cam0", "cam1", "cam2"]
    assert set(ctl.world.scene.cams) == {"cam0", "cam1", "cam2"}
    win = AcquireWindow(ctl, sim=True)
    try:
        for cid in ("cam2", "cam0", "cam1"):  # connected in any order, kept in the rig's
            ctl.connect_camera(next(c for c in ctl.rig["cameras"] if c["id"] == cid))
        assert list(ctl.cams) == ["cam0", "cam1", "cam2"]
        win._refresh_views(select=GRID)
        assert "● cam2" in win.cams_label.text()
        assert [win.view_combo.itemData(i) for i in range(win.view_combo.count())] == ["cam0", "cam1", "cam2", GRID]
        assert set(win.grid.tiles) == {"cam0", "cam1", "cam2"}
        t_end = time.monotonic() + 5
        while time.monotonic() < t_end and any(th.latest is None for th in ctl.cams.values()):
            app.processEvents()

        # calibration: lenses, then the positions of all three cameras
        dlg = CalibrationDialog(win)
        assert set(dlg.pos_views) == {"cam0", "cam1", "cam2"} and set(dlg.places.views) == set(dlg.pos_views)
        for cid in ctl.cams:
            save_lens(ctl.rig["paths"]["calibration_file"], dlg._rig().lens_truth(cid))
        dlg.tabs.setCurrentIndex(1)
        dlg._lens_status()
        assert dlg.lens_status.text().count("✓") == 3
        for k in range(14):  # the simulated board held still at each of its poses in turn, captured by hand
            dlg._t0 = time.monotonic() - (k * 2.5 + 0.9)
            dlg._last_t = 0.0
            dlg._capture_snap = True
            dlg._tick_positions()
        assert len(dlg.snaps.snapshots) >= 6
        dlg._set_origin()
        sol = solve_rig(dlg.spec, dlg.snaps.snapshots, dlg._lenses(), "cam0", dlg.origin)
        assert set(sol.poses) == {"cam0", "cam1", "cam2"} and not sol.unplaced
        dlg._solved(sol)
        dlg._save_positions()
        assert set(load_poses(ctl.rig["paths"]["calibration_file"])[0]) == {"cam0", "cam1", "cam2"}
        assert win.cal_label.text().count("⌖") == 3
        dlg.close()
    finally:
        for c in list(ctl.cams):
            ctl.disconnect_camera(c)
        ctl.shutdown()
        win.close()
        app.processEvents()
