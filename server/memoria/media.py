"""Reading media files: hashing, EXIF, thumbnails, video metadata.

Performance-relevant choices:

- Identity hash is BLAKE2b (stdlib, faster than SHA-256). Images are hashed
  in full. Videos are hashed by (first 4 MB + last 4 MB + size): reading a
  2 GB file end-to-end off a hard drive takes ~20s and buys nothing —
  a video whose head, tail and exact size all match is the same video.
- Thumbnails are WebP: ~30% smaller than JPEG at the same quality, so the
  cache stays smaller and the grid loads faster. Two sizes: 400px for the
  grid, 1600px for the lightbox.
- Videos get a poster frame via ffmpeg at t=1s (t=0 is often black).
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps

from . import tools

pillow_heif.register_heif_opener()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

_CHUNK = 4 * 1024 * 1024  # 4 MB


def content_hash(path: Path, kind: str) -> str:
    h = hashlib.blake2b(digest_size=20)
    size = path.stat().st_size
    with open(path, "rb") as f:
        if kind == "video" and size > 3 * _CHUNK:
            h.update(f.read(_CHUNK))
            f.seek(-_CHUNK, 2)
            h.update(f.read(_CHUNK))
            h.update(str(size).encode())
        else:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """Identity hash of an image already read into memory. Identical to
    content_hash(path, "photo") — both are BLAKE2b over the whole file — so
    the indexer can read a photo ONCE (hash + decode from the same bytes)
    instead of reading it off disk twice. Videos keep the head+tail hash in
    content_hash: slurping a 2 GB file into RAM would be worse, not better."""
    h = hashlib.blake2b(digest_size=20)
    h.update(data)
    return h.hexdigest()


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None


# ---- EXIF --------------------------------------------------------------------


def _to_degrees(value) -> float:
    """EXIF GPS is (degrees, minutes, seconds) as rationals."""
    d, m, s = (float(Fraction(v)) for v in value)
    return d + m / 60 + s / 3600


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def read_image_meta(path: Path, *, data: bytes | None = None) -> dict:
    """date_taken, width, height, camera, lat, lng — from EXIF where possible.

    `data`, when given, is the file already in memory (see hash_bytes): the
    image is decoded from those bytes instead of re-read off disk.
    """
    meta: dict = {
        "date_taken": _mtime_iso(path),
        "width": 0,
        "height": 0,
        "camera": None,
        "lat": None,
        "lng": None,
    }
    with Image.open(io.BytesIO(data) if data is not None else path) as img:
        # Logical dimensions = raw pixel size, swapped when EXIF orientation
        # says the image is rotated a quarter turn (5/6/7/8). We deliberately
        # do NOT exif_transpose here just to read the size: that physically
        # rotates pixels and forces a FULL-resolution decode of every photo
        # (Image.open is otherwise lazy — img.size is free). Nearly every phone
        # photo carries an orientation tag, so that decode was pure waste, paid
        # again in make_image_thumbs. Reading the tag costs nothing.
        exif = img.getexif()
        w, h = img.size
        if exif.get(0x0112) in (5, 6, 7, 8):  # rotated 90°/270°
            w, h = h, w
        meta["width"], meta["height"] = w, h

        if exif:
            make = (exif.get(0x010F) or "").strip()
            model = (exif.get(0x0110) or "").strip()
            if model:
                # "Apple iPhone 13" -> "iPhone 13" (model usually repeats make)
                meta["camera"] = model if not make or model.startswith(make) else f"{make} {model}"

            exif_ifd = exif.get_ifd(0x8769)
            raw_date = exif_ifd.get(0x9003) or exif.get(0x0132)  # DateTimeOriginal, else DateTime
            if raw_date:
                try:
                    meta["date_taken"] = (
                        datetime.strptime(str(raw_date), "%Y:%m:%d %H:%M:%S").isoformat()
                    )
                except ValueError:
                    pass

            gps = exif.get_ifd(0x8825)
            if gps and 2 in gps and 4 in gps:
                try:
                    lat = _to_degrees(gps[2])
                    lng = _to_degrees(gps[4])
                    if gps.get(1) == "S":
                        lat = -lat
                    if gps.get(3) == "W":
                        lng = -lng
                    if lat or lng:
                        meta["lat"], meta["lng"] = round(lat, 6), round(lng, 6)
                except (ValueError, ZeroDivisionError, TypeError):
                    pass
    return meta


# ---- Video metadata (ffprobe) --------------------------------------------------


def read_video_meta(path: Path) -> dict:
    meta: dict = {
        "date_taken": _mtime_iso(path),
        "width": 0,
        "height": 0,
        "camera": None,
        "lat": None,
        "lng": None,
        "duration": None,
    }
    out = subprocess.run(
        [
            tools.path_or_name("ffprobe"), "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,side_data_list:stream_tags=rotate",
            "-show_entries", "format=duration:format_tags",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return meta
    info = json.loads(out.stdout or "{}")

    streams = info.get("streams") or [{}]
    stream = streams[0]
    meta["width"] = stream.get("width") or 0
    meta["height"] = stream.get("height") or 0
    # Rotation means the display dimensions are swapped (portrait phone video)
    rotate = str((stream.get("tags") or {}).get("rotate", "0"))
    side = stream.get("side_data_list") or []
    side_rot = next((abs(int(float(s.get("rotation", 0)))) for s in side if "rotation" in s), 0)
    if rotate in ("90", "270") or side_rot in (90, 270):
        meta["width"], meta["height"] = meta["height"], meta["width"]

    fmt = info.get("format") or {}
    if fmt.get("duration"):
        try:
            meta["duration"] = round(float(fmt["duration"]), 2)
        except ValueError:
            pass

    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    raw_date = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    if raw_date:
        try:
            meta["date_taken"] = datetime.fromisoformat(
                raw_date.replace("Z", "+00:00")
            ).isoformat()
        except ValueError:
            pass
    model = tags.get("com.apple.quicktime.model")
    if model:
        meta["camera"] = model
    loc = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    if loc:
        # ISO 6709: "+17.0163+054.0924+021.000/" -> lat, lng
        try:
            body = loc.rstrip("/")
            parts, cur = [], ""
            for ch in body:
                if ch in "+-" and cur:
                    parts.append(cur)
                    cur = ch
                else:
                    cur += ch
            parts.append(cur)
            if len(parts) >= 2:
                meta["lat"], meta["lng"] = round(float(parts[0]), 6), round(float(parts[1]), 6)
        except ValueError:
            pass
    return meta


# ---- Thumbnails ----------------------------------------------------------------


def thumb_paths(thumbs_dir: Path, photo_hash: str) -> tuple[Path, Path]:
    """Sharded by the first 2 hash chars: 50k files in one folder makes NTFS sad."""
    shard = thumbs_dir / photo_hash[:2]
    return shard / f"{photo_hash}_t.webp", shard / f"{photo_hash}_p.webp"


def make_image_thumbs(
    src: Path, thumbs_dir: Path, photo_hash: str, *, data: bytes | None = None
) -> None:
    """`data`, when given, is the file already in memory — decoded from those
    bytes instead of re-read off disk (the indexer hashes and decodes the same
    read)."""
    thumb, preview = thumb_paths(thumbs_dir, photo_hash)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data) if data is not None else src) as img:
        # draft() lets the JPEG decoder emit a downscaled image directly (1/2,
        # 1/4, 1/8 via the DCT), so a 48 MP source is decoded at a fraction of
        # full resolution. It MUST come before any pixel access, so before
        # exif_transpose — the old order (transpose first) forced a full decode
        # and defeated it. No-op for non-JPEG formats. LANCZOS from the drafted
        # size still yields a crisp thumbnail.
        img.draft("RGB", (1600, 1600))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        pre = img.copy()
        pre.thumbnail((1600, 1600), Image.LANCZOS)
        pre.save(preview, "WEBP", quality=85)
        pre.thumbnail((400, 400), Image.LANCZOS)
        pre.save(thumb, "WEBP", quality=80)


def make_video_thumbs(src: Path, thumbs_dir: Path, photo_hash: str, duration: float | None) -> None:
    thumb, preview = thumb_paths(thumbs_dir, photo_hash)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    seek = 1.0 if (duration or 0) > 2 else 0.0
    tmp = thumb.parent / f"{photo_hash}_poster.jpg"
    result = subprocess.run(
        [tools.path_or_name("ffmpeg"), "-y", "-v", "error", "-ss", str(seek), "-i", str(src),
         "-frames:v", "1", "-q:v", "3", str(tmp)],
        capture_output=True, timeout=120,
    )
    if result.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"ffmpeg poster failed: {result.stderr.decode(errors='replace')[:200]}")
    try:
        with Image.open(tmp) as img:
            pre = img.copy()
            pre.thumbnail((1600, 1600), Image.LANCZOS)
            pre.save(preview, "WEBP", quality=85)
            pre.thumbnail((400, 400), Image.LANCZOS)
            pre.save(thumb, "WEBP", quality=80)
    finally:
        tmp.unlink(missing_ok=True)
