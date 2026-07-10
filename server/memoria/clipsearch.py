"""Semantic search: type "beach sunset", get beach sunsets.

How it works (the whole trick in three sentences): CLIP is a model trained
on 400M image-caption pairs to map images AND text into the same 512-dim
vector space, where matching pairs land close together. At index time we
embed every photo once. At search time we embed the *query text* and rank
photos by cosine similarity — no tags, no labels, no per-photo work.

Model: ViT-B-32 (laion2b) — ~600 MB downloaded once, good quality/speed
balance on CPU. The embedding matrix lives in RAM (50k photos ≈ 100 MB)
and brute-force cosine over it takes single-digit milliseconds.
"""

from __future__ import annotations

import threading

import numpy as np
from PIL import Image

from . import config, db, media

# Below this cosine similarity a result is noise, not a match.
MIN_SCORE = 0.20

_lock = threading.Lock()
_model = None
_preprocess = None
_tokenizer = None

# In-memory matrix cache, invalidated whenever new embeddings are written
_matrix: np.ndarray | None = None
_hashes: list[str] = []


def _get_model():
    global _model, _preprocess, _tokenizer
    with _lock:
        if _model is None:
            import open_clip
            _model, _, _preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32",
                pretrained="laion2b_s34b_b79k",
                cache_dir=str(config.models_dir() / "clip"),
            )
            _model.eval()
            _tokenizer = open_clip.get_tokenizer("ViT-B-32")
    return _model, _preprocess, _tokenizer


def process_pending(status: dict) -> None:
    global _matrix
    import torch

    conn = db.get_conn()
    pending = conn.execute(
        "SELECT hash FROM photos WHERE clip_done = 0 AND live_of IS NULL"
    ).fetchall()
    if not pending:
        return
    model, preprocess, _ = _get_model()

    status.update(total=len(pending), done=0)
    batch: list[tuple[str, "torch.Tensor"]] = []

    def flush() -> None:
        global _matrix
        if not batch:
            return
        with torch.no_grad():
            tensors = torch.stack([t for _, t in batch])
            embs = model.encode_image(tensors)
            embs = embs / embs.norm(dim=-1, keepdim=True)
        with db.tx() as c:
            for (h, _), emb in zip(batch, embs):
                c.execute(
                    "INSERT OR REPLACE INTO clip_embeddings (photo_hash, embedding) VALUES (?, ?)",
                    (h, emb.numpy().astype(np.float32).tobytes()),
                )
                c.execute("UPDATE photos SET clip_done = 1 WHERE hash = ?", (h,))
        batch.clear()
        _matrix = None  # invalidate the search cache

    for i, row in enumerate(pending):
        status.update(done=i, current=row["hash"][:12])
        thumb, _ = media.thumb_paths(config.thumbs_dir(), row["hash"])
        if not thumb.exists():
            with db.tx() as c:
                c.execute("UPDATE photos SET clip_done = 1 WHERE hash = ?", (row["hash"],))
            continue
        try:
            with Image.open(thumb) as img:
                batch.append((row["hash"], preprocess(img.convert("RGB"))))
        except OSError:
            continue
        if len(batch) >= 16:
            flush()
    flush()
    status.update(done=len(pending), current=None)


def _load_matrix() -> tuple[np.ndarray, list[str]]:
    global _matrix, _hashes
    if _matrix is None:
        rows = db.get_conn().execute(
            "SELECT photo_hash, embedding FROM clip_embeddings"
        ).fetchall()
        _hashes = [r["photo_hash"] for r in rows]
        if rows:
            _matrix = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        else:
            _matrix = np.zeros((0, 512), dtype=np.float32)
    return _matrix, _hashes


def search(text: str, limit: int = 200) -> list[str]:
    """Photo hashes ranked by semantic match to `text`."""
    import torch

    matrix, hashes = _load_matrix()
    if matrix.shape[0] == 0:
        return []
    model, _, tokenizer = _get_model()
    with torch.no_grad():
        tokens = tokenizer([text])
        query = model.encode_text(tokens)
        query = (query / query.norm(dim=-1, keepdim=True)).numpy().astype(np.float32)[0]

    scores = matrix @ query  # embeddings are normalized → dot product = cosine
    order = np.argsort(-scores)[:limit]
    return [hashes[i] for i in order if scores[i] >= MIN_SCORE]
