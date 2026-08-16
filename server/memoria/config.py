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


def get_data_dir() -> Path | None:
    """The configured data directory, or None if first-run setup hasn't happened."""
    raw = load_config().get("data_dir")
    return Path(raw) if raw else None


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
