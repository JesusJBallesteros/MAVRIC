# Third-party components

MAVRIC's own code is MIT-licensed (see [LICENSE](LICENSE)). It does not copy or ship third-party code, except the
RFID firmware noted below. Its dependencies are installed by the user (conda / pip), each under its own licence.

| Component | Used for | Licence |
|---|---|---|
| NumPy, pandas, PyArrow | data handling | BSD-3-Clause / Apache-2.0 |
| Matplotlib | analysis plots | Matplotlib licence (PSF-based, BSD-compatible) |
| PySide6 (Qt for Python) | GUI | LGPL-3.0 (used unmodified, as a separately installed library) |
| pyqtgraph | live plots | MIT |
| OpenCV (opencv-python-headless) | image I/O | Apache-2.0 |
| PyAV | video encoding/decoding | BSD-3-Clause |
| FFmpeg, inside the PyAV wheels | codecs | LGPL-2.1+. The wheels also contain **x264 / x265 (GPL-2.0+)**, used for H.264/H.265. |
| python-sounddevice + PortAudio | audio I/O | MIT |
| python-soundfile + libsndfile | FLAC/WAV files | BSD-3-Clause / LGPL-2.1 |
| pyserial | Arduino link | BSD-3-Clause |
| PyYAML | configuration | MIT |
| pytest | tests | MIT |
| Spinnaker SDK / PySpin (Teledyne FLIR) | FLIR cameras (optional) | proprietary, free download; not redistributed |
| pypylon (Basler) | Basler cameras (optional) | proprietary licence of Basler; not redistributed |
| Arduino core and SoftwareSerial | firmware | LGPL-2.1 |

## Firmware derived from MultiRFID

`firmware/MultiRFID_Reader_ID20LA_sync/` is a modified copy of the ID-20LA sketch of
[MultiRFID](https://github.com/JesusJBallesteros/MultiRFID) (J. J. Ballesteros, GPL-3.0-or-later). It keeps that
licence. `firmware/MAVRIC_IO/` is original MAVRIC code (MIT).

## Notes for redistribution

- **Source code:** MAVRIC is distributed as source, so the MIT licence applies to it as is.
- **Bundled packages:** whoever ships a bundled package (installer, frozen executable, Docker image) that includes
  the PyAV wheels distributes x264 (GPL-2.0+). In that case the GPL's terms for those binaries must be honoured.
- **Qt:** an LGPL-compliant bundle must also let users replace the Qt libraries.
