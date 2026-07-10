"""Catalogue backup + autosave.

The irreplaceable user data is tiny: names, favorites, albums, hidden/private
flags, and manual EXIF edits — all in memoria.db. Everything else is
rebuildable from the originals. So a "backup" is just a safe copy of that one
file, made with SQLite's online-backup API (a plain file copy of a live WAL
database can be torn). Autosave reuses the same machinery after each index
sync and on quit, keeping a rolling set so a bad edit is always recoverable.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

KEEP = 12  # rolling backups to retain per kind (manual / auto)


def _backup_dir() -> Path:
    bdir = config.get_data_dir() / "backups"
    bdir.mkdir(exist_ok=True)
    return bdir


def _prune(bdir: Path, prefix: str, keep: int) -> None:
    files = sorted(bdir.glob(f"{prefix}*.db"))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)


def make_backup(auto: bool = False) -> Path:
    bdir = _backup_dir()
    prefix = "memoria-auto-" if auto else "memoria-"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{prefix}{stamp}.db"
    dst = sqlite3.connect(dest)
    try:
        db.get_conn().backup(dst)
    finally:
        dst.close()
    _prune(bdir, prefix, KEEP)
    return dest


def _latest_mtime() -> float | None:
    bdir = config.get_data_dir() / "backups"
    files = list(bdir.glob("memoria*.db")) if bdir.exists() else []
    return max((f.stat().st_mtime for f in files), default=None)


def is_dirty() -> bool:
    """Have there been catalogue writes since the last backup? Derived from
    file mtimes so we don't have to thread a dirty flag through every endpoint."""
    last = _latest_mtime()
    if last is None:
        return True
    try:
        return config.db_path().stat().st_mtime > last + 1
    except OSError:
        return False


def status() -> dict:
    bdir = config.get_data_dir() / "backups"
    files = sorted(bdir.glob("memoria*.db")) if bdir.exists() else []
    last = max((f.stat().st_mtime for f in files), default=None)
    return {
        "count": len(files),
        "lastBackupAt": (
            datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None
        ),
        "dirty": is_dirty(),
    }
