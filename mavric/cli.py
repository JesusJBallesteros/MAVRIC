"""Command line entry point: `mavric <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _rig(args):
    from .config import load_rig

    return load_rig(args.rig)


def cmd_acquire(args):
    rig = _rig(args)
    if args.headless:
        from .acquire import AcquisitionController, simulation_controller, stall_text

        ctl = simulation_controller(rig, seed=args.seed) if args.sim else AcquisitionController(rig)
        try:
            print("connecting RFID ...")
            ctl.connect_rfid(getattr(ctl, "_sim_source", None))
            if not args.no_camera:
                for cam in ctl.rig["cameras"]:
                    print(f"connecting camera {cam['id']} ...")
                    ctl.connect_camera(cam)
            if (args.audio or ctl.audio_cfg.enabled) and not args.no_audio:
                print(f"connecting audio {ctl.audio_cfg.describe()} ...")
                ctl.connect_audio()
            if args.arm_outputs:
                errs = ctl.start_outputs()
                for dev, err in errs.items():
                    print(f"  output device {dev} failed: {err}")
                ctl.outputs.arm(ctl.presence())
                print(f"outputs armed: {len(ctl.outputs.cfg.rules)} rule(s)")
            sess = ctl.start_recording(args.name, "headless")
            print(f"recording to {sess.path} for {args.duration} s")
            t_end = time.time() + args.duration
            while time.time() < t_end:
                time.sleep(1)
                st = ctl.status()
                cams = " ".join(f"{k}:{v['fps']:.1f}fps rec={v['recorded']} drop={v['rec_dropped']}"
                                + (f" {stall_text(v)}" if stall_text(v) else "")
                                for k, v in st["cameras"].items())
                rf = st.get("rfid", {})
                au = st.get("audio")
                aud = (f" audio={au.get('recorded_s', 0):.1f}s sync_edges={au['sync_edges']}"
                       + (f" flags={au['flags']}" if au["flags"] else "")) if au else ""
                print(f"  t={st['elapsed_s']:6.1f}s rfid={rf.get('lines', 0)} lines ({rf.get('rate_hz', 0):.1f}/s) "
                      f"{cams}{aud} presence={ctl.presence()}")
                for tr in st.get("triggers", []):
                    print(f"    trigger {tr['rule']}->{tr['camera']}: {tr['clips']} clips"
                          + (f", recording {tr['clip_s']:.1f}s" if tr["recording"] else "")
                          + (f" ERROR {tr['error']}" if tr["error"] else ""))
                errs = [rf.get("error")] + [v["error"] for v in st["cameras"].values()]
                if au:
                    errs += [au.get("error"), au.get("rec_error")]
                if any(errs):
                    print("  ERROR:", [e for e in errs if e])
            sess = ctl.stop_recording()
            print(json.dumps(sess.meta["stats"], indent=2))
        finally:
            ctl.shutdown()
        return
    from .gui.acquire_window import main as gui_main

    gui_main(rig, sim=args.sim)


def cmd_review(args):
    from .gui.review_window import main as gui_main

    gui_main(args.session, annotator=args.annotator, only=args.only)


def cmd_analyze(args):
    from .analysis.pipeline import run_analysis

    s = run_analysis(args.session, bin_s=args.bin, gap_s=args.gap, tol_s=args.tol, plots=not args.no_plots,
                     use_curated=args.curated, response_window_s=args.window)
    sy = s["sync"]
    print(f"session {s['session']}")
    for seg, f in sy.get("rfid_segments", {}).items():
        print(f"  RFID clock seg {seg}: drift {f['drift_ppm']:+.1f} ppm, latency median {f['latency_med_s'] * 1e3:.1f} ms")
    cam = sy.get("camera")
    if cam:
        fit = cam.get("fit")
        print(f"  camera: {cam['n_frames']} frames, camera drops {cam['camera_dropped']}, recorder drops "
              f"{cam['recorder_dropped']}" + (f", drift {fit['drift_ppm']:+.1f} ppm" if fit else ", no hw timestamps"))
    led = sy.get("led")
    if led:
        if led.get("validated"):
            print(f"  LED/SYNC: {led['n_matched']}/{led['n_sync']} matched, offset {led['offset_s'] * 1e3:+.1f} ms, "
                  f"|resid| median {led['resid_med_abs_s'] * 1e3:.1f} ms p95 {led['resid_p95_abs_s'] * 1e3:.1f} ms "
                  f"-> {'PASS' if led.get('pass') else 'FAIL'}")
        else:
            print(f"  LED/SYNC: not validated ({led['n_led']} LED onsets, {led['n_sync']} SYNC lines)")
    au = sy.get("audio")
    if au:
        pu = au["pulses"]
        line = (f"  audio: {len(au['channels'])} ch @ {au['sample_rate'] / 1000:g} kHz, {au['duration_s']:.1f} s, "
                f"{au['segments']} segment(s)" + (f", driver flags {au['flags']}" if au["flags"] else ""))
        if au.get("validated"):
            line += (f"; sync pulses {pu['n_matched']}/{pu['n_sync']} matched, drift {pu['drift_ppm']:+.1f} ppm, "
                     f"straight-line residual median {pu['lin_resid_med_ms']:.3f} ms "
                     f"-> {'PASS' if au.get('pass') else 'FAIL'}")
        else:
            line += (f"; mapped by host clock only ({pu['n_onsets']} pulses found in the audio, "
                     f"{pu['n_sync']} SYNC lines)")
        print(line)
    if "comparison" in s:
        o = s["comparison"]["overall"]
        e = s["comparison"]["events"]
        print(f"  labeled {s['comparison']['coverage_s']:.0f} s: presence sensitivity {o['presence_sensitivity']:.1%}, "
              f"precision {o['presence_precision']:.1%}, ID accuracy {o['id_accuracy_when_detected']:.1%}, "
              f"kappa {o['kappa']:.2f}")
        if e:
            print(f"  bouts: recall {e['event_recall']:.1%}, precision {e['event_precision']:.1%}, "
                  f"arrival latency median {e['onset_latency_med_s']:+.2f} s")
    else:
        print("  no manual labels yet (label in `mavric review` or import BORIS)")
    se = s.get("sound_events")
    if se:
        print(f"  sound events: {se['n']} ({', '.join(f'{k} {v}' for k, v in se['by_type'].items())}), "
              f"reviewed {se['reviewed_s'] / 60:.1f} min" + (" (no sound_coverage.csv: whole recording assumed)"
                                                              if se.get("coverage_assumed") else ""))
        if se.get("per_min_by_subject"):
            print("    per minute: " + ", ".join(f"{k} {v:.2f}" for k, v in se["per_min_by_subject"].items()))
        pb = se.get("playback")
        if pb:
            line = (f"    played sounds: {pb['n_played']}, {pb['n_reviewed']} with the {pb['window_s']:g} s around "
                    f"reviewed")
            if pb.get("answered") is not None:
                lat = pb.get("first_latency_med_s")
                line += (f"; answered {pb['answered']:.0%}" + (f", first answer median {lat:.2f} s" if lat else "")
                         + f"; sounds per minute before {pb['rate_before_per_min']:.1f}, "
                           f"after {pb['rate_after_per_min']:.1f}")
            print(line)
    print(f"  outputs in {Path(args.session) / 'analysis'}")


def cmd_render(args):
    from .analysis.labels_io import load_labels
    from .analysis.pipeline import prepare
    from .analysis.render import render_annotated
    from .session import Session

    sess = Session.open(args.session)
    P = prepare(sess)
    rng = (args.start, args.stop) if args.stop else None
    p = render_annotated(P, load_labels(sess), t_range=rng, audio=args.audio, spectrogram=not args.no_spectrogram,
                         span_s=args.span, fmax=args.fmax,
                         progress=lambda f: print(f"\r  {f:5.1%}", end="", flush=True))
    print(f"\n{p}")


def cmd_timeline(args):
    from .analysis.labels_io import load_labels
    from .analysis.pipeline import prepare
    from .analysis.timeline import export_timeline
    from .session import Session

    sess = Session.open(args.session)
    out = export_timeline(prepare(sess), load_labels(sess), audio=not args.no_audio,
                          progress=lambda f: print(f"\r  audio {f:5.1%}", end="", flush=True))
    print(f"\n{out}  (README.txt says how to open it)")


def cmd_import_boris(args):
    from .analysis.labels_io import import_boris, save_coverage, save_labels
    from .analysis.pipeline import prepare
    from .session import Session

    sess = Session.open(args.session)
    P = prepare(sess)
    bmap = dict(kv.split("=", 1) for kv in args.map_behavior or [])
    smap = dict(kv.split("=", 1) for kv in args.map_subject or [])
    lab, cov = import_boris(sess, args.file, P.frame_t, behavior_map=bmap, subject_map=smap,
                            coverage=args.coverage)
    save_labels(sess, lab)
    save_coverage(sess, cov, "boris")
    print(f"imported {len(lab)} intervals, coverage {cov}")


def cmd_simulate(args):
    from .sim import simulate_session

    s = simulate_session(args.out, args.duration, seed=args.seed, name=args.name, label_noise_s=args.noise,
                         label_coverage_s=args.label_span, audio=args.audio or bool(args.playback) or args.sound_labels,
                         playback_every_s=args.playback or 0.0, sound_labels=args.sound_labels)
    print(s.path)


def cmd_ports(args):
    from .rfid.serial_reader import list_ports

    for p in list_ports():
        vid = f"{p['vid']:04X}:{p['pid']:04X}" if p["vid"] else ""
        print(f"{p['device']:8s} {vid:10s} {p['description']}")
    try:
        from pypylon import pylon

        for d in pylon.TlFactory.GetInstance().EnumerateDevices():
            print(f"basler   {d.GetModelName()} serial={d.GetSerialNumber()}")
    except Exception:
        pass
    try:
        from .video.flir import list_flir_cameras

        for d in list_flir_cameras():
            print(f"flir     {d['model']} serial={d['serial']}")
    except Exception as e:
        print(f"flir     (Spinnaker error: {e})")


def cmd_link_test(args):
    from .config import TagMap
    from .rfid.protocol import KIND_BAD, KIND_TAG, parse_line
    from .rfid.serial_reader import SerialLineSource

    rig = _rig(args)
    tags = TagMap.load(rig["paths"]["tags_file"])
    src = SerialLineSource(args.port or rig["rfid"]["port"], int(rig["rfid"]["baud"]))
    print(f"opening {src.port} (board resets, waiting) ...")
    src.open()
    t_end = time.time() + args.seconds
    try:
        while time.time() < t_end:
            raw = src.readline()
            if raw is None:
                continue
            ln = parse_line(raw)
            extra = ""
            if ln.kind == KIND_TAG:
                extra = f"-> {tags.name(ln.tag)} at {rig['antennas'].get(ln.reader, ln.reader)}" + (
                    "" if ln.checksum_ok else "  CHECKSUM FAIL")
            elif ln.kind == KIND_BAD:
                extra = f"BAD {ln.raw!r}"
            if ln.kind != "NONE" or args.all:
                print(f"{ln.micros or '':>11} {ln.kind:4s} {ln.tag:12s} r{ln.reader} {extra}")
    finally:
        src.close()


def cmd_camtest(args):
    """Grab from configured cameras and report fps, drops, hardware clock and sync warnings."""
    from .config import resolve_path
    from .video.check import cameras_check

    rig = _rig(args)
    cams = {c["id"]: c for c in rig["cameras"]}
    if not cams:
        print(f"No cameras in {rig['_path']}")
        return 2
    ids = list(cams) if args.cam == "all" else [args.cam or rig["cameras"][0]["id"]]
    missing = [c for c in ids if c not in cams]
    if missing:
        print(f"No camera {missing[0]!r} in {rig['_path']} (have {sorted(cams)}, or 'all')")
        return 2
    cfgs = [dict(cams[c], **({"backend": args.backend} if args.backend else {})) for c in ids]
    world = None
    if any(c.get("backend") == "synthetic" for c in cfgs):
        from .sim import SimWorld

        world = SimWorld.random(duration_s=3600, seed=0)
    pngs = [resolve_path(rig["paths"]["sessions_dir"]) / f"_camtest_{c}.png" for c in ids]
    together = " at the same time" if len(ids) > 1 else ""
    names = ", ".join(f"{c} ({cfg.get('backend')})" for c, cfg in zip(ids, cfgs))
    print(f"testing {names}{together} for {args.seconds:g} s ...")
    reps = cameras_check(cfgs, args.seconds, world=world, save_pngs=pngs,
                         led_pulse_s=float(rig["sync"].get("led_pulse_s", 0.08)))
    for cid, rep in zip(ids, reps):
        _print_camtest(cid if len(ids) > 1 else "", rep)
    return 0 if all(r["frames"] for r in reps) else 1


def _print_camtest(cid: str, rep: dict) -> None:
    if cid:
        print(f"{cid}:")
    info = rep["info"]
    link = f" usb={info['link_speed']}" if info.get("link_speed") else ""
    if info.get("link_limit_mb_s"):
        link += f" limit={info['link_limit_mb_s']:.0f} MB/s"
    print(f"  device   {info.get('model', '')} serial={info.get('serial', '')} fw={info.get('firmware', '')}{link}")
    print(f"  image    {info.get('width')}x{info.get('height')} {info.get('pixel_format') or ''} "
          f"configured {info.get('fps', 0):.2f} fps")
    print(f"  grabbed  {rep['frames']} frames, measured {rep.get('measured_fps', 0):.2f} fps, "
          f"incomplete {rep['failed_frames']}, timeouts {rep['timeouts']}, "
          f"camera drops {rep.get('camera_dropped', 'n/a')}")
    if rep.get("problem") and (rep["failed_frames"] or rep["timeouts"]):
        print(f"  last problem: {rep['problem']}")
    if "host_interval_ms" in rep:
        print(f"  interval median {rep['host_interval_ms']['median']:.1f} ms, p99 {rep['host_interval_ms']['p99']:.1f} ms")
    hw = rep.get("hw_clock")
    if hw:
        print(f"  hw clock drift {hw['drift_ppm']:+.1f} ppm vs host, delivery latency median "
              f"{hw['latency_med_ms']:.1f} ms p95 {hw['latency_p95_ms']:.1f} ms")
    for w in rep["warnings"]:
        print(f"  WARNING  {w}")
    if rep.get("snapshot"):
        print(f"  snapshot {rep['snapshot']}")


def cmd_outputs(args):
    """List, test or fire the event-driven outputs configured in config/outputs.yaml."""
    import time as _time

    from .config import TagMap, resolve_path
    from .outputs.config import OutputsConfig
    from .outputs.engine import ActionEngine

    rig = _rig(args)
    path = resolve_path(rig["paths"].get("outputs_file", "config/outputs.yaml"))
    cfg = OutputsConfig.load(path)
    for w in cfg.validate():
        print(f"WARNING {w}")
    if args.what == "list":
        print(f"{path}")
        for name, d in cfg.devices.items():
            params = ", ".join(f"{k}={v}" for k, v in d.items() if k != "type")
            print(f"  device {name:12s} {d['type']:11s} {params}")
        for r in cfg.rules:
            print(f"  rule   {r.name:12s} {'on ' if r.enabled else 'off'} {r.describe()} {r.params}")
        from .triggers import TriggerRule

        for c in cfg.clips:
            print(f"  clip   {c['name']:12s} {TriggerRule.from_cfg(c).describe()}")
        return 0
    if args.what == "test":
        if args.name not in cfg.devices:
            print(f"no device {args.name!r} in {path}")
            return 2
        cfg = OutputsConfig({args.name: cfg.devices[args.name]}, [])
    elif args.what == "fire":
        rule = next((r for r in cfg.rules if r.name == args.name), None)
        if rule is None:
            print(f"no rule {args.name!r} in {path}")
            return 2
        cfg = OutputsConfig({rule.device: cfg.devices[rule.device]}, [rule])
    eng = ActionEngine(cfg, TagMap.load(rig["paths"]["tags_file"]), rig["antennas"])
    errors = eng.start()
    try:
        for dev, err in errors.items():
            print(f"device {dev}: {err}")
        if errors:
            return 1
        if args.what == "test":
            print(f"{args.name}: {eng.test_device(args.name)}")
        else:
            eng.fire_now(args.name, args.event)
            _time.sleep(1.5 if cfg.rules[0].action in ("hold", "hold_record") else 0.5)
            for row in eng.log:
                if row.get("rule"):
                    print(f"  {row['event']:6s} {row['status']:6s} {row['result']}")
        return 0
    finally:
        eng.stop()


def cmd_audio(args):
    """List audio devices, or bench-check the configured interface."""
    from .audio.config import AudioConfig

    rig = _rig(args)
    cfg = AudioConfig.from_rig(rig)
    if args.what == "devices":
        from .audio.devices import find_device, list_devices

        devs = list_devices()
        try:
            match = find_device(cfg.device, cfg.host_api, cfg.n_in_open, cfg.n_out_open, devs)["index"]
        except RuntimeError:
            match = None
        print(f"configured: {cfg.describe()}, {cfg.n_in_open} input(s), {cfg.n_out_open} output(s)")
        for d in devs:
            if not (d["inputs"] or d["outputs"]):
                continue
            mark = "  <- configured" if d["index"] == match else ""
            print(f"  {d['index']:3d} {d['host_api'][:16]:16s} in={d['inputs']:2d} out={d['outputs']:2d} "
                  f"{d['name']}{mark}")
        if match is None:
            print(f"no device matches {cfg.device!r} on {cfg.host_api} with enough channels "
                  f"(rig.yaml audio.device / host_api)")
        return 0
    from .config import resolve_path

    if args.what in ("sounds", "play"):
        return _audio_play(args, cfg)
    from .audio.check import audio_check

    world = None
    if args.sim:
        from .sim import SimWorld

        cfg.host_api = "sim"
        world = SimWorld.random(duration_s=3600, seed=0)
    save = resolve_path(rig["paths"]["sessions_dir"]) / "_audiotest.flac"
    print(f"testing {cfg.describe()} for {args.seconds:g} s"
          + (f", then a 1 kHz tone on each speaker at {args.tone_db:g} dBFS" if args.tones else "") + " ...")
    rep = audio_check(cfg, args.seconds, tones=args.tones, tone_db=args.tone_db, world=world, save_path=save)
    i = rep["info"]
    lat = i.get("latency_s") or [None, None]
    print(f"  device   {i.get('device')} [{i.get('host_api')}] {i.get('sample_rate') or 0:.0f} Hz, block {cfg.block}"
          + (f", latency in {lat[0] * 1e3:.1f} ms" if lat[0] else "")
          + (f" / out {lat[1] * 1e3:.1f} ms" if len(lat) > 1 and lat[1] else ""))
    print(f"  received {rep['seconds']:.1f} s" + (f", driver flags {rep['flags']}" if rep["flags"] else ""))
    for c in rep["channels"]:
        print(f"  in {c['input']:2d} {c['name']:8s} peak {c['peak_dbfs']:6.1f} dBFS  rms {c['rms_dbfs']:6.1f} dBFS"
              + (f"  CLIPPED {c['clipped']}" if c["clipped"] else ""))
    sy = rep.get("sync")
    if sy:
        iv = sy.get("interval_s")
        print(f"  sync     {sy['pulses']} pulses ({sy['edges']} edges)"
              + (f", step {sy['step_dbfs']:.0f} dBFS" if "step_dbfs" in sy else "")
              + (f", width {sy['width_ms']:.1f} ms" if "width_ms" in sy else "")
              + (f", intervals {iv['min']:.1f}-{iv['max']:.1f} s" if iv else "")
              + (", inverted" if sy["polarity"] < 0 else ""))
    ck = rep.get("clock")
    if ck:
        print(f"  clock    {ck['drift_ppm']:+.1f} ppm vs PC, block delivery median {ck['delivery_ms_med']:.1f} ms "
              f"p95 {ck['delivery_ms_p95']:.1f} ms")
    if rep.get("tones"):
        print(f"  playback round trip {rep['round_trip_ms']:.1f} ms (driver); the rest of each delay is the air "
              f"(~2.9 ms per metre)")
    for tn in rep.get("tones", []):
        heard = ", ".join(f"{m}: {r['level_dbfs']:.0f} dBFS (+{r['above_background_db']:.0f} dB), "
                          f"{r['delay_ms']:.1f} ms" + (f" (air {r['air_ms']:.1f} ms)" if "air_ms" in r else "")
                          if r["heard"] else f"{m}: not heard"
                          for m, r in tn["mics"].items())
        print(f"  tone     {tn['speaker']} (out {tn['output']}) -> {heard}")
    for w in rep["warnings"]:
        print(f"  WARNING  {w}")
    if rep.get("saved"):
        print(f"  saved    {rep['saved']}")
    return 1 if (not rep["seconds"] or rep["flags"].get("in_over")) else 0


def _audio_play(args, cfg) -> int:
    """`audio sounds`: list the stimulus library. `audio play <sound>`: play it on the speakers and wait."""
    import time

    from .audio.library import SoundLibrary
    from .config import resolve_path

    lib = SoundLibrary(resolve_path(cfg.sounds_dir), cfg.sample_rate)
    if args.what == "sounds":
        print(f"{len(lib.sounds) - 1} sound(s) in {lib.folder}" + ("" if lib.folder.is_dir() else " (folder missing)")
              + f", played at {cfg.sample_rate} Hz mono")
        for name in lib.names:
            s = lib.sounds[name]
            src = f"{s.source_rate} Hz" + (f", {s.source_channels} ch" if s.source_channels > 1 else "")
            print(f"  {name:28s} {s.duration_s * 1e3:7.0f} ms  peak {s.peak_dbfs:6.1f} dBFS  ({src})")
        for n in lib.notes:
            print(f"  NOTE  {n}")
        return 0
    if not args.name:
        print("which sound? `mavric audio sounds` lists them (test_tone is built in)")
        return 2
    from .audio.player import Player
    from .audio.stream import AudioStream

    world = None
    if args.sim:
        from .sim import SimWorld

        cfg.host_api = "sim"
        world = SimWorld.random(duration_s=3600, seed=0)
    if not cfg.speakers:
        print("no speakers configured (rig.yaml audio.speakers)")
        return 2
    st = AudioStream(cfg, world=world)
    st.open()
    try:
        player = Player(st, lib)
        try:
            _ids, text = player.play(args.name, args.speaker, args.gain_db, source="cli")
        except KeyError as e:
            print(e.args[0] if e.args else e)
            return 2
        print(f"playing {text} ({cfg.describe()}) ...")
        time.sleep(0.1)
        t_end = time.monotonic() + 30
        while st.mixer.busy and time.monotonic() < t_end:
            time.sleep(0.05)
        time.sleep(0.2)
        limited = st.mixer.limited.tolist()
    finally:
        st.close()
    if any(limited):
        print(f"  WARNING  the limiter clipped {sum(limited)} sample(s) (audio.max_level_dbfs "
              f"{cfg.max_level_dbfs:g}): lower --gain-db")
    return 0


def cmd_sounds(args):
    """Sound events of a session: automatic proposals, Audacity/Raven export and import."""
    from .analysis.pipeline import prepare
    from .audio.events import export_events, import_events, load_events, save_events
    from .session import Session

    sess = Session.open(args.session)
    if not sess.audio:
        print(f"{sess.path.name} has no audio")
        return 2
    P = prepare(sess)
    if args.what == "detect":
        from .audio.detect import detect_events
        from .audio.source import AudioSource

        src = AudioSource(sess, P.audio)
        df = detect_events(src, band=(args.fmin, args.fmax), on_db=args.on_db,
                           progress=lambda f: print(f"\r  {f:5.1%}", end="", flush=True))
        src.close()
        out = sess.analysis_dir / "sound_proposals.csv"
        df.to_csv(out, index=False)
        print(f"\n{len(df)} proposals -> {out} (review them in `mavric review`: Y accepts, R rejects)")
        return 0
    if args.what == "export":
        events = load_events(sess)
        for p in export_events(sess, P.audio, events, args.format):
            print(p)
        print(f"{len(events)} events")
        return 0
    if not args.file:
        print("import needs a label file")
        return 2
    new = import_events(sess, P.audio, args.file, args.format, file_index=args.file_index, annotator=args.annotator)
    save_events(sess, load_events(sess) + new)
    print(f"imported {len(new)} events into {sess.file('sound_events.csv')}")
    return 0


def _pair(text: str | None, sep: str = "x", n: int = 2):
    if not text:
        return None
    vals = [int(v) for v in text.replace(",", sep).split(sep)]
    if len(vals) != n:
        raise SystemExit(f"expected {n} numbers in {text!r}")
    return tuple(vals)


def _grab(cfg: dict, n: int = 8):
    """One frame from a camera that the acquire window is not using (after a few frames to settle)."""
    from .video.backends import make_backend

    be = make_backend(cfg)
    be.open()
    img = None
    try:
        for k in range(10 * n):
            fr = be.grab(2.0)
            if fr is not None:
                img = fr.image
            if img is not None and k >= n:
                break
    finally:
        be.close()
    return img


def cmd_calibrate(args):
    """Camera calibration: sticker sheet, board check, lens calibration from saved images, summary."""
    from .config import resolve_path
    from .video.calibration import BoardDetector, BoardSpec, calibrate_lens, load_calibrations, save_lens

    rig = _rig(args)
    spec = BoardSpec.from_rig(rig)
    path = resolve_path(rig["paths"].get("calibration_file", "config/calibration.yaml"))
    if spec.problems():
        print("calibration.board in rig.yaml: " + "; ".join(spec.problems()))
        return 2
    if args.what == "sheet":
        from .video.sheet import default_sheet_path, make_sheet

        out = make_sheet(spec, args.out or default_sheet_path(spec, path.parent / "calibration"), args.paper)
        print(f"{out}\n  {spec.describe()}: print pages 2-3 at 100 % (actual size); page 1 says where each "
              "sticker goes")
        return 0
    if args.what == "show":
        cals = load_calibrations(path)
        if not cals:
            print(f"no lens calibration in {path} yet (acquire: Calibrate cameras...)")
        for c in cals.values():
            print(f"{c.summary()}\n    {c.image_size[0]}x{c.image_size[1]}, {c.model} model, {c.date}"
                  + (f", serial {c.serial}" if c.serial else ""))
            for why in c.verdict()[1]:
                print(f"    - {why}")
        return 0
    det = BoardDetector(spec)
    cams = {c["id"]: c for c in rig["cameras"]}
    if args.what == "check":
        import cv2

        if args.image:
            img = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
            if img is None:
                print(f"cannot read {args.image}")
                return 2
        else:
            cid = args.cam or next(iter(cams))
            if cid not in cams:
                print(f"no camera {cid!r} in rig.yaml (have {', '.join(cams)})")
                return 2
            img = _grab(cams[cid])
            if img is None:
                print(f"no frame from {cid}")
                return 2
        d = det.detect(img)
        print(f"{len(d.marker_ids)}/{spec.n_markers} markers, {d.n}/{spec.n_corners} corners"
              + (f"; MISPLACED OR TURNED: {', '.join(map(str, d.misplaced))} (see the map on page 1 of the sticker "
                 "sheet)" if d.misplaced else "; no misplaced sticker"))
        missing = sorted(set(range(spec.n_markers)) - set(d.marker_ids.tolist()))
        if missing and len(missing) < spec.n_markers:
            print(f"  not seen: {', '.join(map(str, missing))} (out of view, covered, glare, or too small)")
        if args.save_image:
            from .video.calibration import draw_detection

            cv2.imwrite(args.save_image, draw_detection(img, d, spec))
            print(f"  {args.save_image}")
        return 0 if d.n and not d.misplaced else 1
    # lens: from a folder of saved images
    import cv2

    if not args.images or not args.cam:
        print("lens needs --cam and --images FOLDER")
        return 2
    files = sorted(f for f in Path(args.images).iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp",
                                                                                     ".tif", ".tiff"))
    views = []
    for f in files:
        d = det.detect(cv2.imread(str(f), cv2.IMREAD_UNCHANGED))
        ok = d.n >= 12 and not d.misplaced
        print(f"  {f.name}: {d.n} corners" + (f", misplaced {d.misplaced}" if d.misplaced else "")
              + ("" if ok else "  (left out)"))
        if ok:
            views.append(d)
    if len(views) < 3:
        print(f"only {len(views)} usable images")
        return 2
    cal = calibrate_lens(views, spec, args.cam, args.model)
    print(cal.summary())
    for why in cal.verdict()[1]:
        print(f"  - {why}")
    if args.save:
        print(save_lens(path, cal, views))
    return 0 if cal.verdict()[0] else 1


def cmd_compress(args):
    """Raw (or any) videos -> <name>_compressed.mp4 (H.264, every frame kept, constant frame rate)."""
    from .video.compress import compress_path, compress_session, ffmpeg_command, options, session_jobs
    from .video.settings import video_cfg

    rig = _rig(args)
    o = options(**{**video_cfg(rig)["compress"], **{k: v for k, v in {
        "codec": args.codec, "crf": args.crf, "preset": args.preset, "gop": args.gop,
        "scale": _pair(args.scale), "downscale": args.downscale, "crop": _pair(args.crop, ",", 4)}.items()
        if v is not None}})
    path = Path(args.path)

    def progress(label, k, n):
        print(f"\r  {label}: {k}" + (f"/{n} frames ({k / n:.0%})" if n else " frames"), end="", flush=True)

    is_session = (path / "session.json").exists()
    if args.print_ffmpeg:
        if is_session:
            from .session import Session

            for job in session_jobs(Session.open(path), args.cam, not args.no_clips):
                print(ffmpeg_command(job["parts"], job["out"], job["fps"], o))
        else:
            print(ffmpeg_command([path], path.with_name(path.stem + "_compressed.mp4"), 30.0, o))
        return 0
    print(f"compressing with {o['codec']} crf {o['crf']} preset {o['preset']}, keyframe every {o['gop']} frames"
          + (f", scale {o['scale']}" if o.get("scale") else "") + (f", downscale {o['downscale']}x"
                                                                   if (o.get("downscale") or 1) > 1 else "")
          + (f", crop {o['crop']}" if o.get("crop") else ""))
    if is_session:
        from .session import Session

        res = compress_session(Session.open(path), o, args.cam, not args.no_clips, args.overwrite, progress)
    else:
        res = compress_path(path, o, args.overwrite, progress)
    print()
    for r in res:
        if r.get("skipped"):
            print(f"  {r['file']}: skipped ({r['skipped']}; --overwrite to redo)")
        else:
            print(f"  {r['file']}: {r['frames']} frames, {r['width']}x{r['height']}, {r['bytes'] / 1e6:.0f} MB, "
                  f"{r['seconds']:.0f} s")
    return 0


def cmd_selftest(args):
    """Run the test suite of this checkout, independent of the current folder."""
    import pytest

    from .config import SOURCE_ROOT

    tests = SOURCE_ROOT / "tests"
    if not tests.is_dir():
        print(f"No tests folder at {tests}. selftest needs the source checkout (pip install -e .).")
        return 2
    return pytest.main([str(tests), "-q", "--rootdir", str(SOURCE_ROOT), *args.pytest_args])


def main(argv=None):
    p = argparse.ArgumentParser(prog="mavric", description="MAVRIC: synchronized video, audio and RFID "
                                                           "acquisition, review, analysis and outputs")
    p.add_argument("--rig", default=None, help="rig.yaml (default config/rig.yaml)")
    from .about import version_string

    p.add_argument("--version", action="version", version=version_string())
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("acquire", help="acquisition GUI (or --headless)")
    a.add_argument("--sim", action="store_true", help="simulated cage instead of hardware")
    a.add_argument("--headless", action="store_true", help="record without a window, then exit")
    a.add_argument("--duration", type=float, default=30, help="(headless) recording length (s)")
    a.add_argument("--name", default="headless", help="(headless) session name")
    a.add_argument("--seed", type=int, default=0, help="(sim) random seed of the simulated cage")
    a.add_argument("--no-camera", action="store_true", help="(headless) record without cameras")
    a.add_argument("--arm-outputs", action="store_true", help="(headless) start and arm the output rules")
    a.add_argument("--audio", action="store_true", help="(headless) record audio even if audio.enabled is false")
    a.add_argument("--no-audio", action="store_true", help="(headless) do not record audio")
    a.set_defaults(fn=cmd_acquire)

    r = sub.add_parser("review", help="review GUI: place labels, RFID curation, sound events (video and/or audio)")
    r.add_argument("session")
    r.add_argument("--annotator", default="", help="your initials; skips the start dialog")
    r.add_argument("--only", choices=["video", "audio"], help="review only the video or only the audio")
    r.set_defaults(fn=cmd_review)

    z = sub.add_parser("analyze", help="sync, bouts, comparison, plots")
    z.add_argument("session")
    z.add_argument("--bin", type=float, default=0.5, help="comparison bin (s)")
    z.add_argument("--gap", type=float, default=0.5, help="max gap inside an RFID bout (s)")
    z.add_argument("--tol", type=float, default=1.0, help="event matching tolerance (s)")
    z.add_argument("--curated", action="store_true", help="drop RFID bouts marked spurious in review")
    z.add_argument("--window", type=float, default=10.0, help="how long after a played sound an answer counts (s)")
    z.add_argument("--no-plots", action="store_true")
    z.set_defaults(fn=cmd_analyze)

    v = sub.add_parser("render", help="annotated video")
    v.add_argument("session")
    v.add_argument("--start", type=float, default=0.0, help="session time to start at (s)")
    v.add_argument("--stop", type=float, default=None, help="session time to stop at (s; default: end)")
    v.add_argument("--audio", default="auto",
                   help="sound track: auto (two mics as stereo), stereo, mix, none, or one microphone's name")
    v.add_argument("--no-spectrogram", action="store_true", help="no spectrogram strip under the picture")
    v.add_argument("--span", type=float, default=6.0, help="seconds shown in the spectrogram strip")
    v.add_argument("--fmax", type=float, default=None, help="top of the spectrogram strip, Hz "
                   "(default: audio.display_fmax_hz in rig.yaml, as in review)")
    v.set_defaults(fn=cmd_render)

    tl = sub.add_parser("timeline", help="video-aligned audio and label tracks for Audacity, Ethograph, BORIS")
    tl.add_argument("session")
    tl.add_argument("--no-audio", action="store_true", help="label tracks and timeline.csv only, no WAV")
    tl.set_defaults(fn=cmd_timeline)

    b = sub.add_parser("import-boris", help="import BORIS aggregated events as manual labels")
    b.add_argument("session")
    b.add_argument("file")
    b.add_argument("--map-behavior", nargs="*", help="BORIS behaviour=ROI name pairs")
    b.add_argument("--map-subject", nargs="*", help="BORIS subject=subject name pairs")
    b.add_argument("--coverage", choices=["media", "events"], default="media",
                   help="labeled span: whole media file, or first-to-last event")
    b.set_defaults(fn=cmd_import_boris)

    s = sub.add_parser("simulate", help="write a synthetic session with ground truth")
    s.add_argument("out", nargs="?", default="sessions", help="folder to write the session into")
    s.add_argument("--duration", type=float, default=300, help="session length (s)")
    s.add_argument("--seed", type=int, default=0, help="random seed")
    s.add_argument("--name", default="sim", help="session name")
    s.add_argument("--noise", type=float, default=0.15, help="label timing noise (s)")
    s.add_argument("--label-span", type=float, default=None, help="label only the first N s")
    s.add_argument("--audio", action="store_true", help="also simulate the audio inputs (mics + sync pulse)")
    s.add_argument("--playback", type=float, default=None, metavar="SECONDS",
                   help="with audio: play a stimulus about every SECONDS; perched subjects answer some")
    s.add_argument("--sound-labels", action="store_true", help="with audio: write the true calls as sound events")
    s.set_defaults(fn=cmd_simulate)

    sub.add_parser("ports", help="list serial ports and Basler / FLIR cameras").set_defaults(fn=cmd_ports)

    lt = sub.add_parser("link-test", help="print decoded RFID lines from the Arduino")
    lt.add_argument("--port", default=None)
    lt.add_argument("--seconds", type=float, default=15, help="how long to print lines")
    lt.add_argument("--all", action="store_true", help="also print NONE heartbeat lines")
    lt.set_defaults(fn=cmd_link_test)

    ct = sub.add_parser("camtest", help="grab from one camera (or all at once): fps, drops, hardware clock, sync warnings")
    ct.add_argument("--cam", default=None, help="camera id from rig.yaml (default: first), or 'all' to grab from "
                    "every camera at the same time")
    ct.add_argument("--seconds", type=float, default=5.0, help="how long to grab")
    ct.add_argument("--backend", default=None, help="override the backend, e.g. synthetic")
    ct.set_defaults(fn=cmd_camtest)

    op = sub.add_parser("outputs", help="list / test devices / fire rules from config/outputs.yaml")
    op.add_argument("what", choices=["list", "test", "fire"])
    op.add_argument("name", nargs="?", default="", help="device (test) or rule (fire)")
    op.add_argument("--event", default=None, help="fire: enter | leave | start | stop")
    op.set_defaults(fn=cmd_outputs)

    au = sub.add_parser("audio", help="audio devices, bench check, stimulus library and playback")
    au.add_argument("what", choices=["devices", "test", "sounds", "play"],
                    help="devices | test | sounds (list the library) | play <sound>")
    au.add_argument("name", nargs="?", default=None, help="play: sound name (`mavric audio sounds`)")
    au.add_argument("--speaker", default="all", help="play: speaker name(s) or output(s), comma-separated, or all")
    au.add_argument("--gain-db", type=float, default=0.0, help="play: gain applied to the file (dB)")
    au.add_argument("--seconds", type=float, default=10.0, help="test: how long to record and measure")
    au.add_argument("--tones", action="store_true", help="test: play a 1 kHz tone on each speaker in turn")
    au.add_argument("--tone-db", type=float, default=-30.0, help="tone level, dBFS")
    au.add_argument("--sim", action="store_true", help="test / play: simulated interface")
    au.set_defaults(fn=cmd_audio)

    so = sub.add_parser("sounds", help="sound events: detect proposals, export / import Audacity or Raven labels")
    so.add_argument("what", choices=["detect", "export", "import"])
    so.add_argument("session")
    so.add_argument("file", nargs="?", default=None, help="import: label file")
    so.add_argument("--format", choices=["audacity", "raven"], default=None,
                    help="export: default audacity; import: guessed from the file name")
    so.add_argument("--file-index", type=int, default=0, help="import: audio file the labels were made on (0 = first)")
    so.add_argument("--annotator", default="")
    so.add_argument("--fmin", type=float, default=300.0, help="detect: band low edge (Hz)")
    so.add_argument("--fmax", type=float, default=8000.0, help="detect: band high edge (Hz)")
    so.add_argument("--on-db", type=float, default=12.0, help="detect: dB above the noise floor to start an event")
    so.set_defaults(fn=cmd_sounds)

    ca = sub.add_parser("calibrate", help="camera calibration: sticker sheet, board check, lens from images, show")
    ca.add_argument("what", choices=["sheet", "check", "lens", "show"])
    ca.add_argument("--out", help="sheet: PDF to write (default: config/calibration/charuco_stickers_....pdf)")
    ca.add_argument("--paper", choices=["a4", "letter"], default="a4", help="sheet: paper size")
    ca.add_argument("--cam", help="check: camera to grab a frame from (not while acquire uses it); lens: its id")
    ca.add_argument("--image", help="check: an image file instead of a camera")
    ca.add_argument("--save-image", help="check: write the image with the corners found and misplaced markers")
    ca.add_argument("--images", help="lens: folder of images of the board")
    ca.add_argument("--model", choices=["standard", "rational"], default="standard", help="lens: distortion model")
    ca.add_argument("--save", action="store_true", help="lens: save to config/calibration.yaml")
    ca.set_defaults(fn=cmd_calibrate)

    cp = sub.add_parser("compress", help="raw recordings -> <name>_compressed.mp4 (H.264, DeepLabCut-ready)")
    cp.add_argument("path", help="session folder, video file, or folder of videos")
    cp.add_argument("--crf", type=int, default=None, help="quality: lower = better/larger (default from rig: 18)")
    cp.add_argument("--preset", default=None, help="x264 speed/size trade-off, e.g. fast, medium, slow")
    cp.add_argument("--gop", type=int, default=None, help="keyframe every N frames (default 30)")
    cp.add_argument("--codec", default=None, choices=["libx264", "libx265"])
    cp.add_argument("--scale", default=None, help="output size WxH, e.g. 640x512")
    cp.add_argument("--downscale", type=int, default=None, help="divide width and height by this factor")
    cp.add_argument("--crop", default=None, help="x,y,w,h before scaling")
    cp.add_argument("--cam", nargs="*", default=None, help="session: only these cameras")
    cp.add_argument("--no-clips", action="store_true", help="session: skip triggered clips")
    cp.add_argument("--overwrite", action="store_true")
    cp.add_argument("--print-ffmpeg", action="store_true", help="only print the equivalent ffmpeg commands")
    cp.set_defaults(fn=cmd_compress)

    st = sub.add_parser("selftest", help="run the test suite (works from any folder); extra args go to pytest")
    st.set_defaults(fn=cmd_selftest)

    args, extra = p.parse_known_args(argv)
    if args.cmd == "sounds" and args.what == "export" and args.format is None:
        args.format = "audacity"
    if args.cmd == "selftest":
        args.pytest_args = extra  # e.g. mavric selftest -k sync -x
    elif extra:
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    from .config import ConfigError

    try:
        return args.fn(args)
    except ConfigError as e:  # a broken config file: say where, without a traceback
        print(f"mavric: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
