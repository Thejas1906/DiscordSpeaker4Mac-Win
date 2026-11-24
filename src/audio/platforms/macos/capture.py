"""macOS capture.

Sources
  * tap:system         whole-system audio via the native Core Audio process-tap helper (macOS 14.2+, no driver)
  * tap:app:<name>     one application's audio via the same helper (per-process tap)
  * input:<device>     any Core Audio input through PortAudio (sounddevice): BlackHole, Loopback, aggregate
                       devices, microphones. Works on every macOS version and is the fallback for < 14.2.

ScreenCaptureKit was evaluated and not used: it needs the broader Screen Recording permission, offers
no per-device routing and adds no capability the process tap lacks. Core Audio taps and BlackHole cover
both the "no driver" and "virtual device" cases.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from ...base import CaptureBackend
from ...models import (BackendUnavailableError, CaptureSource, CaptureState, DeviceInfo, DeviceUnavailableError,
                       PermissionDeniedError, SourceKind)

log = logging.getLogger("DiscordSpeaker.macos")

TAP_SYSTEM = "tap:system"
WATCHDOG_S = 3.0


def macos_version() -> Tuple[int, int]:
    try:
        parts = platform.mac_ver()[0].split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return (0, 0)


def find_helper() -> Optional[str]:
    cands = [os.environ.get("DS_TAP_HELPER")]
    here = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cands += [base / "dsaudiotap", Path(sys.executable).parent / "dsaudiotap",
                  Path(sys.executable).parent.parent / "Resources" / "dsaudiotap"]
    cands += [here / "tap" / "dsaudiotap", here / "tap" / "build" / "dsaudiotap"]
    for c in cands:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


class MacOSCapture(CaptureBackend):
    platform = "macos"
    supports_app_capture = True

    def __init__(self, pipeline, on_event=None, helper_path: Optional[str] = None, sd_module=None):
        super().__init__(pipeline, on_event)
        self._helper = helper_path
        self._sd = sd_module
        self._proc: Optional[subprocess.Popen] = None
        self._last_data = 0.0

    # ---- helpers ------------------------------------------------------------
    def helper(self) -> Optional[str]:
        if self._helper is None:
            self._helper = find_helper() or ""
        return self._helper or None

    def tap_supported(self) -> bool:
        return self.helper() is not None and macos_version() >= (14, 2)

    def sd(self):
        if self._sd is None:
            try:
                import sounddevice as sd
            except Exception as e:  # PortAudio missing
                raise BackendUnavailableError(f"sounddevice/PortAudio unavailable: {e}")
            self._sd = sd
        return self._sd

    def _apps(self) -> list:
        h = self.helper()
        if not h:
            return []
        try:
            res = subprocess.run([h, "list-apps"], capture_output=True, text=True, timeout=5, check=True)
            return json.loads(res.stdout)
        except Exception as e:
            log.warning("list-apps failed: %s", e)
            return []

    # ---- discovery -------------------------------------------------------------
    def list_devices(self) -> List[DeviceInfo]:
        out = []
        try:
            sd = self.sd()
            default_in, default_out = sd.default.device
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"]:
                    out.append(DeviceInfo(f"input:{d['name']}", d["name"], "input", default=(i == default_in),
                                          sample_rate=int(d["default_samplerate"]), channels=d["max_input_channels"]))
                if d["max_output_channels"]:
                    out.append(DeviceInfo(f"output:{d['name']}", d["name"], "output", default=(i == default_out),
                                          sample_rate=int(d["default_samplerate"]), channels=d["max_output_channels"]))
        except BackendUnavailableError as e:
            log.warning("%s", e)
        return out

    def list_sources(self) -> List[CaptureSource]:
        srcs: List[CaptureSource] = []
        if self.tap_supported():
            srcs.append(CaptureSource(TAP_SYSTEM, "System audio (Core Audio tap)", SourceKind.SYSTEM,
                                      detail="No driver needed; requires System Audio Recording permission"))
            for a in self._apps():
                if a.get("playing"):
                    srcs.append(CaptureSource(f"tap:app:{a['name']}", f"App: {a['name']}", SourceKind.APPLICATION,
                                              detail=f"pid {a['pid']}"))
        else:
            srcs.append(CaptureSource(TAP_SYSTEM, "System audio (needs macOS 14.2+ and the tap helper)",
                                      SourceKind.SYSTEM, available=False))
        for d in self.list_devices():
            if d.kind == "input":
                virtual = any(k in d.name.lower() for k in ("blackhole", "loopback", "soundflower", "aggregate", "multi-output"))
                srcs.append(CaptureSource(d.id, d.name + (" (virtual device)" if virtual else ""), SourceKind.INPUT,
                                          sample_rate=d.sample_rate, channels=d.channels))
        return srcs

    def list_applications(self) -> List[str]:
        return [a["name"] for a in self._apps() if a.get("playing")]

    def route_application(self, application: str) -> Tuple[bool, str]:
        """Switch capture to a matching application's audio (macOS captures per-process; it does not move streams)."""
        target = application.strip().casefold()
        if not target:
            return False, "Provide an application name, such as spotify or safari."
        if not self.tap_supported():
            return False, "Per-application capture needs macOS 14.2+ and the dsaudiotap helper."
        for a in self._apps():
            if target in a["name"].casefold() or target in a.get("bundle", "").casefold():
                if not a.get("playing"):
                    continue
                running = self.state in (CaptureState.STARTING, CaptureState.RUNNING, CaptureState.RECOVERING)
                self.select_source(f"tap:app:{a['name']}")
                return True, (f"Now capturing audio from '{a['name']}' only." if running else
                              f"'{a['name']}' selected; only its audio will be captured when streaming starts.")
        return False, f"No application matching '{application}' is playing audio right now."

    # ---- capture -----------------------------------------------------------------
    def _run_capture(self, src: CaptureSource, stop: threading.Event) -> None:
        self._last_data = time.monotonic()
        if src.id.startswith("tap:"):
            self._run_tap(src, stop)
        else:
            self._run_input(src, stop)

    def _run_tap(self, src: CaptureSource, stop: threading.Event) -> None:
        h = self.helper()
        if not h or macos_version() < (14, 2):
            raise BackendUnavailableError("Core Audio process taps need macOS 14.2+ and the dsaudiotap helper")
        cmd = [h, "capture"]
        if src.id.startswith("tap:app:"):
            name = src.id[len("tap:app:"):].casefold()
            pids = [str(a["pid"]) for a in self._apps() if a["name"].casefold() == name]
            if not pids:
                raise DeviceUnavailableError(f"'{name}' is not producing audio")
            for p in pids:
                cmd += ["--pid", p]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self._proc = proc
        try:
            header = proc.stderr.readline().decode(errors="replace").strip()
            try:
                info = json.loads(header)
            except json.JSONDecodeError:
                info = {}
            if "rate" not in info:
                proc.wait(3)
                self._raise_for_exit(proc.returncode, header)
            rate = int(info["rate"])
            chunk = (rate // 100) * 2 * 4
            while not stop.is_set():
                data = self._read_exact(proc, chunk, stop)
                if data is None:
                    break
                self._last_data = time.monotonic()
                self.pipeline.push(np.frombuffer(data, dtype="<f4").reshape(-1, 2), rate)
                self._running()
            if stop.is_set():
                return
            proc.wait(2) if proc.poll() is None else None
            self._raise_for_exit(proc.returncode, "")
        finally:
            self._kill(proc)
            self._proc = None

    @staticmethod
    def _raise_for_exit(code, msg: str):
        if code == 2:
            raise PermissionDeniedError(
                "macOS blocked audio capture. Open System Settings > Privacy & Security > "
                "Screen & System Audio Recording, allow DiscordSpeaker under 'System Audio Recording Only', then restart.")
        if code == 75:
            raise DeviceUnavailableError("default output device changed; re-attaching")
        if code == 3:
            raise DeviceUnavailableError("the selected application is not producing audio")
        raise DeviceUnavailableError(f"capture helper exited ({code}) {msg}".strip())

    def stop(self, timeout: float = 5.0) -> None:
        """Interrupt the helper first: a per-app tap is silent while that app is paused, so the reader
        would otherwise sit in a blocking read and stop()/select_source() would stall."""
        self._stop.set()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.send_signal(signal.SIGTERM)      # EOF unblocks the reader thread
            except Exception:
                pass
        super().stop(timeout)

    def _read_exact(self, proc, n, stop) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            if stop.is_set():
                return None
            part = proc.stdout.read(n - len(buf))
            if not part:
                return None
            buf += part
        return bytes(buf)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
        for f in (proc.stdout, proc.stderr):
            try:
                f and f.close()
            except Exception:
                pass

    def _run_input(self, src: CaptureSource, stop: threading.Event) -> None:
        sd = self.sd()
        name = src.id.split(":", 1)[1]
        dev = next((i for i, d in enumerate(sd.query_devices())
                    if d["name"] == name and d["max_input_channels"] > 0), None)
        if dev is None:
            raise DeviceUnavailableError(f"input device '{name}' is not connected")
        info = sd.query_devices(dev)
        rate = int(info["default_samplerate"])
        pref = self.pipeline.config.preferred_sample_rate
        ch = min(int(info["max_input_channels"]), 8)

        def cb(indata, frames, t, status):
            if status:
                self.pipeline.health.overruns += 1 if status.input_overflow else 0
            self._last_data = time.monotonic()
            self.pipeline.push(indata.copy(), use_rate)
            self._running()

        use_rate = rate
        for r in ([pref] if pref else []) + [rate]:
            try:
                sd.check_input_settings(device=dev, channels=ch, samplerate=r, dtype="float32")
                use_rate = r
                break
            except Exception:
                continue
        stream = sd.InputStream(device=dev, channels=ch, samplerate=use_rate, dtype="float32",
                                blocksize=0, latency="low", callback=cb)
        with stream:
            while not stop.wait(0.25):
                if not stream.active or time.monotonic() - self._last_data > WATCHDOG_S:
                    raise DeviceUnavailableError("input device stopped delivering audio (disconnected or system slept)")
