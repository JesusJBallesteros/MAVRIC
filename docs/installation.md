# Installing MAVRIC from scratch (Windows)

How to set up a MAVRIC acquisition and analysis PC, starting from a clean Windows installation. It covers the tools,
the drivers and vendor software for the supported hardware, MAVRIC itself, the firmware and the first checks.
For a PC that already has Git, conda and the drivers, the short version in the [README](../README.md#install) is
enough. Once installed, continue with the [quick-use guide](quickstart.md): configuration, device setup and the
first session.

- **Starting point:** Windows 10 or 11, 64-bit, with internet access. Nothing else is assumed.
- **Hardware covered:** RFID Arduino MEGA 2560 with ID-20LA readers, I/O Arduino UNO, FLIR / Point Grey USB3
  cameras (e.g. Chameleon3), Basler cameras, MOTU M4 audio interface, Intan RHD recording controller, MATLAB. Skip
  the parts your rig doesn't use.
- **Time:** about 1–2 hours, mostly downloads.
- **Tested versions** are listed in [§10](#10-tested-versions).

**Contents**
1. [Before you start](#1-before-you-start)
2. [Windows settings](#2-windows-settings)
3. [Base tools: Git, conda, runtimes](#3-base-tools-git-conda-runtimes)
4. [Drivers and vendor software](#4-drivers-and-vendor-software)
5. [MAVRIC](#5-mavric)
6. [Your rig configuration](#6-your-rig-configuration)
7. [Firmware](#7-firmware)
8. [First checks, device by device](#8-first-checks-device-by-device)
9. [Optional tools](#9-optional-tools)
10. [Tested versions](#10-tested-versions)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Before you start

**You need:**
- **Administrator rights** for the drivers (Arduino, Spinnaker, MOTU, Intan). On a managed (institutional) PC, ask IT to
  install the items marked *admin* below, or to grant temporary rights.
- **Accounts:**

  | Account | For | Required? |
  |---|---|---|
  | Teledyne (FLIR) | Spinnaker SDK and PySpin downloads | yes, for FLIR cameras |
  | MathWorks | MATLAB | only for MATLAB outputs |
  | GitHub | cloning works without one; needed to push or publish | optional |
  | MOTU | registration unlocks bundled software; the driver itself is free | optional |

- **Disk space:** about 15 GB for software and the environment. Raw video needs about **50 GB per camera-hour**
  (1280×1024 Mono8, 30 fps, lossless FFV1), so put sessions on a large internal NTFS drive.
- **Leave the hardware unplugged** until its driver is installed (§4).

**Two ways to install the general tools:**
- **winget** (built into Windows 11; the package IDs below were checked). Run the commands in **PowerShell**.
- **Installers** downloaded from the vendor pages linked in each step.

---

## 2. Windows settings

Long recordings must not be interrupted by sleep, USB power saving or restarts.

1. **Power plan:** *Settings → System → Power* → Power mode **Best performance**; screen and sleep: **Never** while
   plugged in. The same from PowerShell:
   ```powershell
   powercfg /change standby-timeout-ac 0
   ```
   ```powershell
   powercfg /change hibernate-timeout-ac 0
   ```
2. **USB selective suspend off** (cameras and Arduinos must never be powered down): *Control Panel → Power Options →
   Change plan settings → Change advanced power settings → USB settings → USB selective suspend setting → Disabled*.
3. **Windows Update:** set *active hours* to cover your recording times, and pause updates before long sessions.
4. **Data folder:** create a sessions folder on the data drive, for example `D:\MAVRIC_sessions`.
   - Keep it **outside OneDrive** or any synced folder: syncing 50 GB/h of video slows recording down.
   - Optional, with IT's agreement: exclude it from real-time antivirus scanning, which costs CPU on large writes.

---

## 3. Base tools: Git, conda, runtimes

You do **not** need a separate Python installation: conda creates MAVRIC's own Python 3.12 in §5. A Python from
python.org, if present, does not interfere.

| Tool | Why | winget | Installer |
|---|---|---|---|
| **Git** | get and update MAVRIC | `winget install -e --id Git.Git` | [git-scm.com](https://git-scm.com/downloads/win) |
| **Miniforge** (recommended) or **Miniconda** | Python environments | `winget install -e --id CondaForge.Miniforge3` or `winget install -e --id Anaconda.Miniconda3` | [conda-forge.org/download](https://conda-forge.org/download/) · [Miniconda](https://www.anaconda.com/download/success) |
| **Visual C++ Redistributable 2015–2022 x64** | runtime for PySpin and other binary packages (Spinnaker also installs it) | `winget install -e --id Microsoft.VCRedist.2015+.x64` | [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe) |
| GitHub CLI (optional) | sign in to GitHub, publish releases | `winget install -e --id GitHub.cli` | [cli.github.com](https://cli.github.com/) |
| A text editor (optional) | edit `config/*.yaml` | `winget install -e --id Microsoft.VisualStudioCode` | [code.visualstudio.com](https://code.visualstudio.com/) |

**Notes**
- **Git installer defaults** are fine. Keep *"Checkout Windows-style, commit Unix-style line endings"*.
- **Miniforge or Miniconda:** both work. Install *for the current user* (no admin needed), and don't add it to PATH:
  you use its own prompt.
  - Miniforge uses only the free conda-forge channel, like MAVRIC's `environment.yml`.
  - Miniconda's base uses Anaconda's channels, whose terms of service can require a licence for large organisations.
    Check your institution's policy.
- **From here on,** "the conda prompt" means **Miniforge Prompt** or **Anaconda Prompt (miniconda3)** in the Start
  menu.
- **Git identity** (once, only if you will commit). GitHub's noreply address
  (`<id>+<username>@users.noreply.github.com`, shown in GitHub → Settings → Emails) keeps your e-mail private:
  ```bash
  git config --global user.name "Your Name"
  ```
  ```bash
  git config --global user.email "you@example.org"
  ```
- **GitHub CLI**, if installed: `gh auth login`, then choose GitHub.com → HTTPS → browser.

---

## 4. Drivers and vendor software

Install each one **before** plugging in its device. Restart Windows once at the end of this section.

### 4.1 Arduino IDE 2 (RFID MEGA and I/O UNO) · *admin for the drivers*

- **Install:** `winget install -e --id ArduinoSA.IDE.stable`, or from [arduino.cc/en/software](https://www.arduino.cc/en/software/).
  - When the installer asks to install **device software / USB drivers**, accept every prompt.
- **Board package:** open the IDE → *Boards Manager* → install **Arduino AVR Boards** (MEGA 2560 and UNO).
- **Clone boards only:** if a board shows up in Device Manager as *USB2.0-Serial* or *CH340* without a COM port,
  install the WCH **CH341SER** driver from [wch-ic.com](https://www.wch-ic.com/downloads/CH341SER_EXE.html).
  Genuine Arduino boards need no extra driver.
- **Command-line uploader:** `arduino-cli.exe` comes with the IDE, in
  `%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\` for a per-user install, or under
  `C:\Program Files\Arduino IDE\…` for an all-users install. The IDE alone is enough to flash the firmware (§7).

### 4.2 FLIR / Point Grey cameras: Spinnaker SDK · *admin*

1. Sign in at [Teledyne Spinnaker SDK](https://www.teledynevisionsolutions.com/products/spinnaker-sdk/) → Download →
   **Windows**. Get two files of the **same version**:
   - **SDK:** `SpinnakerSDK_FULL_<version>_x64.exe`
   - **Python package:** `spinnaker_python-<version>-cp312-cp312-win_amd64` (zip or wheel) — **cp312** = Python 3.12.
2. **Run the SDK installer:**
   - Choose the **USB3** interface driver. GigE is needed only for GigE cameras.
   - The installer also adds SpinView, the Visual C++ runtimes and the Teledyne GigE components.
   - If you install the GigE driver, also install any GigE driver security patch listed on the same download
     page.
3. **Plug the camera into a USB 3 port:** a motherboard port or a PCIe USB 3 card, with a ≤ 5 m screw-locking cable.
4. **Open SpinView** (Start menu → Spinnaker → SpinView). Check that the camera is listed and streams.
   - Close SpinView before starting MAVRIC: only one program can open a camera.
   - Optional: update the camera firmware from SpinView.
5. **Keep the Python package** for §5.3.

**Bandwidth:** each 1280×1024 camera at 30 fps uses about 39 MB/s. One USB 3 controller carries about 384 MB/s,
so six cameras is about the limit on one controller. Add a PCIe USB 3 card with one controller per port beyond that.

### 4.3 Basler cameras (only if you use them) · *admin*

Install the **pylon Software Suite** from [baslerweb.com](https://www.baslerweb.com/en/software/pylon/), with the USB3
(and GigE, if needed) drivers. Check the camera in *pylon Viewer*. MAVRIC's environment already contains `pypylon`.

### 4.4 MOTU M4 audio interface · *admin*

1. Download the **M Series driver for Windows** from [motu.com/m4-start](https://motu.com/m4-start)
   (registration is optional; the driver is free). Install it, then plug in the M4 (USB-C).
2. MAVRIC uses the driver's **ASIO** interface; nothing else is needed. Set the sample rate to **48 kHz** if the MOTU
   control panel asks for one.
3. Windows may make outputs 3–4 the default playback device. Set the default playback device back to outputs 1–2
   in *Sound settings* if Windows sounds go to the wrong speakers.
4. **Connections** (wiring in [wiring.md §7](wiring.md#7-audio-motu-m4-two-rode-nt5-sync-pulse-four-speakers)):
   microphones on In 1–2 with 48 V on, the sync cable from the RFID MEGA (D50) on In 3, speakers on Out 1–4. The
   headphone output carries Out 1–2 only, which is enough for a first check without speakers.

### 4.5 Intan RHD recording controller (only for Intan outputs) · *admin*

1. Download **Intan RHX** from [intantech.com/downloads](https://intantech.com/downloads.html?tabSelect=Software).
   Install it and accept the USB driver it offers for the controller.
2. Connect the controller and check that RHX finds it.
3. **For MAVRIC's *Intan RHX* output device only:** in RHX, *Network → Remote TCP Control → Command port* (5000 by
   default) → **Connect**. TTL outputs from the I/O Arduino into the controller's digital inputs need no software.

### 4.6 MATLAB (only for MATLAB outputs)

Install **MATLAB R2026a** (or newer) from your MathWorks account. R2026a's Engine API supports Python 3.12, which
MAVRIC needs. The engine is added to MAVRIC's environment in §5.3.

---

## 5. MAVRIC

All commands in this section run in **the conda prompt**.

### 5.1 Get the code

```bash
cd /d C:\Code
```

(Create `C:\Code` first if it doesn't exist: `mkdir C:\Code`.)

```bash
git clone https://github.com/JesusJBallesteros/MAVRIC.git
```

```bash
cd MAVRIC
```

### 5.2 Create the environment and install

```bash
conda env create -f environment.yml
```

This creates the `mavric` environment (Python 3.12, numpy, pandas, PySide6, PyAV with FFmpeg, OpenCV, sounddevice
with ASIO, pypylon, pytest). It takes a few minutes.

```bash
conda activate mavric
```

```bash
pip install --no-deps -e .
```

`--no-deps` keeps pip from replacing conda's packages; `-e` means updates through `git pull` apply immediately.

### 5.3 Hardware extras in the environment

**PySpin (FLIR cameras):** use the file downloaded in §4.2, same version as the SDK. Unzip it first if it is a zip.

```bash
pip install --no-deps "C:\path\to\spinnaker_python-<version>-cp312-cp312-win_amd64.whl"
```

**MATLAB Engine (MATLAB outputs);** adjust the version folder to your MATLAB:

```bash
pip install "C:\Program Files\MATLAB\R2026a\extern\engines\python"
```

### 5.4 Check the installation

```bash
mavric --version
```

It prints `MAVRIC <version> (commit …, date)`.

```bash
mavric selftest
```

All tests should pass (about 2 minutes). The first run also creates `config\rig.yaml` and the other config files
from `config\examples\`.

```bash
mavric acquire --sim
```

This opens the acquisition window on a simulated rig: five subjects moving between four places, cameras,
microphones and the sync pulse. No hardware is needed.

**Every later session:** open the conda prompt, then `conda activate mavric`. To update MAVRIC:

```bash
git pull
```

```bash
conda env update -f environment.yml --prune
```

---

## 6. Your rig configuration

The repository ships only example settings (`config\examples\`). Your own files in `config\`, your recordings in
`sessions\` and your stimuli in `sounds\` are ignored by git, so tag IDs, serial numbers, ports, ROIs and data stay
on this PC.

- **Moving from another PC:** copy its `config\*.yaml`, `config\tags.csv`, `config\rois.json` and the `sounds\`
  folder into `C:\Code\MAVRIC\`, replacing the example copies. Then check the ports and serials (below).
- **Starting fresh:** edit the copies in `config\`. Every setting is explained in the
  [quick-use guide §3](quickstart.md#3-configuration).

| File | Set |
|---|---|
| `rig.yaml` | `rfid.port` (COM port of the MEGA, or `auto`), `antennas` (reader number → place name), `cameras` (backend `flir`, serial number, fps, `record: continuous` or `triggered`), `audio` (device `MOTU`, `host_api: ASIO`, inputs with the sync input, speakers, `enabled`), `paths.sessions_dir` |
| `tags.csv` | one row per tag: `tag_id,subject,color` |
| `outputs.yaml` | I/O Arduino port, rules and camera clips (easier from the acquire window: *Outputs…*) |
| `sounds\` | stimuli to play on the speakers (WAV/FLAC, ideally 48 kHz mono); create the folder if you play sounds |

- **Recordings on the data drive:** in `rig.yaml`, set `paths: sessions_dir: D:\MAVRIC_sessions`.
- **Camera serials:** shown by `mavric ports` and in SpinView.
- **Run `mavric` from any folder** (optional):
  ```bash
  conda env config vars set MAVRIC_ROOT=C:\Code\MAVRIC
  ```
  then `conda deactivate` and `conda activate mavric`.

ROIs are drawn later in the acquire window (*Place ROIs* → *Draw / reset* → *Save ROIs to rig*).

---

## 7. Firmware

Flash each board once, and again after firmware changes. Wiring and pin maps: [wiring.md](wiring.md).

| Board | Sketch | Board type (FQBN) |
|---|---|---|
| RFID Arduino MEGA 2560 | `firmware\MultiRFID_Reader_ID20LA_sync` | `arduino:avr:mega` |
| I/O Arduino UNO (outputs) | `firmware\MAVRIC_IO` | `arduino:avr:uno` |

**With the Arduino IDE:** *File → Open* the `.ino` file, *Tools → Board* and *Port*, then **Upload**.

**With arduino-cli** (from `C:\Code\MAVRIC`; use the `arduino-cli.exe` path from §4.1 if it is not on PATH):

```bash
arduino-cli compile --fqbn arduino:avr:mega firmware/MultiRFID_Reader_ID20LA_sync
```

```bash
arduino-cli upload --fqbn arduino:avr:mega -p COM3 firmware/MultiRFID_Reader_ID20LA_sync
```

Replace `COM3` with the board's port (`mavric ports`, or Device Manager → Ports). Repeat for the UNO with
`arduino:avr:uno` and `firmware/MAVRIC_IO`.

**Close the Arduino IDE's Serial Monitor** before using MAVRIC: only one program can open a COM port.

---

## 8. First checks, device by device

Connect one device at a time and check it before recording. The full bring-up order, with wiring checks, is in
[wiring.md §8](wiring.md).

| Device | Command | Expect |
|---|---|---|
| Everything connected | `mavric ports` | the MEGA (and UNO) COM ports, the FLIR / Basler cameras with serials |
| RFID MEGA + readers | `mavric link-test --port COM3` | steady lines at 8 Hz; each tag shows at the reader you hold it to |
| Camera | `mavric camtest --cam cam0` | measured fps, no drops, clock fit; snapshot in `sessions\_camtest_cam0.png` |
| MOTU M4 | `mavric audio devices`, then `mavric audio test --tones` | the M4 marked `<- configured`; tones on each speaker, levels and sync pulses on the inputs |
| Speakers / headphones | `mavric audio play test_tone --speaker spk1 --gain-db -20` | a short 1 kHz beep; then `mavric audio sounds` lists your stimuli |
| I/O Arduino | `mavric outputs test io` | `MAVRIC_IO 1.0 …` |
| Intan RHX (TCP) | acquire → *Outputs…* → select the Intan device → *Test* | RHX answers |
| MATLAB | in MATLAB: `matlab.engine.shareEngine('mavric')`, then *Test* on the MATLAB device | MATLAB answers |
| Whole rig | `mavric acquire` | live view, live RFID, sync LED box blinking every 2–6 s, audio meters |

Then make a short test recording and run `mavric analyze sessions\<session>`: the sync check should say **PASS**.
The [quick-use guide](quickstart.md) takes it from here: drawing the ROIs, recording, reviewing.

---

## 9. Optional tools

| Tool | For | winget / link |
|---|---|---|
| VLC | watching raw `.mkv` (FFV1) and compressed `.mp4` videos | `winget install -e --id VideoLAN.VLC` |
| FFmpeg | running the commands `mavric compress --print-ffmpeg` shows (MAVRIC itself doesn't need it) | `winget install -e --id Gyan.FFmpeg` |
| Audacity | checking and editing sound labels (`mavric sounds export/import`, Audacity labels) | `winget install -e --id Audacity.Audacity` |
| Raven Pro / Lite | sound labels as Raven selection tables | [ravensoundsoftware.com](https://www.ravensoundsoftware.com/) |
| BORIS | labels as ground truth (`mavric import-boris`) | [boris.unito.it](https://www.boris.unito.it/) |
| Notepad++ | a light editor for YAML and CSV | `winget install -e --id Notepad++.Notepad++` |
| DeepLabCut | pose estimation on the compressed videos | in its **own** conda environment, never inside `mavric`; see the [DeepLabCut installation guide](https://deeplabcut.github.io/DeepLabCut/docs/installation.html) |

---

## 10. Tested versions

MAVRIC 0.8.x was tested on Windows 11 24H2 with:

| Component | Version |
|---|---|
| Git / GitHub CLI | 2.50.0 / 2.98.0 |
| Miniconda (conda base) | py313_25.5.1 |
| `mavric` environment | Python 3.12.15, numpy 2.5.4, pandas 3.1.0, PySide6 6.11.2, pyqtgraph 0.14.0, PyAV 19.0.2, opencv-python-headless 5.0.0.93, sounddevice 0.5.6, soundfile 0.14.0, pypylon 26.9, pyserial 3.5 |
| Spinnaker SDK / PySpin | 4.4.0.246 / spinnaker_python 4.4.0.246 cp312 |
| Arduino IDE / Arduino AVR Boards | 2.3.10 / 1.8.8 |
| MATLAB | R2026a |
| Visual C++ Redistributable 2015–2022 x64 | 14.50 |
| VLC / FFmpeg | 3.0.24 / 8.0.1 |

Newer versions should work. The one hard rule is that **PySpin must match the Spinnaker SDK version** and be the
**cp312** build.

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `conda` is not recognized | use the conda prompt from the Start menu, not a plain terminal |
| `mavric` is not recognized | `conda activate mavric` first |
| `import PySpin` fails | the wheel doesn't match the SDK version or isn't cp312; reinstall the matching pair |
| Camera not found / busy | close SpinView; check `mavric ports`; use another USB 3 port |
| Arduino has no COM port | reinstall the Arduino IDE drivers; for clones, the CH341SER driver |
| COM port busy | close the Arduino Serial Monitor and any other MAVRIC window |
| M4 not listed in `mavric audio devices` | reinstall the MOTU driver, replug the M4, check `audio.device: MOTU` and `host_api: ASIO` in `rig.yaml` |
| "Could not load the Qt platform plugin" | `pip uninstall opencv-python`, keeping `opencv-python-headless` |
| `mavric selftest` fails right after installing | check that `conda activate mavric` was run first; otherwise open an [issue](https://github.com/JesusJBallesteros/MAVRIC/issues) with the output |

More detail: [README](../README.md) (user manual), [quickstart.md](quickstart.md) (configuration and setup),
[reference.md](reference.md) (every option), [wiring.md](wiring.md) (wiring and bring-up).
