"""SQLite access layer.

Design decisions that matter (see PROJECT-SCOPE.md §5):

- WAL mode: readers never block the writer, so the UI stays responsive while
  the indexer is writing thousands of rows.
- `files` and `photos` are separate tables. A photo is identified by the
  hash of its *content*; the same shot on C: and on the backup drive is one
  `photos` row with two `files` rows. That's what makes dedup and
  drive-letter changes survivable.
- Everything in `photos` except `favorite` is rebuildable from the originals.
  The irreplaceable user data — names, favorites, albums — is tiny and easy
  to back up (it's just memoria.db).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
  id            INTEGER PRIMARY KEY,
  volume_serial TEXT UNIQUE,
  label         TEXT,
  mount         TEXT,               -- last seen root, e.g. "E:\\"
  online        INTEGER DEFAULT 1,
  last_seen     TEXT
);

CREATE TABLE IF NOT EXISTS folders (
  id       INTEGER PRIMARY KEY,
  path     TEXT UNIQUE NOT NULL,    -- folder the user asked us to index
  drive_id INTEGER REFERENCES drives(id)
);

CREATE TABLE IF NOT EXISTS files (
  id           INTEGER PRIMARY KEY,
  folder_id    INTEGER REFERENCES folders(id),
  drive_id     INTEGER REFERENCES drives(id),
  path         TEXT UNIQUE NOT NULL,
  size         INTEGER NOT NULL,
  mtime        REAL NOT NULL,
  content_hash TEXT,                -- NULL until hashed; FK -> photos.hash
  status       TEXT DEFAULT 'ok'    -- ok | missing (drive unplugged / file gone)
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);

CREATE TABLE IF NOT EXISTS photos (
  hash       TEXT PRIMARY KEY,      -- content hash = identity
  kind       TEXT NOT NULL,         -- photo | video
  filename   TEXT NOT NULL,
  date_taken TEXT NOT NULL,         -- ISO 8601 (EXIF, else file mtime)
  width      INTEGER NOT NULL,
  height     INTEGER NOT NULL,
  camera     TEXT,
  lat        REAL,
  lng        REAL,
  place      TEXT,                  -- reverse-geocoded "City, Region"
  duration   REAL,                  -- seconds, videos only
  live_of    TEXT,                  -- hash of the still this MOV belongs to (Live Photo)
  favorite   INTEGER DEFAULT 0,     -- user data, NOT rebuildable
  phash      TEXT,                  -- perceptual hash for duplicate detection
  faces_done INTEGER DEFAULT 0,     -- ML stage progress flags: an interrupted
  clip_done  INTEGER DEFAULT 0,     -- run resumes exactly where it stopped
  indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_photos_date ON photos(date_taken DESC);
CREATE INDEX IF NOT EXISTS idx_photos_place ON photos(place);

CREATE TABLE IF NOT EXISTS people (
  id         INTEGER PRIMARY KEY,
  name       TEXT,                  -- NULL until the user names the cluster
  cover_face INTEGER
);

CREATE TABLE IF NOT EXISTS faces (
  id         INTEGER PRIMARY KEY,
  photo_hash TEXT NOT NULL REFERENCES photos(hash),
  x REAL, y REAL, w REAL, h REAL,   -- bounding box, fractions of image size
  embedding  BLOB NOT NULL,         -- float32[512] from insightface
  person_id  INTEGER REFERENCES people(id)
);
CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_hash);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS albums (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_photos (
  album_id   INTEGER NOT NULL REFERENCES albums(id),
  photo_hash TEXT NOT NULL REFERENCES photos(hash),
  added_at   TEXT NOT NULL,
  PRIMARY KEY (album_id, photo_hash)
);

CREATE TABLE IF NOT EXISTS clip_embeddings (
  photo_hash TEXT PRIMARY KEY REFERENCES photos(hash),
  embedding  BLOB NOT NULL          -- float32[512] from open_clip
);

CREATE TABLE IF NOT EXISTS kv (     -- small settings: home override, etc.
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS dupe_ignored (   -- duplicate groups the user dismissed
  group_id TEXT PRIMARY KEY,
  ignored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_overrides (  -- user edits to auto-detected trips
  trip_id    TEXT PRIMARY KEY,
  name       TEXT,                 -- custom title (NULL = keep auto place name)
  place      TEXT,                 -- custom location label
  cover_hash TEXT                  -- chosen cover photo (NULL = auto first four)
);

CREATE TABLE IF NOT EXISTS trip_excludes (  -- photos the user removed from an
  trip_id    TEXT NOT NULL,        -- auto-detected trip (trips are derived, so a
  photo_hash TEXT NOT NULL,        -- removal has to be remembered separately)
  PRIMARY KEY (trip_id, photo_hash)
);

CREATE TABLE IF NOT EXISTS trip_hidden (   -- whole trips the user deleted; trips
  trip_id    TEXT PRIMARY KEY           -- are auto-derived, so a deletion is only
);                                       -- remembered here and subtracted each scan

CREATE TABLE IF NOT EXISTS failed_files (  -- files the indexer couldn't process,
  path       TEXT PRIMARY KEY,     -- so a user can see WHICH photos didn't make
  folder_id  INTEGER,              -- it in and why, instead of only a console
  error      TEXT NOT NULL,        -- line. Cleared automatically once the file
  failed_at  TEXT NOT NULL         -- indexes successfully on a later run.
);
"""

_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread — sqlite3 connections aren't thread-safe.

    Also reconnects transparently if the data directory changed underneath us
    (the 'change data location' setting), so cached per-thread connections
    never keep pointing at the old database file."""
    conn = getattr(_local, "conn", None)
    path = str(config.db_path())
    if conn is not None and getattr(_local, "path", None) != path:
        conn.close()
        conn = None
    if conn is None:
        conn = _connect()
        _local.conn = conn
        _local.path = path
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def tx():
    """Write transaction: commit on success, roll back on error."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Columns added after the first release. CREATE TABLE IF NOT EXISTS doesn't
# touch existing tables, so late additions must be ALTERed in — checked
# against PRAGMA table_info so the migration is idempotent.
MIGRATIONS = [
    ("photos", "hidden", "ALTER TABLE photos ADD COLUMN hidden INTEGER DEFAULT 0"),
    ("photos", "private", "ALTER TABLE photos ADD COLUMN private INTEGER DEFAULT 0"),
    # A face the user marked "not this person": detached from its cluster and
    # kept out of clustering so it can't silently rejoin.
    ("faces", "detached", "ALTER TABLE faces ADD COLUMN detached INTEGER DEFAULT 0"),
    # A cover image uploaded from disk for a trip (filename under
    # data_dir/trip_covers/); takes precedence over a chosen trip photo.
    ("trip_overrides", "cover_image", "ALTER TABLE trip_overrides ADD COLUMN cover_image TEXT"),
]


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(ddl)


def kv_get(key: str) -> str | None:
    row = get_conn().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
