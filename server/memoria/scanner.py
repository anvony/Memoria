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


def add_folder(path: str) -> int:
    folder = Path(path)
    if not folder.is_dir():
        raise ValueError(f"Not a folder: {path}")
    drive_id = ensure_drive(folder)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO folders (path, drive_id) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET drive_id = excluded.drive_id",
            (str(folder), drive_id),
        )
        row = conn.execute("SELECT id FROM folders WHERE path = ?", (str(folder),)).fetchone()
    return row["id"]


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
        for dirpath, dirnames, filenames in os.walk(root):
            # skip hidden/system dirs (recycle bin, thumbnail caches…)
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "@"))]
            for name in filenames:
                if Path(name).suffix.lower() not in MEDIA_EXTS:
                    continue
                fpath = str(Path(dirpath) / name)
                seen.add(fpath)
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                row = known.get(fpath)
                if row and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                    if row["status"] != "ok":
                        c.execute("UPDATE files SET status = 'ok' WHERE id = ?", (row["id"],))
                    if row["content_hash"] is None:
                        todo.append(row["id"])  # scanned before but never indexed
                    continue
                if row:
                    c.execute(
                        "UPDATE files SET size = ?, mtime = ?, content_hash = NULL, status = 'ok' "
                        "WHERE id = ?",
                        (st.st_size, st.st_mtime, row["id"]),
                    )
                    todo.append(row["id"])
                else:
                    cur = c.execute(
                        "INSERT INTO files (folder_id, drive_id, path, size, mtime) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (folder_id, drive_id, fpath, st.st_size, st.st_mtime),
                    )
                    todo.append(cur.lastrowid)

        # files that vanished from disk
        for fpath, row in known.items():
            if fpath not in seen:
                c.execute("UPDATE files SET status = 'missing' WHERE id = ?", (row["id"],))
    return todo
