"""Per-user config / log / lock locations (no extra dependency)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "DiscordSpeaker"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = Path(os.environ.get("DS_CONFIG_DIR", base / APP))
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP / "Logs"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / APP
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP
    base = Path(os.environ.get("DS_LOG_DIR", base))
    base.mkdir(parents=True, exist_ok=True)
    return base


def lock_dir() -> Path:
    d = config_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d
