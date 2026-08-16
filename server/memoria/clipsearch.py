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

# M4: the visual encoder runs on the DirectML GPU through ONNX Runtime (the same
# onnxruntime-directml the face stage uses — no new dependency). None until built;
# _onnx_failed latches so we don't retry a broken export every flush and just use
# the torch CPU path. The TEXT encoder stays on torch CPU: it runs once per search
# query, not per photo, so it isn't worth converting.
_ONNX_VISUAL_FILE = "visual_vitb32.onnx"
_onnx_session = None
_onnx_failed = False

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


def _export_visual_onnx(path) -> None:
    """One-time export of open_clip's visual tower to ONNX (fixed 224x224 input,
    a dynamic batch axis), cached in the data dir next to the weights — same
    'build once, reuse forever' pattern as the model download. encode_image(x) is
    just self.visual(x), so the ONNX output equals the torch encoder's (we
    L2-normalize either way), keeping new embeddings comparable to any already
    stored by the torch path."""
    import torch

    model, _, _ = _get_model()
    path.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch.zeros(1, 3, 224, 224, dtype=torch.float32)
    tmp = path.with_name(path.name + ".tmp")
    with torch.no_grad():
        torch.onnx.export(
            model.visual, dummy, str(tmp),
            input_names=["pixel_values"], output_names=["image_embeds"],
            dynamic_axes={"pixel_values": {0: "batch"}, "image_embeds": {0: "batch"}},
            opset_version=17,
        )
    tmp.replace(path)  # atomic: a crash mid-export never leaves a half-written file


def _get_onnx_visual():
    """ONNX Runtime session for the CLIP visual encoder on the DirectML GPU (M4).
    Returns None — and the caller falls back to the torch CPU path — whenever
    DirectML isn't available or the export/session fails, so the CLIP stage is
    never broken by this, only accelerated when it can be. `_onnx_failed` latches
    so a failure isn't re-attempted on every batch."""
    global _onnx_session, _onnx_failed
    if _onnx_session is not None:
        return _onnx_session
    if _onnx_failed:
        return None
    with _lock:
        if _onnx_session is None and not _onnx_failed:
            try:
                import onnxruntime as ort
                if config.force_cpu():
                    print("[clip] MEMORIA_FORCE_CPU set — using the CPU path.")
                    _onnx_failed = True
                    return None
                if "DmlExecutionProvider" not in ort.get_available_providers():
                    _onnx_failed = True  # CPU-only box: the torch path is equivalent
                    return None
                path = config.models_dir() / "clip" / _ONNX_VISUAL_FILE
                if not path.exists():
                    _export_visual_onnx(path)
                _onnx_session = ort.InferenceSession(
                    str(path),
                    providers=["DmlExecutionProvider", "CPUExecutionProvider"],
                )
            except Exception as exc:
                print(f"[clip] GPU (ONNX/DirectML) unavailable, using CPU: {exc}")
                _onnx_failed = True
                return None
    return _onnx_session


def process_pending(status: dict, base: int = 0) -> int:
    """Embed every photo still missing a CLIP vector. Returns how many were
    processed. `base` is the running total across cycles — see the matching
    explanation in faces.process_pending; without it the bar restarts at zero
    every pass while indexing is still feeding new photos in."""
    global _matrix
    import torch

    conn = db.get_conn()
    pending = conn.execute(
        "SELECT hash FROM photos WHERE clip_done = 0 AND live_of IS NULL"
    ).fetchall()
    if not pending:
        status.update(total=base, done=base, current=None)
        return 0
    model, preprocess, _ = _get_model()

    status.update(total=base + len(pending), done=base)
    batch: list[tuple[str, "torch.Tensor"]] = []

    def flush() -> None:
        global _matrix
        if not batch:
            return
        session = _get_onnx_visual()
        if session is not None:  # M4: GPU (DirectML) via ONNX
            arr = np.stack([t.numpy() for _, t in batch]).astype(np.float32)
            out = session.run(["image_embeds"], {"pixel_values": arr})[0]
            out = out / np.linalg.norm(out, axis=-1, keepdims=True)
            embs = [row.astype(np.float32) for row in out]
        else:                    # CPU fallback (no GPU, or export failed)
            with torch.no_grad():
                tensors = torch.stack([t for _, t in batch])
                enc = model.encode_image(tensors)
                enc = enc / enc.norm(dim=-1, keepdim=True)
            embs = [row.numpy().astype(np.float32) for row in enc]
        with db.tx() as c:
            for (h, _), emb in zip(batch, embs):
                c.execute(
                    "INSERT OR REPLACE INTO clip_embeddings (photo_hash, embedding) VALUES (?, ?)",
                    (h, emb.tobytes()),
                )
                c.execute("UPDATE photos SET clip_done = 1 WHERE hash = ?", (h,))
        batch.clear()
        _matrix = None  # invalidate the search cache

    for i, row in enumerate(pending):
        status.update(done=base + i, current=row["hash"][:12])
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
        if len(batch) >= 32:  # bigger batch amortises per-call overhead on CPU;
            flush()           # 224x224 inputs make the memory cost trivial
    flush()
    status.update(done=base + len(pending), current=None)
    return len(pending)


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
