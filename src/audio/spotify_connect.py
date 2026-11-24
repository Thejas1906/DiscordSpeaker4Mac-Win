"""Spotify Connect receiver: the bot shows up in Spotify's "Connect to a device" list.

Runs librespot (MIT, https://github.com/librespot-org/librespot) with its `pipe` backend. librespot decodes
Spotify's own stream and writes raw float32 / 44.1 kHz / stereo PCM to stdout; that goes straight into the
audio pipeline (resample once, buffer, libopus). Nothing is captured from the computer's speakers, so there is
no OS audio routing, no capture permission, and a Bluetooth headset's call mode cannot degrade it.

Requirements: a Spotify **Premium** account. librespot is an unofficial, reverse-engineered client; Spotify's
terms do not formally permit third-party clients, so use it at your own discretion.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

from .models import (BackendUnavailableError, CaptureSource, DeviceUnavailableError, SourceKind)

log = logging.getLogger("DiscordSpeaker.spotify")

SPOTIFY_SOURCE_ID = "spotify:connect"
RATE = 44100
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 4          # float32


MISSING_MESSAGE = ("Spotify Connect needs 'librespot', which was not found. Install it (macOS: brew install librespot; "
                   "Windows/Linux: https://github.com/librespot-org/librespot) or use the packaged DiscordSpeaker app.")


def find_librespot() -> Optional[str]:
    cands = [os.environ.get("DS_LIBRESPOT")]
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        roots = [Path(getattr(sys, "_MEIPASS", exe.parent)), exe.parent, exe.parent.parent / "Frameworks",
                 exe.parent.parent / "Resources"]
        for r in roots:
            cands += [r / "librespot", r / "librespot.exe"]
    cands += [shutil.which("librespot"), "/opt/homebrew/bin/librespot", "/usr/local/bin/librespot"]
    for c in cands:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


class SpotifyConnectState:
    """What the receiver reports about itself (shown in the UI)."""

    def __init__(self):
        self.device_name = ""
        self.now_playing = ""
        self.user = ""
        self.connected = False      # a Spotify client has selected this device at least once this session
        self.log_tail = deque(maxlen=12)

    def as_dict(self) -> dict:
        return {"device_name": self.device_name, "now_playing": self.now_playing, "user": self.user,
                "connected": self.connected}


_LOADING = re.compile(r"Loading <(.+?)> with Spotify URI")
_AUTH = re.compile(r"Authenticated as '(.+?)'")
_CONNECTED = re.compile(r"(Connecting|Connected) to .*(AP|accesspoint)|Authenticated as", re.I)


def build_command(binary: str, device_name: str, cache_dir: str, bitrate: int = 320) -> list:
    return [binary, "--name", device_name, "--backend", "pipe", "--format", "F32", "--bitrate", str(bitrate),
            "--cache", cache_dir, "--disable-audio-cache", "--device-type", "speaker",
            "--initial-volume", "100", "--volume-ctrl", "fixed"]


def run(backend, src: CaptureSource, stop: threading.Event) -> None:
    """Blocking capture body for the Spotify Connect source (called by the backend supervisor)."""
    binary = find_librespot()
    if not binary:
        raise BackendUnavailableError(MISSING_MESSAGE)
    st: SpotifyConnectState = backend.spotify
    st.device_name = backend.device_name
    cache = backend.spotify_cache_dir or os.path.join(tempfile.gettempdir(), "discordspeaker-spotify")
    os.makedirs(cache, exist_ok=True)
    cmd = build_command(binary, backend.device_name, cache)
    log.info("Starting Spotify Connect device '%s'", backend.device_name)
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    backend._proc = proc

    def read_stderr():
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            st.log_tail.append(line)
            m = _LOADING.search(line)
            if m:
                st.now_playing = m.group(1)
                st.connected = True
            m = _AUTH.search(line)
            if m:
                st.user, st.connected = m.group(1), True
            if "Using StdoutSink" in line:
                backend._running()           # receiver is up and discoverable

    threading.Thread(target=read_stderr, name="librespot-log", daemon=True).start()
    carry = b""
    try:
        while not stop.is_set():
            data = proc.stdout.read(RATE // 100 * BYTES_PER_FRAME)       # ~10 ms, blocks while paused
            if not data:
                break
            data = carry + data
            usable = len(data) - len(data) % BYTES_PER_FRAME
            carry = data[usable:]
            if usable:
                backend.pipeline.push(np.frombuffer(data[:usable], dtype="<f4").reshape(-1, CHANNELS), RATE)
        if not stop.is_set():
            tail = " | ".join(list(st.log_tail)[-3:])
            raise DeviceUnavailableError(f"Spotify Connect receiver exited ({proc.poll()}): {tail}"[:300])
    finally:
        st.now_playing = ""
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        for f in (proc.stdout, proc.stderr):
            try:
                f and f.close()
            except Exception:
                pass
        backend._proc = None


def source(available: Optional[bool] = None) -> CaptureSource:
    ok = (find_librespot() is not None) if available is None else available
    return CaptureSource(SPOTIFY_SOURCE_ID, "Spotify Connect (appears in Spotify's device list)", SourceKind.APPLICATION,
                         available=ok, detail="Premium required. Select the bot in Spotify → Connect to a device")
