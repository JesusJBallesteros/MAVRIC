# MAVRIC: project brief

**About this document**
- **Source:** the two original project notes (aims and software requirements; not published).
- **Wording:** the original wording is kept; only typos and grammar are fixed.
- **Post-hoc edits:** decisions and findings that came after the notes were written are added as clearly marked
  post-hoc edits, so the original intent stays readable.
- **Related documents:** [implementation plan](implementation_plan.md) for the technical plan, [README](../README.md)
  for installation and use.

> **Post-hoc edit (2026-10-19):** the original notes are kept unchanged, outside the public repository.

---

## 1. Aim

**Goal.** Establish a Video+RFID (MAVRIC) system that optimizes the identification of individuals in a dedicated
space.

**Order of work.** First under offline conditions, later on live streams.

## 2. Principal goals

1. **Verify RFID detection.** Check that RFID detection is correct in a setup with several antennas and several
   individuals.
2. **Collect and label video data.**
3. **Merge both data streams.** Bring them together so that the actual **ID–location–time** triads can be verified
   and estimated in a way we can trust.

## 3. Specific needs

**Video dataset**
- Collect camera recordings that allow a human to label each individual in time and space.
- A dedicated GUI will be set up so the human can view the recordings offline and assign IDs to locations and times.
- This produces the video dataset of ID–location–times.

**RFID dataset**
- Collect RFID tag readings that place IDs at locations and times.
- The system polls and saves the data automatically; an RFID dataset of ID–location–times must be extracted from it.

**Cross-validation**
- The two datasets must be crossed to verify RFID efficiency and accuracy against the human-labeled ground truth.
- This should happen in the same dedicated GUI used for video labeling.

> **Post-hoc edit (2026-10-19), how this was implemented:**
> - **Video dataset:** `mavric review` does the human labeling in a *blind* mode, where RFID is hidden so it cannot
>   bias the ground truth. The time the annotator actually watched is recorded as the labeled coverage.
> - **RFID dataset:** the automatic polling feeds `rfid_raw.csv`. The analysis turns it into presence bouts per
>   pigeon and perch.
> - **Crossing the datasets:**
>   - The review GUI's *curate* mode shows both streams, jumps between disagreements and lets you mark RFID bouts as
>     spurious or valid.
>   - Its *Analyze* button (and `mavric analyze`) computes accuracy per perch and per pigeon, a confusion matrix, and
>     arrival/departure latency.
>   - BORIS can produce the ground truth as a stopgap, via `mavric import-boris`.

## 4. Further applications

**Video helped by RFID.** Once RFID detection works efficiently, handle complex video situations where RFID
information helps, optimizing detection the other way around. For example, when the video ID is unclear or fails,
RFID could provide the missing information.

> **Post-hoc edit (2026-10-19): second application.**
> - **What:** trigger video recording only when RFID detects a tag, on the main cameras or on other cameras not
>   dedicated to identification.
> - **Status:** implemented as trigger rules in `rig.yaml`. Each rule has a pre-trigger ring buffer, a post-roll, a
>   maximum clip length, and can drive a camera that records clips only. See the README, "RFID-triggered clips".

> **Post-hoc edit (2026-10-19): path for the video-ID application.** Research identified the closest existing
> designs:
> - **PyMouseTracks** assigns RFID reads to video tracks inside a reader's region.
> - **Live Mouse Tracker** and **TIDe** do RFID-anchored identity assignment.
> - **Planned approach:** DeepLabCut SuperAnimal-Bird for detection, then RFID-anchored identity assignment to the
>   tracklets (for example, a custom `stitch_tracklets(weight_func=...)` in DeepLabCut).
> - **Status:** not part of the first milestone.

## 5. What the project needs

**People and animals**
- An intern with a set number of hours and access to the installations.
- A group of 4–5 individuals (pigeons) to record, each with a known ID (tagged).

**RFID perches**
- 4 perches, properly wired, with all electronics safely connected and powered.
- A planned layout inside the cage.
- A description of the RFID system will be provided.

> **Post-hoc edit (2026-10-19): readers and perches.**
> - **Readers:** ID-20LA modules with **integrated antennas**, one per perch, on the Arduino MEGA. They read
>   125 kHz EM4100-type tags only (FDX-B is not supported on this path), so the pigeons' tags must be checked.
> - **No anti-collision:** the ID-20LA has none, but this is not a practical concern because the perch size allows
>   only one bird at a time.
> - **What to measure:** the read zone of the integrated antenna, set by where the module is mounted under the
>   perch, has to be measured with the real leg rings.

**Camera(s)**
- One or more cameras. A hardware description will be provided.

> **Post-hoc edit (2026-10-19): camera.**
> - **Type:** an **industrial camera** (Basler, FLIR or IDS); the exact vendor and model are still to be provided.
> - **Supported now:** Basler, through pypylon, using hardware timestamps, frame counters and optionally GPIO line
>   state.
> - **Still stubs:** FLIR and IDS adapters, to be filled in once the model is known.

> **Post-hoc edit (2026-10-19): Point Grey Chameleon3 assessed.** The candidate camera was checked against
> the [Chameleon3 technical reference](https://www.teledynevisionsolutions.com/products/chameleon3-usb3/) (v3.0, 2015).
> - **Verdict:** suitable, with conditions.
> - **Model choice:**
>   - The CM3-U3-13Y3 is the best fit: 1.3 MP global-shutter CMOS, 91 fps Mono8 at full frame.
>   - The 13S2 is fine: 30 fps.
>   - The 28S4 is marginal: 13 fps at full frame. It would need a reduced resolution, or a sync LED pulse of
>     ≥ 160 ms.
> - **Timing data available:** a hardware timestamp and frame counter for every frame.
> - **Not available:** a per-frame GPIO line-state chunk, so the sync LED in the image is used.
> - **Needed:**
>   - a CS-mount lens
>   - a USB 3.0 PCIe 2.0 port at the cage PC
>   - Teledyne FLIR's **Spinnaker SDK** with a PySpin wheel for Python 3.12 (FlyCapture2 is no longer developed)
>   - the `flir` backend in MAVRIC, **now implemented** and tested against a simulated PySpin
> - **Still to do:** run `mavric camtest` on the real camera.
> - **Availability:** Teledyne lists the Chameleon3 USB3 as legacy / not recommended for new designs. Using a unit
>   already on hand is fine; buying new, a Blackfly S USB3 works with the same backend and adds a line-status chunk.
> - **Details:** README, "FLIR / Point Grey cameras".

**Cage**
- A dedicated cage holding the birds, one or more cameras and the RFID perches.
- Big enough for distribution and movement in three dimensions.

**PC**
- A PC next to the cage, with dedicated MATLAB and Python configurations.

> **Post-hoc edit (2026-10-19): PC configuration.**
> - **Python:** a conda (Miniconda) environment, defined in `environment.yml` on conda-forge with Python 3.12.
> - **MATLAB:** not required for MAVRIC, but it can read every output (CSV/JSON). The original MultiRFID MATLAB tool
>   still runs with the new firmware.

**Software**
- Dedicated software (see section 6).

## 6. Software

### 6.1 Two levels

**During acquisition**
- An easy GUI to prepare, trigger and save the data from RFID polling and video recording.
- The two streams must be synchronized well enough that no significant time drift appears.
  - *Editorial note:* the original reads "synchronized enough so significant time drifts appear". It is read here as
    "so that **no** significant time drifts appear".

**After acquisition**
- A GUI, perhaps the same one, to manually label the videos, curate the RFID data, merge both, and visualize the
  results in quantitative and qualitative terms.
- The list of outputs is open-ended. Examples: accuracy plots, time spent by each ID at each location, videos labeled
  post hoc.

> **Post-hoc edit (2026-10-19): implemented as separate tools.**
> - **`mavric acquire`:** live view, ROIs, live RFID, recording, triggered clips.
> - **`mavric review`:** labeling, curation, analysis results, rendering.
> - **`mavric analyze` and `mavric render`:** the same steps from the command line.
> - **Outputs:**
>   - accuracy per perch and per pigeon
>   - a confusion matrix
>   - bout timing error
>   - a per-pigeon occupancy timeline
>   - time budget and perch-to-perch transitions
>   - sync diagnostics
>   - an annotated video

### 6.2 RFID hardware support

**Tool.** The RFID hardware is supported by the tool in `C:\Code\multirfid-standalone`.

**Hardware path used.** Only the Arduino-based simultaneous polling of 4 antennas is used here, not the DORSET
hardware yet.

**Platform.** That tool allows a MATLAB-centered control GUI. Python is a possible alternative; the RFID tool would
then need adapting.

> **Post-hoc edit (2026-10-19): Python chosen.**
> - **Port:** the RFID line protocol was ported to `mavric/rfid/`. It adds EM4100 checksum verification, unwrapping of
>   the 32-bit `micros()` counter, detection of board resets, and logging written to disk as it arrives (crash-safe).
> - **Firmware:** a variant in `firmware/` adds a sync LED pulse (`SYNC` lines) and a `BOOT` marker. The line format
>   is unchanged.
> - **Issues found in MultiRFID while porting** (not fixed in that repository):
>   - The PC timestamp is local time stored as if it were UTC.
>   - The `micros()` wrap goes undetected after sorting.
>   - "Time at reader" is computed as a span rather than as occupancy.
>   - Data is kept only in RAM until the session stops.
>   - The details are in the implementation plan.

### 6.3 Candidate tools for labeling and analysis (a benefit of Python)

**DeepLabCut**
- [Original repository / docs](https://deeplabcut.github.io/DeepLabCut/README.html)
- [Bird-focused SuperAnimal model](https://huggingface.co/DeepLabCut/DeepLabCutModelZoo-SuperAnimal-Bird/tree/main)
- [Multi-animal project guidance (Uni Marburg)](https://gitlab.uni-marburg.de/polgari/deeplabcut_project/-/blob/677ea7ec173d9165752e16d17cd84f4af6903dc8/docs/maDLC_UserGuide.md)

**Ethograph**
- [Ethograph](https://akseli-ilmanen.github.io/ethograph/) is fairly new software built specifically for analysing
  movement.
- It can combine data from several streams, including DeepLabCut.
- It has not been tested with our RFID data.

> **Post-hoc edit (2026-10-19): evaluation.**
> - **DeepLabCut 3.x** (PyTorch) works on Windows with an NVIDIA GPU.
> - **SuperAnimal-Bird** gives bird detection plus 42 keypoints with no training, but **no identity** across frames.
>   DeepLabCut's own identity head only helps if the birds look different.
> - **The Marburg guide** is a copy of the older TensorFlow-era official guide, with no bird-specific content.
> - **Ethograph** (v0.2.x) is pre-alpha with one developer. It has no ROI drawing and no accuracy plots, but its data
>   model could hold perch × pigeon labels.
> - **Decision:** ground truth uses the dedicated review GUI, with BORIS as a stopgap. DeepLabCut and an Ethograph
>   export are deferred to later phases. Python 3.12 keeps the environment compatible with DeepLabCut.

### 6.4 Acquisition: live view and ROIs

**Constraint.** Acquisition comes before analysis and is not tied to the analysis software.

**Live view.** The camera recordings (hardware and positions to be specified) feed live into the acquisition GUI.

**ROIs**
- Up to 4 ROIs can be set (for now) to restrict the labeling.
- They are placed where the RFID antennas are, and named after the known antennas.
- The RFID data can be shown live and tied to these ROIs.

> **Post-hoc edit (2026-10-19): ROIs implemented.**
> - **Perch ROIs:** one polygon per antenna, named from `rig.yaml → antennas`. An ROI lights up in the pigeon's
>   colour while its antenna reads a tag.
> - **Extra ROI:** a box over the sync LED, used to check synchronization.

### 6.5 Synchronization

**Requirement.** Synchronize both streams in the easiest possible way, then combine them afterwards for the analysis
phase.

> **Post-hoc edit (2026-10-19): approach chosen.**
> - **Shared clock:** every serial line and every camera frame is stamped with the same host monotonic clock, in one
>   process.
> - **Clock fits:** the Arduino and camera hardware clocks are fitted to that host clock offline.
> - **Validation:** an Arduino-driven LED, flashing at random intervals and visible to the camera, checks the result.
>   The acceptance is a median residual under 1 frame and a p95 under 100 ms.
> - **Why the fit matters:** the MultiRFID example data showed an Arduino clock drift of about 145 ppm, roughly
>   0.5 s per hour, which the fit corrects.
> - **Not needed:** a hardware frame trigger is not required at this accuracy.

## 7. Very first goal

**Goal.** Set everything up and run a fully integrated recording that gives interpretable results on the locations
of individuals over time.

> **Post-hoc edit (2026-10-19): status.**
> - **Done:** the software for this goal is in place and checked end to end on simulated sessions with known ground
>   truth, including a real-time simulated acquisition.
> - **Next steps:**
>   1. Bench bring-up: confirm the tag type, map readers to perches, measure the read zone and crosstalk, and check
>      camera placement. See the README, "Bring-up checklist".
>   2. A first integrated recording with the pigeons.
>   3. Blind labeling of 10–15 minutes.
>   4. `mavric analyze`.

---

## Decision log

| Date | Decision | Origin |
|---|---|---|
| 2026-10-19 | Python for acquisition and analysis (MATLAB optional on the outputs) | Planning discussion |
| 2026-10-19 | Custom review GUI for ground truth; BORIS as stopgap | Planning discussion |
| 2026-10-19 | Industrial camera (vendor/model to be specified); Basler backend first | Planning discussion |
| 2026-10-19 | Firmware sync addition kept as a copy in `MAVRIC/firmware` | Planning discussion |
| 2026-10-19 | ID-20LA readers with integrated antennas | Clarification by the project lead |
| 2026-10-19 | Two birds on one perch treated as not expected (perch size) | Clarification by the project lead |
| 2026-10-19 | RFID-triggered recording added as an application and implemented | Request by the project lead |
| 2026-10-19 | Installation through a conda (Miniconda) environment | Request by the project lead |
| 2026-10-19 | Point Grey Chameleon3 assessed as a candidate camera; Spinnaker `flir` backend implemented | Request by the project lead |
| 2026-10-21 | Event-driven outputs: Phase A (rules engine, Outputs menu) and B (I/O Arduino) built; motor (TMCM-1141) on hold | Request by the project lead |
| 2026-10-21 | Audio planned (`docs/audio_plan.md`): two NT5 mics on the MOTU M4, sync pulse recorded on an audio channel, sound-event labeling, stimulus playback as an Outputs device | Request by the project lead |
| 2026-10-21 | Audio decisions: 4 powered speakers on the M4, D22 sync cable to M4 In 3, 48 kHz, customizable event types, emitter = tagged subject or the system (auto-labeled), stimuli < 2 s. AU0–AU2 (recording, live view, sync) built | Project lead |
| 2026-10-21 | Second Chameleon3 (cam1, clips only); camera clip rules editable in the acquire GUI (Outputs… → Camera clips); ROIs fixed to the first rig.yaml camera | Bench test by the project lead |
| 2026-10-23 | Renamed V+RFID → **MAVRIC** (Multimodal Audio-Video-RFID Interaction and Control): the suite now covers video, audio, RFID identification and event-driven task control. Package and command `mavric`; firmware `MAVRIC_IO` (old `VRFID_IO` still accepted) | Project lead |
| 2026-10-23 | Release preparation (v0.8.0): hover tips on every control, Help and About windows, MIT licence (RFID firmware stays GPL-3.0 as a MultiRFID derivative), README rewritten as a manual with screenshots from simulated data; suite name: MAVRIC (see the rename entry) | Request by the project lead |
| 2026-10-23 | Video settings in the acquire GUI (config/video.yaml); raw recording by default (lossless FFV1 chunks of ~2 GB); `mavric compress` → `<name>_compressed.mp4` with DeepLabCut-oriented defaults (H.264 CRF 18, keyframe every 30 frames, constant rate, optional 2× downscale) | Request by the project lead |
| 2026-10-23 | All-cameras monitor view in acquisition; AU3 built: sound events in `mavric review` (audio lanes, listening, customizable types, emitter = tagged subject / system / unknown with suggestions, automatic proposals, Audacity/Raven exchange) | Request by the project lead |
| 2026-10-27 | Sync pulse moved from MEGA D22 to **D52** (firmware `SYNC_PIN`); the LED, camera, Intan and audio sync wires follow it | Project lead |
| 2026-10-27 | **D50** and **D48** give the same sync pulse as D52 (firmware `SYNC_PINS`), so the LED keeps D52 and the camera, Intan and audio wires can have their own pins | Project lead |
| 2026-10-28 | Final sync wiring: **D52** the sync LED only (a visible red LED, 330 Ω), **D50** the audio sync (10 kΩ / 1.8 kΩ divider, TRS tip = signal, ring = GND, into M4 In 3), **D48** spare for Intan or the camera opto input. The 12 V MOSFET sync LED option is dropped | Project lead |
