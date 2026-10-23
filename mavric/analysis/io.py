"""Loading raw session tables with stable dtypes."""

from __future__ import annotations

import pandas as pd

from ..session import RFID_FILE, Session


def load_rfid_raw(sess: Session) -> pd.DataFrame:
    df = pd.read_csv(
        sess.file(RFID_FILE),
        dtype={"kind": str, "tag": str, "raw": str},
        keep_default_na=False,
        na_values={"micros": [""], "arduino_us": [""], "checksum_ok": [""]},
    )
    df["host_t"] = df["host_t"].astype(float)
    df["reader"] = pd.to_numeric(df["reader"], errors="coerce").fillna(0).astype(int)
    df["segment"] = df["segment"].astype(int)
    return df


def load_frames(sess: Session, cam_id: str | None = None) -> pd.DataFrame:
    cam = sess.camera(cam_id)
    df = pd.read_csv(sess.file(cam.get("frames_file", f"frames_{cam['id']}.csv")))
    for c in ("hw_ticks", "hw_counter", "line_status", "led_mean"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def video_path(sess: Session, cam_id: str | None = None):
    cam = sess.camera(cam_id)
    return sess.file(cam.get("video_file", f"{cam['id']}.mp4"))
