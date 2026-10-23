"""Compress raw recordings (or any videos) into shareable, DeepLabCut-ready H.264 MP4 files.

    mavric compress <session>            every camera -> <cam>_compressed.mp4, every clip -> clips/<clip>_compressed.mp4
    mavric compress <file or folder>     each video -> <name>_compressed.mp4 next to it
                                        (<x>_raw_000.mkv, _001, ... are joined into <x>_compressed.mp4)

The output keeps every frame, in order, at a constant frame rate (PTS = frame
index / fps), so frames_<cam>.csv still gives each frame's true time. Fixed
keyframe spacing keeps frame seeking exact for labeling tools and DeepLabCut's
frame extraction. Raw files are never modified or deleted.
"""

from __future__ import annotations

import re
import shlex
import time
from fractions import Fraction
from pathlib import Path

import av
import cv2
import numpy as np

DEFAULTS = {
    "codec": "libx264",
    "crf": 18,  # visually lossless for pose estimation; 23 = smaller, still fine for DeepLabCut
    "preset": "medium",
    "gop": 30,  # keyframe every 30 frames (1 s at 30 fps), no scene-cut keyframes
    "pix_fmt": "yuv420p",  # plays everywhere (OpenCV, BORIS, browsers)
    "scale": None,  # (width, height), or None
    "downscale": None,  # integer factor (2 = half width and height), instead of scale
    "crop": None,  # (x, y, width, height) before scaling, or None
}
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".m4v"}
SUFFIX = "_compressed"


def options(**kw) -> dict:
    o = dict(DEFAULTS)
    o.update({k: v for k, v in kw.items() if v is not None})
    return o


def _out_size(w: int, h: int, o: dict) -> tuple[int, int]:
    if o.get("crop"):
        w, h = int(o["crop"][2]), int(o["crop"][3])
    if o.get("scale"):
        w, h = int(o["scale"][0]), int(o["scale"][1])
    elif o.get("downscale") and int(o["downscale"]) > 1:
        w, h = w // int(o["downscale"]), h // int(o["downscale"])
    return w - w % 2, h - h % 2


def _transform(img: np.ndarray, o: dict, size: tuple[int, int]) -> np.ndarray:
    if o.get("crop"):
        x, y, w, h = (int(v) for v in o["crop"])
        img = img[y:y + h, x:x + w]
    if (o.get("scale") or o.get("downscale")) and (img.shape[1], img.shape[0]) != tuple(size):
        img = cv2.resize(img, tuple(size), interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(img[: size[1], : size[0]])


def compress_parts(parts: list[Path], out: Path, fps: float, o: dict | None = None, progress=None,
                   expected_frames: int | None = None) -> dict:
    """Decode `parts` in order and encode them as one H.264 file (written via <out>.part, then renamed)."""
    o = options(**(o or {}))
    out = Path(out)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    t_start = time.perf_counter()
    oc = av.open(str(tmp), mode="w", options={"movflags": "+faststart"})
    st = None
    n = 0
    gray = None
    try:
        for p in parts:
            with av.open(str(p)) as ic:
                for frame in ic.decode(video=0):
                    if st is None:
                        gray = frame.format.name in ("gray", "gray8", "gray16le", "gray10le", "gray12le")
                        size = _out_size(frame.width, frame.height, o)
                        st = oc.add_stream(o["codec"], rate=Fraction(fps).limit_denominator(1001))
                        st.width, st.height = size
                        st.pix_fmt = o["pix_fmt"]
                        g = int(o["gop"])
                        st.codec_context.gop_size = g
                        st.options = {"crf": str(o["crf"]), "preset": str(o["preset"])}
                        if o["codec"] in ("libx264", "libx265"):
                            key = "x264-params" if o["codec"] == "libx264" else "x265-params"
                            st.options[key] = f"keyint={g}:min-keyint={g}:scenecut=0"
                    img = frame.to_ndarray(format="gray" if gray else "bgr24")
                    img = _transform(img, o, (st.width, st.height))
                    vf = av.VideoFrame.from_ndarray(img, format="gray" if gray else "bgr24")
                    vf = vf.reformat(format=o["pix_fmt"])
                    vf.pts = n
                    for pkt in st.encode(vf):
                        oc.mux(pkt)
                    n += 1
                    if progress and n % 200 == 0:
                        progress(n)
        if st is not None:
            for pkt in st.encode():
                oc.mux(pkt)
    finally:
        oc.close()
    if expected_frames is not None and n != expected_frames:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{out.name}: decoded {n} frames, expected {expected_frames}; nothing written")
    tmp.replace(out)
    return {"file": out.name, "frames": n, "width": st.width if st else 0, "height": st.height if st else 0,
            "bytes": out.stat().st_size, "seconds": time.perf_counter() - t_start,
            "geometry_changed": bool(o.get("crop") or o.get("scale") or (o.get("downscale") or 1) > 1),
            "options": {k: o[k] for k in ("codec", "crf", "preset", "gop", "pix_fmt", "scale", "downscale", "crop")}}


def ffmpeg_command(parts: list[Path], out: Path, fps: float, o: dict | None = None) -> str:
    """The equivalent ffmpeg command line (for reference or for running ffmpeg by hand)."""
    o = options(**(o or {}))
    g = int(o["gop"])
    vf = []
    if o.get("crop"):
        x, y, w, h = o["crop"]
        vf.append(f"crop={w}:{h}:{x}:{y}")
    if o.get("scale"):
        vf.append(f"scale={o['scale'][0]}:{o['scale'][1]}:flags=area")
    elif o.get("downscale") and int(o["downscale"]) > 1:
        k = int(o["downscale"])
        vf.append(f"scale=trunc(iw/{k}/2)*2:trunc(ih/{k}/2)*2:flags=area")
    inp = ["-i", str(parts[0])] if len(parts) == 1 else \
        ["-f", "concat", "-safe", "0", "-i", "chunks.txt"]  # chunks.txt: one "file 'path'" line per raw chunk
    cmd = ["ffmpeg", *inp, "-map", "0:v:0", "-c:v", o["codec"], "-preset", str(o["preset"]), "-crf", str(o["crf"]),
           "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0", "-pix_fmt", o["pix_fmt"],
           "-r", f"{fps:g}", "-fps_mode", "passthrough", "-movflags", "+faststart"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    return shlex.join(cmd + [str(out)])


# --------------------------------------------------------------------------- sessions


def session_jobs(sess, cams: list[str] | None = None, clips: bool = True) -> list[dict]:
    """What a session compresses: one job per continuously recorded camera and per clip."""
    from .reader import video_parts

    jobs = []
    for cam in sess.meta.get("cameras", []):
        if cams and cam["id"] not in cams:
            continue
        if not (cam.get("video_files") or cam.get("record_format") == "raw" or cam.get("video_file")):
            continue
        try:
            parts = video_parts(sess, cam, prefer="raw")
        except FileNotFoundError:
            continue
        if len(parts) == 1 and parts[0][0].stem.endswith(SUFFIX):
            continue  # only a compressed file left
        n = sum(p[2] for p in parts) if all(p[2] is not None for p in parts) else None
        jobs.append({"kind": "camera", "id": cam["id"], "parts": [p[0] for p in parts], "frames": n,
                     "fps": float(cam.get("fps") or 30), "out": sess.file(f"{cam['id']}{SUFFIX}.mp4")})
    if clips:
        for i, c in enumerate(sess.meta.get("clips", [])):
            if cams and c.get("camera") not in cams:
                continue
            files = c.get("files") or ([{"file": c["file"]}] if c.get("file") else [])
            paths = [sess.file(f["file"]) for f in files if sess.file(f["file"]).exists()]
            if not paths:
                continue
            stem = re.sub(r"_raw_\d{3}$", "", paths[0].stem)
            cam = next((x for x in sess.meta.get("cameras", []) if x["id"] == c.get("camera")), {})
            jobs.append({"kind": "clip", "id": i, "parts": paths, "frames": c.get("frames"),
                         "fps": float(cam.get("fps") or 30), "out": paths[0].with_name(f"{stem}{SUFFIX}.mp4")})
    return jobs


def compress_session(sess, o: dict | None = None, cams: list[str] | None = None, clips: bool = True,
                     overwrite: bool = False, progress=None) -> list[dict]:
    """Compress a session's recordings; records the results in session.json."""
    results = []
    for job in session_jobs(sess, cams, clips):
        if job["out"].exists() and not overwrite:
            results.append({"file": job["out"].name, "skipped": "exists"})
            continue
        label = f"{job['kind']} {job['id']}"
        res = compress_parts(job["parts"], job["out"], job["fps"], o,
                             progress=(lambda k, lab=label: progress(lab, k, job["frames"])) if progress else None,
                             expected_frames=job["frames"])
        rel = job["out"].relative_to(sess.path).as_posix()
        info = {"file": rel, **{k: res[k] for k in ("frames", "width", "height", "bytes", "geometry_changed",
                                                    "options")}}
        if job["kind"] == "camera":
            sess.update_camera(job["id"], compressed_file=rel, compressed=info)
        else:
            with sess._lock:
                sess.meta["clips"][job["id"]].update(compressed_file=rel)
                sess.save_meta()
        results.append({**res, "file": rel})
    return results


def compress_path(path: str | Path, o: dict | None = None, overwrite: bool = False, progress=None) -> list[dict]:
    """A video file, or every video in a folder; raw chunk series are joined."""
    path = Path(path)
    files = [path] if path.is_file() else sorted(p for p in path.iterdir() if p.suffix.lower() in VIDEO_EXT)
    files = [p for p in files if not p.stem.endswith(SUFFIX) and ".part" not in p.suffixes]
    groups: dict[Path, list[Path]] = {}
    for p in files:
        m = re.match(r"(.*)_raw_\d{3}$", p.stem)
        out = p.with_name(f"{m.group(1) if m else p.stem}{SUFFIX}.mp4")
        groups.setdefault(out, []).append(p)
    results = []
    for out, parts in groups.items():
        if out.exists() and not overwrite:
            results.append({"file": out.name, "skipped": "exists"})
            continue
        with av.open(str(parts[0])) as c:
            st = c.streams.video[0]
            fps = float(st.average_rate or st.guessed_rate or 30)
        results.append(compress_parts(parts, out, fps, o,
                                      progress=(lambda k, n=out.name: progress(n, k, None)) if progress else None))
    return results
