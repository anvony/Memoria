"""Walking source folders and keeping the `files` table true.

Change detection is (size, mtime): if both match the DB row, the file is
assumed unchanged and skipped — that's what makes re-index runs take seconds
instead of hours. Content is only hashed when a file is new or changed.

Drives are identified by their volume serial number, not the letter:
E: today can be F: tomorrow, but the serial survives.
"""

from __future__ import annotations

import ctypes
import os
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .media import MEDIA_EXTS

# Give up on a file after this many failed indexing attempts. A truncated /
# corrupt file (e.g. "image file is truncated") fails every pass but keeps
# content_hash = NULL, so without a cap the scanner hands it back on every scan
# and the pipeline retries it forever — never reaching the faces/CLIP stages.
# The count resets if the file's size/mtime change (it was edited/replaced, so
# the old failures no longer apply).
MAX_INDEX_ATTEMPTS = 5


def volume_info(path: Path) -> tuple[str, str]:
    """(volume_serial_hex, label) for the drive containing `path`."""
    root = Path(path.anchor)  # "E:\\"
    buf_label = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)),
        buf_label, len(buf_label),
        ctypes.byref(serial), None, None, None, 0,
    )
    if not ok:
        return (f"unknown-{root.drive.lower()}", root.drive)
    label = buf_label.value or root.drive
    return (f"{serial.value:08x}", label)


def ensure_drive(path: Path) -> int:
    serial, label = volume_info(path)
    now = datetime.now(timezone.utc).isoformat()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO drives (volume_serial, label, mount, online, last_seen) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(volume_serial) DO UPDATE SET "
            "  label = excluded.label, mount = excluded.mount, online = 1, last_seen = excluded.last_seen",
            (serial, label, path.anchor, now),
        )
        row = conn.execute("SELECT id FROM drives WHERE volume_serial = ?", (serial,)).fetchone()
    return row["id"]


def _is_within(inner: Path, outer: Path) -> bool:
    """True if `inner` is `outer` or lives underneath it (case-insensitive on
    Windows). Used to resolve overlapping source folders."""
    try:
        inner.relative_to(outer)
        return True
    except ValueError:
        return False


def _norm(path: Path) -> Path:
    return Path(os.path.normcase(os.path.normpath(str(path))))


def add_folder(path: str) -> int:
    folder = Path(path)
    if not folder.is_dir():
        raise ValueError(f"Not a folder: {path}")
    # Overlapping source folders would index the same files twice, and because
    # files.path is globally unique the second scan collides and kills the run.
    # So a source folder and its parent can never both be in the list — the
    # broader folder always wins. (Paths are normcased so casing/separators
    # can't slip an overlap past this.)
    norm = _norm(folder)
    conn = db.get_conn()
    absorb: list[int] = []  # existing child folders this new folder supersedes
    for row in conn.execute("SELECT id, path FROM folders"):
        other = _norm(Path(row["path"]))
        if other == norm:
            return row["id"]  # exact re-add (maybe different casing): reuse it
        if _is_within(norm, other):
            return row["id"]  # already covered by a broader folder → nothing to add
        if _is_within(other, norm):
            absorb.append(row["id"])  # this folder is their parent → fold them in
    drive_id = ensure_drive(folder)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO folders (path, drive_id) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET drive_id = excluded.drive_id",
            (str(folder), drive_id),
        )
        fid = conn.execute(
            "SELECT id FROM folders WHERE path = ?", (str(folder),)
        ).fetchone()["id"]
        # Re-parent each superseded child folder's files onto this new parent,
        # then drop the now-redundant child folder row. This is metadata only —
        # the files keep their content_hash, thumbnails, favorites and album
        # membership (identity is the content hash, not the path), so absorbing
        # a child costs zero re-indexing. The child's files are already inside
        # this folder on disk, so the parent's own scan would reach them anyway.
        for cid in absorb:
            conn.execute(
                "UPDATE files SET folder_id = ?, drive_id = ? WHERE folder_id = ?",
                (fid, drive_id, cid),
            )
            conn.execute("DELETE FROM folders WHERE id = ?", (cid,))
    return fid


def remove_folder(folder_id: int) -> None:
    """Forget a source folder and its files. Photos whose only files lived here
    disappear from the library (their thumbs stay on disk; a vacuum can clean
    them later). Never touches the actual files."""
    with db.tx() as conn:
        conn.execute("DELETE FROM files WHERE folder_id = ?", (folder_id,))
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        # photos with no remaining files are orphans — drop them from the library
        conn.execute(
            "DELETE FROM photos WHERE hash NOT IN (SELECT DISTINCT content_hash FROM files "
            "WHERE content_hash IS NOT NULL)"
        )


def refresh_drives() -> list[int]:
    """Re-check whether each source folder's drive is currently connected,
    without a full re-scan. Returns the drive ids that just came back online
    (the caller can kick off indexing for them). Letters can change, so a drive
    is 'online' when any folder we index on it is reachable right now."""
    conn = db.get_conn()
    came_online: list[int] = []
    drives = conn.execute("SELECT id, mount, online FROM drives").fetchall()
    with db.tx() as c:
        for d in drives:
            folders = conn.execute(
                "SELECT path FROM folders WHERE drive_id = ?", (d["id"],)
            ).fetchall()
            if folders:
                online = any(Path(f["path"]).exists() for f in folders)
            else:
                online = bool(d["mount"]) and Path(d["mount"]).exists()
            c.execute("UPDATE drives SET online = ? WHERE id = ?", (int(online), d["id"]))
            if online and not d["online"]:
                came_online.append(d["id"])
    return came_online


def _iter_media(root: Path):
    """Recurse with os.scandir, yielding (path, stat_result) for media files.

    os.walk uses scandir underneath but throws the DirEntry objects away, so the
    old code paid a second syscall with os.stat() per file. On Windows the
    directory entry already carries size/mtime, so entry.stat() reads them for
    free — halving syscalls on a 100k-file scan (what makes the 'nothing
    changed' re-scan on startup snappy). Hidden/system dirs (recycle bin,
    thumbnail caches) are skipped by name, same as before.
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            scan = os.scandir(current)
        except OSError:
            continue
        with scan:
            for entry in scan:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not entry.name.startswith((".", "$", "@")):
                            stack.append(entry.path)
                        continue
                except OSError:
                    continue
                if os.path.splitext(entry.name)[1].lower() not in MEDIA_EXTS:
                    continue
                try:
                    yield entry.path, entry.stat()
                except OSError:
                    continue


def scan_folder(folder_id: int) -> list[int]:
    """Sync the files table with disk. Returns file ids needing (re)indexing."""
    conn = db.get_conn()
    folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if folder is None:
        return []
    root = Path(folder["path"])
    if not root.exists():
        # Drive unplugged: mark, don't delete — the photos stay browsable
        with db.tx() as c:
            c.execute("UPDATE files SET status = 'missing' WHERE folder_id = ?", (folder_id,))
            c.execute("UPDATE drives SET online = 0 WHERE id = ?", (folder["drive_id"],))
        return []

    drive_id = ensure_drive(root)
    known = {
        row["path"]: row
        for row in conn.execute("SELECT * FROM files WHERE folder_id = ?", (folder_id,))
    }
    todo: list[int] = []
    seen: set[str] = set()

    with db.tx() as c:
        for fpath, st in _iter_media(root):
            seen.add(fpath)
            row = known.get(fpath)
            if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                if row["status"] != "ok":
                    c.execute("UPDATE files SET status = 'ok' WHERE id = ?", (row["id"],))
                # Scanned before but never indexed — re-queue it, UNLESS it has
                # already failed MAX_INDEX_ATTEMPTS times (a broken file we've
                # given up on, so it stops blocking the rest of the pipeline).
                if row["content_hash"] is None and (row["attempts"] or 0) < MAX_INDEX_ATTEMPTS:
                    todo.append(row["id"])
                continue
            if row:
                # size/mtime changed -> the file was edited or replaced, so any
                # earlier failures no longer apply: clear the attempt counter and
                # give it a fresh set of tries.
                c.execute(
                    "UPDATE files SET size = ?, mtime = ?, content_hash = NULL, status = 'ok', "
                    "  attempts = 0 WHERE id = ?",
                    (st.st_size, st.st_mtime, row["id"]),
                )
                todo.append(row["id"])
            else:
                # ON CONFLICT DO NOTHING: files.path is globally unique, so if a
                # different (overlapping) source folder already owns this exact
                # path the insert would otherwise raise IntegrityError and kill
                # the whole index run. The file is already tracked — and indexed
                # once — under that folder, so leave it there and skip it here.
                cur = c.execute(
                    "INSERT INTO files (folder_id, drive_id, path, size, mtime) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO NOTHING",
                    (folder_id, drive_id, fpath, st.st_size, st.st_mtime),
                )
                if cur.rowcount:
                    todo.append(cur.lastrowid)

        # files that vanished from disk
        for fpath, row in known.items():
            if fpath not in seen:
                c.execute("UPDATE files SET status = 'missing' WHERE id = ?", (row["id"],))
    return todo
