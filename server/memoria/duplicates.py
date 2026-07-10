"""Duplicate detection and the one write operation in Memoria.

Two kinds of duplicates, found differently:
- Exact: several `files` rows sharing one content hash (the backup-drive copy).
  Free — the indexer already discovered them.
- Visual: different bytes, same pixels (WhatsApp re-compress, resized export).
  Found by perceptual hash: pHash reduces each image to a 64-bit fingerprint
  of its low-frequency structure; small Hamming distance = same picture.

Resolution keeps one file and sends the rest to the Recycle Bin via
send2trash — never a permanent delete (scope §5).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from send2trash import send2trash

from . import db

MAX_DISTANCE = 5  # pHash Hamming distance; ≤5 is conservatively "same picture"


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def ignore_group(group_id: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO dupe_ignored (group_id, ignored_at) VALUES (?, ?)",
            (group_id, datetime.now(timezone.utc).isoformat()),
        )


def find_groups() -> list[dict]:
    """[{id, similarity, date_taken, photos: [hash…], files: [file rows…]}]"""
    conn = db.get_conn()
    ignored = {r["group_id"] for r in conn.execute("SELECT group_id FROM dupe_ignored")}

    groups: list[dict] = []

    # 1) Exact duplicates: one photo, several files
    exact = conn.execute(
        "SELECT content_hash, COUNT(*) AS n FROM files "
        "WHERE content_hash IS NOT NULL AND status = 'ok' "
        "GROUP BY content_hash HAVING n > 1"
    ).fetchall()
    exact_hashes = {r["content_hash"] for r in exact}
    for r in exact:
        photo = conn.execute(
            "SELECT * FROM photos WHERE hash = ?", (r["content_hash"],)
        ).fetchone()
        if photo is None or photo["live_of"] or photo["hidden"] or photo["private"]:
            continue
        groups.append({
            "id": f"dup-exact-{r['content_hash'][:16]}",
            "similarity": 100,
            "date_taken": photo["date_taken"],
            "photo_hashes": [r["content_hash"]],
        })

    # 2) Visual duplicates: different photos with near-identical pHash.
    #    Bucket by pHash prefix first so we don't compare all pairs (O(n²)).
    photos = conn.execute(
        "SELECT hash, phash, date_taken FROM photos "
        "WHERE phash IS NOT NULL AND kind = 'photo' AND live_of IS NULL "
        "AND hidden = 0 AND private = 0"
    ).fetchall()
    buckets: dict[str, list] = defaultdict(list)
    for p in photos:
        buckets[p["phash"][:4]].append(p)

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    similarity: dict[str, int] = {}
    for bucket in buckets.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                d = _hamming(bucket[i]["phash"], bucket[j]["phash"])
                if d <= MAX_DISTANCE:
                    union(bucket[i]["hash"], bucket[j]["hash"])
                    sim = round(100 - d * 100 / 64)
                    root = find(bucket[i]["hash"])
                    similarity[root] = min(similarity.get(root, 100), sim)

    clusters: dict[str, list[str]] = defaultdict(list)
    for p in photos:
        if p["hash"] in parent:
            clusters[find(p["hash"])].append(p["hash"])
    for root, hashes in clusters.items():
        if len(hashes) < 2:
            continue
        # skip if these are all just exact-duplicate photos already reported
        if all(h in exact_hashes for h in hashes):
            pass  # still worth showing: several photos each with several files
        first = conn.execute(
            "SELECT date_taken FROM photos WHERE hash = ?", (hashes[0],)
        ).fetchone()
        groups.append({
            "id": f"dup-visual-{root[:16]}",
            "similarity": similarity.get(root, 95),
            "date_taken": first["date_taken"],
            "photo_hashes": sorted(hashes),
        })

    groups = [g for g in groups if g["id"] not in ignored]
    groups.sort(key=lambda g: g["date_taken"], reverse=True)
    return groups


def files_for_group(group: dict) -> list[dict]:
    conn = db.get_conn()
    out: list[dict] = []
    for h in group["photo_hashes"]:
        rows = conn.execute(
            "SELECT f.*, d.label AS drive_label, p.width, p.height, p.hash AS photo_hash "
            "FROM files f "
            "JOIN photos p ON p.hash = f.content_hash "
            "LEFT JOIN drives d ON d.id = f.drive_id "
            "WHERE f.content_hash = ? AND f.status = 'ok'",
            (h,),
        ).fetchall()
        out.extend(dict(r) for r in rows)
    return out


def resolve(group: dict, keep_file_id: int) -> int:
    """Recycle every file in the group except the keeper. Returns count removed."""
    files = files_for_group(group)
    if not any(f["id"] == keep_file_id for f in files):
        raise ValueError("keeper is not part of this group")

    removed = 0
    keeper_hash = next(f["content_hash"] for f in files if f["id"] == keep_file_id)
    for f in files:
        if f["id"] == keep_file_id:
            continue
        path = Path(f["path"])
        if path.exists():
            send2trash(str(path))  # Recycle Bin, never os.remove
            # send2trash is supposed to MOVE to the Recycle Bin. If the file is
            # somehow still on disk afterwards, it was copied (or the move was
            # blocked) — abort loudly rather than dropping the catalogue row and
            # leaving a stray original that re-indexes as a fresh duplicate.
            if path.exists():
                raise RuntimeError(
                    f"could not move {path} to the Recycle Bin (still on disk); "
                    "left the catalogue untouched"
                )
        with db.tx() as c:
            c.execute("DELETE FROM files WHERE id = ?", (f["id"],))
            # photo row survives only if other files still reference it
            left = c.execute(
                "SELECT COUNT(*) AS n FROM files WHERE content_hash = ?",
                (f["content_hash"],),
            ).fetchone()["n"]
            if left == 0 and f["content_hash"] != keeper_hash:
                # carry favorites over so resolving a duplicate never loses a heart
                fav = c.execute(
                    "SELECT favorite FROM photos WHERE hash = ?", (f["content_hash"],)
                ).fetchone()
                if fav and fav["favorite"]:
                    c.execute("UPDATE photos SET favorite = 1 WHERE hash = ?", (keeper_hash,))
                c.execute("DELETE FROM faces WHERE photo_hash = ?", (f["content_hash"],))
                c.execute("DELETE FROM clip_embeddings WHERE photo_hash = ?", (f["content_hash"],))
                c.execute("DELETE FROM album_photos WHERE photo_hash = ?", (f["content_hash"],))
                c.execute("DELETE FROM photos WHERE hash = ?", (f["content_hash"],))
        removed += 1
    return removed
