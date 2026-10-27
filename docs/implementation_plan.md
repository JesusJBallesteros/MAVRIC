# MAVRIC — Implementation Plan

## Context

**Goal:** a Video+RFID system that gives trustworthy **ID × location × time** records for 4–5 RFID-tagged pigeons in one cage.
- The cage has **4 RFID perches**, read by the Arduino-based 4-antenna poller from `C:\Code\multirfid-standalone`, plus 1 or more cameras.
- **Step 1:** check the RFID against human-labeled video (the ground truth).
- **Later:**
  - use RFID to resolve identity when video tracking fails;
  - **trigger video recording only when RFID detects a tag**, on the main cameras or on other cameras not used for identification.

**First milestone:** one fully integrated recording that is synced, labeled and merged, and gives results you can interpret: accuracy, time at each location, and an annotated video.

### What the research found

**1. The existing RFID tool (`multirfid-standalone`)**
- **How it works**
  - A MATLAB script (`Multiple_RFID.m`) with a key-press window only. There is no real GUI and no live display.
  - Hardware: an Arduino MEGA with 4 independent 125 kHz EM4100 readers (ID-2LA/12LA/20LA or Grove), each on its own UART.
  - Every 125 ms the board sends one ASCII line per occupied antenna, `micros,TAG12HEX,reader`, at 500000 baud with CRLF endings. When no antenna holds a tag it sends `NONE,0`, which doubles as a heartbeat.
- **Timing problems**
  - The PC timestamp records when MATLAB read the line, not when it arrived.
  - `posixtime(datetime('now'))` stores local time as if it were UTC, so values are off by 1–2 h and jump at daylight-saving changes.
  - `micros()` wraps every 71.6 min.
  - The Arduino clock drifts about 145 ppm, roughly 0.5 s per hour.
  - There are no sync or trigger hooks.
- **Robustness:** all data stays in RAM until you press `s`, so a crash loses the session.
- **Analysis bugs**
  - The rollover unwrap runs after sorting, so it never triggers.
  - "Time at reader" is the first-to-last-read span, not real occupancy.
  - The co-presence measure pools all perches.
- **Hardware notes**
  - Readers will be **ID-20LA with the antenna built in**, which matches the TIR-gated `MultiRFID_Reader_ID20LABoards.ino` sketch. The Grove path is not used.
  - The module has to sit in or under each perch, and the read range with the actual pigeon tag must be checked.
  - Only 125 kHz tags are read; FDX-B is not.
  - ID-20LA has no anti-collision. That is not a practical concern, because the perch is only big enough for one pigeon.
- **No off-the-shelf RFID + video tool fits.** The closest relatives:
  - **PyMouseTracks** (MIT): RFID readers are drawn as regions in the video image, and reads are assigned to tracks with fixed rules.
  - **pyEcoHAB**: turns raw reads into visits.
  - **TIDe**: assigns identities with integer linear programming.
  - **Aeon** uses RFID to check video identity, the same direction as our step 1.
  - Published perch-RFID validations report 80–93% detection, and as low as 39% agreement at single-antenna level. So doing validation first is the right call.

**2. Labeling tools**
- **BORIS** v10 is mature and has what we need:
  - pigeons as subjects, perches as state events
  - a 1-second binary-table export for comparison
  - an external time-series plot (max 2 series)
- **Ethograph** v0.2.x looks promising but is pre-alpha and has one developer:
  - PyQt6/pyqtgraph, xarray data, `labels.tsv`
  - no ROI drawing on video, no accuracy plots
- **DeepLabCut 3.x** (PyTorch)
  - **SuperAnimal-Bird**: works with no training (detector plus 42 keypoints). It does not tell individuals apart.
  - **Identity head**: only useful if the pigeons look different.
  - **`stitch_tracklets(weight_func=…)`**: the hook for feeding RFID into tracking later.
  - It is a tool for later phases, not for ground-truth labeling.

**3. Sync**
- Timestamp every camera frame and every serial line on the same host clock, in one process.
- An Arduino-driven LED flashing at random intervals in the camera view validates the offset and drift afterwards (the pyControl "Rsync" idea).
- That is easy and trustworthy at sub-second accuracy. A hardware TTL frame trigger is not needed for perch presence.

## Decisions (confirmed)
| Topic | Decision |
|---|---|
| Platform | **Python** for both acquisition and analysis |
| Labeling | **Custom review GUI**, with **BORIS as a stopgap** for the first milestone recording (via an importer) |
| Camera | **Industrial camera** (Basler/FLIR/IDS). Use the vendor SDK backend with hardware frame timestamps and counters |
| Firmware | The sync addition goes in a **copy inside `MAVRIC/firmware`**. The original repo is untouched |

## Recommended approach

Build a **Python package `mavric` in `C:\Code\MAVRIC`** with two GUI modes, **Acquire** and **Review/Analyze**, using PySide6 + pyqtgraph + OpenCV.
- Keep the Arduino firmware as it is, apart from adding a sync LED.
- Analysis uses pandas.
- DeepLabCut and Ethograph come in later as optional integrations.
- MATLAB keeps working: the existing tool still runs, and every output is plain CSV/Parquet.

**Why Python rather than MATLAB:**
- One process can timestamp frames and serial lines on the same monotonic clock using threads.
- Camera SDKs are available: OpenCV, pypylon, PySpin.
- There is a direct path to DLC/SuperAnimal-Bird, Ethograph and pynapple.
- No licence is needed on the cage PC.
- The RFID protocol is about 40 lines to port.

### Project layout
```
C:\Code\MAVRIC\
  pyproject.toml            # pyside6, pyqtgraph, opencv-python, pyserial, numpy, pandas, pyarrow, pyyaml, av, matplotlib
  firmware/MultiRFID_Reader_ID20LA_sync/MultiRFID_Reader_ID20LA_sync.ino
  config/rig.example.yaml   # COM port, camera backend/index/fps, antenna names, sync-LED pin
  config/tags.example.csv   # tag_id,pigeon,color   (seeded from make_tag_names.m:26-35)
  mavric/rfid/       protocol.py (parse, EM4100 XOR checksum, micros unwrap), serial_reader.py (thread)
  mavric/video/      backends.py (CameraBackend interface; vendor adapter pypylon | PySpin | ids_peak for the actual camera;
                    OpenCV/file adapter for dev and tests without hardware), recorder.py (PyAV/ffmpeg + frame sidecar)
  mavric/session.py  session folder + session.json (UTC start, perf_counter anchor, config, ROIs, versions)
  mavric/analysis/   sync.py, bouts.py, labels_io.py (own + BORIS import), compare.py, plots.py, render.py
  mavric/gui/        acquire.py, review.py, widgets (video view, ROI editor, timeline lanes)
  tests/            synthetic-data tests for every analysis module
  sessions/         (gitignored) one folder per recording
```

### Phase 0: hardware bring-up (intern, bench)
- **Confirm the tags** are 125 kHz EM4100.
- **Mount one ID-20LA in or under each perch.** The antenna is built in, so where the module sits sets where on the perch a tag can be read.
- **Flash and test:** flash the ID20LA firmware and run `functions\arduino_link_test.m`.
- **Map** each reader number to a perch name, and each tag ID to a pigeon.
- **Bench tests:**
  - **read range and read zone** with the real pigeon tag (leg ring) at real perch heights and angles
  - **crosstalk** between neighbouring perches in the final layout
  - time from a bird landing to the first read (TIR), and from leaving to the 50 ms clear
- **Camera:** placement covering all 4 perches, ≥25 fps, fixed lighting, and a spot where the sync LED is visible.

### Phase 1: firmware sync addition (small and backward-compatible)
- **Base:** a copy of `MultiRFID_Reader_ID20LABoards.ino`.
- **Add:** a free pin (e.g. D52) driving an LED in camera view, with 80 ms pulses at random intervals of 2–6 s. Each pulse emits `<micros>,SYNC,0`.
  - This keeps the 3-field format, so MATLAB just labels it `UNKNOWN`.
- **Optional small fix:** `lastLogTimeUs += LOG_INTERVAL_US` (stops accumulated loop latency).
- **No other changes.** The 8 Hz `NONE` heartbeat is kept, because it is used to detect dropped lines.

### Phase 2: acquisition app (`mavric acquire`)
**Serial thread**
1. Open the port, wait 2 s (the Arduino resets), *then* flush.
2. Read with blocking `readline` and stamp each line with `time.perf_counter_ns()`. This is converted to UTC through a single anchor taken at session start, which avoids the local-time-as-UTC problem.
3. Parse each line, check the checksum, and unwrap `micros`.
4. **Append to `rfid_raw.csv` right away**, flushing regularly, so a crash loses almost nothing.
5. Publish presence-change events (`antenna, tag, on/off, t`) on the internal event bus.

**Camera thread**
- **Capture:** free-running at a fixed fps (25–30) with fixed exposure and gain.
- **Each frame:** take the host timestamp right after it is grabbed. Also read the camera's **hardware timestamp and frame counter**, from chunk data or frame metadata.
- **Dropped frames** are found from gaps in the counter.
- **Clock mapping:** camera clock → host clock with the same robust linear fit used for RFID.
- **Output:** encode with PyAV (H.264, or lossless/MJPEG if the CPU can't keep up) and write `frames_cam0.csv` (frame_idx, hw_counter, hw_t, host_t).
- **Sync-LED ROI:** log the mean brightness of the sync-LED ROI for each frame to `sync_led_cam0.csv`, so sync never needs a second pass over the video.
- **Optional stronger sync (industrial cameras support it):**
  - wire the Arduino sync pin into a camera GPIO input as well;
  - log the line state for each frame (Basler `LineStatusAll` chunk / FLIR / IDS equivalent).
  - This gives frame-exact sync without any image analysis, and the LED stays as a visual check.
- **Later:** an Arduino TTL frame trigger if several cameras need frame-locked capture.

**GUI**
- Live view.
- Draw and edit up to **4 ROIs named after the antennas**, plus 1 sync-LED ROI. They are saved per rig and reused.
- **Live RFID overlay:** an ROI lights up and shows the pigeon name while its antenna reports a tag.
- Status panel: fps, dropped frames, serial line rate, last reads.
- Start, Record and Stop buttons, with a session name and notes.
- Load and save the config.

### Phase 3: offline pipeline (`mavric analyze <session>`)
**`sync.py`**
1. Fit Arduino `micros` to host time with a robust linear fit (on the lower envelope of host−arduino) to get **offset and drift (ppm)**.
2. Check the fit by matching LED onsets in `sync_led_cam0.csv` to the SYNC lines, using interval-pattern matching that tolerates missed pulses.
3. Report latency and residuals.
- **Acceptance:** median residual under 1 frame, and 95th percentile under 100 ms.

**`bouts.py`**
- Turn the 8 Hz samples into presence intervals per pigeon and antenna.
- Configurable gap merge (default 0.5 s) and a minimum bout length.
- Flag UNKNOWN reads and checksum failures, and flag heartbeat gaps (dropped lines).

**`labels_io.py`**
- Read and write our `manual_labels.csv` (annotator, pigeon, roi, start_s, stop_s).
- **Import BORIS** aggregated-events CSV, so ground truth can be made in BORIS before the custom GUI is ready.

**`compare.py`**
Both streams are cut into time bins (e.g. 0.5 s, or per frame), separately for each ROI. Outputs:
- presence sensitivity and precision
- identity accuracy when presence is detected
- confusion matrix of manual vs RFID ID
- event matching (arrivals and departures) within ±1 s, with onset and offset latency
- a separate count of crosstalk-like false positives (a tag read at a perch next to the true one)
- breakdowns per pigeon and per antenna
- a sanity flag if a label or the RFID ever shows two birds in one ROI (not expected, given the perch size)

**`plots.py`**
- an ethogram timeline (manual vs RFID lanes per ROI)
- accuracy bars
- time budget per pigeon per location
- a transition matrix between perches

**`render.py`:** an annotated video showing the ROIs, the RFID ID, the manual label and disagreement flags.

**Outputs:** tidy CSV/Parquet files: `rfid_raw`, `rfid_bouts`, `frames`, `sync_report`, `manual_labels`, `comparison_bins`, `summary`.

### Phase 4: review and labeling GUI (`mavric review <session>`)
**Playback**
- Plays the video with frame-accurate time, mapping frame ↔ host time through `frames_cam0.csv`.
- Shows the ROI overlays and per-ROI timeline lanes in pyqtgraph.

**Labeling**
- **One occupant per ROI.** Select an ROI, then press `1`–`5` to set which pigeon is there from the current frame onward, or `0` for empty.
- A rare "two birds" flag key is kept for edge cases.
- Shortcuts: step back and forth, change speed, jump to the next change.

**Blind mode (default)**
- RFID lanes are hidden while you label, so the ground truth isn't biased by what the RFID says.
- **Curate mode:** RFID is revealed afterwards. Jump to each disagreement, then mark RFID bouts as valid or spurious, or correct the label.

**Analyze tab:** runs the Phase 3 pipeline in place, shows the plots, and exports the annotated video.

### Phase 5: later, not part of the first milestone
- **DLC SuperAnimal-Bird with no training:** finds birds in each ROI to pre-fill occupancy proposals, which speeds up labeling.
- **RFID → identity for video tracklets**, PyMouseTracks-style (assign within an ROI only when exactly one bird is present; propagate the identity backwards and forwards along the tracklet). Later, a DLC `stitch_tracklets` custom `weight_func`, and identity-head training on single-occupancy frames that RFID has confirmed.
- **Ethograph export** (xarray TrialTree plus `labels.tsv`) for viewing several streams together.
- **Live mode:** the same pipeline running on a rolling buffer.
- **RFID-triggered recording.** A rule engine in the acquire app, e.g. "antenna B reads any tag (or tag S03) → record camera 2". Each rule has:
  - a pre-trigger ring buffer of N s, so the landing is captured
  - a post-trigger hold of M s after the last read
  - an option to use cameras that aren't for identification
  - Each clip keeps its own frame sidecar, so it stays synced.
  - **To make this cheap to add later**, Phase 2 is built from the start around an internal event bus: RFID presence-change events go to subscribers (GUI overlay, writer, future triggers). Camera backends also keep a ring buffer.

### Existing code and knowledge to reuse
| What | Where it comes from |
|---|---|
| Line format and parsing | `multirfid-standalone\Multiple_RFID.m:147-161` |
| Firmware base | `Arduino Software\MultiRFID_Reader_ID20LABoards\...ino` |
| Wiring | `Arduino Software\README.md` |
| Tag IDs | `config\make_tag_names.m:26-35` |
| Expected data shape for tests | `examples\rfid_log_30062026_033938.csv` |
| Assignment rules (MIT) | PyMouseTracks |
| Bout heuristics | pyEcoHAB |
| Interval-matching sync idea | pyControl `rsync.py` (GPL-3, so reimplemented) |
| Ground-truth stopgap | BORIS export format |

## Build order (what I implement first after approval)
1. **Project setup**
   - `git init` the MAVRIC folder.
   - Write `pyproject.toml` and the example configs.
   - Create `tags.example.csv` from `make_tag_names.m`.
2. **RFID core, no hardware needed:** `mavric/rfid/protocol.py` plus tests, run against `examples\rfid_log_*.csv`.
3. **Firmware sync sketch.**
4. **Session and recorder modules:**
   - `mavric/session.py`
   - the serial thread
   - camera backends: the file/OpenCV backend first, so everything can be tested without the rig; then the vendor adapter once the camera model is known
5. **Acquire GUI**, including ROIs and the live RFID overlay.
6. **Analysis pipeline:** sync → bouts → BORIS import → compare → plots → render.
7. **Review GUI:** blind labeling, curation, and the analyze tab.

Phase 0 is hands-on rig work, so it runs in parallel with steps 1–4 (the intern).

## Verification
1. **Unit tests (pytest)** with synthetic streams:
   - `micros` wrap
   - 145 ppm drift
   - corrupted checksums
   - dropped lines
   - labels with known accuracy, so `compare.py` must reproduce the injected numbers
2. **Bench test** with the Arduino, 1 tag and the camera:
   - Move the tag through antennas 1→4 while the LED flashes.
   - Check that the live overlay lights the correct ROI.
   - Check that the sync residuals meet the acceptance limits.
3. **Crash test:** kill the process mid-recording. The CSVs and sidecars must survive, and `analyze` must still run.
4. **Long run:** at least 80 min, so the `micros` wrap and drift correction are exercised.
5. **Milestone run:**
   - Record the pigeons for 30–60 min.
   - Label 10–15 min blind in `review`, or in BORIS through the importer.
   - Run `analyze` → accuracy report, time at each location, annotated video.
