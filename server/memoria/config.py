"""Where Memoria keeps things.

Two-level design:
- A tiny pointer file at %LOCALAPPDATA%\\Memoria\\config.json stores ONLY the
  chosen data directory. It must live in a fixed, well-known location or the
  app couldn't find its own data on the next launch.
- The data directory (user-chosen on first run, e.g. E:\\MemoriaData) holds
  everything that grows: memoria.db, the thumbnail cache, ML model downloads.

This split is what makes "keep the multi-GB cache off C:" possible while the
app itself remains findable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Memoria"
CONFIG_FILE = APP_CONFIG_DIR / "config.json"

# The default suggestion shown on first run (the user can change it).
DEFAULT_DATA_DIR = APP_CONFIG_DIR / "data"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(config: dict) -> None:
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def remembered_data_dir() -> Path | None:
    """The data directory recorded in the pointer file, whether or not it still
    exists. `get_data_dir` deliberately hides a folder that's gone; this is for
    telling the user where it used to be."""
    raw = load_config().get("data_dir")
    return Path(raw) if raw else None


def get_data_dir() -> Path | None:
    """The configured data directory — or None if first-run setup hasn't
    happened, OR the folder it points at no longer exists.

    Treating a vanished folder as "not set up" is deliberate. The pointer file
    outlives the data it points at: the user can delete the folder, or the drive
    it lived on can be unplugged. Without this check the app doesn't merely
    misbehave, it refuses to start — `db.init_db()` runs during FastAPI's
    lifespan and sqlite3 raises "unable to open database file" against the
    missing path, so startup fails and the process exits before serving anything.
    There's then no way back: the first-run screen that would fix it is behind a
    server that won't boot.

    Everything downstream already keys off None (main.lifespan skips init_db,
    _require_setup 503s, /setup reports configured=false), so returning None here
    lands the user on the first-run screen — the state they'd be in with a fresh
    install, which is the only sane place to recover from.

    The recorded path is NOT erased (see `remembered_data_dir`), so first-run can
    offer it straight back: reconnect the drive, click through, and nothing was
    lost."""
    path = remembered_data_dir()
    if path is None or not path.is_dir():
        return None
    return path


DATA_DIR_NAME = "MemoriaData"


def resolve_data_dir(chosen: str | Path) -> Path:
    """Where the data should actually live, given the folder the user picked.

    People pick drive roots — `C:\\` or `D:\\` — and writing memoria.db, thumbs/
    and models/ straight into one scatters our files through a folder that isn't
    ours. So unless the pick already IS a Memoria data folder, nest a
    MemoriaData/ inside it and use that.

    Two cases pass through untouched, and both are the "point it at my existing
    library" flow the README describes for upgrading:
      - the folder already contains a memoria.db, or
      - the folder is already called MemoriaData.
    Without those exceptions, re-selecting an existing library would bury it one
    level deeper on every upgrade."""
    chosen = Path(chosen)
    if (chosen / "memoria.db").exists():
        return chosen
    if chosen.name.lower() == DATA_DIR_NAME.lower():
        return chosen
    return chosen / DATA_DIR_NAME


def set_data_dir(path: str | Path) -> Path:
    data_dir = Path(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "thumbs").mkdir(exist_ok=True)
    (data_dir / "models").mkdir(exist_ok=True)
    config = load_config()
    config["data_dir"] = str(data_dir)
    save_config(config)
    return data_dir


def db_path() -> Path:
    data_dir = get_data_dir()
    assert data_dir is not None, "setup has not run"
    return data_dir / "memoria.db"


def thumbs_dir() -> Path:
    data_dir = get_data_dir()
    assert data_dir is not None, "setup has not run"
    return data_dir / "thumbs"


def models_dir() -> Path:
    data_dir = get_data_dir()
    assert data_dir is not None, "setup has not run"
    return data_dir / "models"


def force_cpu() -> bool:
    """True when MEMORIA_FORCE_CPU is set — pin the ML stages to the CPU.

    onnxruntime-directml advertises DmlExecutionProvider whenever it's
    installed, even on a machine with no usable DirectX 12 device (a VM, or a
    GPU too old for DX12). The session is then created happily and stalls on the
    first real inference, which the user sees as a progress bar frozen at a
    batch boundary with no error message — nothing raised, so nothing to catch.

    There's no dependable in-process way to tell a working DML device from one
    that will hang, so this is the manual override for those machines. Faces and
    semantic search still work; they just run on the CPU."""
    return os.environ.get("MEMORIA_FORCE_CPU", "").strip().lower() in {"1", "true", "yes", "on"}
