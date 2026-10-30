# MAVRIC quick-use guide

From an installed MAVRIC to a first recorded, reviewed and analyzed session. The guide covers where everything lives,
what each setting means, how to set up and check each device, and what a session folder contains.

- **Not installed yet?** Follow [installation.md](installation.md) first.
- **The full manual** for each window is the [README](../README.md).
- **Every option in depth:** [reference.md](reference.md). **Wiring and pin maps:** [wiring.md](wiring.md).

**Contents**
1. [The short version](#1-the-short-version)
2. [The MAVRIC folder](#2-the-mavric-folder)
3. [Configuration](#3-configuration)
4. [Setting up the devices](#4-setting-up-the-devices)
5. [Recording a session](#5-recording-a-session)
6. [The session folder](#6-the-session-folder)
7. [After the recording](#7-after-the-recording)

---

## 1. The short version

| Step | Do | Check |
|---|---|---|
| 1. Configure | Edit `config/rig.yaml` (places, cameras, audio) and `config/tags.csv` (tags → subjects) | `mavric ports` lists your devices |
| 2. Flash | Upload `firmware/MultiRFID_Reader_ID20LA_sync` to the RFID Arduino MEGA | `mavric link-test` prints tag lines and `SYNC` every 2–6 s |
| 3. Check devices | One at a time (§4) | each check in §4 passes |
| 4. Acquire | `mavric acquire` → connect → draw ROIs → *Save ROIs to rig* | ROIs light up with tags; the sync box flickers |
| 5. Record | Name, notes, *● Start recording* | the top line shows no warnings |
| 6. Afterwards | `mavric review`, `mavric analyze`, `mavric compress` | the sync check says **PASS** |

Without any hardware, `mavric acquire --sim` runs the whole chain on a simulated cage.

---

## 2. The MAVRIC folder

MAVRIC works from one **project folder** (the "rig folder"), normally the folder you cloned:

```
C:\Code\MAVRIC\
  config\                 your rig's settings; git ignores everything here except examples\
    examples\             templates shipped with MAVRIC
    rig.yaml              devices, places, cameras, audio, sync, paths        you edit it
    tags.csv              tag ID → subject name and colour                    you edit it
    rois.json             place ROIs and the sync LED box (and heights)       written by Acquire
    video.yaml            camera and recording settings                       written by Video settings…
    outputs.yaml          output devices, rules, camera clip rules            written by Outputs…
    sound_types.yaml      sound event types                                   written by Types… (review)
    calibration.yaml      cameras' lens and position; mics and speakers       written by Calibrate cameras…
  sessions\               one folder per recording (git ignores it)
  sounds\                 stimuli to play on the speakers (git ignores it)
  firmware\               Arduino sketches
  docs\                   these guides
```

**Your settings stay on your PC.** Git ignores `config/`, `sessions/` and `sounds/`, so tag IDs, camera serial
numbers, ports and ROIs are never uploaded, and `git pull` never overwrites them.

**First run.** The first `mavric` command copies every file from `config/examples/` into `config/`. It never
overwrites a file that already exists. To start again from an example, delete your copy and run any `mavric` command.

**Where MAVRIC looks for the project folder**, in this order:
1. the folder in the `MAVRIC_ROOT` environment variable;
2. the current folder, if it contains `config/rig.yaml`;
3. the folder MAVRIC was installed from.

To use `mavric` from any folder, set the variable once in the conda prompt:

```bash
conda env config vars set MAVRIC_ROOT=C:\Code\MAVRIC
```

Then run `conda deactivate` and `conda activate mavric`.

**Other options**
- **Sessions on a data drive:** set `paths: sessions_dir: D:\MAVRIC_sessions` in `rig.yaml`.
- **A second rig on the same PC:** keep a second rig file and pass it with `--rig`, e.g.
  `mavric --rig config/rig_b.yaml acquire`.
- **Moving to a new PC:** copy `config/*.yaml`, `config/tags.csv`, `config/rois.json` and `sounds/`. Then check the
  ports and serial numbers, which differ between PCs.

---

## 3. Configuration

All settings are plain text files. Open them in any text editor (Notepad, VS Code, Notepad++). In YAML files,
indentation matters: use spaces, never tabs. Everything after `#` is a comment.

### 3.1 `rig.yaml`: the rig

The file has eight sections. The example in `config/examples/rig.yaml` explains every line; the most important
settings are below.

**`rfid`: the RFID Arduino MEGA**

| Key | Set to | Notes |
|---|---|---|
| `port` | `auto`, or the COM port (e.g. `COM3`) | `auto` takes the first Arduino found. Use the explicit port when more than one Arduino is connected. |
| `baud` | `500000` | Must match the firmware. Leave it. |
| `reset_wait_s`, `round_period_s` | `2.0`, `0.125` | Leave them (the board resets on connect; 8 sampling rounds per second). |

**`antennas`: one line per RFID reader**

```yaml
antennas:
  1: Feeder       # reader 1 in the firmware → the place called "Feeder"
  2: NestBox
  3: PerchLeft
  4: PerchRight
```

- **The number** is the reader number in the firmware (1–4; [wiring.md §2](wiring.md#2-rfid-readers-id-20la-integrated-antenna)).
- **The name** is the *place*. It names the ROI, the label lanes, the analysis tables and the output rules. Use short
  names without spaces, and keep them once you have recorded sessions.

**`cameras`: one entry per camera**

```yaml
cameras:
  - id: cam0              # unique; the first camera carries the place ROIs and the sync LED box
    backend: flir         # flir | basler | opencv | file | synthetic
    device: "12345678"    # serial number (flir, basler), index (opencv), file path (file)
    fps: 30
    exposure_us: 8000     # shorter than 1/fps
    gain_db: 0.0
    pixel_format: Mono8   # Mono8 | BGR8
    record: continuous    # continuous | triggered (clips only)
```

| Key | Meaning |
|---|---|
| `id` | The camera's name in file names (`cam0_raw_000.mkv`, `frames_cam0.csv`). |
| `backend` | `flir`: FLIR / Point Grey (Spinnaker). `basler`: Basler (pylon). `opencv`: webcams. `file`: replay a video. `synthetic`: a test image. |
| `device` | Serial number, from `mavric ports` or SpinView. `""` takes the first camera found, which is fine for one camera only. |
| `fps`, `exposure_us`, `gain_db`, `pixel_format` | Starting values. *Video settings…* in Acquire changes them and saves `video.yaml`. |
| `record` | `continuous`: the whole session. `triggered`: only clips from clip rules (*Outputs… → Camera clips*). |
| `chunk_frame_counter`, `line_status`, `link_limit_mb_s`, `white_balance` | FLIR details; see [reference.md](reference.md#flir--point-grey-cameras-chameleon3-blackfly-s). |

**`video`: recording format.** `record_format: raw` (default) keeps every frame lossless in about 2 GB files, to be
compressed afterwards. `h264` compresses while recording. *Video settings…* sets the same values.

**`triggers`: camera clip rules.** Easier in Acquire (*Outputs… → Camera clips*); see the [README](../README.md#2-sounds-outputs-and-camera-clips).

**`audio`: the audio interface (optional)**

```yaml
audio:
  enabled: false        # true: headless recordings include audio (the window has Connect audio)
  device: MOTU          # part of the device name (`mavric audio devices` lists them)
  host_api: ASIO        # ASIO | WASAPI | MME
  sample_rate: 48000
  inputs:               # interface input → name
    1: micA
    2: micB
    3: sync             # the sync pulse from the MEGA (D50)
  sync_channel: sync
  speakers:             # interface output → name
    1: spk1
    2: spk2
  max_level_dbfs: -6    # playback limiter
  sounds_dir: sounds    # the stimulus library
  mic_near: {micA: [1, 3], micB: [2, 4]}   # antennas beside each microphone
  temperature_c: 20     # room temperature, for the speed of sound
```

| Key | Meaning |
|---|---|
| `device`, `host_api` | Which interface, and through which driver. Use ASIO with a MOTU (its driver provides it). |
| `inputs` | Every input you record, with a name. The sync input must be listed and named in `sync_channel`. |
| `speakers` | Every output you play sounds on, with a name. Leave it empty if you have no speakers. |
| `block`, `latency` | Raise `block` (e.g. 2048) or set `latency: high` if `mavric audio test` reports overflows. |
| `format`, `split_s` | `FLAC` (lossless, smaller) or `WAV`; a new file every `split_s` seconds (default 1 h). |
| `max_level_dbfs` | The loudest the speakers may get; louder mixes are clipped, and MAVRIC warns. |
| `sounds_dir` | Folder of the stimuli (§3.4). |
| `mic_near` | Which antennas (places) each microphone is next to. It ranks the emitter suggestions in review. |
| `temperature_c` | Room temperature. The speed of sound depends on it when the speakers check the device positions (*Calibrate cameras…*, tab 4). |

**`sync`: the sync LED**

| Key | Set to |
|---|---|
| `led_roi_name` | `SYNC`, the name of the box drawn over the LED. Leave it. |
| `led_pulse_s` | `0.080`, the firmware's pulse length. It must last at least 2 frames: at 13 fps, lengthen the pulse in the firmware and here. |

**`calibration`: the camera calibration board**

| Key | Set to |
|---|---|
| `board.squares` | Squares across and down, held with a black square at the top left, e.g. `[8, 10]` |
| `board.square_mm` | The square size, measured: 10 squares with calipers, divided by 10 |
| `board.marker_mm` | The side of the printed markers (the stickers' black square), e.g. `29` |
| `board.dictionary` | `DICT_4X4_50` (enough for up to 50 markers, i.e. 100 squares) |

`mavric calibrate sheet` prints the stickers for this board ([README](../README.md#calibrate-the-cameras)).

**`paths`**

| Key | Default |
|---|---|
| `sessions_dir` | `sessions` (relative to the project folder, or an absolute path) |
| `tags_file`, `rois_file`, `outputs_file`, `sound_types_file`, `calibration_file` | the files in `config/` |

### 3.2 `tags.csv`: who is who

One row per tag:

```
tag_id,subject,color
0A000000010B,S01,#2a78d6
0A0000000208,S02,#eb6834
```

- **`tag_id`:** the 12 hexadecimal characters that `mavric link-test` prints when you hold the tag to a reader.
- **`subject`:** the name used everywhere (labels, analysis, rules). Short, without spaces.
- **`color`:** the colour of that subject in every window and plot.
- **Order matters in review:** the first subject is key `1`, the second key `2`, and so on.
- **Unknown tags** are still recorded, and the analysis lists them.

### 3.3 Files the windows write

You rarely edit these by hand:

| File | Written by | Holds |
|---|---|---|
| `rois.json` | Acquire → *Save ROIs to rig*; *Calibrate cameras…* → *Save places* | one polygon per place (on the first camera's image) and the sync LED box; once placed in 3D, their heights and outlines in mm |
| `video.yaml` | Acquire → *Video settings… → Save* | camera settings and the recording format; they override `rig.yaml` |
| `outputs.yaml` | Acquire → *Outputs… → Save & restart outputs* | output devices, rules, camera clip rules |
| `sound_types.yaml` | Review → Sound events → *Types…* | the list of sound event types (F1–F9) |
| `calibration.yaml` | Acquire → *Calibrate cameras…* → *Save* / *Save positions* / *Save devices* | each camera's lens (focal length, image centre, distortion, error, date) and position in mm, and each microphone's and speaker's position and aim; `calibration/` keeps the board corners of the views used |

Every session keeps a copy of the tag map, the ROIs and the camera calibration it used, so later edits don't change
old sessions.

### 3.4 The sounds folder

Stimuli to play on the speakers go in `sounds/` (or the folder in `audio.sounds_dir`):
- **Formats:** WAV, FLAC or OGG. 48 kHz mono is best; other files are converted when the library loads.
- **Names:** a sound is called by its path without the extension: `sounds/calls/coo_A.wav` is `calls/coo_A`.
- **Built in:** `test_tone` (1 kHz, 200 ms), for checks without files.
- **Check:** `mavric audio sounds` lists every sound with its length and peak level.

---

## 4. Setting up the devices

Set up and check **one device at a time**, in this order. Each check works without the others.

| Device | Needed for | Firmware / driver | Check |
|---|---|---|---|
| RFID Arduino MEGA + readers | identification (required) | `MultiRFID_Reader_ID20LA_sync` | `mavric link-test` |
| Sync LED and sync outputs | checking the timing (required) | the same firmware | the LED blinks; `SYNC` lines |
| Cameras | video | Spinnaker or pylon | `mavric camtest` |
| Audio interface, microphones, speakers | audio | MOTU driver (ASIO) | `mavric audio test --tones` |
| I/O Arduino | TTL outputs | `MAVRIC_IO` | `mavric outputs test io` |
| Intan RHX, MATLAB, programs, network | other outputs | their own software | *Test* in *Outputs…* |

`mavric ports` lists every serial port and camera MAVRIC can see. Run it first.

### 4.1 RFID: Arduino MEGA and readers

1. **Wire** the readers to the MEGA ([wiring.md §2–3](wiring.md#2-rfid-readers-id-20la-integrated-antenna)).
2. **Flash** the firmware (once; [installation.md §7](installation.md#7-firmware)).
3. **Find the port:** `mavric ports`, then set `rfid.port` in `rig.yaml` (or leave `auto`).
4. **Check:**
   ```bash
   mavric link-test --port COM3
   ```
   Hold each tag to each reader in turn. Every reader must report every tag, and a `SYNC` line must appear every
   2–6 s.
5. **Fill in** `antennas` (reader → place) and `tags.csv` (tag → subject) from what you saw.

### 4.2 Sync pulse

The MEGA sends one random pulse train (80 ms pulses, 2–6 s apart) on three pins at once. Each stream records it, and
the analysis matches them to put everything on one clock.

| Pin | Goes to | Needed |
|---|---|---|
| D52 | the sync LED, in view of every camera | required with cameras |
| D50 | the audio interface's sync input, through a divider | required with audio |
| D48 | spare: Intan DIGITAL IN, or a camera's input line | optional |

- **LED:** place it where every camera sees it, but not over a place. In Acquire, draw the *sync LED box* over it.
- **Audio:** the divider and cable are in [wiring.md §7.2](wiring.md#72-sync-cable-d50--m4-in-3).
- **Check:** the LED blinks every few seconds; in Acquire the sync box brightness jumps with each blink and the audio
  panel counts the pulses.

### 4.3 Cameras

1. **Install** the camera software (Spinnaker for FLIR, pylon for Basler) and check the camera in SpinView or pylon
   Viewer. Close it afterwards: only one program can open a camera.
2. **Configure** each camera in `rig.yaml → cameras`, with its serial number from `mavric ports`.
3. **Check:**
   ```bash
   mavric camtest --cam cam0
   ```
   It reports the measured frame rate (it must equal `fps`), dropped frames and timing warnings, and saves a
   snapshot to `sessions/_camtest_cam0.png`. `--cam all` tests every camera at once, which shows USB bandwidth
   problems.
4. **Position:** the first camera must see every place and the sync LED. Use a fixed exposure and gain and even
   light; short exposures avoid motion blur.

Raw video needs about **50 GB per camera-hour** (1280×1024, 30 fps). Acquire warns when the disk has room for less
than 2 h.

### 4.4 Audio: interface, microphones, speakers

1. **Connect** the interface (driver first), the microphones (with phantom power if they need it), the sync cable
   and the speakers ([wiring.md §7](wiring.md#7-audio-motu-m4-two-rode-nt5-sync-pulse-four-speakers)).
2. **Configure** `rig.yaml → audio`: `device`, `inputs` (with the sync input), `speakers`.
3. **Find the interface:**
   ```bash
   mavric audio devices
   ```
   The configured device is marked `<- configured`.
4. **Check inputs and speakers:**
   ```bash
   mavric audio test --tones
   ```
   It plays a tone on each speaker in turn and reports, per input, the level, clipping and the sync pulses
   received. It also measures the round trip and, per microphone, the delay through the air.
5. **Check one sound:**
   ```bash
   mavric audio play test_tone --speaker spk1 --gain-db -10
   ```
   Start quietly. Without speakers, headphones on the interface work for a first check (on a MOTU M4 they carry
   outputs 1–2 only).

### 4.5 Outputs (optional)

Outputs act when RFID sees a subject: a TTL pulse, a recording start in Intan, MATLAB code, a program, a network
message or a sound.

1. **I/O Arduino:** flash `firmware/MAVRIC_IO` and wire its pins ([wiring.md §6](wiring.md#6-io-arduino-event-driven-outputs)).
2. **In Acquire → Outputs… → Devices,** add each device with its settings, then *Save & restart outputs*.
3. **Test** each device: the *Test* button, or
   ```bash
   mavric outputs test io
   ```
4. **Rules tab:** which places, which subjects, which event (arrival, departure, while present), which action.
5. **Arm outputs** before the session. Nothing fires while disarmed; the session's `actions.csv` logs every
   decision either way.

### 4.6 Simulated devices

Every device has a simulated twin, for learning and testing:
- `mavric acquire --sim`: a simulated cage, Arduino, cameras and audio;
- RFID port `SIM` in Acquire, I/O Arduino port `SIM` in *Outputs…*, camera `backend: synthetic`;
- `mavric audio test --sim` and `mavric audio play <sound> --sim`.

---

## 5. Recording a session

**Before the session**
- Disk space: about 50 GB per camera-hour of raw video.
- Sounds: the files you need are in `sounds/` (*↻ sounds* reloads them).
- Outputs: the rules are saved and tested.

**In `mavric acquire`**
1. **Connect:** RFID port → *Connect RFID*; each camera → *Connect camera*; *Connect audio*.
2. **ROIs** (once per setup, on the first camera): *Draw / reset* one polygon per place and fit its corners, then
   *Draw / reset sync LED box* over the LED, then *Save ROIs to rig*.
3. **Check:**
   - a place ROI takes the subject's colour while its reader reads the tag;
   - the sync LED box brightness jumps every 2–6 s;
   - the audio meters move and the sync pulses are counted;
   - the top status line shows no warnings.
4. **Arm outputs**, if you use them.
5. **Record:** enter a session name and notes, then *● Start recording*. Every stream is written to disk as it
   arrives, so a crash keeps everything up to the last second.
6. **Stop:** *■ Stop recording*. The session folder is complete.

**Without the window** (e.g. scheduled recordings):

```bash
mavric acquire --headless --duration 3600 --name night01 --audio --arm-outputs
```

---

## 6. The session folder

Each recording is one folder, `sessions/<date>_<time>_<name>/`. All times are **seconds since the session start**,
on one clock for every stream. Everything is CSV, JSON or standard media, so MATLAB (`readtable`, `jsondecode`),
R and Python read it directly.

```
sessions/20261029_101500_demo/
  session.json             setup, clock, devices, statistics
  tags.csv, rois.json      the tag map and ROIs used
  calibration.yaml         the camera calibration used (if there is one)
  rfid_raw.csv             every line from the RFID Arduino
  cam0_raw_000.mkv …       video (raw, lossless), or cam0.mp4 (H.264)
  frames_cam0.csv          the time of every frame
  audio_000.flac …         audio, one file per hour
  audio_clock.csv          audio block times
  audio_edges.csv          sync pulse edges in the audio
  audio_playback.csv       every sound played on the speakers
  actions.csv              every output decision
  clips/                   camera clips, with their frame times
  manual_labels.csv …      from review
  analysis/                from analyze
```

**Written while recording**

| File | Contents |
|---|---|
| `session.json` | Name, notes, start and stop (UTC and local), MAVRIC version, the clock anchor, a copy of `rig.yaml`, the cameras and audio as used, the sounds library (name, length, SHA-1 of each file), clips, statistics |
| `rfid_raw.csv` | One row per RFID line: `host_t` (arrival, s), `segment` (increments when the Arduino resets), `micros` / `arduino_us` (Arduino clock), `kind` (`TAG`, `NONE`, `SYNC`, `BOOT`), `tag`, `reader`, `checksum_ok`, `raw` |
| `<cam>_raw_000.mkv`, `_001` … | Raw video in ~2 GB files (FFV1, lossless; `.avi` if uncompressed). `<cam>.mp4` when recording H.264 |
| `frames_<cam>.csv` | One row per frame: `frame_idx` (in the video), `grab_idx` (gaps = dropped), `host_t`, `hw_ticks` and `hw_counter` (camera clock and counter), `line_status`, `led_mean` (sync box brightness) |
| `audio_000.flac`, `_001` … | All audio inputs, sample-aligned: one channel per input in `audio.inputs`, in input-number order (names in `session.json → audio`) |
| `audio_clock.csv` | One row per audio block: first `sample`, `n`, host time, interface time, flags (overflows) |
| `audio_edges.csv` | Sync pulse edges: `sample` (fractional), `sign`, `amp` |
| `audio_playback.csv` | One row per sound played: `sample` (recording sample at which it reached the speaker output), `n_samples`, `speaker`, `sound`, `gain_db`, `loop`, `stopped`, `source` (`soundboard`, `rule:<name>`), `latency_samples` |
| `actions.csv` | One row per output decision: times, rule, event, place, subject, tag, device, action, `status` (`ok`, `error`, `dry` = not armed, `suppressed`), latency |
| `clips/` | `<camera>_<rule>_<nnn>` video (raw `_raw_000.mkv` or `.mp4`) and `_frames.csv`, same columns as `frames_<cam>.csv` |

**Written by review**

| File | Contents |
|---|---|
| `manual_labels.csv` | Place labels: `annotator`, `roi` (place), `pigeon` (the subject), `start_s`, `stop_s`, `flag` |
| `label_coverage.csv` | The time each annotator actually watched: `annotator`, `start_s`, `stop_s` |
| `rfid_curation.csv` | RFID bouts marked spurious or valid |
| `sound_events.csv` | Sound events: times, `type`, `emitter`, `channel`, frequency range, `confidence`, `source`, `note` |
| `sound_coverage.csv` | The audio time each annotator reviewed |

**Written by analyze, render and compress**

| File | Contents |
|---|---|
| `analysis/` | `sync_report.json` (clock fits, sync check **PASS**/FAIL), `summary.json`, RFID bouts, RFID vs labels, time budgets, plots, `audio_time_knots.csv` (audio sample → session time), sounds linked to places (`sound_events_linked.csv`, `call_rates.csv`, `playback_responses.csv`, `playback_by_subject.csv`), `sound_proposals.csv`, `annotated_<cam>.mp4`, `timeline/` ([reference](reference.md#3-analyze-mavric-analyze-session)) |
| `<cam>_compressed.mp4` | The H.264 copy from `mavric compress` (every frame, constant rate) |

The `pigeon` column name in the label and analysis tables is historical; it holds the subject name from `tags.csv`.

---

## 7. After the recording

| Step | Command | What you get |
|---|---|---|
| Review | `mavric review sessions/<session>` | Place labels (blind, then curate) and sound events. A start dialog asks your initials and whether to review video, audio or both. |
| Analyze | `mavric analyze sessions/<session>` | The sync check (**PASS**/FAIL), RFID accuracy against your labels, bouts, time budgets, sounds per subject and place, answers to played sounds, and plots in `analysis/`. |
| Render | `mavric render sessions/<session>` | The annotated video; with audio, the sound and a spectrogram strip. |
| Timeline | `mavric timeline sessions/<session>` | Audio and label tracks on the video's clock for Audacity, Ethograph or BORIS. |
| Compress | `mavric compress sessions/<session>` | `<cam>_compressed.mp4`, for sharing and DeepLabCut. The raw files are kept. |

How each window works, the keys and the analysis outputs: the [README](../README.md).
