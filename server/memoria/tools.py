"""Locating the external command-line tools Memoria shells out to.

ffmpeg/ffprobe (video posters + metadata) and ExifTool (the opt-in original
metadata write) are native binaries, not pip packages. `setup.ps1` downloads
them into `server/tools/` so a first-time user needs nothing but the installer
and setup.ps1 — no manual winget/scoop step. We look there first, then fall
back to anything already on PATH (a dev's own install), then an env override.

Keeping this in one place means every caller resolves tools the same way.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# server/tools/ — a sibling of the memoria package, next to where the venv lives.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"

# Per-tool escape hatch, mostly for tests and unusual installs.
_ENV_OVERRIDE = {
    "exiftool": "MEMORIA_EXIFTOOL",
    "ffmpeg": "MEMORIA_FFMPEG",
    "ffprobe": "MEMORIA_FFPROBE",
}


def find(name: str) -> str | None:
    """Absolute path to the tool, or None if it can't be located anywhere."""
    env = _ENV_OVERRIDE.get(name)
    if env:
        override = os.environ.get(env)
        if override and Path(override).exists():
            return override
    bundled = _TOOLS_DIR / f"{name}.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which(name)


def path_or_name(name: str) -> str:
    """Resolved path, or the bare name as a last resort so the OS can still try
    PATH at exec time (and the error message stays recognisable)."""
    return find(name) or name
