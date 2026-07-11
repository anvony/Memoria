"""Face detection, embeddings, and incremental clustering.

Model: insightface "buffalo_l" — detector (SCRFD) + recognizer (ArcFace).
Downloaded once (~300 MB) into the data dir. Runs on the GPU through
DirectML, which works on any Windows GPU without installing CUDA.

Detection runs on the 1600px preview, not the original: plenty of pixels for
faces, and it skips a second slow HEIC decode.

Clustering is greedy and incremental: each new face joins the existing
person whose average embedding (centroid) is most similar, or founds a new
person if nothing is close enough. Unlike batch algorithms (HDBSCAN), this
never reshuffles the people you've already named when new photos arrive.
The price: it can oversplit (one person becoming two clusters) — that's what
the merge endpoint is for.
"""

from __future__ import annotations

import queue
import threading

import numpy as np
from PIL import Image

from . import config, db, media

# Cosine similarity threshold for "same person". ArcFace same-person pairs
# typically score 0.5-0.8; unrelated people < 0.3. 0.45 is a safe middle.
SIMILARITY = 0.45
MIN_FACE_PX = 40      # smaller crops embed too poorly to cluster reliably
MIN_DET_SCORE = 0.55

# M6 knob: longest side (px) of the image handed to the detector. insightface
# resizes to det_size=(640, 640) internally regardless, so the detection working
# resolution is UNCHANGED by this — it only trims the cost of decoding/resizing a
# larger array. Default 1600 == the preview size, i.e. a no-op. Lowering it
# (~800-1000) speeds the decode but shrinks the crops the recognizer embeds,
# which can hurt clustering of small/background faces — validate recall with
# tests/bench_ml.py before lowering. See docs/TODO M6.
DET_MAX_SIDE = 1600
# How many decoded previews the prefetch thread may buffer ahead of the GPU.
_DECODE_QUEUE = 8

_app = None
# Guards lazy model construction: the indexer warms the model on a side thread
# (see indexer._warm_ml_models) while the faces stage may also call in, so two
# threads can race here. Without the lock both see `_app is None` and build the
# ~300 MB model twice.
_app_lock = threading.Lock()


def _get_app():
    global _app
    with _app_lock:
        if _app is None:
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(
                name="buffalo_l",
                root=str(config.models_dir() / "insightface"),
                providers=["DmlExecutionProvider", "CPUExecutionProvider"],
            )
            app.prepare(ctx_id=0, det_size=(640, 640))
            _log_active_provider(app)
            _app = app
    return _app


def active_providers() -> list[str]:
    """The onnxruntime execution providers the face session is actually bound to
    — the definitive 'am I on GPU or CPU?' answer. Reads the live session; falls
    back to the build's advertised providers if the internals shift."""
    app = _get_app()
    try:
        for model in app.models.values():
            session = getattr(model, "session", None)
            if session is not None:
                return list(session.get_providers())
    except Exception:
        pass
    import onnxruntime as ort
    return ort.get_available_providers()


def _log_active_provider(app) -> None:
    """Print whether faces run on the GPU (DirectML) or the CPU. This is the M0
    guard: `onnxruntime-directml` gets silently clobbered by the plain
    `onnxruntime` that insightface pulls in, dropping DmlExecutionProvider and
    forcing CPU. Logging the real provider makes that regression visible instead
    of a silent 5-10x slowdown."""
    try:
        providers: list[str] = []
        for model in app.models.values():
            session = getattr(model, "session", None)
            if session is not None:
                providers = list(session.get_providers())
                break
        on_gpu = "DmlExecutionProvider" in providers
        where = "GPU (DirectML)" if on_gpu else "CPU"
        print(f"[faces] face model ready — running on {where}. providers={providers}")
        if not on_gpu:
            print(
                "[faces] NOTE: DirectML unavailable, so faces run on the CPU. To "
                "restore GPU: pip uninstall -y onnxruntime onnxruntime-directml && "
                "pip install onnxruntime-directml"
            )
    except Exception:
        pass  # visibility only — never let logging break indexing


def _decode_preview(photo_hash: str):
    """Load a photo's preview thumbnail as an RGB numpy array for detection,
    downscaled to DET_MAX_SIDE (M6). Returns None if the preview is missing or
    unreadable — the photo is still marked done, there's just nothing to detect.
    Runs on the prefetch thread: filesystem + PIL/numpy only, no DB, so it's safe
    off the main thread."""
    _, preview = media.thumb_paths(config.thumbs_dir(), photo_hash)
    if not preview.exists():
        return None
    try:
        with Image.open(preview) as img:
            img = img.convert("RGB")
            longest = max(img.size)
            if longest > DET_MAX_SIDE:
                scale = DET_MAX_SIDE / longest
                img = img.resize(
                    (round(img.width * scale), round(img.height * scale)), Image.BILINEAR
                )
            return np.array(img)
    except Exception:
        return None


def process_pending(status: dict) -> None:
    conn = db.get_conn()
    pending = conn.execute(
        "SELECT hash FROM photos WHERE faces_done = 0 AND live_of IS NULL"
    ).fetchall()
    if not pending:
        return
    app = _get_app()
    centroids = _load_centroids()
    status.update(total=len(pending), done=0)

    # M2 producer/consumer: a prefetch thread decodes+downscales the next previews
    # (CPU-bound) while THIS thread runs the detector on the GPU, so the GPU isn't
    # left idle during every decode. Detection, clustering and DB writes all stay
    # on this one thread — the insightface session and the per-thread SQLite
    # connection aren't shared across threads. The queue is bounded, so memory
    # stays flat no matter how far ahead the decoder gets.
    q: "queue.Queue" = queue.Queue(maxsize=_DECODE_QUEUE)
    _END = object()

    def _producer() -> None:
        for r in pending:
            q.put((r["hash"], _decode_preview(r["hash"])))
        q.put(_END)

    threading.Thread(target=_producer, name="memoria-faces-decode", daemon=True).start()

    # M3: buffer face rows and faces_done flags, flush in ~200-row batches (like
    # pHash/CLIP already do) instead of a transaction per photo. Face rows and the
    # faces_done flag for the same photos commit in ONE transaction, so an
    # interrupted run never leaves a photo "done" with its faces missing (or vice
    # versa) — it just re-detects that batch cleanly on resume.
    face_rows: list[tuple] = []   # (photo_hash, x, y, w, h, embedding, person_id)
    done_hashes: list[str] = []   # photos processed (including zero-face ones)

    def flush() -> None:
        if not face_rows and not done_hashes:
            return
        with db.tx() as c:
            if face_rows:
                c.executemany(
                    "INSERT INTO faces (photo_hash, x, y, w, h, embedding, person_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    face_rows,
                )
            c.executemany(
                "UPDATE photos SET faces_done = 1 WHERE hash = ?",
                [(h,) for h in done_hashes],
            )
        face_rows.clear()
        done_hashes.clear()

    processed = 0
    while True:
        item = q.get()
        if item is _END:
            break
        photo_hash, rgb = item
        status.update(done=processed, current=photo_hash[:12])
        processed += 1
        if rgb is not None:
            try:
                detected = app.get(rgb[:, :, ::-1])  # insightface expects BGR
            except Exception:
                detected = []
            h, w = rgb.shape[:2]
            for face in detected:
                x1, y1, x2, y2 = face.bbox
                if face.det_score < MIN_DET_SCORE or (x2 - x1) < MIN_FACE_PX:
                    continue
                emb = face.normed_embedding.astype(np.float32)
                person_id = _assign(emb, centroids)
                # float(...) is load-bearing: x1/w etc. are numpy.float32 scalars,
                # and SQLite can't adapt a numpy type — it silently stores the raw
                # 4 bytes as a BLOB, so face["w"] later reads back as bytes and
                # every geometry read crashes. Coerce to a native float here.
                face_rows.append((
                    photo_hash,
                    float(max(0.0, x1 / w)), float(max(0.0, y1 / h)),
                    float(min(1.0, (x2 - x1) / w)), float(min(1.0, (y2 - y1) / h)),
                    emb.tobytes(), person_id,
                ))
        done_hashes.append(photo_hash)
        if len(face_rows) >= 200 or len(done_hashes) >= 200:
            flush()
    flush()
    _cleanup_orphan_people()
    status.update(done=len(pending), current=None)


def _num(v) -> float:
    """Read a bbox coordinate as a float. Rows written before the float() fix
    stored numpy scalars as raw 4-byte BLOBs; decode those defensively so old
    catalogues don't need a rebuild to show avatars."""
    if isinstance(v, (bytes, bytearray)):
        return float(np.frombuffer(v, dtype=np.float32)[0])
    return float(v)


def _load_centroids() -> dict[int, tuple[np.ndarray, int]]:
    """person_id -> (mean embedding, face count)."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT person_id, embedding FROM faces WHERE person_id IS NOT NULL AND detached = 0"
    )
    sums: dict[int, list] = {}
    for r in rows:
        emb = np.frombuffer(r["embedding"], dtype=np.float32)
        entry = sums.setdefault(r["person_id"], [np.zeros_like(emb), 0])
        entry[0] = entry[0] + emb
        entry[1] += 1
    return {pid: (s / n, n) for pid, (s, n) in sums.items()}


def _assign(emb: np.ndarray, centroids: dict[int, tuple[np.ndarray, int]]) -> int:
    best_id, best_sim = None, SIMILARITY
    for pid, (centroid, _) in centroids.items():
        denom = np.linalg.norm(centroid)
        if denom == 0:
            continue
        sim = float(np.dot(emb, centroid / denom))
        if sim > best_sim:
            best_id, best_sim = pid, sim

    if best_id is None:
        # New cluster. Its row must be committed now — we need the id for the face
        # row we're about to buffer — so a person is created in its own small
        # transaction, separate from the batched face flush. An interrupted run
        # can therefore leave a person with no faces yet; _cleanup_orphan_people
        # drops those at the end.
        with db.tx() as c:
            best_id = c.execute("INSERT INTO people (name) VALUES (NULL)").lastrowid
        centroids[best_id] = (emb.copy(), 1)
    else:
        centroid, n = centroids[best_id]
        centroids[best_id] = ((centroid * n + emb) / (n + 1), n + 1)
    return best_id


def _cleanup_orphan_people() -> None:
    """Drop unnamed clusters that ended up with zero faces — only possible when a
    batched run is interrupted after a person row commits but before its face rows
    flush. Named people and cover-pinned people are never touched, so this can't
    delete anything the user cares about."""
    with db.tx() as c:
        c.execute(
            "DELETE FROM people WHERE name IS NULL AND cover_face IS NULL "
            "AND id NOT IN (SELECT person_id FROM faces WHERE person_id IS NOT NULL)"
        )


def merge_people(from_id: int, into_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE faces SET person_id = ? WHERE person_id = ?", (into_id, from_id))
        c.execute("DELETE FROM people WHERE id = ?", (from_id,))


def _best_face(person_id: int):
    """The face used as this person's key photo: the user's chosen cover if set,
    otherwise the largest (usually clearest) face in their cluster."""
    conn = db.get_conn()
    cover = conn.execute("SELECT cover_face FROM people WHERE id = ?", (person_id,)).fetchone()
    if cover and cover["cover_face"]:
        face = conn.execute(
            "SELECT * FROM faces WHERE id = ? AND person_id = ?",
            (cover["cover_face"], person_id),
        ).fetchone()
        if face is not None:
            return face
    return conn.execute(
        "SELECT f.* FROM faces f JOIN photos p ON p.hash = f.photo_hash "
        "WHERE f.person_id = ? ORDER BY f.w * f.h DESC LIMIT 1",
        (person_id,),
    ).fetchone()


def _crop_face(face, size: int = 200) -> Image.Image | None:
    _, preview = media.thumb_paths(config.thumbs_dir(), face["photo_hash"])
    if not preview.exists():
        return None
    with Image.open(preview) as img:
        img = img.convert("RGB")
        w, h = img.size
        fx, fy, fw, fh = _num(face["x"]), _num(face["y"]), _num(face["w"]), _num(face["h"])
        # expand the tight bbox ~80% so the crop looks like a portrait, not a mask
        cx = (fx + fw / 2) * w
        cy = (fy + fh / 2) * h
        side = max(fw * w, fh * h) * 1.8
        box = (
            int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
            int(min(w, cx + side / 2)), int(min(h, cy + side / 2)),
        )
        crop = img.crop(box)
        crop.thumbnail((size, size), Image.LANCZOS)
        return crop


def avatar_for(person_id: int) -> Image.Image | None:
    """Crop of this person's key face, generated on demand and cached by the API."""
    face = _best_face(person_id)
    if face is None:
        return None
    return _crop_face(face)


def face_crop(face_id: int) -> Image.Image | None:
    """Portrait crop of one specific detected face (for the in-image faces panel)."""
    face = db.get_conn().execute("SELECT * FROM faces WHERE id = ?", (face_id,)).fetchone()
    if face is None:
        return None
    return _crop_face(face)


def set_cover(person_id: int, face_id: int) -> bool:
    """Pin a specific face as the person's key photo. The face must belong to
    them. Returns False if it doesn't."""
    conn = db.get_conn()
    owns = conn.execute(
        "SELECT 1 FROM faces WHERE id = ? AND person_id = ?", (face_id, person_id)
    ).fetchone()
    if owns is None:
        return False
    with db.tx() as c:
        c.execute("UPDATE people SET cover_face = ? WHERE id = ?", (face_id, person_id))
    return True


def detach_face(face_id: int) -> None:
    """The user said this face isn't the person it was clustered under. Unlink
    it and flag it so re-clustering never silently reattaches it."""
    with db.tx() as c:
        c.execute("UPDATE faces SET person_id = NULL, detached = 1 WHERE id = ?", (face_id,))


def delete_person(person_id: int) -> None:
    """Remove a whole face group — for the strangers a photo library inevitably
    collects (a passer-by in the background, a face on a poster). We don't delete
    the face rows: we detach them (person_id NULL, detached = 1) so a later rescan
    never rebuilds the same junk group, then drop the person. The originals are
    untouched; this only affects the People grouping."""
    with db.tx() as c:
        c.execute(
            "UPDATE faces SET person_id = NULL, detached = 1 WHERE person_id = ?",
            (person_id,),
        )
        c.execute("DELETE FROM people WHERE id = ?", (person_id,))
