# Event-driven outputs: feasibility and plan

**Question.** Can MAVRIC send signals to other devices when a perch (an RFID antenna) or a camera ROI becomes active?
Example targets:
- start an Intan recording
- switch LEDs
- run a MATLAB script
- move a small stepper motor: a Trinamic QSH4218-47-20-044 on a **TMCM-1141 v1.3** driver/controller, today run
  by a stored TMCL program that MATLAB triggers

**Short answer.** Not yet, but the architecture is ready for it and every listed target is feasible.
- **What exists today.** Acquisition already publishes RFID presence changes (antenna, tag, on/off, host time) on an
  internal event bus. The RFID-triggered video clips are built on that bus.
- **What is missing:**
  - output drivers
  - a rule engine that maps events to outputs
  - camera-side ROI activity detection: today the camera ROIs are drawn for display and labeling only, and nothing
    analyses the image live
  - a configuration menu

> **Status (2026-10-21).** **Phase A** (rules engine, devices, Outputs menu, logging, faster RFID events) and
> **Phase B** (I/O Arduino firmware `firmware/MAVRIC_IO`, driver, wiring in `docs/wiring.md` §6) are implemented and
> tested. The Arduino board, Intan RHX, MATLAB and the TMCM-1141 have not yet been tested on real hardware.
> - **Deferred:** the camera-GPIO output. Its Chameleon3 nodes could not be verified because the camera was not
>   connected. The I/O Arduino covers TTL needs.
> - **On hold:** **Phase B2** (motor), until the TMCL program and MATLAB code are available.
> - **Next:** **Phase C** (camera ROI activity).

---

## 1. Event sources

| Source | Status | How it would work | Latency (event → PC knows) |
|---|---|---|---|
| **RFID antenna** (any tag, or specific pigeons) | exists | Presence changes from the 8 Hz sampling rounds. | **0.1–0.25 s** today: a round closes when the next one arrives. Closing rounds on a ~5 ms timeout cuts this to ≤ 0.13 s. |
| **RFID, handled on the Arduino itself** | new (optional) | The MEGA compares the parsed tag with a rule and switches a pin directly, with no PC round trip. | **~1–5 ms** after the reader delivers the tag |
| **Camera ROI activity** | new | Per perch ROI, compare the live frame with a reference "empty" background. Fire when the changed-pixel fraction crosses a threshold, with hysteresis and a minimum duration. | ~1–3 frames (**35–100 ms** at 30 fps) |
| **Camera ROI, model-based** (later) | future | DeepLabCut SuperAnimal-Bird detections inside each ROI (GPU). | ~50–150 ms |
| **Combined** | new | Logical conditions, e.g. "RFID says S03 on PerchB **and** the camera ROI PerchB is active". | max of the inputs |

## 2. Output routes

| Route | Hardware | Typical use | Latency / jitter | Feasibility |
|---|---|---|---|---|
| **TTL via an Arduino over USB serial** | A small **dedicated** Arduino (Uno / Nano Every / MEGA) with a MAVRIC I/O sketch, or free pins on the RFID MEGA | Intan DIGITAL IN, LEDs (through a transistor), relays, stepper driver STEP/DIR, any lab device with a TTL input | ~1–4 ms after the PC decides; sub-ms jitter on the board | **High: recommended backbone** |
| **TCP / UDP messages** | Network or localhost, no extra hardware | **Intan RHX** remote control, MATLAB listeners, Bonsai, other PCs | < 5 ms on a LAN, plus receiver time | **High** |
| **MATLAB** | MATLAB R2026a on the same PC (Engine API supports Python 3.12) | Run a script or function with event arguments | 10–50 ms per call plus script runtime; start the engine once per session (~10–20 s) | **High** |
| **Programs / scripts** | none | Any executable or `.bat` / `.py` with event arguments | ~10–100 ms process start | **High** |
| **Camera GPIO out** | Chameleon3 Line 1 (opto out) / Lines 2–3 via `UserOutputValue` | A TTL output without an Arduino | ~1–5 ms (USB register write); only 1–3 lines, and outputs depend on the camera running | Medium |
| **Stepper motor** | QSH4218 on the lab's **TMCM-1141 v1.3** (USB virtual COM port, TMCL protocol; see §4) | Moving doors, feeders, stimuli | ~2–5 ms per TMCL command, then the motion time | **High: no extra hardware** |
| **Bluetooth (BLE)** | BLE module or board | Untethered gadgets | 7.5–50+ ms, irregular, drops packets | **Not recommended** for experiment triggers or sync |
| **Wi-Fi boards** (ESP32, UDP) | ESP32 | Distant non-critical devices (lights) | 5–50 ms, irregular | Low priority |
| **USB relay boards** | off-the-shelf | Mains loads (lamps, pumps) | 10–20 ms mechanical | Medium (via the Arduino plus a relay module is equivalent) |

**Recommendation.** Use one **dedicated I/O Arduino** for all TTL-type outputs, plus **TCP/UDP**, **MATLAB** and
**command** outputs on the PC. Drive the **stepper through its own TMCM-1141 over USB**, so no Arduino is
involved in the motor.
- **Why a separate board:** it keeps the RFID MEGA's timing untouched. Every command is echoed back with the board's
  `micros()`, so output times can be placed on the session clock exactly as RFID reads are.
- **Reusing the RFID MEGA:** it has free pins (D22–D47, D49, D51, D53), so the same command protocol can be added to it later if a
  second board is unwanted.

## 3. Intan (your RHD recording controller)

From the [Intan Recording Controller user guide](https://intantech.com/files/Intan_Recording_Controller_user_guide.pdf):

**Hardware**
- **DIGITAL IN 1–2 (front BNCs):** TTL. 0–0.8 V reads low and 2.0–5.5 V reads high; never exceed 5.5 V. They are
  sampled in sync with the amplifiers (up to 30 kS/s) and can **trigger recording**. An Arduino 5 V output is
  directly compatible.
- **Grounding:** connect the Arduino GND to the controller's **I/O GND** binding post.
- **I/O expander:** adds 14 more digital inputs.
- **DIGITAL OUT 1–2 (rear):** 3.3 or 5 V, for the Intan's own threshold comparators. They are not needed here.

**Software**
- **Episodic triggered recording:** a high level on a chosen digital input starts saving with 1–30 s of pre-trigger
  data. Saving continues until the line goes low, plus 1–9999 s post-trigger. Each episode becomes a new file.
  - MAVRIC can drive this with a **"high while condition holds"** TTL, e.g. high while any bird is on PerchB.
- **Remote control over TCP:** this needs the newer **Intan RHX** software, which supports the RHD recording
  controller. The software in the guide has no network control.
  - RHX acts as a **TCP server** and accepts ASCII commands such as `set runmode run` / `record` / `stop` and
    `get runmode`. MAVRIC can connect as a client and start or stop recordings by rule.
  - Check exact command names against the "RHX TCP Control" guide for the installed version.

**Synchronisation for free.** Route the existing random **sync pulse** (MEGA **D48**, the spare sync output) to **Intan DIGITAL IN 2**. The
ephys file then contains the same pulse train as the RFID log and the video, so all three can be aligned offline to
sub-millisecond on the Intan side. Each event TTL sent to DIGITAL IN 1 additionally marks *when MAVRIC acted*.

## 4. Stepper motor: QSH4218-47-20-044 on a TMCM-1141 v1.3

### 4.1 Hardware

| Item | Value | Notes |
|---|---|---|
| **Motor** | Trinamic QSH4218-47-20-044 | NEMA-17, 1.8°/step, 47 mm long. By the series naming, about 2.0 A RMS per phase and 0.44 N·m. |
| **Supply** | 9–28 V DC, 24 V nominal | |
| **Motor current** | Programmable. Low range up to **1.1 A RMS**; high range up to **2.0 A RMS / 2.8 A peak** | The high range is **new in hardware v1.3**, so it can deliver the motor's full rated current. Start lower: torque needs are usually small. |
| **Motion** | On-board ramp generator (positioning, velocity mode), microstepping | stallGuard2 for sensorless stall detection, coolStep for load-adaptive current |
| **Interfaces** | **USB** (virtual COM port), **RS-485**, opto-isolated **step/dir/enable** input | |
| **I/O** | 3 general-purpose digital inputs (alternatively STOP_L / STOP_R / HOME), 1 analog input, 2 general-purpose outputs | Pin assignment: TMCM-1141 hardware manual, I/O connector table |
| **Programming** | **TMCL** commands from a host ("direct mode"), or a **stored TMCL program** that the module runs by itself ("stand-alone mode", edited in TMCL-IDE) | |

### 4.2 How it is used today, and whether we can control it at will

Today a TMCL program is stored on the module and MATLAB triggers it. Depending on how that program was written, the
trigger is one of:
- a host command that **starts the stored application**
- a host write to a **user variable** that the running program polls
- a level on one of the module's **digital inputs** that the program waits for

**Yes, we can control the motor at will.** TMCL accepts commands from a host over USB at any time. It also lets the
host start, stop and inspect the stored program, and read or write its variables. There are three ways to integrate,
from least to most change:

| Mode | What MAVRIC sends | Change to your protocol | When to use |
|---|---|---|---|
| **1. Trigger the stored program as MATLAB does** | The same TMCL command(s) MATLAB sends today: start the application, or write the trigger variable. **Or** a TTL from the I/O Arduino into one of the module's inputs, if the program waits on an input (lowest latency, no USB contention). | None | First step: reproduce exactly what you have, now driven by RFID or camera rules |
| **2. Parametrised stored program (recommended)** | The program becomes a small command interpreter. It loops on a *command* user variable (code, target/steps, speed), executes the move on the module (absolute/relative move, rotate, stop), clears the command and posts a *status* variable. MAVRIC **and** MATLAB both send moves by writing those variables. | Small rewrite of the TMCL program. I can draft a template. | Any move on demand, while motion stays on the module (safe even if the PC hangs). MATLAB keeps working with the same variables. |
| **3. Direct mode** | Stop the stored program, then send motion commands directly: move to position (absolute/relative), rotate, motor stop, and set axis parameters (current, velocity, acceleration, microsteps). | Program not used during sessions | Maximum flexibility from Python, and the simplest code. Ramps are still generated on the module. |

Two cautions apply to all three modes:
- **Don't mix modes on the same axis.** Direct motion commands while a stand-alone program also moves the axis
  will fight each other. Modes 2 and 3 avoid that.
- **One program per COM port.** Only one program can hold the module's USB port.
  - Either **MAVRIC owns the port** and MATLAB asks MAVRIC for moves (a UDP message), or MATLAB owns it and MAVRIC asks
    MATLAB (Engine API or UDP).
  - Alternatively, use **RS-485 for one client and USB for the other**. TMCL serves both, but simultaneous commands
    still need coordination, as in Mode 2.

### 4.3 Software

**Driver:** a small TMCL client in `mavric/outputs/tmcl.py`, using pyserial and no other dependency (`pytrinamic` as an
optional alternative).
- **Frame:** the standard 9-byte binary frame: module address (default 1), command, type, motor/bank, 32-bit value
  (big-endian), checksum (sum of bytes mod 256).
- **Reply:** also 9 bytes, with status 100 meaning OK.
- **Commands used:**

| Code | Command |
|---|---|
| 1 / 2 | ROR / ROL (rotate right / left) |
| 3 | MST (motor stop) |
| 4 | MVP (move to position, absolute or relative) |
| 5 / 6 | SAP / GAP (set / get axis parameter) |
| 9 / 10 | SGP / GGP (user variables, bank 2) |
| 14 / 15 | SIO / GIO |
| 128 | stop application |
| 129 | run application |
| 135 | application status |
| 136 | firmware version |

These are the standard TMCL numbers. Each one gets checked against the TMCM-1141 firmware manual for the installed
firmware.

**Device settings, applied at session start:**
- COM port and module address
- current range and run/standby current
- max velocity and acceleration, microstep resolution
- **soft position limits**
- integration mode (1/2/3)
- for Mode 1: which command or variable or input starts the program

**Actions available to rules:**
- run stored program
- set variable(s)
- move relative / absolute
- rotate at velocity
- stop
- go home

Every command and its reply (with module status) is logged to `actions.csv`. The motor's actual position (GAP) is
logged after each move.

**Safety**
- Soft limits are enforced in MAVRIC, and on the module in Mode 2.
- Use a refractory period and a maximum number of moves per minute.
- MST (motor stop) on disarm, app exit or error.
- Optional reference/limit switches on STOP_L / STOP_R, and standby current when idle.
- A **"jog / stop" panel** in the Outputs dialog for setup.

## 5. MATLAB

| Way | How | Pros / cons |
|---|---|---|
| **Engine API (recommended)** | `pip install` the `matlabengine` shipped with R2026a into the `mavric` env. In your MATLAB run `matlab.engine.shareEngine('mavric')`. MAVRIC connects and calls `run(script)` or `feval(fn, event)` in the background. | Runs in your live MATLAB workspace; ~10–50 ms per call. MATLAB must be open and shared. |
| **UDP/TCP listener** | A small MATLAB `udpport` / `tcpserver` callback receives `event,roi,pigeon,t` messages and runs code. | Fully decoupled and robust; MATLAB can run on another PC. A few lines of MATLAB to write. |
| `matlab -batch "script"` | A new MATLAB process per event | ~10–20 s startup. **Not** suitable for triggers. |

## 6. The configuration menu

The acquire window gets an **Outputs…** dialog. Everything it sets is saved to `config/outputs.yaml`, snapshotted into
each session, and every executed action is logged.

**Devices tab:** add, test and remove devices.

| Device type | Fields | Test button does |
|---|---|---|
| I/O Arduino | port | blink pin |
| Camera line | camera, line | toggle |
| TCP / UDP endpoint | host, port | send "ping" |
| Intan RHX | host, port | `get runmode` |
| MATLAB | shared-session name | `disp` |
| Command | executable, working dir | run with test args |
| Stepper (TMCM-1141, TMCL over USB) | COM port, module address, mode (stored program / command variables / direct), current range and currents, velocity, acceleration, soft limits | read firmware version and position, jog ±200 µsteps, STOP |

**Rules tab:** a table, one row per rule.

| Field | Choices |
|---|---|
| Enabled | ☑ |
| Source | RFID antenna (1–4 / any) · camera ROI (PerchA… / any) · both (AND) |
| Who | any tag · pigeons [S01, S03…] |
| Event | on enter · on leave · while present |
| Output | a device from the Devices tab |
| Action | pulse *n* ms · high while present · toggle · send text (with `{roi}`, `{pigeon}`, `{t}` placeholders) · run script with args · motor: run stored program / set variable / move relative or absolute / rotate / stop |
| Refractory | min seconds between firings |
| Delay | seconds before acting |

Each row also has a **Test** button.

**Example rules**

| Source | Who | Event | Output | Action |
|---|---|---|---|---|
| RFID PerchB | any | while present | I/O Arduino D30 → Intan DIGITAL IN 1 | high while present (episodic recording) |
| Camera ROI PerchA | — | on enter | I/O Arduino D31 (LED driver) | pulse 500 ms |
| RFID PerchC | S03 | on enter | MATLAB `feeder_reward('{pigeon}', {t})` | run, refractory 30 s |
| RFID any | any | on enter | Intan RHX TCP | `set runmode record` if stopped |
| Camera ROI PerchD | — | on enter | Stepper (TMCM-1141) | run stored program (Mode 1), or move +200 µsteps (Mode 2/3); refractory 10 s |

**Live view.** Each rule's state shows in the status panel: armed, active, last fired, count. A global **"Outputs
armed"** switch lets you rehearse rules without driving hardware.

## 7. Implementation plan

**Phase A: core and menu (no new hardware needed to test)**
1. `mavric/outputs/engine.py`: an **ActionEngine** subscribed to the event bus.
   - Evaluates rules: source/who/event, AND conditions, refractory, delay, while-present hold.
   - Runs actions on a worker thread, so a slow device never blocks acquisition.
2. Output drivers in `mavric/outputs/`:
   - `arduino_io` (serial protocol, echo with micros)
   - `net` (TCP/UDP), `intan_rhx` (TCP client)
   - `matlab` (Engine API, with a UDP fallback), `command` (subprocess)
   - `camera_line` (Spinnaker `UserOutputValue`)
   - `tmcl` (TMCM-1141 over USB: Modes 1–3)
   - `sim` and a **fake TMCL module** (for tests without the motor)
3. Session logging: `actions.csv` (host t, rule, event, device, command, result, device echo).
   - The analysis places actions on the timeline and reports trigger latency per rule, from the RFID or camera event
     to the output echo.
4. GUI: the **Outputs…** dialog (devices and rules tables, Test buttons, armed switch) and live rule status.
   `mavric outputs-test` CLI.
5. Tests with fake devices and the simulated cage: latency, refractory and hold semantics, logging.
6. Faster RFID events: close a sampling round about 5 ms after its last line instead of on the next round.

**Phase B: I/O Arduino firmware and wiring**
1. `firmware/MAVRIC_IO/`, a line protocol:
   - `S pin v` set, `P pin ms` pulse, `T pin` toggle
   - `M steps speed accel` move (stepper on STEP/DIR/EN), `E` stop
   - every command echoed as `<micros>,ACK,<id>`
2. The `arduino_io` driver with a clock fit for the echoes.
3. Update `docs/wiring.md` for:
   - Arduino → Intan DIN BNC (plus I/O GND)
   - LED via an NPN/MOSFET with a resistor
   - relay module
   - MEGA D48 → Intan DIN 2 for sync
   - TMCM-1141: 24 V supply, motor, USB, optional limit switches, optional I/O Arduino TTL into a module input
4. Bench test: LED pulse latency measured with the camera and the LED/SYNC tools.

**Phase B2: TMCM-1141 integration (on your motor)**
1. From your files: read the current TMCL program and the MATLAB code that triggers it. Identify the trigger
   (command, variable or input), module address and firmware version.
2. **Mode 1:** MAVRIC reproduces the MATLAB trigger. Bench test: an RFID or camera rule fires the stored program,
   and the latency and reply are logged.
3. **Mode 2:** draft a parametrised TMCL program (`firmware/tmcm1141/mavric_motion.tmc`) with command/status
   variables and soft limits. Keep a MATLAB helper (`mavric_motor_move.m`) that uses the same variables, so MATLAB
   workflows keep working. You review it before it goes on the module.
4. **Mode 3** (direct) as an alternative for sessions that need free control.
5. Safety checks on the bench: limits, stop on disarm/exit, refractory, current settings.

**Phase C: camera ROI activity**
1. A background-reference capture ("capture empty cage" button) and a per-ROI changed-fraction detector with
   hysteresis and minimum duration. It runs in the camera thread at ≤ 15 Hz.
2. Live overlay of ROI activity, and a `camera_roi` event source in rules.
3. Validation: compare camera-ROI events with RFID bouts and manual labels using the existing analysis, giving
   sensitivity, false triggers and latency.

**Phase D: options**
- **Intan RHX TCP:** record/stop by rule, and log the RHX file name in the session.
- `pytrinamic` as an alternative TMCL backend, if Trinamic's library is preferred.
- **Rules on the RFID MEGA itself**, uploaded from the PC: ~ms latency for RFID-only rules.
- **DeepLabCut-based ROI occupancy** as a camera source.

**What I need from you for the motor**
- the TMCL program currently stored on the TMCM-1141 (the `.tmc` / `.tmcl` file from TMCL-IDE)
- the MATLAB code that triggers it
- module address (default 1) and firmware version (TMCL-IDE shows both)
- the moves you want (steps, speeds, limits) and what the motor drives
- whether MATLAB must keep driving the motor during MAVRIC sessions

**Risks and mitigations**
- **Outputs firing during setup:** a global armed switch, which is off by default when the app starts.
- **Runaway motor or stimuli:** refractory periods, maximum activations per minute, and a stop/disable on app exit or
  crash (the firmware watchdog drops outputs if no heartbeat arrives for 2 s).
- **Two programs wanting the motor's COM port:** a single owner (MAVRIC or MATLAB) relays the other's requests, or
  RS-485 is used for the second client, coordinated through Mode 2's command variables.
- **Unpredictable latency of PC actions:** every action logs the request time, and the device echo where one exists.
  For anything that needs ms precision, use TTL into the target device (Intan) rather than software messages.
