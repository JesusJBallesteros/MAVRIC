"""AU4 playback: mixer voices, the stimulus library, the playback log, the audio output device, the soundboard."""

import os
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from mavric.audio.config import AudioConfig
from mavric.audio.library import TEST_TONE, SoundLibrary, list_sound_names, resample
from mavric.audio.player import Player
from mavric.audio.recorder import PLAYBACK_FILE, AudioRecorder
from mavric.audio.stream import AudioStream, Mixer

SR = 48000


# --------------------------------------------------------------------------- mixer


def test_mixer_loops_fades_and_logs():
    m = Mixer(2, max_level_dbfs=0, sample_rate=1000, fade_s=0.01)  # fade: 10 samples
    out = np.zeros((64, 2), np.float32)
    pattern = np.arange(1, 6, dtype=np.float32) / 10
    b = m.play(pattern, 0, loop=True, name="b", source="test")
    m.render(out, 0)
    assert np.allclose(out[:, 0], np.tile(pattern, 20)[:64])  # loops without a gap
    m.stop(voice=b)
    m.render(out, 64)
    ramp = (10 - np.arange(10)) / 10
    assert np.allclose(out[:10, 0], np.tile(pattern, 20)[64:74] * ramp) and not out[10:].any()
    start, end = list(m.log)
    assert start == {"kind": "start", "sample": 0, "voice": b, "channel": 0, "name": "b", "gain": 1.0, "loop": True,
                     "source": "test"}
    assert end["kind"] == "end" and end["sample"] == 74 and end["n"] == 74 and end["stopped"]
    assert not m.busy

    c = m.play(np.ones(10), 1)
    m.stop(channel=1)  # before it started: cancelled, never logged
    m.render(out, 128)
    assert not out.any() and len(m.log) == 2 and c > b

    lim = Mixer(1, max_level_dbfs=-6, sample_rate=1000)
    lim.play(np.ones(100), 0)
    lim.render(out[:, :1], 0)
    assert out[:, 0].max() == pytest.approx(10 ** (-6 / 20), rel=1e-6) and lim.limited[0] == 64


# --------------------------------------------------------------------------- library


def _write(path, x, sr):
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x, sr)


def test_library_loads_mono_and_resamples(tmp_path):
    folder = tmp_path / "sounds"
    t = np.arange(int(0.5 * 44100)) / 44100
    s = 0.5 * np.sin(2 * np.pi * 1000 * t)
    _write(folder / "calls" / "coo.wav", np.stack([s, s], axis=1), 44100)  # stereo, 44.1 kHz
    _write(folder / "beep.flac", 0.25 * np.ones(4800), SR)
    (folder / "broken.wav").write_bytes(b"not audio")
    lib = SoundLibrary(folder, SR)
    assert lib.names == [TEST_TONE, "beep", "calls/coo"] == list_sound_names(folder)[:1] + ["beep", "calls/coo"]
    coo = lib.get("calls/coo")
    assert coo.samples.ndim == 1 and coo.duration_s == pytest.approx(0.5, abs=1 / SR)
    assert coo.source_rate == 44100 and coo.source_channels == 2
    spec = np.abs(np.fft.rfft(coo.samples))
    assert np.argmax(spec) * SR / len(coo.samples) == pytest.approx(1000, abs=3)  # still 1 kHz after resampling
    assert np.abs(coo.samples[2000:-2000]).max() == pytest.approx(0.5, abs=0.01)
    assert lib.get("beep.flac").name == "beep" and lib.get("beep").peak_dbfs == pytest.approx(-12.04, abs=0.01)
    with pytest.raises(KeyError, match="no sound"):
        lib.get("nope")
    assert any("broken" in n for n in lib.notes) and any("resampled" in n for n in lib.notes)
    man = lib.manifest()
    assert man["calls/coo"]["file"] == "calls/coo.wav" and len(man["calls/coo"]["sha1"]) == 12
    assert man[TEST_TONE]["duration_s"] == pytest.approx(0.2)
    assert len(resample(np.ones(441), 44100, 48000)) == 480


# --------------------------------------------------------------------------- stream -> recorder playback log


class LoopBackend:
    """Delivers blocks when step() is called; every output comes back on input 1 after `delay` samples."""

    def __init__(self, cfg: AudioConfig, delay: int):
        self.cfg, self.delay = cfg, delay
        self.hist = np.zeros((1 << 16, max(cfg.n_out_open, 1)), np.float32)
        self.k = 0

    def open(self, handler):
        self.handler = handler

    def step(self, n_blocks: int = 1):
        b = self.cfg.block
        for _ in range(n_blocks):
            idx = (self.k + np.arange(b) - self.delay) % len(self.hist)
            x = np.zeros((b, len(self.cfg.names)), np.float32)
            x[:, 0] = self.hist[idx].sum(axis=1) if self.k >= self.delay else 0
            out = np.zeros((b, self.cfg.n_out_open), np.float32)
            self.handler(x, out, b, 100.0 + self.k / SR, 0.0, "")
            self.hist[(self.k + np.arange(b)) % len(self.hist)] = out
            self.k += b

    def info(self):
        return {"device": "loop", "host_api": "test", "latency_s": [0.0, 0.0], "sample_rate": float(SR)}

    def round_trip_samples(self):
        return self.delay

    def close(self):
        pass


def _wait(st, n_blocks):
    t_end = time.monotonic() + 5
    while st.n_blocks < n_blocks and time.monotonic() < t_end:
        time.sleep(0.005)
    assert st.n_blocks >= n_blocks


def test_played_sounds_are_logged_where_they_are_recorded(tmp_path):
    import soundfile as sf

    from mavric.audio.events import playback_events
    from mavric.session import Session

    cfg = AudioConfig(inputs={1: "micA"}, sync_channel="", speakers={1: "spk1", 2: "spk2"}, block=256,
                      max_level_dbfs=0)
    _write(tmp_path / "sounds" / "pip.wav", 0.5 * np.ones(100), SR)
    be = LoopBackend(cfg, delay=512)
    st = AudioStream(cfg, backend=be)
    st.open()
    assert st.play_offset == 512
    player = Player(st, SoundLibrary(tmp_path / "sounds", SR))
    be.step(4)  # the stream runs before the recording starts
    _wait(st, 4)
    rec = AudioRecorder(tmp_path, cfg, t0=100.0)
    rec.start()
    st.attach(rec)
    be.step(2)
    _ids, text = player.play("pip", "spk1", -6.0, source="test")
    assert "pip" in text and "spk1" in text and "LIMITED" not in text
    be.step(4)
    (loop_id,), _ = player.play("pip", "spk2", 0.0, loop=True, source="test")
    be.step(2)
    player.stop(voices=[loop_id])
    be.step(6)
    _wait(st, 18)
    st.detach()
    stats = rec.finish()
    st.close()
    assert stats["played"] == 2
    log = pd.read_csv(tmp_path / PLAYBACK_FILE, dtype={"speaker": str, "sound": str, "source": str})
    a, b = log.iloc[0], log.iloc[1]
    # rendered at stream sample 6*256, at the speaker 512 later, recording started at stream sample 4*256
    assert (a["sample"], a["n_samples"], a["speaker"], a["sound"]) == (6 * 256 + 512 - 4 * 256, 100, "spk1", "pip")
    assert a["gain_db"] == pytest.approx(-6.0) and a["loop"] == 0 and a["stopped"] == 0 and a["source"] == "test"
    assert a["latency_samples"] == 512
    assert b["speaker"] == "spk2" and b["loop"] == 1 and b["stopped"] == 1
    assert b["n_samples"] == 2 * 256 + int(0.01 * SR)  # two blocks, then the 10 ms fade
    x, _ = sf.read(str(tmp_path / "audio_000.flac"), dtype="float32", always_2d=True)
    k = int(a["sample"])
    assert x[k - 1, 0] == 0 and x[k: k + 100, 0] == pytest.approx(0.5 * 10 ** (-6 / 20), abs=1e-4)  # sample-exact
    sess = Session(tmp_path, {"audio": {**rec.info(), "files": rec.files}})
    ev = playback_events(sess, lambda s: np.asarray(s, float) / SR)  # review reads the same log (AU3)
    assert [e.emitter for e in ev] == ["system:spk1", "system:spk2"] and ev[0].start_s == pytest.approx(k / SR)


# --------------------------------------------------------------------------- outputs: audio device and rules


class FakePlayer:
    def __init__(self):
        self.calls = []
        self.library = SimpleNamespace(sounds={"x": 1}, folder="sounds")
        self._next = 0

    def play(self, sound, speakers="all", gain_db=0.0, loop=False, source=""):
        self.calls.append(("play", sound, speakers, gain_db, loop, source))
        self._next += 1
        return [self._next], f"{sound} on {speakers}"

    def stop(self, speakers=None, voices=None):
        self.calls.append(("stop", speakers, voices))
        return "stopped"

    def stop_all(self):
        self.calls.append(("stop_all",))


def test_audio_output_device_actions():
    from mavric.outputs.devices import make_device

    fp = FakePlayer()
    dev = make_device("spk", {"type": "audio"}, {"player": lambda: fp})
    ctx = {"rule": "r1", "subject": "S03", "event": "enter"}
    res = dev.execute("play", {"sound": "{subject}_call", "speakers": "spk2", "gain_db": -6, "loop": False}, ctx)
    assert res == "play S03_call on spk2" and fp.calls[-1] == ("play", "S03_call", "spk2", -6.0, False, "rule:r1")
    dev.execute("hold_play", {"sound": "buzz", "speakers": "all", "gain_db": 0}, {**ctx, "event": "start"})
    assert fp.calls[-1][4] is True  # looped while the rule is active
    dev.execute("hold_play", {"sound": "buzz"}, {**ctx, "event": "stop"})
    assert fp.calls[-1] == ("stop", None, [2])
    dev.execute("stop", {"speakers": "spk1"}, ctx)
    assert fp.calls[-1] == ("stop", "spk1", None)
    dev.safe_state()
    assert fp.calls[-1] == ("stop_all",)
    off = make_device("spk", {"type": "audio"}, {"player": lambda: None})
    with pytest.raises(RuntimeError, match="not connected"):
        off.execute("play", {"sound": "x"}, ctx)
    off.safe_state()  # no audio: nothing to stop, no error


def test_rules_play_sounds_and_disarm_stops_them():
    from mavric.config import TagMap
    from mavric.outputs.config import OutputsConfig, Rule
    from mavric.outputs.engine import ActionEngine

    cfg = OutputsConfig({"spk": {"type": "audio"}}, [
        Rule("greet", "spk", "play", {"sound": "coo_A", "speakers": "spk2", "gain_db": -3.0}, antennas=[2]),
        Rule("buzz", "spk", "hold_play", {"sound": "buzz", "speakers": "all"}, antennas=[1], event="while"),
    ])
    assert cfg.validate() == []
    bad = OutputsConfig({"spk": {"type": "audio"}}, [Rule("b", "spk", "hold_play", {"sound": "x"})])
    assert "meant for 'while'" in bad.validate()[0]
    fp = FakePlayer()
    eng = ActionEngine(cfg, TagMap(), {1: "PlaceA", 2: "PlaceB"}, resources={"player": lambda: fp})
    eng.start()
    try:
        eng.arm()
        eng.on_presence(SimpleNamespace(reader=2, tag="AB12", prev_tag="", host_abs=time.perf_counter()))
        eng.on_presence(SimpleNamespace(reader=1, tag="AB12", prev_tag="", host_abs=time.perf_counter()))
        t_end = time.monotonic() + 3
        while len(fp.calls) < 2 and time.monotonic() < t_end:
            time.sleep(0.01)
        plays = sorted(c for c in fp.calls if c[0] == "play")
        assert plays == [("play", "buzz", "all", 0.0, True, "rule:buzz"),
                         ("play", "coo_A", "spk2", -3.0, False, "rule:greet")]
        eng.disarm()
        assert fp.calls[-1] == ("stop_all",)
    finally:
        eng.stop()


# --------------------------------------------------------------------------- simulated rig, end to end


def test_soundboard_in_a_simulated_recording(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from mavric.acquire import simulation_controller
    from mavric.audio.check import tone_response
    from mavric.config import load_rig
    from mavric.gui.acquire_window import AcquireWindow
    from mavric.gui.tips import missing_tips

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    rig = load_rig()
    rig["paths"] = {**rig["paths"], "sessions_dir": str(tmp_path), "outputs_file": str(tmp_path / "outputs.yaml"),
                    "video_settings_file": str(tmp_path / "v.yaml")}
    rig["audio"] = {**rig["audio"], "speakers": {1: "spk1", 2: "spk2"}, "sounds_dir": str(tmp_path / "sounds")}
    ctl = simulation_controller(rig, seed=3)
    win = AcquireWindow(ctl, sim=True)
    try:
        ctl.connect_audio()
        win._build_audio_views()
        assert win.sound_box.isVisibleTo(win) and win.sb_sound.currentData() == TEST_TONE
        assert win.sb_speaker.count() == 3 and missing_tips(win) == []
        t_end = time.monotonic() + 5
        while ctl.outputs is None and time.monotonic() < t_end:  # the window starts the outputs in a thread
            app.processEvents()
            time.sleep(0.02)
        sess = ctl.start_recording("playback", "test")
        time.sleep(0.6)
        win.sb_speaker.setCurrentText("spk1")
        win.sb_gain.setValue(-6.0)
        win._play_sound()
        time.sleep(1.0)
        win._stop_sounds()
        sess = ctl.stop_recording()
    finally:
        win.close()
        app.processEvents()
    log = pd.read_csv(sess.file(PLAYBACK_FILE), dtype={"speaker": str, "sound": str, "source": str})
    assert list(log["sound"]) == [TEST_TONE] and log["speaker"][0] == "spk1" and log["source"][0] == "soundboard"
    assert log["n_samples"][0] == int(0.2 * SR)
    assert sess.meta["audio"]["sounds"][TEST_TONE]["file"] == "(built in)"
    acts = pd.read_csv(sess.file("actions.csv"), keep_default_na=False)
    assert acts.loc[0, "event"] == "manual" and acts.loc[0, "device"] == "soundboard"
    import soundfile as sf

    x, _ = sf.read(str(sess.file("audio_000.flac")), dtype="float32")
    r = tone_response(x[:, 0], int(log["sample"][0]), SR, dur_s=0.2)  # micA, beside spk1
    assert r["heard"] and r["delay_ms"] == pytest.approx(3.0, abs=0.5)  # only the 3 ms of air remain
