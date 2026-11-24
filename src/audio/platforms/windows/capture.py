"""Windows capture over WASAPI (PyAudioWPatch, a maintained PortAudio fork that exposes WASAPI loopback).

Sources
  * loopback:default     follows the current default output device (re-attaches when it changes)
  * loopback:<device>    loopback of one specific output device - captures everything routed to it,
                         native shared-mode mix format (float32), no driver needed
  * input:<device>       any WASAPI capture endpoint, including VB-Audio Virtual Cable's "CABLE Output"

Per-application capture (Windows 10 2004+/11 "process loopback") needs the ActivateAudioInterfaceAsync COM
API, which this PortAudio layer does not expose; it is NOT implemented. To isolate one app, use Windows
Settings > System > Sound > Volume mixer to send it to a virtual cable and capture the cable's output.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

from ...base import CaptureBackend
from ...models import (BackendUnavailableError, CaptureSource, DeviceInfo, DeviceUnavailableError, SourceKind)

log = logging.getLogger("DiscordSpeaker.windows")

DEFAULT_LOOPBACK = "loopback:default"
CABLE_HINT = "VB-Audio Virtual Cable"


class WindowsCapture(CaptureBackend):
    platform = "windows"
    supports_app_capture = False

    def __init__(self, pipeline, on_event=None, pa_factory=None):
        super().__init__(pipeline, on_event)
        self._pa_factory = pa_factory
        self._last_data = 0.0

    def _pa(self):
        if self._pa_factory:
            return self._pa_factory()
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            raise BackendUnavailableError(f"PyAudioWPatch is not installed: {e}")
        return pyaudio.PyAudio()

    @staticmethod
    def _wasapi_index(pa) -> int:
        import_ok = pa.get_host_api_info_by_type(13)    # paWASAPI == 13
        return import_ok["index"]

    # ---- discovery ---------------------------------------------------------------
    def _enumerate(self, pa) -> Tuple[list, list, Optional[dict]]:
        """Returns (outputs, loopbacks, inputs...) restricted to the WASAPI host API."""
        wasapi = self._wasapi_index(pa)
        outputs, loopbacks, inputs = [], [], []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d.get("hostApi") != wasapi:
                continue
            if d.get("isLoopbackDevice"):
                loopbacks.append(d)
            elif d.get("maxInputChannels", 0) > 0:
                inputs.append(d)
            elif d.get("maxOutputChannels", 0) > 0:
                outputs.append(d)
        default = None
        try:
            default = pa.get_default_wasapi_loopback()
        except Exception:
            pass
        return outputs, loopbacks, inputs, default

    def list_devices(self) -> List[DeviceInfo]:
        pa = self._pa()
        try:
            outputs, loopbacks, inputs, default = self._enumerate(pa)
            res = [DeviceInfo(f"output:{d['name']}", d["name"], "output", sample_rate=int(d["defaultSampleRate"]),
                              channels=d["maxOutputChannels"]) for d in outputs]
            res += [DeviceInfo(f"input:{d['name']}", d["name"], "input", sample_rate=int(d["defaultSampleRate"]),
                               channels=d["maxInputChannels"]) for d in inputs]
            res += [DeviceInfo(f"loopback:{d['name']}", d["name"], "input", loopback=True,
                               default=bool(default and d["index"] == default["index"]),
                               sample_rate=int(d["defaultSampleRate"]), channels=d["maxInputChannels"])
                    for d in loopbacks]
            return res
        finally:
            pa.terminate()

    def list_sources(self) -> List[CaptureSource]:
        try:
            pa = self._pa()
        except BackendUnavailableError as e:
            log.warning("%s", e)
            return []
        try:
            outputs, loopbacks, inputs, default = self._enumerate(pa)
        finally:
            pa.terminate()
        srcs: List[CaptureSource] = []
        if default:
            srcs.append(CaptureSource(DEFAULT_LOOPBACK, f"System audio - default output ({default['name']})",
                                      SourceKind.LOOPBACK, int(default["defaultSampleRate"]), default["maxInputChannels"]))
        for d in loopbacks:
            srcs.append(CaptureSource(f"loopback:{d['name']}", f"Loopback: {d['name']}", SourceKind.LOOPBACK,
                                      int(d["defaultSampleRate"]), d["maxInputChannels"]))
        for d in inputs:
            virtual = CABLE_HINT.lower() in d["name"].lower() or "cable" in d["name"].lower()
            srcs.append(CaptureSource(f"input:{d['name']}", d["name"] + (" (virtual cable)" if virtual else ""),
                                      SourceKind.INPUT, int(d["defaultSampleRate"]), d["maxInputChannels"]))
        return srcs

    def route_application(self, application: str):
        return False, ("Per-application capture is not available on Windows. Use Windows Settings > Sound > Volume "
                       "mixer to send the app to a virtual cable (e.g. VB-Audio Cable), then select the cable's "
                       "capture endpoint as the source.")

    # ---- capture --------------------------------------------------------------------
    def _resolve(self, pa, src: CaptureSource) -> dict:
        outputs, loopbacks, inputs, default = self._enumerate(pa)
        if src.id == DEFAULT_LOOPBACK:
            if not default:
                raise DeviceUnavailableError("no default output device")
            return default
        kind, name = src.id.split(":", 1)
        pool = loopbacks if kind == "loopback" else inputs
        for d in pool:
            if d["name"] == name:
                return d
        raise DeviceUnavailableError(f"device '{name}' is not connected")

    def _run_capture(self, src: CaptureSource, stop: threading.Event) -> None:
        pa = self._pa()
        stream = None
        try:
            import pyaudiowpatch as pyaudio
            info = self._resolve(pa, src)
            rate = int(info["defaultSampleRate"])
            pref = self.pipeline.config.preferred_sample_rate
            ch = max(1, min(int(info["maxInputChannels"]), 8))
            use_rate = rate
            self._last_data = time.monotonic()
            failed = threading.Event()

            def cb(in_data, frame_count, time_info, status):
                if status:
                    self.pipeline.health.overruns += 1
                self._last_data = time.monotonic()
                self.pipeline.push(np.frombuffer(in_data, dtype="<f4").reshape(-1, ch).copy(), use_rate)
                self._running()
                return (None, pyaudio.paContinue)

            candidates = ([pref] if pref and pref != rate else []) + [rate]
            last_err = None
            for r in candidates:
                try:
                    stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=r, input=True,
                                     input_device_index=info["index"], frames_per_buffer=r // 100,
                                     stream_callback=cb)
                    use_rate = r
                    break
                except Exception as e:
                    last_err = e
            if stream is None:
                raise DeviceUnavailableError(f"cannot open '{info['name']}': {last_err}")
            stream.start_stream()
            followed = info["name"]
            last_check = time.monotonic()
            while not stop.wait(0.25):
                if not stream.is_active():
                    raise DeviceUnavailableError("capture stream stopped (device removed or changed)")
                # Loopback streams legitimately deliver nothing while the system is silent,
                # so health is judged by endpoint presence, not by data arrival.
                if time.monotonic() - last_check > 1.5:
                    last_check = time.monotonic()
                    probe = self._pa()
                    try:
                        cur = self._resolve(probe, src)
                    finally:
                        probe.terminate()
                    if cur["name"] != followed:
                        self._emit("disconnected", f"default output changed to {cur['name']}")
                        raise DeviceUnavailableError("default output device changed; re-attaching")
                    self._running()   # idle loopback is still healthy
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()
