# MAVRIC reference

> The detailed reference: installation variants, every option, the file formats and how the synchronization
> works. The short user manual is the [README](../README.md).


**What it does.** Video + RFID identification of individual pigeons on RFID perches. It acquires synchronized
camera video and Arduino RFID reads, supports human ground-truth labeling, and validates RFID against video.
The output is trustworthy **ID × location × time** records.

**Scope.** The first target is a single cage:
- 4–5 tagged pigeons
- 4 RFID perches, each with one ID-20LA reader on an Arduino MEGA (the [MultiRFID](https://github.com/JesusJBallesteros/MultiRFID) Arduino path)
- one industrial camera

| Stage | What it does | Command |
|---|---|---|
| Acquire | Live camera, perch ROIs tied to antennas, live RFID overlay, synchronized recording | `mavric acquire` |
| Label / curate | Blind ground-truth labeling of perch occupancy; RFID curation; disagreement navigation | `mavric review <session>` |
| Analyze | Clock sync + LED validation, RFID bouts, accuracy vs labels, time budgets, plots | `mavric analyze <session>` |
| Render | Annotated video (ROIs, RFID ID, label, disagreements) | `mavric render <session>` |
| BORIS stopgap | Use BORIS labels as ground truth | `mavric import-boris <session> <export.tsv>` |
| Triggered clips | Record video only while RFID detects a tag (any camera, incl. ones not used for ID) | `triggers:` in `rig.yaml` |
| Outputs | Drive TTL lines (Intan, LEDs, relays), Intan RHX, MATLAB, programs or network messages from RFID events | acquire → **Outputs…**, `mavric outputs` |

## Documentation

- [Project brief](project_brief.md): aims, needs and decisions, with post-hoc edits.
- [Implementation plan](implementation_plan.md): research findings and the technical plan.
- [Event-driven outputs](outputs_plan.md): feasibility, plan and status.
- [Audio](audio_plan.md): two-microphone recording and sync, sound-event labeling (built); speaker playback
  and analysis (planned).
- [Wiring and pin-map](wiring.md): readers, Arduino MEGA, sync LED and camera GPIO, with a
  [diagram](wiring_diagram.svg).

## Install (conda / Miniconda)

The tool runs in its own conda environment, defined in [`environment.yml`](../environment.yml):
- conda-forge packages only (no Anaconda `defaults` channel)
- Python 3.12, which keeps DeepLabCut 3.x usable in later phases
- a few PyPI wheels, explained in the file

**1. Install Miniconda**, if it isn't installed yet, from <https://docs.conda.io/en/latest/miniconda.html>. Run every
command below in the **Anaconda Prompt (miniconda3)**.

**2. Create the environment and install MAVRIC** into it (editable, so `git pull` updates the code):

```bash
cd C:\Code\MAVRIC
conda env create -f environment.yml
conda activate mavric
pip install --no-deps -e .
```

**3. Optional, run `mavric` from any folder.** By default, configuration (`config/`) and recordings (`sessions/`) are
found in the current folder or in the checkout. To point the environment at the rig folder instead, set:

```bash
conda env config vars set MAVRIC_ROOT=C:\Code\MAVRIC
conda deactivate
conda activate mavric
```

**4. Check the installation** (from any folder):

```bash
mavric --help
mavric selftest
```

The `mavric` command is now available whenever the environment is active (`conda activate mavric`).
`mavric selftest` runs the project's test suite (about 1 minute) wherever you start it. Extra pytest arguments pass
through, e.g. `mavric selftest -k sync`. A plain `pytest -q` also works, but only from inside `C:\Code\MAVRIC`.

**Updating:**

```bash
git pull
conda env update -f environment.yml --prune
```

**Removing:** `conda env remove -n mavric`

**Cameras**
- **Basler:** install the **pylon Software Suite** from Basler (USB3 / GigE drivers, pylon Viewer). The environment
  already contains `pypylon`, the Python binding. `mavric ports` lists the cameras it detects.
- **FLIR / Point Grey (Chameleon3, Blackfly S, ...):** needs the Spinnaker SDK and PySpin; see
  [FLIR / Point Grey cameras](#flir--point-grey-cameras-chameleon3) below.
- **IDS (peak):** the adapter is a stub in `mavric/video/backends.py`. Implement it like `FlirCamera`
  (`mavric/video/flir.py`): return the hardware timestamp and frame id with every frame.
- **Check any configured camera** before recording: `mavric camtest --cam cam0`. It reports the measured fps, drops,
  the hardware-clock fit and any sync warnings, and saves a snapshot to `sessions/_camtest_cam0.png`.

**Troubleshooting**
- **`pytest` shows hundreds of collection errors** (numpy import errors from other programs' folders, `PermissionError`
  on `My Documents`, `Local Settings`, ...): pytest was started outside the project folder. It then scans every folder
  below the current one, including other programs' bundled Python packages. Run `mavric selftest` instead, or `cd` into
  `C:\Code\MAVRIC` first.
- **"Could not load the Qt platform plugin"** usually means a second OpenCV with its own Qt got into the environment.
  Fix it with `pip uninstall opencv-python`, keeping `opencv-python-headless`.
- **Keep pip from replacing conda packages:** install MAVRIC with `pip install --no-deps -e .` as shown above. The
  dependencies already come from `environment.yml`; without `--no-deps`, pip may pull a second copy of numpy or Qt
  from PyPI.

**Without conda (alternative):** a plain virtual environment also works:

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -e ".[gui,basler,dev]"
```

### FLIR / Point Grey cameras (Chameleon3)

The `flir` backend (`mavric/video/flir.py`) drives FLIR / Point Grey USB3 cameras through **Spinnaker** (PySpin).
FlyCapture2, the SDK in the 2015 Chameleon3 manual, is no longer developed. The Chameleon3 USB3 is supported by
Spinnaker.

**1. Install the Spinnaker SDK.** Download it from Teledyne FLIR (free account): *Spinnaker SDK, Windows x64*.
Install it with the **USB3** driver. Check that **SpinView** shows the camera and streams.
- If features are missing, update the camera firmware from SpinView.

**2. Install PySpin into the `mavric` environment.** From the same download page, get the **Python** package for
**the same SDK version** and **Python 3.12** (`spinnaker_python-<version>-cp312-cp312-win_amd64`). Unzip it, then:

```bash
conda activate mavric
pip install --no-deps spinnaker_python-<version>-cp312-cp312-win_amd64.whl
python -c "import PySpin; v = PySpin.System.GetInstance().GetLibraryVersion(); print(v.major, v.minor, v.type, v.build)"
mavric ports
```

- `--no-deps` keeps conda's numpy. If `import PySpin` complains about numpy, use the numpy version named in the
  wheel's README.
- The wheel must match the installed SDK version, otherwise you get an import error or crashes.

**3. Configure the camera in `config/rig.yaml`:**

```yaml
cameras:
  - id: cam0
    backend: flir
    device: ""           # serial number from `mavric ports`, or "" for the first camera
    fps: 30
    exposure_us: 8000
    gain_db: 0.0
    pixel_format: Mono8  # BGR8 for colour models
    line_status: false   # Chameleon3: no per-frame line-status chunk
```

Then run `mavric camtest --cam cam0`.

**Chameleon3 models**

| Model | Max frame rate | Fit for MAVRIC |
|---|---|---|
| **CM3-U3-13Y3M/C** (1280×1024, CMOS, global shutter) | 91 fps Mono8, 50 fps RGB | Best fit |
| **CM3-U3-13S2M/C** (1288×964, CCD) | 30 fps | Fine |
| **CM3-U3-28S4M/C** (1928×1448, CCD) | 13 fps full frame (24 fps at 964×764 binned mono, 25 fps at 800×600) | Only marginal |

All models are global shutter, which gives no motion distortion with flapping wings.

- **28S4 at 13 fps:** lengthen the firmware `SYNC_PULSE_US` to ≥ 160 000, and set `sync.led_pulse_s: 0.16` to match.
  `mavric camtest` and the recording warn when the LED pulse covers fewer than 2 frames.

**Chameleon3 hardware notes**
- **Lens:** not included. The mount is CS; a C-mount lens needs the 5 mm adapter. Choose the focal length for the
  cage size and the sensor size (1/2" for the 13Y3, 1/3" for the 13S2).
- **USB 3.0 port:** use a PCIe 2.0 host controller or a built-in USB 3 port, with a 3–5 m cable. A 5 m cable is not
  recommended on laptops.
- **Bandwidth:** 1280×1024 Mono8 at 30 fps is about 39 MB/s per camera, so two cameras on one controller are
  normally fine. Each camera still sends every frame in a burst at full link speed, though, and some host controllers
  then lose packets. If a camera works alone but delivers only incomplete images next to another one, cap each
  camera's share with `link_limit_mb_s` (e.g. `150`) under that camera in `rig.yaml`. Or give it its own USB 3
  controller (a PCIe card).
- **A camera connects but shows no image:** the status line says `NO FRAMES for N s` and counts incomplete images
  and timeouts, with the last reason. Compare `mavric camtest --cam cam1` (alone) with `mavric camtest --cam all`
  (every camera at once).
  - **Incomplete images:** the USB link. Check that `usb=SuperSpeed` appears in the camtest output (not
    `HighSpeed`, which is USB 2: wrong port, cable or hub). If the problem appears only with `all`, set
    `link_limit_mb_s`.
  - **Incomplete images, "chunk data invalid" (`IMAGE_CHUNK_DATA_INVALID`):** Spinnaker can't parse the extra
    per-frame data that MAVRIC turns on for the camera's frame counter (seen with a Chameleon3). MAVRIC now turns it
    off by itself after 3 such images and keeps streaming; drops are then counted from the stream's frame IDs. To
    skip the attempt, set `chunk_frame_counter: false` for that camera. The camera keeps this setting until it is
    unplugged, so if SpinView shows the same error, unplug the camera once. MAVRIC now turns it back off when it
    disconnects.
  - **Timeouts:** the camera is not sending. The camtest warns if `TriggerMode` is still On, e.g. left on in a
    saved SpinView user set; turn it off in SpinView. Also close SpinView before connecting in MAVRIC.
- **Mounting:** mount the camera on a heat-conducting bracket (it runs warm). Operating range is 0–45 °C and 20–80 %
  humidity, non-condensing.
- **Mono vs colour:** mono models have no IR-cut filter and can work with 850 nm IR lighting. Colour models block IR
  above about 680 nm, but colour helps human labelling (plumage, coloured leg bands).

**Sync on the Chameleon3**
- **Per frame:** the backend records the device timestamp (`TimestampIncrement` ns/tick) and the camera's
  `FrameCounter` chunk.
- **No per-frame GPIO state:** the Chameleon3's GenICam chunk list has no line-status entry, so the **sync LED in the
  image** validates video↔RFID timing. That is accurate to about ½ frame, which is enough for perch presence.
- **Optional, for frame-exact sync later:** the opto-isolated input (Line 0, pin 9, 0–30 V) accepts the Arduino's 5 V
  sync signal; connect OPTO_GND (pin 7) first. The opto output (Line 1) can send *exposure active* to the Arduino.
  Using either needs firmware/software work and is not required now.

## Try it without hardware

With the environment active (`conda activate mavric`):

```bash
mavric acquire --sim                 # GUI with a simulated cage + simulated Arduino
mavric simulate sessions --duration 300 --audio
mavric audio test --sim --tones      # simulated audio interface, speakers heard by the mics
mavric review sessions/<the simulated session>
mavric analyze sessions/<the simulated session>
```

**What simulated sessions contain:**
- ground truth (`ground_truth.csv`) and noisy "manual" labels
- a +145 ppm Arduino clock drift, which is the value measured on the real board
- RFID onset latency, dropouts and crosstalk
- camera frame drops
- the sync LED
- with `--audio`:
  - two microphones with pigeon calls (`ground_truth_calls.csv`)
  - the sync pulse on an AC-coupled line input
  - a +25 ppm interface clock

The test suite checks that the pipeline recovers all of these.

## Hardware

The full pin-map (ID-20LA module pins, MEGA pin usage, Chameleon3 GPIO connector) and the bring-up order are in
**[docs/wiring.md](wiring.md)**.

**Readers:** Arduino MEGA 2560 with 4× ID-20LA (integrated antenna). Wire them as in the MultiRFID
`Arduino Software/README.md`:

| Reader | MEGA RX pin | TIR pin |
|---|---|---|
| 1 | 19 | 2 |
| 2 | 17 | 3 |
| 3 | 15 | 4 |
| 4 | 10 (SoftwareSerial) | 5 |

**Sync pins (new):** D52, D50 and D48 give the same pulse ([docs/wiring.md §3](wiring.md)).
- **D52, sync LED:** D52 → 330 Ω → red LED → GND. Place it where every camera sees it; a small diffuser helps.
- **D50, audio:** a 10 kΩ / 1.8 kΩ divider into MOTU M4 In 3 (TRS: tip = signal, ring = GND).
- **D48, spare:** for Intan DIGITAL IN 2, or a camera input line for frame-exact sync (Basler: Line 1 opto-in,
  then set `line_status: true` in `rig.yaml`, and the camera records the line state with every frame).

**Firmware:** `firmware/MultiRFID_Reader_ID20LA_sync/`. It is the MultiRFID ID-20LA sketch plus:
- an LED pulse of 80 ms at random 2–6 s intervals, each logged as `<micros>,SYNC,0`;
- a `<micros>,BOOT,0` line at start-up;
- fixed-cadence sampling rounds.

The line format is unchanged, so `Multiple_RFID.m` still runs with this firmware; it shows SYNC/BOOT lines as `UNKNOWN`.

Flash it with:

```bash
arduino-cli compile --fqbn arduino:avr:mega firmware/MultiRFID_Reader_ID20LA_sync
```

```bash
arduino-cli upload --fqbn arduino:avr:mega -p COM3 firmware/MultiRFID_Reader_ID20LA_sync
```

(`arduino-cli.exe` ships with Arduino IDE 2 under `resources/app/lib/backend/resources/`.)

### Bring-up checklist (bench, before birds)

1. **Tags:** check they are 125 kHz EM4100. This path cannot read FDX-B.
2. **Readers and tag map:** run `mavric link-test --port COM3`. Hold each tag at each reader, then:
   - fill in `config/tags.csv` (tag → pigeon);
   - fill in `config/rig.yaml → antennas` (reader → perch name).
3. **Read zone:** with the real leg ring, measure the read range at real perch heights and angles. The ID-20LA's
   built-in antenna has a short range, so its position under the perch decides where a bird is detected.
4. **Crosstalk:** in the final layout, check that a tag on one perch is never read on its neighbour.
5. **Latency:** measure the time from landing to first read, and from leaving until the read clears.
6. **Camera:** all 4 perches and the LED in view, ≥25 fps, fixed exposure and gain, even lighting. Run
   `mavric camtest`: no drops, a measured fps equal to the configured one, and no warnings.
7. **Short test recording:** 5 min, moving tags by hand. In `mavric analyze`, the LED/SYNC check should say **PASS**.
8. **Long test recording:** ≥80 min, so the `micros()` wrap at 71.6 min is exercised. Drift should be stable.
9. **Audio** (if used): `mavric audio test --tones`, then a 5-minute recording. In `mavric analyze`, the audio line should
   say **PASS** ([docs/wiring.md §7](wiring.md)).

## Workflow

### 1. Acquire: `mavric acquire`
1. Connect the RFID reader and the camera. The live view runs before you record anything.
2. Draw **one ROI per antenna**. Choose *Draw / reset*, drag the corners, and click an edge to add a corner. Then draw the
   **sync LED box**.
3. Choose *Save ROIs to rig*, which writes `config/rois.json`.
4. Check the live view: an ROI lights up in the pigeon's colour while its antenna reads a tag, and the live table and
   event list show arrivals and departures. The LED box brightness must jump with each flash.
5. Enter a session name and notes, then record. Every stream is written to disk continuously, so a crash keeps
   everything up to the last second.

### Video settings, raw recording and compression

**Video settings… (acquire window, next to View).** Per camera:
- frame rate, exposure, gain, brightness (black level), gamma, pixel format;
- region of interest (width, height, offsets; a smaller image allows a higher frame rate and less data);
- continuous or clips-only recording.

Exposure, gain, brightness and gamma change live (⚡); the rest reconnect the camera. *Save* writes
`config/video.yaml`, which overrides `rig.yaml` (`rig.yaml` keeps its comments).

**Raw recording (default).** Every frame is kept bit-exact, in files of about 2 GB (adjustable):
- **Files:** `cam0_raw_000.mkv`, `cam0_raw_001.mkv`, …
- **Codec:** FFV1, lossless, about a third of the uncompressed size. Uncompressed AVI is the alternative.
- **Data rate:** at 1280×1024 and 30 fps, measured on cam0 (gain 0):
  - FFV1: about 50 GB per camera-hour
  - uncompressed: 142 GB per camera-hour

  Noisier images (higher gain) compress less.
- **Before recording:** the window warns when the disk has room for less than 2 h.
- **Inside a session:** review, analysis and rendering read the chunks directly.
- **Clips:** RFID-triggered clips are raw too.
- *H.264 while recording* is still available (about 1 GB/h, lossy).

**Compression (afterwards, raw files are kept).** Each recording becomes `<name>_compressed.mp4` in the same folder.
- **What it makes:**
  - `cam0_raw_000…` → `cam0_compressed.mp4`
  - `clips/cam1_rule_001_raw_000.mkv` → `clips/cam1_rule_001_compressed.mp4`
  - any other video → `<name>_compressed.mp4`
- **What it keeps:** every frame, in order, at a constant frame rate, so `frames_cam0.csv` still times each frame.
- **Where to run it:**
  - *Compress videos…* in `mavric review`
  - *Video settings → Compress a session…*
  - the command line

```bash
mavric compress sessions/<session>                     # H.264, CRF 18, keyframe every 30 frames
mavric compress sessions/<session> --downscale 2       # 640x512: smaller, faster DeepLabCut
mavric compress sessions/<session> --crf 23 --preset slow
mavric compress D:/videos                              # every video in a folder
mavric compress sessions/<session> --print-ffmpeg      # the equivalent ffmpeg commands
```

**For DeepLabCut**, the defaults follow DLC's own guidance:
- **Codec and quality:** H.264 MP4 at CRF 18–23. The re-encoding recipe in DLC's docs uses exactly this.
- **Keyframes and frame rate:** constant frame rate, and a keyframe every 30 frames. DLC extracts frames by seeking
  with OpenCV, which needs regular keyframes.
- **Frame count:** checked after encoding. DLC warns when metadata and real frame counts differ.
- **Frame size:** DLC's developers advise small frames. A 2× downscale (`--downscale 2`, 640×512 here) or a crop to
  the perches speeds up training and analysis with little accuracy cost. Downscaled or cropped files no longer match
  the ROIs, so the review window keeps using the raw video.
- **Consistency:** train and analyse on videos compressed the same way.
- **Exposure:** short exposures avoid motion blur better than higher frame rates. Grayscale is fine.

Sources:
- [DLC video re-encoding tips](https://deeplabcut.github.io/DeepLabCut/docs/recipes/io.html#tips-on-video-re-encoding-and-preprocessing)
- [DLC user guide](https://deeplabcut.github.io/DeepLabCut/docs/main-workflows/user-guide.html)
- [Mathis & Warren 2018](https://www.biorxiv.org/content/10.1101/457242v1.full-text): robust to video compression and
  downsampling

### RFID-triggered clips (optional)

Use these to record video only while RFID detects a tag. This works on the main camera or on extra cameras not used
for identification, such as a close-up of one perch.

**1. Add the camera to `rig.yaml`**, with its serial number (`mavric ports` lists the FLIR cameras):

```yaml
cameras:
  - id: cam0             # the first camera carries the perch ROIs and the sync LED box
    backend: flir
    device: "12345678"
    record: continuous
  - id: cam1
    backend: flir
    device: "12345679"
    record: triggered    # nothing but clips; "continuous" also keeps the full video
```

**Pin every camera to its serial number** once there is more than one. `device: ""` means "the first camera found",
so two cameras would both pick the same one.

In the acquire window:
- **Connect camera:** pick each camera in the *Camera* list and connect it.
- **View:** chooses which connected camera is shown. *All cameras* shows every connected camera side by side for
  monitoring: frame rate, recording, clips only, and a clip in progress. ROIs are only shown in the single view of
  the first camera.
- **ROIs:** they are drawn on the first camera of `rig.yaml`, whatever the connection order.

**How many cameras.** Measured on the cage PC (i7-11700K, one USB 3 controller), at 1280×1024 Mono8:
- **Encoding:** the PC encodes about 330 frames/s in total. Plan for about **6 continuous cameras at 30 fps**,
  or about 12 at 15 fps.
- **USB:** one controller carries about 384 MB/s, and each camera at 30 fps needs 39 MB/s.
- **Clips-only cameras:** they only cost encoding while a clip is recording.
- **Beyond that:** add a PCIe USB 3 card with one controller per port, and lower the frame rate or use 2×2
  decimation.

**2. Add rules: acquire → Outputs… → Camera clips.**
- **Each rule sets:** a camera, antennas, birds, and the seconds before and after the presence.
- **Saving:** the rules go to `config/outputs.yaml` (`clips:`) and apply from the next recording.

The same rules can also be written in `rig.yaml`:

```yaml
triggers:
  - name: perchB
    camera: cam1
    antennas: [2]        # or "any"
    match: any           # or [S01, S03] (pigeon names) / tag IDs
    pre_s: 3             # from the camera's ring buffer, i.e. before the first read
    post_s: 5            # after the last read
    max_s: 300           # longer clips are split
```

**How clips behave:**
- A clip starts on the RFID round in which a matching tag appears.
- A matching arrival during the post-roll extends the clip.
- Birds that are already present when recording starts trigger immediately.

**Where clips go:**
- Video: `clips/<camera>_<rule>_<nnn>.mp4`.
- Timing: `clips/<…>_frames.csv`, with the same columns and session clock as `frames_<cam>.csv`.
- Registry: `session.json["clips"]`, listing the trigger time, antenna, tag/pigeon, the number of frames taken from the
  ring buffer, and why the clip ended.

The acquire window shows each rule's state. `mavric analyze` writes `analysis/clips.csv`, which places every clip on
the session timeline (camera clock fit) and links it to the RFID bout that triggered it.

**Ring buffer memory:** pre_s × fps × frame size. For example, 3 s × 30 fps × 1 MB is about 90 MB per camera.

### Event-driven outputs (optional)

**What it does:** RFID events can drive other devices: TTL lines (Intan, LEDs, relays), Intan RHX, MATLAB, programs and
network messages. Background, feasibility and latency figures are in [docs/outputs_plan.md](outputs_plan.md);
wiring is in [docs/wiring.md §6](wiring.md).

**Setup**
1. In the acquire window, open **Outputs…**.
2. **Devices tab:** add the devices.
3. **Rules tab:** add rules. Each rule sets:
   - which antennas (or any)
   - which birds (or any)
   - the event: **on enter**, **on leave** or **while present** (a start/stop pair)
   - the device action
   - refractory period, delay, and maximum firings per minute
4. **Save** writes `config/outputs.yaml` and restarts the outputs.
5. Nothing is driven until you press **Arm outputs**. Disarming releases every held output.

**Device types**

| Device | Actions | Setup |
|---|---|---|
| **I/O Arduino** (`firmware/MAVRIC_IO`) | `pulse` (pin, ms) · `hold` (pin high while present) · `set` · `toggle` | Flash the sketch and set the COM port. Port `SIM` is a software emulator. |
| **Intan RHX** (TCP) | `record` · `run` · `stop` · `hold_record` (record while present) · `send` (any command) | In RHX: Network → Remote TCP Control → Commands, port 5000, Connect |
| **MATLAB** (Engine API) | `eval` (code) · `call` (function + args) · `run` (script) | `pip install "<matlabroot>\extern\engines\python"` in the mavric env, then in MATLAB `matlab.engine.shareEngine('mavric')` |
| **Program / script** | `run` with arguments | started once per action, without waiting for it to finish |
| **UDP / TCP message** | `send` · `hold` (start/stop texts) | e.g. to a MATLAB `udpport` listener, Bonsai, another PC |
| **Simulated** | `mark` | logs only |

**Placeholders.** Texts and arguments accept `{event} {rule} {antenna} {roi} {pigeon} {tag} {t} {utc} {session}`.
`{t}` is the session time in seconds, so `{t:.3f}` gives milliseconds.

**Logging.** Every decision is written to the session's `actions.csv`:
- `ok`, `error`, `dry` (not armed) or `suppressed` (refractory, rate limit)
- event time and send time, with the latency in between
- the I/O board's own `micros()` for each acknowledgement

`mavric analyze` adds `analysis/actions_summary.csv`: firings, errors and latency per rule.

**Command line**

```bash
mavric outputs list
mavric outputs test io
mavric outputs fire intan_perchB
mavric acquire --headless --sim --arm-outputs --duration 30
```

**Timing.** RFID presence events now reach the rules about 5 ms after a sampling round's lines arrive, instead of one
round later. In the simulator the rule → I/O board acknowledgement took about 11 ms in total. The firmware switches the
pin before acknowledging.

### Audio: two microphones and speakers (optional)

**What it does**
- **Records** the two Rode NT5s and the RFID sync pulse through the MOTU M4, as one multichannel FLAC per hour.
  The channels are independent and sample-aligned.
- **Shows** live level meters and scrolling spectrograms of both microphones.
- **Places** every audio sample on the session timeline, within microseconds of the RFID events, using the recorded
  sync pulse.
- **Speakers:** the four speakers on Out 1–4 are opened in the same stream.
  - Today they play the test tones.
  - Stimulus playback from rules comes with AU4.

The plan and status are in [docs/audio_plan.md](audio_plan.md); wiring is in [docs/wiring.md §7](wiring.md).

**Setup**
1. Install the MOTU M-Series driver and connect the M4 (§7 of the wiring guide).
2. Check `rig.yaml → audio` (device, inputs, speakers), then run:
   ```bash
   mavric audio devices
   ```
   ```bash
   mavric audio test --tones
   ```
3. In `mavric acquire`, press **Connect audio**. Meters and spectrograms appear, and the next recording includes
   the audio.
   - For headless runs, set `audio.enabled: true` or pass `--audio`.

**Analysis:** `mavric analyze` reports, in `sync_report.json["audio"]`:
- the audio clock drift
- the sync pulses matched
- **PASS** or **FAIL**

It also writes `analysis/audio_time_knots.csv` (sample → session time).

**Without the sync cable**, audio is aligned by the host clock alone (a few ms).

### 2. Label: `mavric review <session>`
**Blind mode (the default).** RFID is hidden, so the labels stay independent of it.

| Key | Action |
|---|---|
| ↑/↓ | Choose a perch |
| `1`–`5` | From now on this pigeon is on the perch |
| `0` | Empty |
| Del | Remove the change point in effect |
| Space | Play / pause |
| ←/→ | Step one frame |
| Shift / Ctrl + ←/→ | Jump 1 s / 10 s |
| `[` `]` | Change speed |

The time you actually watch is tracked as the *reviewed coverage* (grey band). Only that time is used as ground truth.

**Curate mode.** RFID lanes are shown.

| Key | Action |
|---|---|
| `D` | Jump to the next disagreement |
| `X` | Mark the RFID bout under the cursor as spurious |
| `V` | Mark it valid |

Choose *Analyze* for the report, and *Render* for the annotated video.

**Sound events** (sessions with audio).

**Lanes:** under the perch timeline:
- a waveform overview of both microphones
- one spectrogram per microphone, shown when the view spans 60 s or less (zoom with the wheel)

**Sound events panel**

| Key | Action |
|---|---|
| `S` / `E` | Start / end a sound event at the playhead |
| `F1`–`F9` | Event type (edit the list with *Types…*, saved in `config/sound_types.yaml`) |
| `Alt+1`–`9`, `Alt+0` | Emitter = pigeon 1–9, or unknown (*system:<speaker>* is in the list) |
| `A` | Use the top emitter suggestion |
| `Y` / `R` | Accept / reject the automatic proposal at or after the playhead |
| `,` `.` | Previous / next event or proposal |
| `L` | Listen around the playhead, or to the selected event (at the playback speed) |
| Shift+Del | Delete the selected event; drag its edges to adjust it |

**More**
- **Listening with the video:** tick *Play audio with the video*. Slower speeds lower the pitch, which helps with
  fast or high calls.
- **Emitter suggestions** combine:
  - which microphone the sound was louder on (and reached first, for broadband sounds)
  - which birds RFID places on a perch at that moment
  - `audio.mic_near` in `rig.yaml`: which perches each microphone is next to.
- **Played sounds:** sounds the rig played (`audio_playback.csv`, written by playback) appear automatically with
  emitter *system:<speaker>*.

**Files**
- **Events:** `sound_events.csv`.
- **Reviewed time:** `sound_coverage.csv`, recorded while *Labeling sounds* is ticked.

**Command line**

```bash
mavric sounds detect <session>                 # automatic proposals (analysis/sound_proposals.csv)
mavric sounds export <session> --format raven  # or audacity: one label file per audio file
mavric sounds import <session> labels.txt      # Audacity label track or Raven selection table
```

**BORIS instead (stopgap):** set it up in BORIS, then import the export.
1. Make one *subject* per pigeon, using the names from `tags.csv`.
2. Make one *state* behaviour per perch, named exactly like the ROIs (or use `--map-behavior`).
3. Code the session's `cam0.mp4` and export **Aggregated events** as TSV.
4. Run `mavric import-boris <session> export.tsv`.

### 3. Analyze: `mavric analyze <session>`

Writes `analysis/` in the session folder:

| File | Content |
|---|---|
| `sync_report.json` | Clock fits (offset, drift ppm, latency), LED/SYNC match and residuals, stream health (dropped rounds, bad lines, checksum failures, unknown tags) |
| `rfid_samples.csv`, `rfid_bouts.csv` | Tag reads on the video timeline; bouts per pigeon × perch |
| `comparison_bins.csv`, `accuracy_per_roi.csv`, `accuracy_per_pigeon.csv`, `confusion_seconds.csv`, `event_matching.csv` | RFID vs labels |
| `time_budget_*.csv`, `transitions_rfid.csv` | Time per pigeon per perch, perch-to-perch moves |
| `*.png` | Occupancy timeline per pigeon, accuracy per perch, confusion, bout timing error, time budget, transitions, sync diagnostics |
| `summary.json` | Everything above, in one file |

**Metrics.** Computed per perch, in time bins (default 0.5 s) within the reviewed coverage:

| Metric | Meaning |
|---|---|
| Presence sensitivity | Of the time a bird was on the perch, the share RFID saw a tag |
| Precision | Of the time RFID saw a tag, the share a bird was really there |
| Identity accuracy when detected | Correct ID among detected time |
| Agreement, Cohen's κ | Overall agreement between RFID and labels |
| Crosstalk vs phantom | A false read is *crosstalk* when that pigeon was really on another perch, otherwise *phantom* |

**Bout level** (±1 s tolerance): recall, precision, arrival/departure latency, and fragmentation of a visit into several RFID bouts.

## How synchronization works

1. **One host clock.** Every serial line and every frame is stamped with the host monotonic clock on arrival, in one
   process. `session.json` stores that clock's anchor to UTC.
2. **RFID clock fit.** Arrival = event + latency, with latency ≥ 0. Fitting the *lower envelope* of host time against
   Arduino `micros()` recovers the Arduino's offset and drift, so RFID times are free of USB jitter.
   - The fit is done per Arduino reset segment.
   - The `micros()` wrap is unwrapped.
3. **Camera clock fit.** Camera hardware timestamps are fitted to host time the same way.
4. **LED validation.** The firmware logs each LED rising edge. The camera sees it, either as brightness in the LED box
   or as a GPIO line state. After steps 2–3 the two pulse trains must coincide.
   - The constant pipeline offset between them is measured and applied.
   - **Acceptance:** median |residual| < 1 frame and p95 < 100 ms.
   - The random intervals make matching robust to missed or extra pulses.
5. **Audio.** The same sync pulse is recorded on an audio input.
   - Its edges are found to a fraction of a sample and matched to the `SYNC` lines.
   - Audio samples are mapped to session time by interpolating through the matched pulses.
   - Without the sync channel, a host-clock fit of the audio blocks is used instead.

**Why not a hardware trigger:** a TTL frame trigger is not needed for perch presence at this accuracy. It remains an
option for frame-locked multi-camera capture.

## Session folder

```
sessions/<YYYYmmdd_HHMMSS>_<name>/
  session.json        start (UTC + local), clock anchor, rig snapshot, cameras, stats
  tags.csv rois.json  snapshot of the tag map and ROIs used
  rfid_raw.csv        host_t, segment, micros, arduino_us, kind, tag, reader, checksum_ok, raw
  cam0_raw_000.mkv …  raw video, lossless FFV1 chunks (~2 GB; .avi if uncompressed); cam0.mp4 when recording H.264
  cam0_compressed.mp4 made by `mavric compress` (H.264, every frame, constant rate)
  frames_cam0.csv     frame_idx, grab_idx, host_t, hw_ticks, hw_counter, line_status, led_mean
  manual_labels.csv   annotator, roi, pigeon, start_s, stop_s, flag
  label_coverage.csv  annotator, start_s, stop_s
  rfid_curation.csv   antenna, tag, t, start_s, stop_s, verdict
  audio_000.flac      audio inputs (micA, micB, sync), 48 kHz / 24 bit, one file per hour
  audio_clock.csv     sample, n, host_t, adc_t, flags (block deliveries, for the audio clock fit)
  audio_edges.csv     sample, sign, amp (sync pulse edges, in samples)
  clips/              RFID-triggered clips: <camera>_<rule>_<nnn>.mp4 + _frames.csv
  analysis/
```

All times are seconds since session start. Everything is plain CSV/JSON, so MATLAB reads it directly
(`readtable`, `jsondecode`).

## Layout

```
firmware/        Arduino sketch with sync LED
config/          rig.yaml, tags.csv, rois.json (written by the acquire GUI)
mavric/rfid/      protocol (parse, EM4100 checksum, micros unwrap), serial thread, presence tracker
mavric/video/     camera backends (Basler, FLIR/Spinnaker, OpenCV, file, synthetic; IDS stub), recorder (raw
                 lossless chunks or H.264), reader, compress, settings (config/video.yaml), camtest
mavric/analysis/  sync, bouts, labels I/O + BORIS, compare, plots, render, clips, pipeline
mavric/gui/       acquire and review windows (sound_panel: audio lanes and sound-event labeling)
mavric/acquire.py acquisition controller (no GUI), event bus for presence changes
mavric/triggers.py RFID-triggered clip rules (ring-buffer pre-roll, post-roll, max length)
mavric/outputs/   event-driven outputs: rules engine, devices (I/O Arduino, Intan RHX, MATLAB, program, UDP/TCP)
mavric/audio/     audio stream (PortAudio), recorder, sync edge detector, simulated interface, bench check;
                 review side: source (reading, overview, spectrograms), detect (proposals, suggestions), events
firmware/MAVRIC_IO/  I/O Arduino sketch (TTL pulses / holds, watchdog)
mavric/sim.py     simulated cage for testing and demos
tests/           pytest suite (unit + simulated end-to-end + real-time headless run)
```

## Next steps (not in this milestone)

- **DeepLabCut SuperAnimal-Bird:** detect birds in each ROI with no training, to pre-fill label proposals.
- **RFID → video identity:** PyMouseTracks-style assignment, a DLC `stitch_tracklets(weight_func=...)` hook, and
  identity-head training on single-occupancy frames that RFID has confirmed.
- **Ethograph export:** xarray TrialTree + `labels.tsv`.
- **Live mode:** the analysis pipeline on a rolling buffer.
