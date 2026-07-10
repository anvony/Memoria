"""The indexing pipeline — the heart of the backend.

One background thread works through stages; the UI polls /api/index/status.
Per scope §2 there is no daemon: indexing runs only while the app is open,
and because change detection makes re-runs cheap, that's enough.

Stage order matters:
  1. scan      — cheap directory walk, finds what needs work
  2. index     — hash + EXIF + thumbnails per file (the bulk of the time)
  3. pair      — Live Photo stills adopt their companion MOVs
  4. phash     — perceptual hashes for the duplicates screen
  5. faces     — detection + embedding + clustering   (only if ML installed)
  6. clip      — semantic embeddings                  (only if ML installed)

Every stage is incremental: it only touches rows that don't have its output
yet, so an interrupted run resumes where it left off.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, geocode, media, scanner

_lock = threading.Lock()
_thread: threading.Thread | None = None

status: dict = {
    "state": "idle",
    "total": 0,
    "done": 0,
    "current": None,
    "error": None,
}


def ml_available() -> bool:
    try:
        import insightface  # noqa: F401
        import open_clip  # noqa: F401
        return True
    except ImportError:
        return False


def ml_enabled() -> bool:
    """Faces + semantic search are opt-in: the packages being installed isn't
    enough, the user must turn them on (they're heavy and download ~1 GB of
    models). Off by default."""
    return db.kv_get("ml_enabled") == "1"


def start() -> bool:
    """Kick off a full incremental pass. Returns False if already running."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(target=_run, name="memoria-indexer", daemon=True)
        _thread.start()
        return True


def start_rebuild_thumbs() -> bool:
    """Regenerate every thumbnail from the originals — used after 'clear cache'.
    Runs on the same single worker thread as indexing."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _thread = threading.Thread(target=_rebuild_thumbs, name="memoria-thumbs", daemon=True)
        _thread.start()
        return True


def _rebuild_thumbs() -> None:
    try:
        _set("indexing", total=0, done=0, current=None, error=None)
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT p.hash, p.kind, p.duration, "
            "  (SELECT path FROM files WHERE content_hash = p.hash AND status = 'ok' LIMIT 1) AS path "
            "FROM photos p"
        ).fetchall()
        rows = [r for r in rows if r["path"]]
        _set("indexing", total=len(rows), done=0)
        for i, r in enumerate(rows):
            status.update(done=i, current=Path(r["path"]).name)
            src = Path(r["path"])
            try:
                if r["kind"] == "photo":
                    media.make_image_thumbs(src, config.thumbs_dir(), r["hash"])
                else:
                    media.make_video_thumbs(src, config.thumbs_dir(), r["hash"], r["duration"])
            except Exception as exc:
                print(f"[thumbs] regen failed for {src}: {exc}")
        status.update(done=len(rows), current=None)
        _set("done")
    except Exception as exc:
        traceback.print_exc()
        _set("error", error=str(exc))
    finally:
        db.close_conn()


def _set(state: str, **kw) -> None:
    status.update({"state": state, **kw})


def _run() -> None:
    try:
        _set("scanning", total=0, done=0, current=None, error=None)
        geocode.warm_up()

        conn = db.get_conn()
        folder_ids = [r["id"] for r in conn.execute("SELECT id FROM folders")]
        todo: list[int] = []
        for fid in folder_ids:
            todo.extend(scanner.scan_folder(fid))

        _set("indexing", total=len(todo), done=0)
        for i, file_id in enumerate(todo):
            row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            if row is None:
                continue
            status.update(done=i, current=Path(row["path"]).name)
            try:
                _index_file(row)
            except Exception as exc:  # one broken file must not kill the run
                print(f"[indexer] failed on {row['path']}: {exc}")
        status.update(done=len(todo), current=None)

        _pair_live_photos()
        _compute_phashes()

        if ml_available() and ml_enabled():
            from . import faces as faces_mod
            _set("faces")
            faces_mod.process_pending(status)
            from . import clipsearch
            _set("clip")
            clipsearch.process_pending(status)

        # Autosave the catalogue after every completed sync (scope: "after each
        # sync"). Cheap — the DB is small — and keeps a rolling safety net.
        try:
            from . import backup
            backup.make_backup(auto=True)
        except Exception as exc:
            print(f"[indexer] autosave failed: {exc}")

        _set("done")
    except Exception as exc:
        traceback.print_exc()
        _set("error", error=str(exc))
    finally:
        db.close_conn()


def _index_file(row) -> None:
    path = Path(row["path"])
    kind = media.kind_of(path)
    if kind is None:
        return
    file_hash = media.content_hash(path, kind)

    conn = db.get_conn()
    exists = conn.execute("SELECT hash FROM photos WHERE hash = ?", (file_hash,)).fetchone()
    with db.tx() as c:
        c.execute("UPDATE files SET content_hash = ? WHERE id = ?", (file_hash, row["id"]))
    if exists:
        return  # same content already indexed (a duplicate file) — nothing to redo

    meta = media.read_image_meta(path) if kind == "photo" else media.read_video_meta(path)

    place = None
    if meta["lat"] is not None and meta["lng"] is not None:
        place = geocode.place_name(meta["lat"], meta["lng"])

    if kind == "photo":
        media.make_image_thumbs(path, config.thumbs_dir(), file_hash)
    else:
        media.make_video_thumbs(path, config.thumbs_dir(), file_hash, meta.get("duration"))

    with db.tx() as c:
        c.execute(
            "INSERT INTO photos (hash, kind, filename, date_taken, width, height, camera, "
            "  lat, lng, place, duration, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_hash, kind, path.name, meta["date_taken"],
                meta["width"] or 0, meta["height"] or 0, meta["camera"],
                meta["lat"], meta["lng"], place, meta.get("duration"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def _pair_live_photos() -> None:
    """iPhone Live Photos are a still (HEIC/JPG) plus a ~3s MOV. Mark the MOV
    as `live_of` the still so the timeline shows ONE entry (the still) instead
    of a photo plus a phantom 3-second video.

    Two ways to match, because iCloud/Windows exports often RENAME both halves
    to random names (CVXZ4746.MOV) that no longer share a basename:
      1. Same folder + same filename stem  (clean camera-roll case)
      2. Same folder + capture timestamps within 2s  (renamed case) — a Live
         Photo's clip is stamped with its still's exact capture time.
    Each still can adopt at most one clip; each clip pairs to its nearest
    unclaimed still, so bursts don't cross-pair.
    """
    conn = db.get_conn()
    videos = conn.execute(
        "SELECT p.hash, p.date_taken, f.path FROM photos p JOIN files f ON f.content_hash = p.hash "
        "WHERE p.kind = 'video' AND p.duration <= 4 AND p.live_of IS NULL"
    ).fetchall()
    if not videos:
        return
    stills = conn.execute(
        "SELECT p.hash, p.date_taken, f.path FROM photos p JOIN files f ON f.content_hash = p.hash "
        "WHERE p.kind = 'photo'"
    ).fetchall()

    def epoch(iso: str) -> float | None:
        try:
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return None

    by_stem: dict[tuple, str] = {}
    stills_by_dir: dict[str, list] = {}
    for r in stills:
        folder = str(Path(r["path"]).parent).lower()
        by_stem[(folder, Path(r["path"]).stem.lower())] = r["hash"]
        stills_by_dir.setdefault(folder, []).append((r["hash"], epoch(r["date_taken"])))

    claimed: set[str] = set()
    pairs: list[tuple[str, str]] = []  # (video_hash, still_hash)

    for v in videos:
        folder = str(Path(v["path"]).parent).lower()
        # 1) exact stem match
        still = by_stem.get((folder, Path(v["path"]).stem.lower()))
        if still and still not in claimed:
            claimed.add(still)
            pairs.append((v["hash"], still))
            continue
        # 2) nearest still by timestamp within 2 seconds
        vt = epoch(v["date_taken"])
        if vt is None:
            continue
        best, best_dt = None, 2.0
        for still_hash, st in stills_by_dir.get(folder, []):
            if st is None or still_hash in claimed:
                continue
            dt = abs(st - vt)
            if dt <= best_dt:
                best, best_dt = still_hash, dt
        if best is not None:
            claimed.add(best)
            pairs.append((v["hash"], best))

    with db.tx() as c:
        for video_hash, still_hash in pairs:
            c.execute("UPDATE photos SET live_of = ? WHERE hash = ?", (still_hash, video_hash))


def _compute_phashes() -> None:
    """Perceptual hash from the 400px thumbnail (not the original: 100x faster
    and pHash doesn't need more pixels than that)."""
    import imagehash
    from PIL import Image

    conn = db.get_conn()
    pending = conn.execute("SELECT hash FROM photos WHERE phash IS NULL").fetchall()
    for row in pending:
        thumb, _ = media.thumb_paths(config.thumbs_dir(), row["hash"])
        if not thumb.exists():
            continue
        try:
            with Image.open(thumb) as img:
                ph = str(imagehash.phash(img))
        except OSError:
            continue
        with db.tx() as c:
            c.execute("UPDATE photos SET phash = ? WHERE hash = ?", (ph, row["hash"]))
