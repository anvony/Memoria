"""All HTTP endpoints. The response shapes are the types.ts contract the
frontend has been built against since Phase 1 — swap-in, not rewrite.

URL identity note: photo ids in URLs are content hashes, so thumbnail URLs
are immutable — the browser can cache them forever (Cache-Control below).
"""

from __future__ import annotations

import io
import math
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from send2trash import send2trash

from . import (
    backup, config, db, duplicates, exif_writer, geocode, indexer, media, privacy,
    scanner, trips,
)
from .models import (
    Album, DuplicateFile, DuplicateGroup, IndexStatus, MlStatus, Person, Photo,
    PhotoFace, Place, SetupState, SourceFolder, StorageInfo, Trip,
)

router = APIRouter(prefix="/api")

CACHE_FOREVER = {"Cache-Control": "public, max-age=31536000, immutable"}


def _configured() -> bool:
    return config.get_data_dir() is not None


def _require_setup() -> None:
    if not _configured():
        raise HTTPException(503, "Memoria has not been set up yet")


# ---- Health & first-run setup ------------------------------------------------


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "configured": _configured(),
        "mlAvailable": indexer.ml_available(),
    }


@router.get("/setup")
def get_setup() -> SetupState:
    data_dir = config.get_data_dir()
    return SetupState(
        configured=data_dir is not None,
        data_dir=str(data_dir) if data_dir else None,
        default_data_dir=str(config.DEFAULT_DATA_DIR),
    )


class SetupBody(BaseModel):
    dataDir: str


@router.post("/setup")
def post_setup(body: SetupBody) -> SetupState:
    config.set_data_dir(body.dataDir)
    db.init_db()
    return get_setup()


# ---- Photos --------------------------------------------------------------------


def _photo_rows_to_models(rows) -> list[Photo]:
    conn = db.get_conn()
    face_map: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT photo_hash, person_id FROM faces WHERE person_id IS NOT NULL"
    ):
        face_map.setdefault(r["photo_hash"], []).append(f"person-{r['person_id']}")

    size_map: dict[str, int] = {}
    for r in conn.execute(
        "SELECT content_hash, MAX(size) AS s FROM files "
        "WHERE content_hash IS NOT NULL AND status = 'ok' GROUP BY content_hash"
    ):
        size_map[r["content_hash"]] = r["s"]

    out = []
    for r in rows:
        h = r["hash"]
        out.append(Photo(
            id=h,
            filename=r["filename"],
            thumb_url=f"/api/photos/{h}/thumb",
            preview_url=f"/api/photos/{h}/preview",
            date_taken=r["date_taken"],
            width=r["width"] or 400,
            height=r["height"] or 300,
            is_favorite=bool(r["favorite"]),
            face_count=len(face_map.get(h, [])),
            place=r["place"],
            lat=r["lat"],
            lng=r["lng"],
            camera=r["camera"],
            person_ids=face_map.get(h, []),
            kind=r["kind"],
            duration=r["duration"],
            size_bytes=size_map.get(h),
        ))
    return out


# hidden = 0 AND private = 0 in the base query means hidden photos and the
# Private space are invisible to EVERY normal surface (timeline, people,
# trips, places, albums, search) without each endpoint remembering to filter.
VISIBLE = "hidden = 0 AND private = 0"

_PHOTOS_SQL = (
    f"SELECT * FROM photos WHERE live_of IS NULL AND {VISIBLE} "
    "{where} ORDER BY date_taken DESC"
)


def _list_photos(where: str = "", params: tuple = ()) -> list[Photo]:
    rows = db.get_conn().execute(_PHOTOS_SQL.format(where=where), params).fetchall()
    return _photo_rows_to_models(rows)


@router.get("/photos")
def list_photos() -> list[Photo]:
    _require_setup()
    return _list_photos()


class FavoriteBody(BaseModel):
    value: bool


@router.post("/photos/{photo_id}/favorite")
def set_favorite(photo_id: str, body: FavoriteBody) -> dict:
    _require_setup()
    with db.tx() as c:
        c.execute("UPDATE photos SET favorite = ? WHERE hash = ?", (int(body.value), photo_id))
    return {"ok": True}


def _check_media_access(photo_id: str, pt: str | None) -> None:
    """Media for photos in the Private space requires the unlock token
    (?pt= because <img>/<video> tags can't send headers). 404 — not 403 —
    so a locked space doesn't even confirm the photo exists."""
    row = db.get_conn().execute(
        "SELECT private FROM photos WHERE hash = ?", (photo_id,)
    ).fetchone()
    if row is not None and row["private"] and not privacy.valid(pt):
        raise HTTPException(404)


@router.get("/photos/{photo_id}/thumb")
def photo_thumb(photo_id: str, pt: str | None = None) -> FileResponse:
    _require_setup()
    _check_media_access(photo_id, pt)
    thumb, _ = media.thumb_paths(config.thumbs_dir(), photo_id)
    if not thumb.exists():
        raise HTTPException(404)
    return FileResponse(thumb, media_type="image/webp", headers=CACHE_FOREVER)


@router.get("/photos/{photo_id}/preview")
def photo_preview(photo_id: str, pt: str | None = None) -> FileResponse:
    _require_setup()
    _check_media_access(photo_id, pt)
    _, preview = media.thumb_paths(config.thumbs_dir(), photo_id)
    if not preview.exists():
        raise HTTPException(404)
    return FileResponse(preview, media_type="image/webp", headers=CACHE_FOREVER)


@router.get("/photos/{photo_id}/original")
def photo_original(photo_id: str, pt: str | None = None):
    """The actual file — used for video playback (FileResponse handles the
    Range requests <video> needs for seeking). 404s while the drive is offline."""
    _require_setup()
    _check_media_access(photo_id, pt)
    row = db.get_conn().execute(
        "SELECT path FROM files WHERE content_hash = ? AND status = 'ok' LIMIT 1",
        (photo_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "file offline or missing")
    return FileResponse(row["path"])


@router.get("/photos/{photo_id}/location")
def photo_location(photo_id: str) -> dict:
    """Fuller location for the info panel: the detailed reverse-geocoded label
    plus raw coordinates, so the viewer can show 'City, District, Region, CC'
    (or fall back to coordinates) instead of only the short place name."""
    _require_setup()
    row = db.get_conn().execute(
        "SELECT lat, lng, place FROM photos WHERE hash = ?", (photo_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    detail = None
    if row["lat"] is not None and row["lng"] is not None:
        detail = geocode.place_detail(row["lat"], row["lng"])
    return {"place": row["place"], "detail": detail, "lat": row["lat"], "lng": row["lng"]}


@router.get("/photos/{photo_id}/live")
def photo_live_video(photo_id: str, pt: str | None = None):
    """The Live Photo companion MOV for a still, if one exists."""
    _require_setup()
    _check_media_access(photo_id, pt)
    row = db.get_conn().execute(
        "SELECT f.path FROM photos v JOIN files f ON f.content_hash = v.hash "
        "WHERE v.live_of = ? AND f.status = 'ok' LIMIT 1",
        (photo_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404)
    return FileResponse(row["path"])


# ---- Delete / Hide -----------------------------------------------------------


class PhotoIdsBody(BaseModel):
    photoIds: list[str]


def _purge_thumbs(photo_hash: str) -> None:
    thumb, preview = media.thumb_paths(config.thumbs_dir(), photo_hash)
    thumb.unlink(missing_ok=True)
    preview.unlink(missing_ok=True)


def _delete_catalogue_rows(c, photo_hash: str) -> None:
    c.execute("DELETE FROM faces WHERE photo_hash = ?", (photo_hash,))
    c.execute("DELETE FROM clip_embeddings WHERE photo_hash = ?", (photo_hash,))
    c.execute("DELETE FROM album_photos WHERE photo_hash = ?", (photo_hash,))
    c.execute("DELETE FROM files WHERE content_hash = ?", (photo_hash,))
    c.execute("DELETE FROM photos WHERE hash = ?", (photo_hash,))


@router.post("/photos/delete")
def delete_photos(body: PhotoIdsBody) -> dict:
    """Recycle every file of each photo (and its Live Photo clip), then drop
    the catalogue rows and cached thumbnails. send2trash = Windows Recycle
    Bin, never a permanent delete (scope §5). Files on an offline drive can't
    be recycled — they leave the catalogue now and, if that folder is still
    indexed when the drive returns, will be re-discovered."""
    _require_setup()
    conn = db.get_conn()
    recycled = 0
    for h in body.photoIds:
        targets = [h] + [
            r["hash"]
            for r in conn.execute("SELECT hash FROM photos WHERE live_of = ?", (h,))
        ]
        for th in targets:
            for f in conn.execute(
                "SELECT path FROM files WHERE content_hash = ?", (th,)
            ).fetchall():
                p = Path(f["path"])
                if p.exists():
                    send2trash(str(p))
                    recycled += 1
            with db.tx() as c:
                _delete_catalogue_rows(c, th)
            _purge_thumbs(th)
    return {"ok": True, "recycled": recycled}


@router.post("/photos/hide")
def hide_photos(body: PhotoIdsBody) -> dict:
    """Hide from Memoria: the photo disappears from every view and its cached
    thumbnails are removed, but the original file is untouched."""
    _require_setup()
    with db.tx() as c:
        for h in body.photoIds:
            c.execute("UPDATE photos SET hidden = 1 WHERE hash = ?", (h,))
    for h in body.photoIds:
        _purge_thumbs(h)
    return {"ok": True}


@router.get("/hidden")
def hidden_count() -> dict:
    _require_setup()
    n = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM photos WHERE hidden = 1"
    ).fetchone()["n"]
    return {"count": n}


@router.post("/hidden/unhide")
def unhide_all() -> dict:
    """Bring every hidden photo back, regenerating the purged thumbnails from
    the originals. Offline-drive photos unhide too, but their thumbnails wait
    for the next index run after the drive returns."""
    _require_setup()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT p.hash, p.kind, p.duration, "
        "  (SELECT path FROM files WHERE content_hash = p.hash AND status = 'ok' LIMIT 1) AS path "
        "FROM photos p WHERE p.hidden = 1"
    ).fetchall()
    for r in rows:
        if not r["path"]:
            continue
        src = Path(r["path"])
        try:
            if r["kind"] == "photo":
                media.make_image_thumbs(src, config.thumbs_dir(), r["hash"])
            else:
                media.make_video_thumbs(src, config.thumbs_dir(), r["hash"], r["duration"])
        except Exception as exc:
            print(f"[unhide] thumbnail regen failed for {src}: {exc}")
    with db.tx() as c:
        c.execute("UPDATE photos SET hidden = 0 WHERE hidden = 1")
    return {"ok": True, "count": len(rows)}


# ---- Private space -------------------------------------------------------------


class PasswordBody(BaseModel):
    password: str
    currentPassword: str | None = None


@router.get("/privacy/status")
def privacy_status(x_privacy_token: str | None = Header(None)) -> dict:
    _require_setup()
    n = db.get_conn().execute(
        "SELECT COUNT(*) AS n FROM photos WHERE private = 1 AND live_of IS NULL"
    ).fetchone()["n"]
    return {
        "configured": privacy.configured(),
        "unlocked": privacy.valid(x_privacy_token),
        "count": n,
    }


@router.post("/privacy/password")
def privacy_set_password(body: PasswordBody) -> dict:
    _require_setup()
    if len(body.password) < 4:
        raise HTTPException(400, "password must be at least 4 characters")
    if privacy.configured() and not privacy.verify(body.currentPassword or ""):
        raise HTTPException(403, "current password is wrong")
    privacy.set_password(body.password)
    return {"ok": True}


@router.post("/privacy/unlock")
def privacy_unlock(body: PasswordBody) -> dict:
    _require_setup()
    token = privacy.unlock(body.password)
    if token is None:
        raise HTTPException(403, "wrong password")
    return {"token": token}


@router.post("/privacy/lock")
def privacy_lock(x_privacy_token: str | None = Header(None)) -> dict:
    privacy.lock(x_privacy_token)
    return {"ok": True}


def _require_privacy(token: str | None) -> None:
    if not privacy.valid(token):
        raise HTTPException(401, "private space is locked")


@router.get("/privacy/photos")
def privacy_photos(x_privacy_token: str | None = Header(None)) -> list[Photo]:
    _require_setup()
    _require_privacy(x_privacy_token)
    rows = db.get_conn().execute(
        "SELECT * FROM photos WHERE live_of IS NULL AND private = 1 "
        "ORDER BY date_taken DESC"
    ).fetchall()
    photos = _photo_rows_to_models(rows)
    # media endpoints demand the token for private photos; bake it into the URLs
    for p in photos:
        p.thumb_url += f"?pt={x_privacy_token}"
        p.preview_url += f"?pt={x_privacy_token}"
    return photos


@router.post("/privacy/add")
def privacy_add(body: PhotoIdsBody) -> dict:
    """Move photos into the Private space. Deliberately does NOT require the
    space to be unlocked: adding is a one-way push that only ever hides things,
    so gating it behind the password just meant unlocking (and thus temporarily
    exposing) the space to hide a new photo. Accessing the Private tab still
    needs the password — that's the asymmetry the user asked for.

    Private is exclusive: a private photo must not live in any album or trip, so
    its album membership is dropped here (trips are auto-derived and already skip
    private photos, so they fall out on their own)."""
    _require_setup()
    if not privacy.configured():
        raise HTTPException(400, "set a Private-space password first")
    with db.tx() as c:
        for h in body.photoIds:
            c.execute("UPDATE photos SET private = 1 WHERE hash = ?", (h,))
            c.execute("DELETE FROM album_photos WHERE photo_hash = ?", (h,))
    return {"ok": True}


@router.post("/privacy/remove")
def privacy_remove(body: PhotoIdsBody, x_privacy_token: str | None = Header(None)) -> dict:
    _require_setup()
    _require_privacy(x_privacy_token)
    with db.tx() as c:
        for h in body.photoIds:
            c.execute("UPDATE photos SET private = 0 WHERE hash = ?", (h,))
    return {"ok": True}


# ---- Batch EXIF edit -----------------------------------------------------------


class ExifBody(BaseModel):
    photoIds: list[str]
    dateTaken: str | None = None
    lat: float | None = None
    lng: float | None = None
    place: str | None = None
    # Opt-in: also write the changes into the original files (needs ExifTool).
    # Off by default — Memoria's rule is catalogue-only edits (scope §5).
    writeOriginal: bool = False


@router.post("/photos/exif")
def edit_exif(body: ExifBody) -> dict:
    """Batch metadata edit — timestamp and/or location. By default writes to
    Memoria's catalogue only; the originals are never touched (scope §5). When
    lat/lng are given without a place name, the offline geocoder supplies one.
    With `writeOriginal`, the same fields are also written into the original
    HEIC/MOV via ExifTool and each photo's catalogue identity is migrated to the
    file's new content hash (see exif_writer)."""
    _require_setup()
    sets: list[str] = []
    params: list = []
    place_final: str | None = None
    if body.dateTaken:
        sets.append("date_taken = ?")
        params.append(body.dateTaken)
    if body.lat is not None and body.lng is not None:
        place_final = body.place or geocode.place_name(body.lat, body.lng)
        sets.extend(["lat = ?", "lng = ?", "place = ?"])
        params.extend([body.lat, body.lng, place_final])
    elif body.place:
        place_final = body.place.strip()
        sets.append("place = ?")
        params.append(place_final)
    if not sets:
        raise HTTPException(400, "nothing to change")
    with db.tx() as c:
        for h in body.photoIds:
            c.execute(f"UPDATE photos SET {', '.join(sets)} WHERE hash = ?", (*params, h))

    out: dict = {"ok": True, "updated": len(body.photoIds)}
    wants_file_write = bool(body.dateTaken) or (body.lat is not None and body.lng is not None)
    if body.writeOriginal and wants_file_write:
        out["writeOriginal"] = exif_writer.write_originals(
            body.photoIds, body.dateTaken, body.lat, body.lng, place_final
        )
    return out


# ---- Catalogue backup ----------------------------------------------------------


@router.get("/backup")
def backup_status() -> dict:
    _require_setup()
    return backup.status()


@router.post("/backup")
def run_backup() -> dict:
    """Copy the catalogue with SQLite's online-backup API — safe even while
    the indexer is writing (a plain file copy of a live WAL database is not)."""
    _require_setup()
    backup.make_backup()
    return backup.status()


@router.post("/backup/quit")
def backup_on_quit() -> dict:
    """Called by the app as it closes (beforeunload / native quit). Autosaves
    only if the catalogue changed since the last backup, so a plain browse-and-
    close writes nothing."""
    _require_setup()
    if backup.is_dirty():
        backup.make_backup(auto=True)
        return {"saved": True}
    return {"saved": False}


# ---- Storage: data dir, thumbnail cache, ML models ---------------------------


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


class RevealBody(BaseModel):
    path: str


@router.post("/reveal")
def reveal_path(body: RevealBody) -> dict:
    """Open Windows Explorer. Backend and app run on the same machine (localhost
    only), so this reveals the user's own folder. For a file, Explorer opens its
    folder with the file selected (highlighted); for a folder, it just opens it."""
    _require_setup()
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(404, "path not found")
    if p.is_dir():
        os.startfile(str(p))  # noqa: S606 (Windows-only)
    else:
        # `explorer /select,<file>` highlights the file. explorer often exits
        # non-zero even on success, so don't check the return code.
        subprocess.Popen(["explorer", f"/select,{p}"])
    return {"ok": True}


@router.get("/photos/{photo_id}/file")
def photo_file(photo_id: str) -> dict:
    """The on-disk path of an existing file backing this photo, for the info
    panel's 'reveal in Explorer' link. Prefers a file on an online drive."""
    _require_setup()
    row = db.get_conn().execute(
        "SELECT path FROM files WHERE content_hash = ? AND status = 'ok' "
        "ORDER BY (SELECT online FROM drives d WHERE d.id = files.drive_id) DESC LIMIT 1",
        (photo_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no file on disk for this photo")
    return {"path": row["path"]}


@router.get("/storage")
def storage_info() -> StorageInfo:
    _require_setup()
    data_dir = config.get_data_dir()
    db_file = config.db_path()
    return StorageInfo(
        data_dir=str(data_dir),
        db_bytes=db_file.stat().st_size if db_file.exists() else 0,
        cache_dir=str(config.thumbs_dir()),
        cache_bytes=_dir_size(config.thumbs_dir()),
        models_dir=str(config.models_dir()),
        models_bytes=_dir_size(config.models_dir()),
        free_bytes=shutil.disk_usage(data_dir).free,
    )


@router.post("/cache/clear")
def clear_cache() -> dict:
    """Delete every cached thumbnail (safe — they're rebuildable) and kick off
    a background regeneration so the library doesn't go blank."""
    _require_setup()
    thumbs = config.thumbs_dir()
    if thumbs.exists():
        for child in thumbs.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    thumbs.mkdir(exist_ok=True)
    started = indexer.start_rebuild_thumbs()
    return {"ok": True, "rebuilding": started}


class DataDirBody(BaseModel):
    dataDir: str


@router.post("/data-dir")
def change_data_dir(body: DataDirBody) -> dict:
    """Move the whole data directory (database + cache + models) to a new
    location. Refused while indexing so nothing is half-written mid-move."""
    _require_setup()
    if indexer.status["state"] not in ("idle", "done", "error"):
        raise HTTPException(409, "can't move data while indexing — wait for it to finish")
    src = config.get_data_dir()
    dst = Path(body.dataDir)
    if dst == src:
        return {"ok": True, "dataDir": str(src)}
    if dst.exists() and any(dst.iterdir()):
        raise HTTPException(400, "choose an empty or new folder")

    db.close_conn()  # this thread; get_conn() reconnects lazily to the new path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    config.set_data_dir(dst)
    return {"ok": True, "dataDir": str(dst)}


# ---- Faces + semantic search: opt-in toggle & model download -----------------

_ml_download = {"downloading": False, "progress": 0.0, "message": None}


def _ml_status_dict() -> dict:
    models = config.models_dir()
    present = (models / "insightface").exists() or (models / "clip").exists()
    return {
        "installed": indexer.ml_available(),
        "enabled": indexer.ml_enabled(),
        "models_present": present,
        "models_bytes": _dir_size(models),
        "models_dir": str(models),
        "downloading": _ml_download["downloading"],
        "progress": _ml_download["progress"],
        "message": _ml_download["message"],
    }


@router.get("/ml/status")
def ml_status() -> dict:
    _require_setup()
    return _ml_status_dict()


def _download_models() -> None:
    _ml_download.update(downloading=True, progress=0.05, message="Preparing face model…")
    try:
        from . import faces as faces_mod
        faces_mod._get_app()  # downloads insightface buffalo_l on first call
        _ml_download.update(progress=0.6, message="Preparing search model…")
        from . import clipsearch
        clipsearch._get_model()  # downloads open_clip weights on first call
        _ml_download.update(progress=1.0, message="Ready. Run a rescan to detect faces.")
    except Exception as exc:  # keep the message so the UI can show what failed
        _ml_download.update(message=f"Download failed: {exc}")
    finally:
        _ml_download.update(downloading=False)


@router.post("/ml/enable")
def ml_enable() -> dict:
    """Turn on faces + semantic search: persist the opt-in and download the
    model weights in the background (progress via GET /ml/status)."""
    _require_setup()
    if not indexer.ml_available():
        raise HTTPException(
            400,
            "The ML packages aren't installed. Re-run server/setup.ps1 without "
            "-SkipML (needs the C++ build tools), then try again.",
        )
    db.kv_set("ml_enabled", "1")
    if not _ml_download["downloading"]:
        threading.Thread(target=_download_models, name="memoria-ml-dl", daemon=True).start()
    return _ml_status_dict()


@router.post("/ml/disable")
def ml_disable() -> dict:
    _require_setup()
    db.kv_set("ml_enabled", "0")
    return _ml_status_dict()


# ---- People --------------------------------------------------------------------


def _person_row(r, count: int) -> Person:
    return Person(
        id=f"person-{r['id']}",
        name=r["name"],
        avatar_url=f"/api/people/person-{r['id']}/avatar",
        photo_count=count,
    )


def _pid(person_id: str) -> int:
    try:
        return int(person_id.removeprefix("person-"))
    except ValueError:
        raise HTTPException(404, "no such person")


@router.get("/people")
def list_people() -> list[Person]:
    _require_setup()
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT p.*, COUNT(DISTINCT f.photo_hash) AS n FROM people p "
        "JOIN faces f ON f.person_id = p.id "
        "JOIN photos ph ON ph.hash = f.photo_hash AND ph.hidden = 0 AND ph.private = 0 "
        "GROUP BY p.id HAVING n >= 2 ORDER BY n DESC"
    ).fetchall()
    return [_person_row(r, r["n"]) for r in rows]


@router.get("/people/{person_id}")
def get_person(person_id: str) -> Person:
    _require_setup()
    pid = _pid(person_id)
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if row is None:
        raise HTTPException(404)
    n = conn.execute(
        "SELECT COUNT(DISTINCT f.photo_hash) AS n FROM faces f "
        "JOIN photos ph ON ph.hash = f.photo_hash AND ph.hidden = 0 AND ph.private = 0 "
        "WHERE f.person_id = ?",
        (pid,),
    ).fetchone()["n"]
    return _person_row(row, n)


@router.get("/people/{person_id}/photos")
def person_photos(person_id: str) -> list[Photo]:
    _require_setup()
    pid = _pid(person_id)
    return _list_photos(
        "AND hash IN (SELECT photo_hash FROM faces WHERE person_id = ?)", (pid,)
    )


class NameBody(BaseModel):
    name: str


@router.post("/people/{person_id}/name")
def rename_person(person_id: str, body: NameBody) -> dict:
    _require_setup()
    pid = _pid(person_id)
    with db.tx() as c:
        c.execute("UPDATE people SET name = ? WHERE id = ?", (body.name.strip() or None, pid))
    return {"ok": True}


class MergeBody(BaseModel):
    intoId: str


@router.post("/people/{person_id}/merge")
def merge_person(person_id: str, body: MergeBody) -> dict:
    _require_setup()
    from . import faces as faces_mod
    faces_mod.merge_people(_pid(person_id), _pid(body.intoId))
    return {"ok": True}


def _avatar_cache(pid: int) -> Path:
    return config.thumbs_dir() / "avatars" / f"{pid}.webp"


@router.get("/people/{person_id}/avatar")
def person_avatar(person_id: str) -> Response:
    _require_setup()
    pid = _pid(person_id)
    cache = _avatar_cache(pid)
    if not cache.exists():
        from . import faces as faces_mod
        crop = faces_mod.avatar_for(pid)
        if crop is None:
            raise HTTPException(404)
        cache.parent.mkdir(parents=True, exist_ok=True)
        crop.save(cache, "WEBP", quality=85)
    return FileResponse(cache, media_type="image/webp")


class CoverBody(BaseModel):
    faceId: int


@router.post("/people/{person_id}/delete")
def delete_person_endpoint(person_id: str) -> dict:
    """Remove a face group (a stranger, a poster face). Detaches its faces so a
    rescan won't rebuild it, then drops the person. Originals are untouched."""
    _require_setup()
    pid = _pid(person_id)
    from . import faces as faces_mod
    faces_mod.delete_person(pid)
    _avatar_cache(pid).unlink(missing_ok=True)
    return {"ok": True}


@router.post("/people/{person_id}/cover")
def set_person_cover(person_id: str, body: CoverBody) -> dict:
    """Pin a specific detected face as this person's key photo."""
    _require_setup()
    pid = _pid(person_id)
    from . import faces as faces_mod
    if not faces_mod.set_cover(pid, body.faceId):
        raise HTTPException(400, "that face doesn't belong to this person")
    _avatar_cache(pid).unlink(missing_ok=True)  # force regen on next fetch
    return {"ok": True}


@router.get("/photos/{photo_id}/faces")
def photo_faces(photo_id: str) -> list[PhotoFace]:
    """Every face detected in this image, with the person it belongs to (name +
    key-photo avatar) and its box, so the viewer can label who's in the shot."""
    _require_setup()
    rows = db.get_conn().execute(
        "SELECT f.id, f.x, f.y, f.w, f.h, f.person_id, p.name "
        "FROM faces f LEFT JOIN people p ON p.id = f.person_id "
        "WHERE f.photo_hash = ? ORDER BY f.w * f.h DESC",
        (photo_id,),
    ).fetchall()
    from .faces import _num
    out = []
    for r in rows:
        pid = r["person_id"]
        out.append(PhotoFace(
            face_id=r["id"],
            person_id=f"person-{pid}" if pid else None,
            name=r["name"],
            avatar_url=f"/api/people/person-{pid}/avatar" if pid else None,
            crop_url=f"/api/faces/{r['id']}/crop",
            x=_num(r["x"]), y=_num(r["y"]), w=_num(r["w"]), h=_num(r["h"]),
        ))
    return out


@router.get("/faces/{face_id}/crop")
def face_crop_img(face_id: int) -> Response:
    _require_setup()
    cache = config.thumbs_dir() / "faces" / f"{face_id}.webp"
    if not cache.exists():
        from . import faces as faces_mod
        crop = faces_mod.face_crop(face_id)
        if crop is None:
            raise HTTPException(404)
        cache.parent.mkdir(parents=True, exist_ok=True)
        crop.save(cache, "WEBP", quality=85)
    return FileResponse(cache, media_type="image/webp", headers=CACHE_FOREVER)


@router.post("/faces/{face_id}/detach")
def detach_face_endpoint(face_id: int) -> dict:
    """'This person is not in this image' — unlink the face from its cluster so
    the mistaken match stops showing (and won't silently re-cluster back)."""
    _require_setup()
    from . import faces as faces_mod
    faces_mod.detach_face(face_id)
    return {"ok": True}


# ---- Trips ---------------------------------------------------------------------


def _trip_model(t: dict) -> Trip:
    ov = db.get_conn().execute(
        "SELECT name, place, cover_hash, cover_image FROM trip_overrides WHERE trip_id = ?",
        (t["id"],),
    ).fetchone()
    hashes = t["photo_hashes"]
    cover = ov["cover_hash"] if ov and ov["cover_hash"] in hashes else None
    ordered = [cover] + [h for h in hashes if h != cover] if cover else hashes
    cover_urls = [f"/api/photos/{h}/thumb" for h in ordered[:4]]

    # An uploaded image wins over a chosen trip photo, and shows full-bleed
    # (signalled to the card by a truthy cover_hash).
    uploaded = _trip_cover_file(t["id"]) if ov and ov["cover_image"] else None
    if uploaded and uploaded.exists():
        cover_urls = [f"/api/trips/{t['id']}/cover-image?v={int(uploaded.stat().st_mtime)}"] + cover_urls
        cover = "uploaded"

    return Trip(
        id=t["id"],
        place=(ov["place"] if ov and ov["place"] else None) or t["place"],
        start_date=t["start_date"],
        end_date=t["end_date"],
        photo_count=len(hashes),
        cover_urls=cover_urls,
        name=ov["name"] if ov and ov["name"] else None,
        cover_hash=cover,
    )


def _trip_cover_dir() -> Path:
    d = config.get_data_dir() / "trip_covers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trip_cover_file(trip_id: str) -> Path | None:
    row = db.get_conn().execute(
        "SELECT cover_image FROM trip_overrides WHERE trip_id = ?", (trip_id,)
    ).fetchone()
    if not row or not row["cover_image"]:
        return None
    return _trip_cover_dir() / row["cover_image"]


@router.get("/trips")
def list_trips() -> list[Trip]:
    _require_setup()
    return [_trip_model(t) for t in trips.detect_trips()]


def _find_trip(trip_id: str) -> dict:
    for t in trips.detect_trips():
        if t["id"] == trip_id:
            return t
    raise HTTPException(404, "no such trip")


@router.get("/trips/{trip_id}")
def get_trip(trip_id: str) -> Trip:
    _require_setup()
    return _trip_model(_find_trip(trip_id))


@router.get("/trips/{trip_id}/photos")
def trip_photos(trip_id: str) -> list[Photo]:
    _require_setup()
    t = _find_trip(trip_id)
    marks = ",".join("?" for _ in t["photo_hashes"])
    return _list_photos(f"AND hash IN ({marks})", tuple(t["photo_hashes"]))


@router.get("/trips/{trip_id}/people")
def trip_people(trip_id: str) -> list[Person]:
    """Everyone photographed on this trip, most-photographed first — for the
    little face strip at the top of the trip."""
    _require_setup()
    t = _find_trip(trip_id)
    if not t["photo_hashes"]:
        return []
    marks = ",".join("?" for _ in t["photo_hashes"])
    rows = db.get_conn().execute(
        "SELECT p.*, COUNT(DISTINCT f.photo_hash) AS n FROM people p "
        "JOIN faces f ON f.person_id = p.id "
        f"WHERE f.photo_hash IN ({marks}) "
        "GROUP BY p.id ORDER BY n DESC",
        tuple(t["photo_hashes"]),
    ).fetchall()
    return [_person_row(r, r["n"]) for r in rows]


@router.post("/trips/{trip_id}/photos/remove")
def remove_trip_photos(trip_id: str, body: PhotoIdsBody) -> dict:
    """Remove specific photos from an auto-detected trip. Trips aren't a stored
    membership — they're re-derived from dates/places every request — so the
    only way a removal survives a re-detect is to record it and subtract it back
    out in trips.detect_trips (see trip_excludes). The photos themselves are
    untouched; they just stop belonging to this trip."""
    _require_setup()
    with db.tx() as c:
        for h in body.photoIds:
            c.execute(
                "INSERT OR IGNORE INTO trip_excludes (trip_id, photo_hash) VALUES (?, ?)",
                (trip_id, h),
            )
    return {"ok": True}


class TripEditBody(BaseModel):
    name: str | None = None
    place: str | None = None
    coverHash: str | None = None


@router.post("/trips/{trip_id}")
def edit_trip(trip_id: str, body: TripEditBody) -> Trip:
    """Override an auto-detected trip's title, location, and/or cover photo.
    Stored separately from detection so a re-scan never wipes the edit."""
    _require_setup()
    t = _find_trip(trip_id)
    name = (body.name or "").strip() or None
    place = (body.place or "").strip() or None
    cover = body.coverHash if body.coverHash in t["photo_hashes"] else None
    with db.tx() as c:
        c.execute(
            "INSERT INTO trip_overrides (trip_id, name, place, cover_hash) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(trip_id) DO UPDATE SET name = excluded.name, "
            "  place = excluded.place, cover_hash = excluded.cover_hash",
            (trip_id, name, place, cover),
        )
        # Choosing one of the trip's own photos as the cover clears any
        # previously-uploaded image so the choice actually takes effect.
        if cover:
            c.execute("UPDATE trip_overrides SET cover_image = NULL WHERE trip_id = ?", (trip_id,))
    return _trip_model(t)


class TripCoverUploadBody(BaseModel):
    path: str  # a local image file the user picked (native dialog gives a path)


@router.post("/trips/{trip_id}/cover-upload")
def upload_trip_cover(trip_id: str, body: TripCoverUploadBody) -> Trip:
    """Use a picked local image as this trip's cover. The file is copied into the
    data dir (never referenced in place, so moving/deleting the original can't
    break the card); nothing leaves the machine."""
    _require_setup()
    t = _find_trip(trip_id)
    src = Path(body.path)
    if not src.exists() or src.suffix.lower() not in media.IMAGE_EXTS:
        raise HTTPException(400, "pick an image file")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", trip_id)
    dst = _trip_cover_dir() / f"{safe}{src.suffix.lower()}"
    for old in _trip_cover_dir().glob(f"{safe}.*"):
        old.unlink(missing_ok=True)  # only one cover per trip
    shutil.copy2(src, dst)
    with db.tx() as c:
        c.execute(
            "INSERT INTO trip_overrides (trip_id, cover_image) VALUES (?, ?) "
            "ON CONFLICT(trip_id) DO UPDATE SET cover_image = excluded.cover_image",
            (trip_id, dst.name),
        )
    return _trip_model(t)


@router.get("/trips/{trip_id}/cover-image")
def trip_cover_image(trip_id: str) -> FileResponse:
    _require_setup()
    f = _trip_cover_file(trip_id)
    if f is None or not f.exists():
        raise HTTPException(404, "no uploaded cover")
    return FileResponse(str(f), headers={"Cache-Control": "no-cache"})


# ---- Places --------------------------------------------------------------------


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")


@router.get("/places")
def list_places() -> list[Place]:
    _require_setup()
    rows = db.get_conn().execute(
        "SELECT place, COUNT(*) AS n, AVG(lat) AS lat, AVG(lng) AS lng, "
        "  (SELECT hash FROM photos p2 WHERE p2.place = p.place AND p2.live_of IS NULL "
        f"   AND p2.hidden = 0 AND p2.private = 0 ORDER BY date_taken DESC LIMIT 1) AS cover "
        f"FROM photos p WHERE place IS NOT NULL AND live_of IS NULL AND {VISIBLE} "
        "GROUP BY place ORDER BY n DESC"
    ).fetchall()
    return [
        Place(
            id=_slug(r["place"]),
            name=r["place"],
            lat=r["lat"],
            lng=r["lng"],
            photo_count=r["n"],
            cover_url=f"/api/photos/{r['cover']}/thumb",
        )
        for r in rows
    ]


@router.get("/places/{place_id}")
def get_place(place_id: str) -> Place:
    _require_setup()
    for p in list_places():
        if p.id == place_id:
            return p
    raise HTTPException(404, "no such place")


@router.get("/places/{place_id}/photos")
def place_photos(place_id: str) -> list[Photo]:
    _require_setup()
    place = get_place(place_id)
    return _list_photos("AND place = ?", (place.name,))


# ---- Albums --------------------------------------------------------------------


def _album_model(r) -> Album:
    conn = db.get_conn()
    # Only VISIBLE photos count toward an album: a hidden photo (reversible)
    # keeps its album_photos row but shouldn't inflate the count or be the
    # cover; a private photo has its row removed outright (Private is exclusive).
    ids = [
        row["photo_hash"]
        for row in conn.execute(
            "SELECT ap.photo_hash FROM album_photos ap JOIN photos p ON p.hash = ap.photo_hash "
            f"WHERE ap.album_id = ? AND p.{VISIBLE} ORDER BY p.date_taken DESC",
            (r["id"],),
        )
    ]
    return Album(
        id=f"album-{r['id']}",
        name=r["name"],
        created_at=r["created_at"],
        photo_ids=ids,
        cover_url=f"/api/photos/{ids[0]}/thumb" if ids else None,
    )


def _aid(album_id: str) -> int:
    try:
        return int(album_id.removeprefix("album-"))
    except ValueError:
        raise HTTPException(404, "no such album")


@router.get("/albums")
def list_albums() -> list[Album]:
    _require_setup()
    rows = db.get_conn().execute("SELECT * FROM albums ORDER BY created_at DESC").fetchall()
    return [_album_model(r) for r in rows]


@router.post("/albums")
def create_album(body: NameBody) -> Album:
    _require_setup()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO albums (name, created_at) VALUES (?, ?)",
            (body.name.strip(), datetime.now(timezone.utc).isoformat()),
        )
        album_id = cur.lastrowid
    row = db.get_conn().execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
    return _album_model(row)


@router.get("/albums/{album_id}")
def get_album(album_id: str) -> Album:
    _require_setup()
    row = db.get_conn().execute("SELECT * FROM albums WHERE id = ?", (_aid(album_id),)).fetchone()
    if row is None:
        raise HTTPException(404)
    return _album_model(row)


@router.post("/albums/{album_id}/name")
def rename_album(album_id: str, body: NameBody) -> dict:
    _require_setup()
    with db.tx() as c:
        c.execute("UPDATE albums SET name = ? WHERE id = ?", (body.name.strip(), _aid(album_id)))
    return {"ok": True}


@router.post("/albums/{album_id}/delete")
def delete_album(album_id: str) -> dict:
    """Delete the album itself — its row and its membership rows — but never the
    photos or videos inside it. Those are just un-grouped; they stay in the
    library (and in any other album). Removing the album_photos rows here also
    keeps every photo's album count honest."""
    _require_setup()
    aid = _aid(album_id)
    with db.tx() as c:
        c.execute("DELETE FROM album_photos WHERE album_id = ?", (aid,))
        c.execute("DELETE FROM albums WHERE id = ?", (aid,))
    return {"ok": True}


@router.get("/albums/{album_id}/photos")
def album_photos(album_id: str) -> list[Photo]:
    _require_setup()
    album = get_album(album_id)
    if not album.photo_ids:
        return []
    marks = ",".join("?" for _ in album.photo_ids)
    return _list_photos(f"AND hash IN ({marks})", tuple(album.photo_ids))


class AlbumPhotosBody(BaseModel):
    photoIds: list[str]


@router.post("/albums/{album_id}/photos")
def add_album_photos(album_id: str, body: AlbumPhotosBody) -> dict:
    _require_setup()
    aid = _aid(album_id)
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as c:
        for h in body.photoIds:
            c.execute(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_hash, added_at) "
                "VALUES (?, ?, ?)",
                (aid, h, now),
            )
    return {"ok": True}


@router.post("/albums/{album_id}/photos/remove")
def remove_album_photos(album_id: str, body: AlbumPhotosBody) -> dict:
    _require_setup()
    aid = _aid(album_id)
    with db.tx() as c:
        for h in body.photoIds:
            c.execute(
                "DELETE FROM album_photos WHERE album_id = ? AND photo_hash = ?", (aid, h)
            )
    return {"ok": True}


# ---- Duplicates ----------------------------------------------------------------


def _dup_group_model(g: dict) -> DuplicateGroup | None:
    files = duplicates.files_for_group(g)
    if len(files) < 2:
        return None
    return DuplicateGroup(
        id=g["id"],
        similarity=g["similarity"],
        date_taken=g["date_taken"],
        files=[
            DuplicateFile(
                file_id=str(f["id"]),
                path=f["path"],
                drive=f["drive_label"] or "Unknown drive",
                size_bytes=f["size"],
                width=f["width"] or 0,
                height=f["height"] or 0,
                modified_at=datetime.fromtimestamp(f["mtime"], tz=timezone.utc).isoformat(),
                thumb_url=f"/api/photos/{f['photo_hash']}/thumb",
                preview_url=f"/api/photos/{f['photo_hash']}/preview",
            )
            for f in files
        ],
    )


@router.get("/duplicates")
def list_duplicates() -> list[DuplicateGroup]:
    _require_setup()
    out = []
    for g in duplicates.find_groups():
        model = _dup_group_model(g)
        if model is not None:
            out.append(model)
    return out


class ResolveBody(BaseModel):
    keepFileId: str


@router.post("/duplicates/{group_id}/resolve")
def resolve_duplicates(group_id: str, body: ResolveBody) -> dict:
    _require_setup()
    group = next((g for g in duplicates.find_groups() if g["id"] == group_id), None)
    if group is None:
        raise HTTPException(404, "no such duplicate group")
    removed = duplicates.resolve(group, int(body.keepFileId))
    return {"ok": True, "removed": removed}


@router.post("/duplicates/{group_id}/ignore")
def ignore_duplicate(group_id: str) -> dict:
    """Dismiss a duplicate group so it stops showing on the page (the files are
    left exactly as they are)."""
    _require_setup()
    duplicates.ignore_group(group_id)
    return {"ok": True}


# ---- Search --------------------------------------------------------------------

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

# "@lat,lng" with an optional radius: "@17.02, 54.09" or "@17.02,54.09 5km".
_COORD_RE = re.compile(
    r"^@?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    r"(?:\s+(\d+(?:\.\d+)?)\s*k?m?)?\s*$",
    re.IGNORECASE,
)


def _parse_coord_query(q: str) -> tuple[float, float, float | None] | None:
    """(lat, lng, radius_km|None) if the query is a coordinate, else None.
    Requires the leading '@' OR an in-range lat/lng pair so plain '2023' etc.
    never trip it."""
    m = _COORD_RE.match(q)
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if not q.lstrip().startswith("@") and m.group(3) is None and "," not in q:
        return None
    radius = float(m.group(3)) if m.group(3) else None
    return lat, lng, radius


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _search_near(lat: float, lng: float, radius: float | None) -> list[Photo]:
    """Photos with GPS, sorted by distance to (lat, lng). With a radius (km)
    only those within it; otherwise the nearest 200."""
    rows = db.get_conn().execute(
        _PHOTOS_SQL.format(where="AND lat IS NOT NULL AND lng IS NOT NULL")
    ).fetchall()
    scored = sorted(
        ((_haversine_km(lat, lng, r["lat"], r["lng"]), r) for r in rows),
        key=lambda t: t[0],
    )
    if radius is not None:
        scored = [t for t in scored if t[0] <= radius]
    else:
        scored = scored[:200]
    return _photo_rows_to_models([r for _, r in scored])


@router.get("/search")
def search(q: str) -> list[Photo]:
    """Structured matches first (precise), then semantic extras (fuzzy)."""
    _require_setup()
    q = q.strip()
    if not q:
        return []
    # Coordinate search: "@17.02,54.09" (optionally "... 5km") returns photos at
    # or nearest that point, closest first. Checked before token matching.
    coords = _parse_coord_query(q)
    if coords:
        return _search_near(*coords)
    tokens = q.lower().split()
    conn = db.get_conn()

    name_rows = conn.execute("SELECT id, name FROM people WHERE name IS NOT NULL").fetchall()
    name_to_pid = {r["name"].lower(): r["id"] for r in name_rows}
    person_photo_map: dict[int, set[str]] = {}
    for r in conn.execute("SELECT person_id, photo_hash FROM faces WHERE person_id IS NOT NULL"):
        person_photo_map.setdefault(r["person_id"], set()).add(r["photo_hash"])

    structured: list = []
    for row in conn.execute(_PHOTOS_SQL.format(where=""), ()):
        date = row["date_taken"]
        month = MONTHS[int(date[5:7]) - 1] if len(date) >= 7 else ""
        ok = True
        for token in tokens:
            if (row["place"] or "").lower().find(token) >= 0:
                continue
            if row["filename"].lower().find(token) >= 0:
                continue
            if (row["camera"] or "").lower().find(token) >= 0:
                continue
            if date[:4] == token:
                continue
            if len(token) >= 3 and month.startswith(token):
                continue
            pid = name_to_pid.get(token)
            if pid is not None and row["hash"] in person_photo_map.get(pid, set()):
                continue
            ok = False
            break
        if ok:
            structured.append(row)

    results = _photo_rows_to_models(structured)
    seen = {p.id for p in results}

    if indexer.ml_available():
        from . import clipsearch
        try:
            semantic_hashes = [h for h in clipsearch.search(q) if h not in seen]
            if semantic_hashes:
                marks = ",".join("?" for _ in semantic_hashes)
                rows = conn.execute(
                    f"SELECT * FROM photos WHERE hash IN ({marks}) AND {VISIBLE}",
                    tuple(semantic_hashes),
                ).fetchall()
                by_hash = {r["hash"]: r for r in rows}
                ordered = [by_hash[h] for h in semantic_hashes if h in by_hash]
                results.extend(_photo_rows_to_models(ordered))
        except Exception as exc:
            print(f"[search] semantic search failed: {exc}")

    return results


# ---- Settings: folders & indexing ------------------------------------------------


@router.get("/folders")
def list_folders() -> list[SourceFolder]:
    _require_setup()
    rows = db.get_conn().execute(
        "SELECT fo.*, d.label AS drive_label, d.online, "
        "  (SELECT COUNT(*) FROM files WHERE folder_id = fo.id AND status = 'ok') AS n "
        "FROM folders fo LEFT JOIN drives d ON d.id = fo.drive_id"
    ).fetchall()
    return [
        SourceFolder(
            id=r["id"], path=r["path"], drive_label=r["drive_label"],
            online=bool(r["online"]), file_count=r["n"],
        )
        for r in rows
    ]


class FolderBody(BaseModel):
    path: str


@router.post("/folders")
def add_folder(body: FolderBody) -> dict:
    _require_setup()
    try:
        folder_id = scanner.add_folder(body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    indexer.start()
    return {"ok": True, "id": folder_id}


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int) -> dict:
    _require_setup()
    scanner.remove_folder(folder_id)
    return {"ok": True}


@router.post("/folders/refresh")
def refresh_folders() -> list[SourceFolder]:
    """Re-check which source drives are connected (after plugging one in) and
    index anything on a drive that just came back."""
    _require_setup()
    if scanner.refresh_drives():
        indexer.start()
    return list_folders()


@router.get("/index/status")
def index_status() -> IndexStatus:
    return IndexStatus(**indexer.status, ml_available=indexer.ml_available())


@router.post("/index/start")
def index_start() -> dict:
    _require_setup()
    started = indexer.start()
    return {"ok": True, "started": started}
