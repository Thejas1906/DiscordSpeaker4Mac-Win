"""Linux capture: PulseAudio/PipeWire monitor sources read through `parec` as native float32.

parec ships in pulseaudio-utils (already in the Docker image), asks for float32le at the
source's own rate and channel count (no server-side resampling, no 16-bit step) and the
pipeline's resampler owns any conversion. If parec is missing, FFmpeg (-f pulse, 16-bit) is used.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from typing import List, Optional

import numpy as np

from ...base import CaptureBackend
from ...models import (BackendUnavailableError, CaptureSource, DeviceInfo,
                       DeviceUnavailableError, SourceKind)
from . import pulse

log = logging.getLogger("DiscordSpeaker.linux")


class LinuxCapture(CaptureBackend):
    platform = "linux"
    supports_app_capture = True   # via routing apps into the virtual sink

    def __init__(self, pipeline, on_event=None, sink_name: str = None, auto_create_sink: bool = True,
                 script_path: Optional[str] = None, list_fn=None):
        super().__init__(pipeline, on_event)
        self.sink_name = sink_name or os.getenv("SINK_NAME", "DiscordSpeaker")
        self.auto_create_sink = auto_create_sink
        self.script_path = script_path
        self._list = list_fn or pulse.pulse_list
        self._proc: Optional[subprocess.Popen] = None

    # ---- discovery ---------------------------------------------------------
    def list_devices(self) -> List[DeviceInfo]:
        out = []
        for s in self._list("sinks"):
            rate, ch = self._spec(s)
            out.append(DeviceInfo(id=s["name"], name=s.get("description", s["name"]), kind="output",
                                  sample_rate=rate, channels=ch))
        for s in self._list("sources"):
            rate, ch = self._spec(s)
            out.append(DeviceInfo(id=s["name"], name=s.get("description", s["name"]), kind="input",
                                  sample_rate=rate, channels=ch, loopback=self._is_monitor(s)))
        return out

    @staticmethod
    def _spec(s: dict) -> tuple:
        """pactl --format=json reports e.g. "s16le 2ch 44100Hz" (a string); tolerate a dict as well."""
        spec = s.get("sample_specification") or ""
        if isinstance(spec, dict):
            return spec.get("rate"), spec.get("channels")
        m = re.search(r"(\d+)ch\s+(\d+)Hz", str(spec))
        return (int(m.group(2)), int(m.group(1))) if m else (None, None)

    @staticmethod
    def _is_monitor(s: dict) -> bool:
        return bool(s.get("monitor_source") or s.get("monitor_of_sink")
                    or s.get("properties", {}).get("device.class") == "monitor"
                    or str(s.get("name", "")).endswith(".monitor"))

    def list_sources(self) -> List[CaptureSource]:
        srcs = []
        try:
            raw = self._list("sources")
        except Exception as e:
            log.error("Cannot list pulse sources: %s", e)
            return []
        for s in raw:
            rate, ch = self._spec(s)
            mon = self._is_monitor(s)
            srcs.append(CaptureSource(id=s["name"], name=s.get("description", s["name"]),
                                      kind=SourceKind.LOOPBACK if mon else SourceKind.INPUT,
                                      sample_rate=rate, channels=ch))
        own = f"{self.sink_name}.monitor"
        srcs.sort(key=lambda x: (x.id != own, x.kind != SourceKind.LOOPBACK, x.name))
        return srcs

    def list_applications(self) -> List[str]:
        return pulse.list_playing_apps(self._list)

    # ---- routing -------------------------------------------------------------
    def route_application(self, application: str):
        return pulse.route_app_stream(application, self.sink_name, pulse_list_fn=self._list,
                                      is_active=lambda n: pulse.is_sink_active(n, self._list),
                                      ensure=lambda n: pulse.ensure_sink_active(n, self.script_path))

    def _prepare(self) -> None:
        if self.auto_create_sink and not pulse.is_sink_active(self.sink_name, self._list):
            pulse.ensure_sink_active(self.sink_name, self.script_path)
        if self._source_id is None:
            self._source_id = f"{self.sink_name}.monitor"

    # ---- capture ---------------------------------------------------------------
    def _command(self, src: CaptureSource) -> tuple:
        rate = src.sample_rate or 48000
        ch = min(src.channels or 2, 8)
        if shutil.which("parec"):
            return ["parec", f"--device={src.id}", "--format=float32le", f"--rate={rate}",
                    f"--channels={ch}", "--latency-msec=20", "--raw"], "f32le", rate, ch
        if shutil.which("ffmpeg"):
            return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "pulse",
                    "-sample_rate", str(rate), "-channels", str(ch), "-i", src.id,
                    "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"], "s16le", rate, ch
        raise BackendUnavailableError("Neither 'parec' nor 'ffmpeg' is installed.")

    def _run_capture(self, src: CaptureSource, stop: threading.Event) -> None:
        cmd, fmt, rate, ch = self._command(src)
        log.info("Capturing '%s' (%s %d Hz x%d) via %s", src.id, fmt, rate, ch, cmd[0])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._proc = proc
        bps = 4 if fmt == "f32le" else 2
        chunk = (rate // 100) * ch * bps          # 10 ms reads
        try:
            while not stop.is_set():
                data = self._read_exact(proc, chunk, stop)
                if data is None:
                    break
                if fmt == "f32le":
                    frames = np.frombuffer(data, dtype="<f4").reshape(-1, ch)
                else:
                    frames = (np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0).reshape(-1, ch)
                self.pipeline.push(frames, rate)
                self._running()
            if not stop.is_set():
                raise DeviceUnavailableError("audio server connection lost (capture process exited)")
        finally:
            self.kill_process(proc)
            self._proc = None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        p = self._proc
        if p is not None and p.poll() is None:
            p.terminate()          # unblocks the reader with EOF
        super().stop(timeout)

    @staticmethod
    def _read_exact(proc, n: int, stop: threading.Event) -> Optional[bytes]:
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
    def kill_process(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(2)
        if proc.stdout:
            proc.stdout.close()
