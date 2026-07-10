"""Write metadata back into the *original* files (opt-in, v1.3).

Memoria's default rule is "originals are never modified" — every edit lives in
the catalogue only. This module implements the deliberate, user-armed exception:
the "also edit the original file" toggle in the metadata editor.

Two hard facts shaped the design:

1. **Only ExifTool can edit HEIC *and* MOV losslessly.** Pillow would have to
   re-encode (degrading the image); piexif is JPEG/TIFF only. So we shell out to
   an external `exiftool` binary. It's optional: if it isn't installed the edit
   still updates the catalogue and we report `toolAvailable = False`.

2. **A photo's identity IS its content hash.** Writing new bytes into a file
   changes that hash, so a naive write would make the next rescan treat the file
   as a brand-new photo and orphan the old one (losing its faces, album
   membership, favourite, thumbnails). To prevent that we *migrate the identity
   in the same operation*: re-hash the edited file and carry every catalogue row
   that referenced the old hash across to the new one (`remap_photo_hash`).

The write itself uses `-overwrite_original` (no `_original` backup clutter — the
user chose "overwrite in place"; pixels are never touched, only metadata atoms).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import config, db, media, tools


def find_tool() -> str | None:
    """Locate the ExifTool binary (setup.ps1 puts it in server/tools/; a dev's
    own PATH install also works). None means the feature degrades gracefully."""
    return tools.find("exiftool")


def _fmt_dt(iso: str) -> str:
    """Memoria stores ISO 8601 ("2026-07-09T14:30:00"); EXIF wants
    "2026:07:09 14:30:00". Be lenient about a missing time."""
    date, _, time = iso.partition("T")
    time = (time or "12:00:00")[:8]
    if len(time) == 5:  # "HH:MM" -> "HH:MM:00"
        time += ":00"
    return f"{date.replace('-', ':')} {time}"


def _args_for(kind: str, dt: str | None, lat: float | None, lng: float | None) -> list[str]:
    """The tag assignments for one file. ExifTool silently skips tags that don't
    apply to a container (with `-m`), so we can be generous."""
    args: list[str] = []
    if dt:
        stamp = _fmt_dt(dt)
        if kind == "video":
            # QuickTime keeps its own date atoms; -AllDates doesn't reach them.
            args += [f"-QuickTime:CreateDate={stamp}", f"-QuickTime:ModifyDate={stamp}"]
        else:
            # -AllDates = DateTimeOriginal + CreateDate + ModifyDate in one go.
            args.append(f"-AllDates={stamp}")
    if lat is not None and lng is not None:
        if kind == "video":
            args.append(f"-QuickTime:GPSCoordinates={lat} {lng}")
        else:
            # ExifTool derives the N/S and E/W ref tags from the sign of the
            # value you feed the *Ref tags — the documented signed-GPS idiom.
            args += [
                f"-GPSLatitude={lat}", f"-GPSLatitudeRef={lat}",
                f"-GPSLongitude={lng}", f"-GPSLongitudeRef={lng}",
            ]
    return args


def _write_file(tool: str, path: Path, kind: str, dt: str | None,
                lat: float | None, lng: float | None) -> None:
    tags = _args_for(kind, dt, lat, lng)
    if not tags:
        return
    cmd = [tool, "-overwrite_original", "-m", "-q", *tags, str(path)]
    # CREATE_NO_WINDOW: don't flash a console when the Tauri-spawned backend
    # shells out (the flag simply doesn't exist off Windows).
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=flags)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "unknown error").strip().splitlines()
        raise RuntimeError(err[-1] if err else "exiftool failed")


def remap_photo_hash(old: str, new: str, *, thumbs_dir: Path | None = None,
                     file_sizes: dict[int, tuple[int, float]] | None = None) -> None:
    """Carry a photo's whole catalogue identity from `old` hash to `new`.

    Every table that keys off a photo hash is repointed. FK checks are deferred
    to commit so the order of the UPDATEs doesn't matter. `file_sizes` maps a
    `files.id` to its post-edit (size, mtime) so a later scan sees no change and
    skips re-hashing. Thumbnails (pixels unchanged) are renamed, not rebuilt."""
    if old == new:
        return
    with db.tx() as c:
        c.execute("PRAGMA defer_foreign_keys=ON")
        c.execute("UPDATE photos SET hash = ? WHERE hash = ?", (new, old))
        c.execute("UPDATE photos SET live_of = ? WHERE live_of = ?", (new, old))
        c.execute("UPDATE files SET content_hash = ? WHERE content_hash = ?", (new, old))
        c.execute("UPDATE faces SET photo_hash = ? WHERE photo_hash = ?", (new, old))
        c.execute("UPDATE album_photos SET photo_hash = ? WHERE photo_hash = ?", (new, old))
        c.execute("UPDATE clip_embeddings SET photo_hash = ? WHERE photo_hash = ?", (new, old))
        c.execute("UPDATE trip_overrides SET cover_hash = ? WHERE cover_hash = ?", (new, old))
        for fid, (size, mtime) in (file_sizes or {}).items():
            c.execute("UPDATE files SET size = ?, mtime = ? WHERE id = ?", (size, mtime, fid))

    thumbs = thumbs_dir or config.thumbs_dir()
    old_t, old_p = media.thumb_paths(thumbs, old)
    new_t, new_p = media.thumb_paths(thumbs, new)
    new_t.parent.mkdir(parents=True, exist_ok=True)
    for src, dst in ((old_t, new_t), (old_p, new_p)):
        try:
            if src.exists():
                os.replace(str(src), str(dst))
        except OSError:
            pass  # a missing/locked thumb just gets regenerated on next view


def write_originals(photo_hashes: list[str], dt: str | None, lat: float | None,
                    lng: float | None, place: str | None) -> dict:
    """Write the given metadata into the originals backing each photo, then
    migrate identities. Also cascades to the MOV half of a Live Photo (those
    videos aren't independently selectable in the UI). Returns a report the
    frontend surfaces to the user."""
    result = {"toolAvailable": False, "filesWritten": 0, "filesFailed": 0, "errors": []}
    tool = find_tool()
    if not tool:
        return result
    result["toolAvailable"] = True

    thumbs = config.thumbs_dir()
    conn = db.get_conn()

    # Expand the selection: each still drags along its Live-Photo video(s).
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()
    for h in photo_hashes:
        row = conn.execute("SELECT hash, kind FROM photos WHERE hash = ?", (h,)).fetchone()
        if row is None or h in seen:
            continue
        targets.append((h, row["kind"]))
        seen.add(h)
        for v in conn.execute("SELECT hash FROM photos WHERE live_of = ?", (h,)).fetchall():
            if v["hash"] not in seen:
                targets.append((v["hash"], "video"))
                seen.add(v["hash"])

    selected = set(photo_hashes)
    for h, kind in targets:
        # A cascaded Live-Photo video wasn't touched by the catalogue UPDATE in
        # api.edit_exif; keep its catalogue row in step with its file here.
        if h not in selected:
            _sync_video_catalogue(conn, h, dt, lat, lng, place)

        files = conn.execute(
            "SELECT id, path FROM files WHERE content_hash = ? AND status = 'ok'", (h,)
        ).fetchall()
        new_by_id: dict[int, tuple[str, int, float]] = {}
        for f in files:
            p = Path(f["path"])
            if not p.exists():
                result["filesFailed"] += 1
                result["errors"].append(f"{p.name}: not found on disk")
                continue
            try:
                _write_file(tool, p, kind, dt, lat, lng)
                st = p.stat()
                hash_kind = "video" if kind == "video" else "photo"
                new_by_id[f["id"]] = (media.content_hash(p, hash_kind), st.st_size, st.st_mtime)
                result["filesWritten"] += 1
            except Exception as e:  # noqa: BLE001 - report, don't abort the batch
                result["filesFailed"] += 1
                result["errors"].append(f"{p.name}: {e}")

        if not new_by_id:
            continue
        # The photo adopts the first edited file's new hash. In the normal
        # single-file case that's the only hash; if two copies diverged, the
        # stragglers are repointed too and reconciled on the next scan.
        target_hash = next(iter(new_by_id.values()))[0]
        sizes = {fid: (sz, mt) for fid, (_nh, sz, mt) in new_by_id.items()}
        if target_hash != h:
            remap_photo_hash(h, target_hash, thumbs_dir=thumbs, file_sizes=sizes)
        else:
            with db.tx() as c:  # bytes unchanged; still refresh size/mtime
                for fid, (sz, mt) in sizes.items():
                    c.execute("UPDATE files SET size = ?, mtime = ? WHERE id = ?", (sz, mt, fid))

    return result


def _sync_video_catalogue(conn, h: str, dt: str | None, lat: float | None,
                          lng: float | None, place: str | None) -> None:
    sets, params = [], []
    if dt:
        sets.append("date_taken = ?"); params.append(dt)
    if lat is not None and lng is not None:
        sets += ["lat = ?", "lng = ?", "place = ?"]; params += [lat, lng, place]
    if not sets:
        return
    with db.tx() as c:
        c.execute(f"UPDATE photos SET {', '.join(sets)} WHERE hash = ?", (*params, h))
