"""PulseAudio / PipeWire-pulse helpers (pactl based). Behaviour preserved from the original bot.py.

All functions accept optional collaborators so the legacy `bot` module (and its tests)
can keep patching `bot.pulse_list`, `bot.is_sink_active`, etc.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Callable, Optional

log = logging.getLogger("DiscordSpeaker")

SINK_PROPS = 'device.description="Discord Speaker"'
# Our virtual sink is created natively at 48 kHz stereo so the monitor needs no server-side resampling.
SINK_FORMAT_ARGS = ["rate=48000", "channels=2", "channel_map=front-left,front-right"]


def pulse_list(kind: str) -> list:
    """Read structured PulseAudio data, independent of locale and numeric sink IDs."""
    res = subprocess.run(["pactl", "--format=json", "list", kind],
                         capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def is_sink_active(sink_name: str, pulse_list_fn: Callable = None) -> bool:
    pulse_list_fn = pulse_list_fn or pulse_list
    try:
        return any(sink["name"] == sink_name for sink in pulse_list_fn("sinks"))
    except Exception as e:
        log.error(f"Error checking sink status: {e}")
        return False


def ensure_sink_active(sink_name: str, script_path: Optional[str] = None,
                       is_active: Callable = None) -> bool:
    is_active = is_active or (lambda n: is_sink_active(n))
    if is_active(sink_name):
        return True
    if script_path and os.path.isfile(script_path):
        log.info(f"Running '{script_path} setup' to activate audio sink...")
        res = subprocess.run([script_path, "setup"], capture_output=True, text=True)
        if res.returncode == 0 and is_active(sink_name):
            log.info(f"Successfully activated sink '{sink_name}'.")
            return True
    # Quoting differs between PipeWire-pulse (first form) and a real PulseAudio daemon (second form).
    for props in (SINK_PROPS, '"device.description=\'Discord Speaker\'"'):
        for fmt in (SINK_FORMAT_ARGS, []):
            try:
                subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={sink_name}",
                                f"sink_properties={props}", *fmt], check=True, capture_output=True)
                return is_active(sink_name)
            except Exception as e:
                last = e
    log.error(f"Failed to create sink: {last}")
    return False


def get_routed_apps(sink_name: str, pulse_list_fn: Callable = None) -> list:
    pulse_list_fn = pulse_list_fn or pulse_list
    try:
        sink_ids = {s["index"] for s in pulse_list_fn("sinks") if s["name"] == sink_name}
        apps = []
        for stream in pulse_list_fn("sink-inputs"):
            if stream["sink"] in sink_ids:
                props = stream.get("properties", {})
                name = props.get("application.name") or props.get("application.process.binary") or "Unknown app"
                if name not in apps:
                    apps.append(name)
        return apps
    except Exception as e:
        log.error(f"Error querying sink inputs: {e}")
        return []


def list_playing_apps(pulse_list_fn: Callable = None) -> list:
    pulse_list_fn = pulse_list_fn or pulse_list
    apps = []
    try:
        for stream in pulse_list_fn("sink-inputs"):
            props = stream.get("properties", {})
            name = props.get("application.name") or props.get("application.process.binary")
            if name and name not in apps:
                apps.append(name)
    except Exception as e:
        log.error(f"Error listing playback apps: {e}")
    return apps


def route_app_stream(application: str, sink_name: str, pulse_list_fn: Callable = None,
                     is_active: Callable = None, ensure: Callable = None) -> tuple:
    pulse_list_fn = pulse_list_fn or pulse_list
    is_active = is_active or (lambda n: is_sink_active(n))
    ensure = ensure or (lambda n: ensure_sink_active(n))
    target = application.strip().casefold()
    if not target:
        return False, "Provide an application name, such as spotify, firefox, or vlc."
    try:
        if not is_active(sink_name) and not ensure(sink_name):
            return False, f"Could not create audio sink '{sink_name}'. Check the host audio connection."
        matches = []
        for stream in pulse_list_fn("sink-inputs"):
            props = stream.get("properties", {})
            names = (props.get("application.name", ""), props.get("application.process.binary", ""))
            if any(target in name.casefold() for name in names):
                matches.append(stream["index"])
        if not matches:
            return False, f"No active playback found for '{application}'. Start playback first, or route it in pavucontrol."
        moved = 0
        for stream_id in matches:
            subprocess.run(["pactl", "move-sink-input", str(stream_id), sink_name], check=True, capture_output=True)
            moved += 1
        return True, f"Routed {moved} playback stream(s) for '{application}' to Discord Speaker ({sink_name})."
    except Exception as e:
        log.error(f"Failed to route application: {e}")
        return False, f"Could not route all streams for '{application}'. Check the bot logs and host audio server."
