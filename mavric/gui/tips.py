"""Hover tips for every control, kept in one place.

`apply_tips(window, ...)` sets them after a window is built: buttons, check
boxes and group boxes by their text, form fields by their row label, other
widgets by attribute name.
"""

from __future__ import annotations

from PySide6 import QtWidgets

ACQUIRE = {
    # devices
    "↻": "Rescan the serial ports",
    "Connect RFID": "Open the RFID Arduino on the selected port (the board resets, about 2 s). Click again to "
                    "disconnect.",
    "Connect camera": "Open the camera chosen on the left and start its live view. Click again to disconnect.",
    "Connect audio": "Open the audio interface set in rig.yaml (audio:): level meters, sync pulse count, "
                     "spectrograms, recording, and the Soundboard when speakers are configured",
    "Video settings…": "Frame rate, exposure, gain, brightness, image size and recording format per camera; "
                       "compression defaults",
    # ROIs
    "Place ROIs (one per antenna) and sync LED": "Regions drawn on the first camera: one per antenna (place), plus a "
                                                 "box over the sync LED",
    "Draw / reset": "Place a new ROI for this antenna; drag its corners onto the place (Edit ROIs must be on)",
    "Draw / reset sync LED box": "Place the box over the sync LED; its brightness must jump with every flash",
    "Edit ROIs (drag corners; click an edge to add a corner)": "Unlock the ROIs for editing (on the ROI camera view)",
    "Save ROIs to rig": "Save the ROIs to config/rois.json; every new recording uses them",
    # live / recording / outputs
    "Live RFID": "Tags read right now, per place",
    "Recording": "Session name, notes and the record button",
    "● Start recording": "Start or stop a session: RFID, video, audio and clips go to sessions/<date>_<name>",
    "Outputs and camera clips (TTL, Intan, MATLAB, sounds, messages, clips)": "Things that happen when RFID detects "
                                                                            "a subject",
    "Outputs…": "Devices (I/O Arduino, Intan, MATLAB, programs, network, speakers), RFID rules, and camera clip "
                "rules",
    "Arm outputs": "Outputs fire only while armed; disarming releases every held output and stops every sound",
    "Audio inputs": "Peak level per input (dBFS) with a falling peak hold; CLIP marks overload. The sync input "
                    "counts the pulses it receives.",
    # soundboard
    "Soundboard": "Play stimuli by hand on any speaker; logged like rule actions (actions.csv, audio_playback.csv)",
    "↻ sounds": "Reload the sound library (audio.sounds_dir, default sounds/) after adding or changing files",
    "loop": "Repeat the sound until Stop all sounds (or disarming)",
    "▶ Play sound": "Play the chosen sound on the chosen speaker(s) now",
    "■ Stop all sounds": "Fade out every sound, from the soundboard and from rules (disarming does this too)",
}

ACQUIRE_ATTR = {
    "port_combo": "Serial port of the RFID Arduino MEGA (SIM = simulated cage, auto = first Arduino found)",
    "cam_combo": "Cameras from rig.yaml: id, backend, serial, recording mode. The first one carries the ROIs.",
    "view_combo": "Camera shown in the live view; 'All cameras' shows every connected camera side by side",
    "table": "Tag read at each place antenna and for how long",
    "events": "Arrivals, departures and output actions, newest first",
    "name_edit": "Session name, used in the folder name",
    "notes_edit": "Free notes stored with the session (subjects, conditions, …)",
    "video": "Live image; place ROIs take the subject's colour while their antenna reads its tag",
    "led_label": "Mean brightness inside the sync LED box: it must jump with each flash",
    "stat_label": "Stream status: RFID line rate, camera frame rates, recording, clips, audio",
    "sb_sound": "Sound from the library: WAV/FLAC files in sounds/ (mono, resampled to the audio rate); "
                "test_tone is built in",
    "sb_speaker": "Speaker(s) to play on (rig.yaml audio.speakers)",
    "sb_gain": "Gain applied to the file (0 dB = as recorded). The limiter clips above audio.max_level_dbfs.",
}

REVIEW = {
    "Mode": "Blind: label without seeing RFID. Curate: compare with RFID.",
    "Blind labeling (RFID hidden)": "Label place occupancy without seeing RFID, so the ground truth stays "
                                    "independent",
    "Curate (show RFID, find disagreements)": "Show RFID lanes; D jumps to the next disagreement, X/V mark RFID "
                                              "bouts spurious/valid",
    "Place (Up/Down)": "The place your keys label",
    "▶ Play": "Play / pause (Space)",
    "follow": "Keep the playhead in view while playing",
    "Save (Ctrl+S)": "Save labels, reviewed spans, curation and sound events (also every minute). Reviewing audio "
                     "only saves just the sound events.",
    "Analyze": "Sync check, RFID vs labels, plots (also: mavric analyze <session>)",
    "Render annotated video": "Video with ROIs, labels and RFID drawn in (analysis/annotated_<cam>.mp4)",
    "Reset reviewed spans": "Forget which time counts as reviewed (the grey band)",
    "Compress videos…": "Make <name>_compressed.mp4 copies (H.264) of the raw video, e.g. for DeepLabCut",
    # start dialog
    "Your initials": "Stored with every label and sound event you make",
    "Review": "What to review; parts this session does not have are greyed out",
    "Video: place labels and RFID curation": "Video with the places: blind labeling of who is on each place, RFID "
                                             "curation, annotated video",
    "Audio: sound events": "Waveform and spectrogram lanes, listening and sound event labels. Without video the "
                           "playhead runs on its own clock and RFID is shown as context.",
}

REVIEW_ATTR = {
    "roi_list": "Place selected for labeling (↑/↓)",
    "speed_box": "Playback speed ([ and ])",
    "plot": "Timeline: label lanes (and RFID lanes in curate mode); click to seek, wheel to zoom",
    "now_label": "State of every place at the playhead",
    "video": "Video frame at the playhead; ROIs show the labeled subject",
}

SOUND = {
    "Labeling sounds (playback counts as reviewed audio)": "While on, watched time is also recorded as reviewed "
                                                           "for sounds (sound_coverage.csv)",
    "Types…": "Edit the list of event types (config/sound_types.yaml)",
    "Start (S)": "Mark the start of a sound event at the playhead",
    "End (E)": "Mark the end: creates the event with the chosen type and emitter",
    "Delete": "Delete the selected sound event (Shift+Del)",
    "Use top suggestion (A)": "Set the emitter to the first suggestion (from mic levels and RFID place occupancy)",
    "Detect proposals": "Find candidate sound events automatically (band energy above the noise floor)",
    "Accept (Y)": "Turn the proposal at or after the playhead into an event",
    "Reject (R)": "Drop the proposal and go to the next one",
    "Play audio during playback": "Play the selected microphone(s) while the playhead runs (Space); pitch follows "
                                  "the speed. On by default without video.",
    "Listen (L)": "Play around the playhead, or the selected event",
    "Import…": "Read an Audacity label track or Raven selection table",
    "Export…": "Write the events as Audacity labels or a Raven selection table (analysis/)",
    "Type (F1–F9)": "Event type for new events, or of the selected event",
    "Emitter (Alt+1–9)": "Who made the sound: a tagged subject, the rig (system:<speaker>) or unknown",
}

SOUND_ATTR = {
    "type_box": "Event type for new events, or of the selected event (F1-F9)",
    "emitter_box": "Who made the sound: a tagged subject, the rig (system:<speaker>) or unknown (Alt+1-9, Alt+0)",
    "listen_box": "Microphone to listen to (both = stereo)",
    "sugg_label": "Ranked emitter suggestions and why",
    "info": "Events, proposals and reviewed time; the selected event",
}

OUTPUTS = {
    "Add…": "Add an item to this list",
    "Edit…": "Edit the selected item",
    "Remove": "Remove the selected item",
    "Test": "Check that the selected device answers (save first)",
    "Test (fire once)": "Run the selected rule once now, even when disarmed (save first)",
    "Save && restart outputs": "Write config/outputs.yaml and restart the outputs (they come back disarmed)",
    "Name": "A unique name",
    "Type": "Kind of device",
    "Antennas (RFID)": "Antennas (places) that trigger the rule (or any)",
    "Who": "Subjects that trigger the rule (or any)",
    "Event": "on enter: arrival · on leave: departure · while present: start on arrival, stop on departure",
    "Device": "Device that performs the action",
    "Action": "What the device does",
    "Timing": "Refractory: minimum time between firings · delay: wait before acting · max/min: rate limit",
    "Camera": "Camera that records the clip",
    "Seconds before the first read": "Pre-roll kept in memory, so the clip starts before the subject is detected",
    "Seconds after the subject leaves": "Post-roll; a new arrival during it extends the clip",
    "Split clips longer than (s)": "Long visits are split into several clips",
    "COM port": "Serial port of the I/O Arduino (SIM = built-in emulator)",
    "Baud rate": "Must match the firmware (115200 for MAVRIC_IO)",
    "Watchdog: all low if PC silent (s, 0 = off)": "Safety: the board switches every pin low if the PC stops "
                                                   "talking",
    "Wait after opening (board reset, s)": "Opening the port resets the Arduino; wait this long",
    "Host": "Address of the receiving computer (127.0.0.1 = this PC)",
    "Command port": "Intan RHX Remote TCP Control command port (5000 by default)",
    "Port": "Network port",
    "Shared session name ('' = start a new MATLAB)": "In MATLAB run matlab.engine.shareEngine('mavric') to connect "
                                                     "to that session",
    "Working folder": "Folder the program / MATLAB starts in",
    "Program (e.g. python.exe, a .bat)": "Program started once per action (not waited for)",
    "Pin": "Arduino digital pin (not 0 or 1)",
    "Pulse length (ms)": "How long the pin stays high",
    "Pin (high while the rule is active)": "High from arrival until departure",
    "Value (0/1)": "Level to set",
    "Command": "Any RHX remote command, e.g. set runmode record",
    "Text": "Message sent; placeholders like {subject} {roi} {t:.3f} are filled in",
    "Text at start": "Sent on arrival",
    "Text at stop": "Sent on departure",
    "MATLAB code": "Evaluated in the shared MATLAB session",
    "Function": "MATLAB function name",
    "Arguments": "Arguments; placeholders like {subject} {roi} {t} are filled in",
    "Script (.m file)": "MATLAB script to run",
    "Note": "Text written to the action log; placeholders like {subject} {roi} are filled in",
    "Sound (name in sounds/, e.g. coo_A; {subject} etc. allowed)": "Library sound (file name without extension); "
                                                                   "{subject}_call picks one per subject",
    "Speaker(s): names or outputs, comma-separated, or all": "Speakers from rig.yaml audio.speakers, e.g. spk2 or "
                                                             "spk1, spk3; all = every speaker",
    "Gain (dB, 0 = as in the file)": "Level change applied to the file; the limiter clips above "
                                     "audio.max_level_dbfs",
    "Loop until a stop action": "Repeat until a stop action, Stop all sounds or disarming",
}

VIDEO = {
    "Apply to camera (reconnects if needed)": "Use these settings now; size, frame rate and pixel format reconnect "
                                              "the camera",
    "Format": "Raw keeps every frame lossless (compress later); H.264 compresses while recording",
    "Raw codec": "FFV1: lossless, about 1/3 the size · uncompressed: largest, least CPU",
    "Raw file size": "A new raw file starts at about this size",
    "Compress a session…": "Compress a session's raw video (or any folder of videos) now",
    "Save": "Save to config/video.yaml (overrides rig.yaml)",
    "Codec": "H.264 (libx264) plays everywhere; H.265 is smaller but slower and less supported",
    "Quality (CRF)": "Lower = better and larger. 18 looks lossless; DeepLabCut works well with 18-23",
    "Preset": "Slower presets make smaller files at the same quality",
    "Keyframe every": "Short keyframe intervals keep frame seeking exact (labeling tools, DeepLabCut)",
    "Size": "Downscaling makes DeepLabCut faster; ROIs no longer match the smaller video",
    "Compress": "Start compressing in the background",
    "Show ffmpeg commands": "The equivalent ffmpeg command lines, to run by hand",
    "Overwrite existing _compressed files": "Redo files that already exist",
    "triggered clips": "Also compress the RFID-triggered clips",
}


def apply_tips(root: QtWidgets.QWidget, by_text: dict, by_attr: dict | None = None, owner=None) -> None:
    """Set tooltips on root's children (existing tooltips are kept)."""
    for w in root.findChildren(QtWidgets.QWidget):
        if w.toolTip():
            continue
        if isinstance(w, QtWidgets.QAbstractButton):
            key = w.text()
        elif isinstance(w, QtWidgets.QGroupBox):
            key = w.title()
        else:
            continue
        if key in by_text:
            w.setToolTip(by_text[key])
    for form in root.findChildren(QtWidgets.QFormLayout):
        for r in range(form.rowCount()):
            lab = form.itemAt(r, QtWidgets.QFormLayout.ItemRole.LabelRole)
            fld = form.itemAt(r, QtWidgets.QFormLayout.ItemRole.FieldRole)
            if lab is None or fld is None or lab.widget() is None:
                continue
            tip = by_text.get(lab.widget().text().replace(" ⚡", ""))
            if tip:
                lab.widget().setToolTip(lab.widget().toolTip() or tip)
                target = fld.widget()
                if target is not None and not target.toolTip():
                    target.setToolTip(tip)
                elif target is None and fld.layout() is not None:
                    for i in range(fld.layout().count()):
                        ww = fld.layout().itemAt(i).widget()
                        if ww is not None and not ww.toolTip():
                            ww.setToolTip(tip)
    for attr, text in (by_attr or {}).items():
        w = getattr(owner, attr, None)
        if isinstance(w, QtWidgets.QWidget) and not w.toolTip():
            w.setToolTip(text)


def missing_tips(root: QtWidgets.QWidget) -> list[str]:
    """Interactive controls without a tooltip (for tests)."""
    kinds = (QtWidgets.QAbstractButton, QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit,
             QtWidgets.QListWidget, QtWidgets.QTableWidget)
    out = []
    for w in root.findChildren(QtWidgets.QWidget):
        if not isinstance(w, kinds) or w.toolTip():
            continue
        p = w.parentWidget()
        if w.objectName().startswith("qt_"):
            continue  # Qt's own internals (e.g. the table corner button)
        if isinstance(p, (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox, QtWidgets.QTabBar)) or \
                type(w).__name__ in ("QToolButton",) or isinstance(w, QtWidgets.QDialogButtonBox):
            continue  # internal parts of composite widgets
        if isinstance(w, QtWidgets.QAbstractButton) and w.text() in ("OK", "Cancel", "Close", "&OK", "&Cancel",
                                                                       "&Close"):
            continue
        out.append(f"{type(w).__name__}({getattr(w, 'text', lambda: '')() or w.objectName()})")
    return out
