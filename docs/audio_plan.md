# Audio: recording, labeling and playback

**Goal.** Add an audio stream to MAVRIC:
- **record** two Rode NT5 microphones as independent channels, synchronized with RFID, video (and Intan)
- **see** the waveforms and spectrograms live and in review
- **label** sound events (start/end, type, possible emitter)
- **play** pre-recorded sounds on chosen speakers, by hand or from rules (RFID now, camera ROIs later)

> **Status (2026-10-21).** **AU0–AU2 are built** and tested in simulation:
> - `mavric audio devices` and `mavric audio test` (bench check)
> - multichannel recording in acquisition, with live meters and spectrograms
> - the sync channel, and the audio clock in `mavric analyze`
>
> They have not yet run on the MOTU M4 (not connected to the development PC). The PortAudio path was checked on a
> USB microphone.
>
> **AU3 (sound events in `mavric review`) is built** (2026-10-23) and tested on simulated sessions.
>
> **Still to build:** AU4 (playback) and AU5 (analysis).
>
> The project lead's decisions are in §7.

---

## 1. Hardware: what each device can do

| Device | Relevant facts | Role in MAVRIC |
|---|---|---|
| **Rode NT5** (×2) | small-diaphragm condenser, cardioid; **needs phantom power** (P48/P24); 20 Hz–20 kHz; −38 dB re 1 V/Pa; max SPL 143 dB; 16 dB-A self-noise | the two recording channels |
| **MOTU M4** (USB-C) | **2 mic/line preamps with individual +48 V**, ~60 dB gain; **2 more balanced line inputs** (3–4); **4 balanced TRS outputs** (+ RCA mirrors); headphones; 44.1–192 kHz, 24-bit; **loopback** channels in the driver | **recording interface** (mics on In 1–2, sync pulse on In 3); **outputs 1–4 can drive up to 4 speakers** on the *same clock* as the recording |
| **MOTU 8A** (USB / Thunderbolt) | **8 line-level inputs, no mic preamps, no phantom**; **8 balanced, DC-coupled line outputs**; ADAT; 44.1–192 kHz; ASIO + WDM on Windows (AVB is Mac-only); discontinued model | **playback to up to 8 speakers**; line-level inputs only (cannot power or amplify the NT5s) |
| **Speakers** | need active (powered) speakers, or an amplifier plus passive speakers, on the line outputs | stimulus playback |

**Two hardware consequences**
- **Mics:** the NT5s **must go into the M4**, which has the phantom power and preamps. The 8A cannot take them
  directly.
- **Speakers:** for **up to 4 speakers, the M4 alone is the simplest and most precise option.** Recording and playback
  then run in one *duplex* stream, on one sample clock, so every played sound is placed in the recording with sample
  accuracy. Use the **8A** when more than 4 speakers are needed, or to keep playback separate from the recording
  interface.

**Pigeon hearing.** Pigeons are unusually sensitive at low frequencies, down into infrasound, and hear up to roughly
7–10 kHz. Recording at **48 kHz** (Nyquist 24 kHz) is ample. Speakers will not reproduce infrasound; choose speakers
whose response covers the stimuli used.

## 2. Synchronization (the key design point)

Audio has its own crystal clock, so it drifts against the PC and the Arduino just like the camera. There are two
layers, mirroring what the camera already does:

1. **Clock fit.** Each audio block arrives in a callback. MAVRIC logs (first sample index, host time) per block and
   fits the lower envelope of sample time against host time, as for camera hardware ticks. This gives the offset and
   drift, about 1 ms after the fit.
2. **Sync pulse recorded as audio (recommended, nearly free).** Feed the RFID MEGA's random **D52 sync pulse** into
   **M4 line input 3**.
   - **Level:** a 5 V TTL is too hot for a line input. Use a **resistor divider** (e.g. 10 kΩ / 1 kΩ → ≈ 0.45 V),
     or 10 kΩ in series into the input with 1 kΩ to ground, then a TS/TRS cable to In 3. Common ground with the MEGA.
   - **Result:** every pulse edge appears in the audio file at sample accuracy (≈ 21 µs at 48 kHz). The same
     pulse-train matcher used for the LED/video then aligns audio ↔ RFID ↔ video to well under a millisecond.
   - **With Intan:** the same D52 train also goes to Intan DIGITAL IN 2, so ephys joins the same timeline.

**Playback timing**
- **On the M4 (duplex):** the exact output sample of each sound is known, and that is its time in the recording.
- **On the 8A:**
  - Log the output stream's sample index at sound start, and fit the 8A's clock like the M4's.
  - For exact verification, **loop a copy back into the recording**: route a "marker" output channel of the 8A, or
    the stimulus itself, into **M4 line input 4**.

## 3. Software choices

| Need | Choice | Why |
|---|---|---|
| Device access | **python-sounddevice** (PortAudio), installed **with pip** | Windows wheels include an **ASIO-capable** PortAudio, selected with `SD_ENABLE_ASIO=1` before import. The conda-forge build has **no ASIO**, so pip it is. WASAPI (shared/exclusive) and WDM also work. |
| Driver API | **ASIO** for the M4 (MOTU M-Series driver); **WASAPI exclusive** (or ASIO) for the 8A | ASIO gives low, stable latency and exact channel selection. Usually only **one ASIO driver per process** is possible, so if the M4 and 8A are used together, put one of them on WASAPI. |
| Files | **python-soundfile** (libsndfile): **FLAC** (lossless, ~½ size) or **RF64/W64 WAV** (no 4 GB limit) | Long sessions: 4 ch × 48 kHz × 24 bit ≈ 2 GB/h WAV, ≈ 1 GB/h FLAC. Files are split per hour, without gaps. |
| Live view | pyqtgraph (already used): level meters, scrolling waveform, **scrolling spectrogram** (STFT on a ring buffer) | Same GUI stack, cheap at 48 kHz |
| Label formats | own `sound_events.csv` plus **crowsetta** for **Audacity label tracks, Raven selection tables, Praat TextGrids** | Annotations can be made or checked in Audacity/Raven/Sonic Visualiser and imported, the way BORIS is for video |
| Later: auto-detection | band-limited energy / onset detector first; trained segmenters later (e.g. DAS) | Pre-segments candidate calls so labeling is review, not search |

## 4. Configuration (`config/rig.yaml`)

As implemented (AU0–AU2):

```yaml
audio:
  enabled: true            # headless acquisition records audio (the GUI has a Connect audio button)
  device: MOTU             # name substring; `mavric audio devices` shows what matches
  host_api: ASIO           # ASIO | WASAPI | MME | DirectSound | WDM-KS
  sample_rate: 48000
  block: 1024              # samples per callback (21 ms); robust against Python hiccups
  latency: high            # PortAudio suggested latency: high | low | seconds
  inputs:                  # M4 input (1-based) -> name; listed in full (no merge with defaults)
    1: micA                # NT5, 48 V on
    2: micB                # NT5, 48 V on
    3: sync                # RFID MEGA D52 through the divider (wiring.md §7)
  sync_channel: sync
  speakers:                # M4 output (1-based) -> name; opened in the same (duplex) stream
    1: spk1
    2: spk2
    3: spk3
    4: spk4
  format: FLAC             # FLAC | WAV (RF64)
  subtype: PCM_24
  split_s: 3600            # one file per hour, no gap between files
  max_level_dbfs: -6       # playback limiter
  display_fmax_hz: 10000   # live spectrogram range
```

AU4 will add `sounds_dir` (the stimulus library).

## 5. Integration plan

### AU0: Hardware and bring-up (bench) [built]
1. **Driver:** install the MOTU M-Series driver.
2. **Mics:** connect the NT5s to M4 In 1–2 and switch 48 V **on**. Set the gain so loud calls peak around −12 dBFS.
   Switch **off** the MON (direct monitoring) buttons, so the mics never reach the speakers.
3. **Sync and speakers:** build the sync divider cable (D52 → In 3), and connect the four powered speakers to
   M4 Out 1–4 (wiring.md §7).
4. **`mavric audio devices`:** lists host APIs, devices and channel counts, and marks the configured device.
5. **`mavric audio test [--tones]`:** records 10 s and reports, per channel:
   - peak and RMS level, and clipping
   - driver over/underflows
   - the sync pulses found (count, width, intervals, step level)
   - the audio clock drift against the PC

   With `--tones` it also plays a 1 kHz, 100 ms tone on each speaker in turn. For each tone it reports, per
   microphone: whether it was heard, its level, and the speaker → mic delay (interface round trip plus the
   distance through the air). The recording is saved to `sessions/_audiotest.flac` for listening.

### AU1: Recording [built]
`mavric/audio/` (stream, recorder, edges, sim, check)
- **Stream:** one PortAudio stream with the configured inputs. It is **duplex**, with the speaker outputs, whenever
  speakers are configured.
- **Threads:** the callback only stamps the host clock, renders the playback mixer and queues the block. A worker
  thread then feeds:
  - the live ring buffer, meters and sync edge detector
  - the recorder's own writer thread, which writes:
    - `audio_000.flac`, …
    - `audio_clock.csv`: per 0.1 s, the least-delayed block delivery, plus every block flagged with an overflow
    - `audio_edges.csv`: sync edges, in samples
- **Session:** `session.json["audio"]` stores the device, rate, channel map, files and statistics, next to the
  cameras.
- **Acquire GUI:** an **Audio panel** with per-channel level meters and clip indicators, a scrolling spectrogram of
  the two mics, and dropout counters.
- **Simulator:** the cage gets a synthetic audio source (calls from the pigeon on a perch, sync pulses on channel 3),
  so everything is testable without hardware, like the synthetic camera.

### AU2: Synchronization [built]
- **Clock fit:** the audio clock (sample ↔ host time) is fitted with the lower-envelope fitter.
- **Sync edges:** detected live, AC-coupling safe and of either polarity. Each is located at its half-height
  crossing, to a fraction of a sample.
- **Pulse matching:** the edges are paired into 80 ms pulses and matched to the RFID `SYNC` lines with the existing
  pulse-train matcher.
- **Map:** audio sample → session time is the piecewise-linear interpolation through the matched pulses, so it is
  exact at each pulse whatever either clock does. Recordings are split into segments at input overflows.
- **Without a sync channel:** the host-clock fit alone is used (a few ms).
- **Outputs:** the report goes to `sync_report.json["audio"]`, and the map to `analysis/audio_time_knots.csv`.
- **In simulation:** audio events land on the RFID timeline within 1 µs (+25 ppm interface drift, AC-coupled sync
  input).

### AU3: Review and labeling [built]

**As built**
- **Code:** `mavric/audio/source.py` (reading, overview, spectrograms), `detect.py` (proposals, two-mic measurements,
  suggestions), `events.py` (files, types, Audacity/Raven), `mavric/gui/sound_panel.py`, and
  `mavric sounds detect|export|import`.
- **Overview:** min/max per 1024 samples, cached in `analysis/audio_envelope.npz`. Spectrograms are computed for the
  visible span when it is 60 s or less.
- **Emitter cues:**
  - **Level difference:** robust; right for 28 of 29 isolated simulated calls.
  - **Arrival-time difference:** GCC-PHAT, used only when its peak is unique. Tonal coos repeat their correlation every
    pitch period, so for coos it mostly stays unused; it helps with broadband sounds.
- **Suggestions in simulation:** the caller is among the top two in 50/50 calls, and first in about two thirds. The
  misses are two birds on the same side, or overlapping calls.
- **Proposals in simulation:** 100% of calls found, median onset error 14 ms. Overlapping calls are merged into one
  proposal.
- **Playback log:** `audio_playback.csv` (speaker, sound, sample, n_samples) is read if present. AU4 will write it.
- **Praat TextGrids:** not included (Audacity and Raven only).

**Original plan**

In `mavric review`:
- **Audio lanes** under the video: a waveform envelope and a spectrogram for each mic, computed in tiles so
  multi-hour sessions stay fast. They are synced to the playhead.
- **Listening:** play the audio around the playhead (selected mic, or both in stereo) while scrubbing or at reduced
  speed.
- **Sound event labeling:** a mode next to perch labeling. Mark **start/end** by keys or by dragging on the
  spectrogram. Assign an event **type** and an **emitter**:
  - **Types:** a customizable list, edited in the GUI and stored with the project (e.g. coo, bow-coo, wing clap,
    noise).
  - **Emitters:** any tagged subject (from `tags.csv`), **the system** (a speaker; played sounds are labeled
    automatically from the playback log, with sound file and speaker), or "unknown".
- **Emitter suggestions**, from data MAVRIC already has:
  - which mic is louder and earlier (level difference and time difference between the two mics), i.e. which side of
    the cage
  - which pigeons RFID places on perches on that side at that moment
  - later, the camera ROI activity

  The suggestions are ranked; the annotator confirms.
- **Files:** `sound_events.csv` (annotator, start_s, stop_s, channel, type, emitter, confidence, f_low/f_high
  optional) plus the coverage of reviewed time.
- **Import/export** of Audacity / Raven / Praat labels through crowsetta.
- **Auto-proposals:** a band-limited energy detector proposes events; the annotator accepts, edits or deletes them.

### AU4: Playback (stimuli)
- **Sound library:** WAV/FLAC files in `sounds/`, loaded into memory and resampled to 48 kHz at start.
  - Stimuli are short (under 2 s, mostly under 1 s), so the whole library sits in memory.
  - Any length plays as is. **No padding to a common length is needed:** the mixer plays each file to its end, and
    the log records the exact start sample and duration.
- **Player:** an always-running output stream with a small mixer. Each speaker channel plays its own queue with a
  gain and a short fade, plus a safety limiter (`max_level_dbfs`).
  - Starting a sound costs no file I/O, so latency is about one block plus the device latency.
  - Every start and stop is logged with its output sample index.
- **New output device type in the Outputs engine:** `audio`.
  - **Actions:** `play` (sound, speaker(s), gain dB, loop), `stop` (speaker or all), `hold_play` (loop while a rule is
    active).
  - **Rules** already provide: on enter / on leave / while present, antennas, birds, refractory, delay, rate limits.
    Example: "S03 lands on PerchB → play `call_A.wav` on spkB once, refractory 30 s".
- **Acquire GUI soundboard:** buttons to play any sound on any speaker by hand (logged like rule actions), and a mute
  / stop-all button. Disarming the outputs also stops all sounds.
- **Schedules (optional):** playlists with randomized intervals, for playback experiments independent of RFID.

### AU5: Analysis and export
- **Linking events:** sound events × RFID bouts tell who was where when a sound occurred. This gives call rates per
  pigeon and per perch, and responses to playback (latency from stimulus to the first call, and which bird).
- **Plots:** sound-event ethograms next to perch occupancy, and call-rate and response-latency plots.
- **Annotated video:** the rendered video gets the audio track (one mic, or a stereo mix) and a spectrogram strip.
- **Ethograph export:** audio, spectrograms, sound labels and perch labels on one timeline.

### AU6: Later
- Automatic call detection and classification (e.g. DAS or a small CNN trained on the labeled events).
- More microphones (the 8A's line inputs, or ADAT preamps) for 2-D localization of the emitter.
- Camera-ROI-triggered playback, once Phase C (camera ROI activity) exists.

## 6. Timing summary

| Path | Typical |
|---|---|
| Audio block (1024 @ 48 kHz, default) | 21 ms |
| Audio ↔ RFID alignment with the sync channel | < 1 ms (sample-accurate pulses) |
| Rule → sound start, M4 ASIO | ~5–15 ms after the rule fires |
| Rule → sound start, 8A WASAPI exclusive | ~10–25 ms |
| RFID event → rule | ~5 ms after the sampling round's lines (already implemented) |
| Sound start known in the recording | exact on the M4 (duplex); exact with a loopback channel on the 8A |

## 7. Decisions (project lead, 2026-10-21)

1. **Speakers:** 4 powered speakers on the **M4** (Out 1–4), in the same duplex stream as the microphones.
2. **Sync cable:** yes, D52 → divider → M4 In 3.
3. **Sample rate:** 48 kHz.
4. **Labels:**
   - **Event types:** customizable.
   - **Emitter:** one of the tagged subjects, or the system (speakers), labeled automatically where possible.
5. **Stimuli:** the most efficient format. Lengths are under 2 s, probably under 1 s.
   - **Format:** WAV or FLAC, 48 kHz, mono. Other rates are resampled once at load.
   - **Length:** padding to a common length is not needed.

**Still open:** whether each speaker needs a calibrated level (SPL meter at the bird's position), for AU4.

## Sources

- [MOTU M4 (Thomann)](https://thomann.ae/motu_m4.htm)
- [MOTU M4 (Juno)](https://www.juno.co.uk/products/motu-m4-usb-c-4x4-audio-interface/755895-01)
- [MOTU 8A (Full Compass)](https://fullcompass.com/prod/523224-motu-8a-16x18-thunderbolt-usb-3-0-avb-ethernet-audio-interface-with-dsp)
- [MOTU 8A (Adorama)](https://www.adorama.com/mt8a.html)
- [Rode NT5 manual](https://edge.rode.com//pdf/products/79/nt5-mp_product_manual.pdf)
- [python-sounddevice installation (ASIO)](https://python-sounddevice.readthedocs.io/en/latest/installation.html)
- [python-sounddevice version history](https://python-sounddevice.readthedocs.io/en/0.5.4/version-history.html)
- [crowsetta](https://crowsetta.readthedocs.io/)
- [Pigeon infrasound hearing (J Comp Physiol A)](https://link.springer.com/article/10.1007/BF00190824)
