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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, geocode, media, scanner

# Stage 2 (hash + EXIF + thumbnails) runs on a pool of worker threads. Plain
# threads win here because everything expensive releases the GIL: Pillow
# decode/encode is C, blake2b releases it for large buffers, ffmpeg/ffprobe are
# subprocesses, file reads are I/O waits. 4 is deliberate — on a spinning HDD or
# USB drive, parallel reads seek-thrash, so we don't go higher.
_INDEX_WORKERS = 4

_lock = threading.Lock()
_thread: threading.Thread | None = None
# Set when a folder is added / a drive reconnects while a pass is already
# running, so the running pipeline does one more full pass instead of leaving
# the new folder unindexed until a manual rescan.
_rescan = threading.Event()

status: dict = {
    "state": "idle",
    "total": 0,
    "done": 0,
    "current": None,
    "error": None,
}

# M7: ML-stage progress, tracked separately from the top-level `status`. Faces
# and CLIP now run on their own thread CONCURRENTLY with scanning/indexing (so
# the GPU works on already-indexed photos instead of idling until the last file
# is hashed), which means they can't share the single set of top-level counters.
# `_run` mirrors this up to `status` only while it's waiting for ML to finish
# draining at the end, so the UI still shows a faces/CLIP bar there.
ml_progress: dict = {"state": "idle", "total": 0, "done": 0, "current": None}


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


def request_rescan() -> None:
    """Queue an indexing pass after a folder is added or a drive reconnects.
    If a pass is already running, this flag makes its pipeline do one more full
    pass (so the new folder is picked up automatically, queued behind the work
    already in flight); otherwise a fresh pass starts."""
    _rescan.set()
    start()


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


def _rebuild_one(r):
    """Pool worker for 'clear cache' regen. Touches only the filesystem (no DB
    writes), so a plain shared thumbs dir is safe across threads."""
    src = Path(r["path"])
    try:
        if r["kind"] == "photo":
            media.make_image_thumbs(src, config.thumbs_dir(), r["hash"])
        else:
            media.make_video_thumbs(src, config.thumbs_dir(), r["hash"], r["duration"])
    except Exception as exc:
        print(f"[thumbs] regen failed for {src}: {exc}")
    return r


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
        done = 0
        with ThreadPoolExecutor(max_workers=_INDEX_WORKERS) as pool:
            for r in as_completed([pool.submit(_rebuild_one, row) for row in rows]):
                done += 1
                status.update(done=done, current=Path(r.result()["path"]).name)
        status.update(done=len(rows), current=None)
        _set("done")
    except Exception as exc:
        traceback.print_exc()
        _set("error", error=str(exc))
    finally:
        db.close_conn()


def _set(state: str, **kw) -> None:
    status.update({"state": state, **kw})


def _index_todo(todo: list[int]) -> None:
    """Run stage 2 (hash + EXIF + thumbnails) over a batch of file ids on the
    worker pool, updating progress. One broken file is recorded, never fatal."""
    _set("indexing", total=len(todo), done=0)
    done = 0
    with ThreadPoolExecutor(max_workers=_INDEX_WORKERS) as pool:
        futures = [pool.submit(_index_one, fid) for fid in todo]
        for fut in as_completed(futures):
            done += 1
            row, exc = fut.result()
            if row is None:
                status.update(done=done)
                continue
            # `current` is best-effort under parallelism — it just shows one
            # of the files in flight, not a strict sequence.
            status.update(done=done, current=Path(row["path"]).name)
            if exc is not None:  # one broken file must not kill the run
                attempts = _record_failure(row, exc)
                if attempts >= scanner.MAX_INDEX_ATTEMPTS:
                    print(f"[indexer] giving up on {row['path']} after {attempts} "
                          f"attempts: {exc}")
                else:
                    print(f"[indexer] failed on {row['path']} "
                          f"(attempt {attempts}/{scanner.MAX_INDEX_ATTEMPTS}): {exc}")
    status.update(done=len(todo), current=None)


def _warm_ml_models() -> None:
    """M1: load the face + CLIP models on a side thread while stage 2 (hash +
    EXIF + thumbnails — CPU/IO-bound, GPU idle) runs, so the models are ready by
    the time the faces/CLIP stages start instead of stalling on a cold ~seconds
    load then. No-op unless ML is installed AND enabled. Warm-up failures only
    log — the real stage runs `_get_app`/`_get_model` again and surfaces any hard
    error there; this thread must never take the run down."""
    if not (ml_available() and ml_enabled()):
        return

    def _warm() -> None:
        try:
            from . import faces as faces_mod
            faces_mod._get_app()
        except Exception as exc:
            print(f"[indexer] face model warm-up skipped: {exc}")
        try:
            from . import clipsearch
            clipsearch._get_model()
        except Exception as exc:
            print(f"[indexer] CLIP model warm-up skipped: {exc}")

    threading.Thread(target=_warm, name="memoria-ml-warmup", daemon=True).start()


def _run() -> None:
    ml_stop = threading.Event()
    ml_thread: threading.Thread | None = None
    try:
        geocode.warm_up()
        _warm_ml_models()

        # M7: run the ML stages (faces + CLIP) CONCURRENTLY with scanning/indexing.
        # A dedicated consumer thread processes already-indexed photos on the GPU
        # while this thread keeps hashing/thumbnailing the rest on the CPU, so the
        # GPU no longer sits idle until the very last file is indexed. SQLite WAL +
        # timeout=30 makes the two writers safe, and each thread owns its own
        # connection. The consumer keeps polling for newly-indexed photos and only
        # exits once `ml_stop` is set (indexing finished) and nothing is pending.
        if ml_available() and ml_enabled():
            ml_thread = threading.Thread(
                target=_ml_consumer, args=(ml_stop,), name="memoria-ml", daemon=True
            )
            ml_thread.start()

        # Outer loop: re-run the whole pipeline if a folder was queued (_rescan)
        # while it was busy — covers a folder added during phash/backup, after the
        # scan loop below has already finished.
        while True:
            _rescan.clear()

            # Scan + index in repeated passes. scan_folder is incremental (it
            # returns only files still missing a content_hash) and re-reads the
            # folders table every pass, so a folder added WHILE indexing is
            # running is picked up on the next pass — queued behind the files
            # already in flight — instead of waiting for a manual rescan.
            while True:
                _set("scanning", total=0, done=0, current=None, error=None)
                conn = db.get_conn()
                folder_ids = [r["id"] for r in conn.execute("SELECT id FROM folders")]
                todo: list[int] = []
                for fid in folder_ids:
                    todo.extend(scanner.scan_folder(fid))
                if not todo:
                    break
                _index_todo(todo)
                _clear_resolved_failures()

            _clear_resolved_failures()
            _pair_live_photos()
            _compute_phashes()

            # Autosave the catalogue after every completed sync (scope: "after
            # each sync"). Cheap — the DB is small — and keeps a rolling safety
            # net.
            try:
                from . import backup
                backup.make_backup(auto=True)
            except Exception as exc:
                print(f"[indexer] autosave failed: {exc}")

            # A folder queued mid-pipeline (after the scan loop drained) makes us
            # run the whole thing once more; everything is incremental so a pass
            # with nothing new is cheap.
            if not _rescan.is_set():
                break

        # Indexing is finished. Tell the ML consumer no more photos are coming and
        # wait for it to drain, mirroring its progress to the top-level status so
        # the UI shows the faces/CLIP bar here instead of jumping straight to
        # "done" while the GPU is still working through the backlog.
        if ml_thread is not None:
            ml_stop.set()
            while ml_thread.is_alive():
                _set(ml_progress.get("state") or "faces",
                     total=ml_progress.get("total", 0),
                     done=ml_progress.get("done", 0),
                     current=ml_progress.get("current"))
                ml_thread.join(timeout=0.5)

        _set("done")
    except Exception as exc:
        traceback.print_exc()
        _set("error", error=str(exc))
        # Don't leave the consumer running against a torn-down run.
        ml_stop.set()
        if ml_thread is not None:
            ml_thread.join(timeout=5)
    finally:
        db.close_conn()


def _ml_pending() -> bool:
    """True if any indexed, non-Live-Photo photo still needs a face or CLIP pass."""
    row = db.get_conn().execute(
        "SELECT 1 FROM photos WHERE live_of IS NULL AND (faces_done = 0 OR clip_done = 0) LIMIT 1"
    ).fetchone()
    return row is not None


def _ml_consumer(stop: threading.Event) -> None:
    """M7: process the ML stages (faces + CLIP) concurrently with indexing.

    Runs on its own thread with its own DB connection. Each cycle it pairs Live
    Photos (so a just-indexed MOV is flagged before the faces/CLIP selects, not
    embedded as a standalone clip) then processes whatever photos are currently
    indexed-but-not-yet-ML'd. When indexing is still going it waits a beat for
    more; once `stop` is set it does final passes until nothing is pending, then
    exits. This is the ONLY caller of the ML stages, so there's never a second
    thread selecting the same `*_done = 0` rows — no duplicate embeddings.

    All progress goes to the module-level `ml_progress`, never the top-level
    `status`, so the indexer's own counters aren't clobbered while both run. Never
    raises: ML is optional, so a model/GPU failure is logged and ends the loop
    without taking indexing down."""
    try:
        from . import clipsearch
        from . import faces as faces_mod
        while True:
            _pair_live_photos()
            ml_progress["state"] = "faces"
            faces_mod.process_pending(ml_progress)
            ml_progress["state"] = "clip"
            clipsearch.process_pending(ml_progress)
            if stop.is_set():
                # A photo may have been indexed during the pass we just ran; loop
                # until a full pass finds nothing left, then we're truly done.
                if not _ml_pending():
                    break
            else:
                ml_progress["state"] = "idle"
                stop.wait(1.0)  # wait for more photos to be indexed (or an early stop)
        ml_progress["state"] = "done"
    except Exception as exc:
        traceback.print_exc()
        ml_progress["state"] = "error"
        print(f"[indexer] ML consumer stopped on error: {exc}")
    finally:
        db.close_conn()


def _index_file(row) -> None:
    path = Path(row["path"])
    kind = media.kind_of(path)
    if kind is None:
        return

    # Read a photo off disk ONCE: hash and decode share the same bytes, so a
    # big archive on a slow HDD/USB isn't read twice per file. Videos keep the
    # head+tail hash from the path — slurping a multi-GB file would be worse.
    data = path.read_bytes() if kind == "photo" else None
    file_hash = media.hash_bytes(data) if data is not None else media.content_hash(path, kind)

    conn = db.get_conn()
    exists = conn.execute("SELECT hash FROM photos WHERE hash = ?", (file_hash,)).fetchone()

    if not exists:
        if kind == "photo":
            meta = media.read_image_meta(path, data=data)
        else:
            meta = media.read_video_meta(path)

        place = None
        if meta["lat"] is not None and meta["lng"] is not None:
            place = geocode.place_name(meta["lat"], meta["lng"])

        if kind == "photo":
            media.make_image_thumbs(path, config.thumbs_dir(), file_hash, data=data)
        else:
            media.make_video_thumbs(path, config.thumbs_dir(), file_hash, meta.get("duration"))

        with db.tx() as c:
            # INSERT OR IGNORE, not INSERT: under the parallel worker pool two
            # files with identical content can both pass the SELECT above before
            # either inserts. hash is the PK, so the second one is a no-op.
            c.execute(
                "INSERT OR IGNORE INTO photos (hash, kind, filename, date_taken, width, height, "
                "  camera, lat, lng, place, duration, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_hash, kind, path.name, meta["date_taken"],
                    meta["width"] or 0, meta["height"] or 0, meta["camera"],
                    meta["lat"], meta["lng"], place, meta.get("duration"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    # Mark the file hashed ONLY now, at the end. If anything above raised, the
    # file keeps content_hash = NULL, so the next scan retries it (and the
    # failure is recorded in failed_files) — the old code set the hash first,
    # which permanently skipped a file that failed thumbnailing, with no photo
    # row and no way to know.
    with db.tx() as c:
        c.execute("UPDATE files SET content_hash = ? WHERE id = ?", (file_hash, row["id"]))


def _index_one(file_id: int):
    """Pool worker: (re)read the file row on this thread's own connection, index
    it, and hand back (row, exception|None) for the main thread to tally. Never
    raises — a broken file must not kill the pool."""
    row = db.get_conn().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        return None, None
    try:
        _index_file(row)
        return row, None
    except Exception as exc:
        return row, exc


def _record_failure(row, exc: Exception) -> int:
    """Record a per-file failure and bump its attempt counter. Returns the new
    attempt count so the caller can log when a file is being given up on. The
    counter is what stops a permanently-broken file (truncated JPG, etc.) from
    being re-queued on every scan pass forever — once it reaches
    `scanner.MAX_INDEX_ATTEMPTS` the scanner stops handing it back, so one bad
    file can't wedge the pipeline in an endless retry loop and block the ML
    stages from starting."""
    with db.tx() as c:
        c.execute("UPDATE files SET attempts = COALESCE(attempts, 0) + 1 WHERE id = ?", (row["id"],))
        attempts = c.execute(
            "SELECT attempts FROM files WHERE id = ?", (row["id"],)
        ).fetchone()["attempts"]
        c.execute(
            "INSERT INTO failed_files (path, folder_id, error, failed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  folder_id = excluded.folder_id, error = excluded.error, failed_at = excluded.failed_at",
            (row["path"], row["folder_id"], str(exc)[:500],
             datetime.now(timezone.utc).isoformat()),
        )
    return attempts


def _clear_resolved_failures() -> None:
    """Drop failure records for files that have since indexed OK (content_hash
    set) or that no longer exist in the files table."""
    with db.tx() as c:
        c.execute(
            "DELETE FROM failed_files WHERE path IN "
            "  (SELECT path FROM files WHERE content_hash IS NOT NULL)"
        )
        c.execute("DELETE FROM failed_files WHERE path NOT IN (SELECT path FROM files)")


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


def _phash_one(photo_hash: str):
    """Pool worker: perceptual hash from the 400px thumbnail. Returns
    (phash, hash) or None. Read-only on the filesystem, no DB access."""
    import imagehash
    from PIL import Image

    thumb, _ = media.thumb_paths(config.thumbs_dir(), photo_hash)
    if not thumb.exists():
        return None
    try:
        with Image.open(thumb) as img:
            return (str(imagehash.phash(img)), photo_hash)
    except OSError:
        return None


def _compute_phashes() -> None:
    """Perceptual hashes for the duplicates screen — from the 400px thumbnail
    (not the original: 100x faster and pHash doesn't need more pixels)."""
    pending = db.get_conn().execute("SELECT hash FROM photos WHERE phash IS NULL").fetchall()
    if not pending:
        return
    # Commit in batches: a transaction per photo means 50k WAL journal writes on
    # a big library. ~200 rows per commit still leaves each committed batch as
    # resume progress if the run is interrupted, without the per-row overhead.
    batch: list[tuple[str, str]] = []  # (phash, hash)

    def flush() -> None:
        if not batch:
            return
        with db.tx() as c:
            c.executemany("UPDATE photos SET phash = ? WHERE hash = ?", batch)
        batch.clear()

    with ThreadPoolExecutor(max_workers=_INDEX_WORKERS) as pool:
        for fut in as_completed([pool.submit(_phash_one, r["hash"]) for r in pending]):
            res = fut.result()
            if res is None:
                continue
            batch.append(res)
            if len(batch) >= 200:
                flush()
    flush()
