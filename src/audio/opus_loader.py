"""Locate libopus, including the copy bundled into packaged builds."""
from __future__ import annotations

import ctypes.util
import glob
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("DiscordSpeaker.opus")


def _candidates():
    env = os.environ.get("DS_OPUS_LIBRARY")
    if env:
        yield env
    if getattr(sys, "frozen", False):
        roots = [Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)), Path(sys.executable).parent,
                 Path(sys.executable).parent.parent / "Frameworks", Path(sys.executable).parent.parent / "Resources"]
        for r in roots:
            for pat in ("libopus*.dylib", "libopus*.so*", "opus*.dll", "libopus*.dll"):
                yield from glob.glob(str(r / pat))
    found = ctypes.util.find_library("opus")
    if found:
        yield found
    if sys.platform == "darwin":
        yield from ("/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib")


def ensure_opus_loaded() -> bool:
    import discord.opus as dopus
    if dopus.is_loaded():
        return True
    for c in _candidates():
        try:
            dopus.load_opus(c)
            log.info("Loaded libopus from %s", c)
            return True
        except Exception:
            continue
    try:
        dopus._load_default()   # discord.py's own search (bundled DLL on Windows wheels)
    except Exception as e:
        log.warning("Could not load libopus: %s", e)
    return dopus.is_loaded()
