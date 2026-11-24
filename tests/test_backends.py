"""Backend behaviour with fake devices: real threads and real subprocesses, no audio hardware.

These validate discovery, start/stop, frame format handling, disconnect/recovery, error mapping and process
cleanup for each platform backend. They do NOT prove the OS APIs work: see tests/test_hardware.py.
"""
import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import _path  # noqa: F401
from _path import fit, pid_alive, rms_db, sine
from audio.models import CaptureState, SourceKind
from audio.pipeline import AudioPipeline
from audio.quality_config import QualityConfig
from audio.platforms.linux.capture import LinuxCapture
from audio.platforms.macos import capture as mac
from audio.platforms.windows.capture import WindowsCapture


def wait_for(cond, timeout=8.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


FAKE_STREAMER = textwrap.dedent('''\
    import os, sys, time, json
    import numpy as np
    argv = sys.argv[1:]
    if argv and argv[0] == "list-apps":
        print(os.environ.get("FAKE_APPS", "[]")); sys.exit(0)
    kv = dict(a.lstrip("-").split("=", 1) for a in argv if "=" in a)
    rate = int(kv.get("rate", os.environ.get("FAKE_RATE", "48000")))
    ch = int(kv.get("channels", "2"))
    mode = os.environ.get("FAKE_MODE", "")
    log = os.environ.get("FAKE_LOG")
    if log:
        open(log, "a").write(" ".join(argv) + "\\n")
    if os.environ.get("FAKE_HEADER", "0") == "1":
        sys.stderr.write(json.dumps({"rate": rate, "channels": 2, "format": "f32le"}) + "\\n"); sys.stderr.flush()
    if mode == "perm":
        sys.exit(2)
    if mode == "silent":                      # e.g. a per-app tap while that app is paused: no data at all
        time.sleep(600)
    gains = [float(g) for g in os.environ.get("FAKE_GAINS", "1,1").split(",")]
    exit_after = float(os.environ.get("FAKE_EXIT_AFTER", "0"))
    exit_code = int(os.environ.get("FAKE_EXIT_CODE", "0"))
    t0 = time.time(); n = 0; blk = rate // 100
    while True:
        t = (n + np.arange(blk)) / rate
        x = np.stack([0.5 * g * np.sin(2 * np.pi * 1000 * t) for g in (gains + [1.0] * ch)[:ch]], axis=1).astype("<f4")
        try:
            sys.stdout.buffer.write(x.tobytes()); sys.stdout.buffer.flush()
        except BrokenPipeError:
            sys.exit(0)
        n += blk
        if exit_after and time.time() - t0 > exit_after:
            sys.exit(exit_code)
        time.sleep(max(0, t0 + n / rate - time.time()))
''')


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def script(self, name):
        """Executable stub. A /bin/sh wrapper execs the venv python (shebangs can't contain spaces)."""
        py = self.dir / f"{name}.py"
        py.write_text(FAKE_STREAMER)
        p = self.dir / name
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n')
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)


def pulse_fake(rate=44100, channels=2, extra=()):
    def fn(kind):
        if kind == "sinks":
            return [{"index": 1, "name": "DiscordSpeaker", "description": "Discord Speaker",
                     "sample_specification": "float32le 2ch 48000Hz"}]
        if kind == "sources":
            return [{"name": "alsa_input.mic", "description": "Mic", "monitor_source": "",
                     "properties": {"device.class": "sound"}, "sample_specification": "s16le 1ch 48000Hz"},
                    {"name": "DiscordSpeaker.monitor", "description": "Monitor of Discord Speaker",
                     "monitor_source": "DiscordSpeaker", "properties": {"device.class": "monitor"},
                     "sample_specification": f"float32le {channels}ch {rate}Hz"}, *extra]
        if kind == "sink-inputs":
            return [{"index": 5, "sink": 1, "properties": {"application.name": "Firefox"}}]
        return []
    return fn


POSIX = unittest.skipIf(sys.platform == "win32", "uses /bin/sh stub executables")


@POSIX
class LinuxBackendTests(TmpDirCase):
    def make(self, **env):
        self.script("parec")
        patcher = patch.dict(os.environ, {"PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}", **env})
        patcher.start(); self.addCleanup(patcher.stop)
        p = AudioPipeline(QualityConfig(buffer_ms=80))
        events = []
        b = LinuxCapture(p, events.append, auto_create_sink=False, list_fn=pulse_fake())
        self.addCleanup(b.stop)
        return p, b, events

    def test_discovery_prefers_own_monitor_and_classifies_loopback(self):
        _, b, _ = self.make()
        srcs = b.list_sources()
        self.assertEqual(srcs[0].id, "DiscordSpeaker.monitor")
        self.assertEqual(srcs[0].kind, SourceKind.LOOPBACK)
        self.assertEqual({d.kind for d in b.list_devices()}, {"output", "input"})
        self.assertEqual(b.list_applications(), ["Firefox"])

    def test_capture_delivers_native_rate_float_and_resamples_once(self):
        p, b, _ = self.make()
        b.start()
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        self.assertTrue(wait_for(lambda: p.health.frames_in > 44100 * 1.2))
        self.assertEqual((p.health.input_rate, p.health.input_channels), (44100, 2))
        out = []
        for _ in range(40):
            time.sleep(0.02); out.append(p.read_frame())
        y = np.concatenate(out)
        self.assertGreater(rms_db(y), -12)
        pid = b._proc.pid
        b.stop()
        self.assertEqual(b.state, CaptureState.STOPPED)
        self.assertTrue(wait_for(lambda: not pid_alive(pid), 3), "parec left running")

    def test_server_loss_recovers_and_reports(self):
        p, b, ev = self.make(FAKE_EXIT_AFTER="0.4")
        b.RETRY_DELAYS = (0.1,)
        b.start()
        self.assertTrue(wait_for(lambda: p.health.restarts >= 1, 6))
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING, 6))
        kinds = [e.kind for e in ev]
        self.assertIn("disconnected", kinds)
        self.assertIn("recovered", kinds)

    def test_missing_source_goes_to_recovering_not_crash(self):
        p, b, ev = self.make()
        b.RETRY_DELAYS = (0.1,)
        b.start("does-not-exist.monitor")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RECOVERING))
        self.assertIn("not available", p.health.last_error)

    def test_switching_source_stops_old_capture_first(self):
        p, b, _ = self.make()
        b._list = pulse_fake(extra=[{"name": "other.monitor", "description": "Other", "monitor_source": "other",
                                     "sample_specification": "s16le 2ch 48000Hz"}])
        b.start()
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        first = b._proc.pid
        b.select_source("other.monitor")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING and b._proc and b._proc.pid != first))
        self.assertFalse(pid_alive(first))

    def test_no_capture_tool_reports_clear_error(self):
        p = AudioPipeline()
        b = LinuxCapture(p, auto_create_sink=False, list_fn=pulse_fake())
        with patch("audio.platforms.linux.capture.shutil.which", return_value=None):
            from audio.models import BackendUnavailableError
            with self.assertRaises(BackendUnavailableError):
                b._command(b.list_sources()[0])


@POSIX
class MacBackendTests(TmpDirCase):
    def make(self, **env):
        helper = self.script("dsaudiotap")
        envp = patch.dict(os.environ, {"FAKE_HEADER": "1", **env}); envp.start(); self.addCleanup(envp.stop)
        ver = patch.object(mac, "macos_version", return_value=(15, 0)); ver.start(); self.addCleanup(ver.stop)
        p = AudioPipeline(QualityConfig(buffer_ms=80)); ev = []
        b = mac.MacOSCapture(p, ev.append, helper_path=helper, sd_module=FakeSD())
        self.addCleanup(b.stop)
        return p, b, ev

    def test_sources_include_system_tap_apps_and_virtual_inputs(self):
        apps = json.dumps([{"pid": 11, "bundle": "com.spotify.client", "name": "Spotify", "playing": True},
                           {"pid": 12, "bundle": "x", "name": "Idle", "playing": False}])
        _, b, _ = self.make(FAKE_APPS=apps)
        ids = {s.id: s for s in b.list_sources()}
        self.assertIn("tap:system", ids); self.assertTrue(ids["tap:system"].available)
        self.assertIn("tap:app:Spotify", ids); self.assertNotIn("tap:app:Idle", ids)
        self.assertIn("input:BlackHole 2ch", ids)
        self.assertIn("virtual", ids["input:BlackHole 2ch"].name)

    def test_tap_unavailable_below_macos_14_2(self):
        _, b, _ = self.make()
        with patch.object(mac, "macos_version", return_value=(13, 6)):
            self.assertFalse(b.list_sources()[0].available)

    def test_system_tap_capture_flows_and_cleans_up(self):
        p, b, _ = self.make(FAKE_RATE="48000", FAKE_GAINS="1,0")
        b.start("tap:system")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        self.assertTrue(wait_for(lambda: p.health.frames_in > 24000))
        out = np.concatenate([(time.sleep(0.02), p.read_frame())[1] for _ in range(30)])
        self.assertGreater(rms_db(out[:, 0]), -12)
        self.assertLess(rms_db(out[:, 1]), -100)            # channel separation preserved end to end
        pid = b._proc.pid
        b.stop()
        self.assertTrue(wait_for(lambda: not pid_alive(pid), 3))

    def test_permission_denied_is_fatal_with_actionable_message(self):
        p, b, ev = self.make(FAKE_MODE="perm")
        b.start("tap:system")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.ERROR))
        self.assertIn("System Audio Recording", p.health.last_error)
        time.sleep(0.5)
        self.assertEqual(p.health.restarts, 0, "must not retry-loop on a permission error")

    def test_stop_and_source_switch_are_prompt_even_when_tap_delivers_no_data(self):
        """Regression: select_source timed out because stop() waited on a blocking read from a silent tap."""
        p, b, _ = self.make(FAKE_MODE="silent")
        b.start("tap:system")
        self.assertTrue(wait_for(lambda: b._proc is not None))
        pid = b._proc.pid
        t0 = time.time()
        b.select_source("input:BlackHole 2ch")                 # stop old capture, start the new one
        self.assertLess(time.time() - t0, 3.0)
        self.assertTrue(wait_for(lambda: not pid_alive(pid), 3), "helper left running")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        b.stop()

    def test_default_output_change_restarts_capture(self):
        p, b, ev = self.make(FAKE_EXIT_AFTER="0.4", FAKE_EXIT_CODE="75")
        b.RETRY_DELAYS = (0.1,)
        b.start("tap:system")
        self.assertTrue(wait_for(lambda: p.health.restarts >= 1, 6))
        self.assertIn("disconnected", [e.kind for e in ev])

    def test_per_app_capture_passes_pids_and_route_switches_source(self):
        log = self.dir / "args.log"
        apps = json.dumps([{"pid": 11, "bundle": "com.spotify.client", "name": "Spotify", "playing": True}])
        p, b, _ = self.make(FAKE_APPS=apps, FAKE_LOG=str(log))
        b.start("tap:system")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        ok, msg = b.route_application("spot")
        self.assertTrue(ok, msg)
        self.assertIn("only", msg)
        self.assertTrue(wait_for(lambda: b.state in (CaptureState.RUNNING, CaptureState.STARTING)))
        self.assertTrue(wait_for(lambda: "capture --pid 11" in log.read_text() if log.exists() else False))
        self.assertFalse(b.route_application("nonexistent")[0])

    def test_input_device_capture_and_disconnect_recovery(self):
        p, b, ev = self.make()
        b.RETRY_DELAYS = (0.1,)
        b.start("input:BlackHole 2ch")
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))
        self.assertEqual(p.health.input_rate, 44100)
        b._sd.kill_streams()                                # device unplugged / system slept
        self.assertTrue(wait_for(lambda: p.health.restarts >= 1))
        self.assertTrue(wait_for(lambda: b.state == CaptureState.RUNNING))


class FakeStream:
    def __init__(self, device, channels, samplerate, callback, **kw):
        self.cb, self.rate, self.ch, self.active = callback, samplerate, channels, True
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        n = 0
        while not self._stop.is_set() and self.active:
            blk = self.rate // 100
            tt = (n + np.arange(blk)) / self.rate
            data = np.stack([0.5 * np.sin(2 * np.pi * 440 * tt)] * self.ch, axis=1).astype(np.float32)
            self.cb(data, blk, None, types.SimpleNamespace(input_overflow=False, __bool__=lambda s: False) and None)
            n += blk
            time.sleep(0.01)

    def __enter__(self):
        self.t.start(); return self

    def __exit__(self, *a):
        self._stop.set(); self.t.join(1)


class FakeSD:
    def __init__(self):
        self.streams = []
        self.default = types.SimpleNamespace(device=(1, 2))

    def query_devices(self, dev=None):
        devs = [{"name": "MacBook Microphone", "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000.0},
                {"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2, "default_samplerate": 44100.0},
                {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000.0}]
        return devs if dev is None else devs[dev]

    def check_input_settings(self, **kw):
        pass

    def InputStream(self, device, channels, samplerate, **kw):
        s = FakeStream(device, channels, samplerate, kw["callback"]); self.streams.append(s); return s

    def kill_streams(self):
        for s in self.streams:
            s.active = False


# ---------------------------------------------------------------- Windows ---------------------------------
class FakeWasapiState:
    def __init__(self):
        self.devices = [
            {"index": 0, "name": "Speakers (Realtek)", "hostApi": 13, "maxInputChannels": 0, "maxOutputChannels": 2, "defaultSampleRate": 48000.0},
            {"index": 1, "name": "Speakers (Realtek) [Loopback]", "hostApi": 13, "maxInputChannels": 2, "maxOutputChannels": 0,
             "defaultSampleRate": 48000.0, "isLoopbackDevice": True},
            {"index": 2, "name": "Headphones (USB) [Loopback]", "hostApi": 13, "maxInputChannels": 2, "maxOutputChannels": 0,
             "defaultSampleRate": 44100.0, "isLoopbackDevice": True},
            {"index": 3, "name": "CABLE Output (VB-Audio Virtual Cable)", "hostApi": 13, "maxInputChannels": 2, "maxOutputChannels": 0,
             "defaultSampleRate": 48000.0},
            {"index": 4, "name": "MME junk", "hostApi": 0, "maxInputChannels": 2, "maxOutputChannels": 0, "defaultSampleRate": 44100.0},
        ]
        self.default_idx = 1
        self.streams = []


class FakeWinStream:
    def __init__(self, state, rate, ch, cb):
        self.rate, self.ch, self.cb, self.live = rate, ch, cb, True
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        n = 0
        while not self._stop.is_set():
            blk = self.rate // 100
            tt = (n + np.arange(blk)) / self.rate
            data = np.stack([0.5 * np.sin(2 * np.pi * 1000 * tt), 0 * tt][: self.ch], axis=1).astype("<f4")
            self.cb(data.tobytes(), blk, None, 0)
            n += blk
            time.sleep(0.01)

    def start_stream(self):
        self.thread.start()

    def is_active(self):
        return self.live

    def stop_stream(self):
        self._stop.set()

    def close(self):
        self._stop.set()


class FakePA:
    def __init__(self, state):
        self.s = state

    def get_host_api_info_by_type(self, t):
        return {"index": 13}

    def get_device_count(self):
        return len(self.s.devices)

    def get_device_info_by_index(self, i):
        return dict(self.s.devices[i])

    def get_default_wasapi_loopback(self):
        return dict(self.s.devices[self.s.default_idx])

    def open(self, format, channels, rate, input, input_device_index, frames_per_buffer, stream_callback):
        st = FakeWinStream(self.s, rate, channels, stream_callback)
        self.s.streams.append(st)
        return st

    def terminate(self):
        pass


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        fake = types.ModuleType("pyaudiowpatch")
        fake.paFloat32, fake.paContinue = 1, 0
        p = patch.dict(sys.modules, {"pyaudiowpatch": fake}); p.start(); self.addCleanup(p.stop)
        self.state = FakeWasapiState()
        self.pipe = AudioPipeline(QualityConfig(buffer_ms=80))
        self.events = []
        self.b = WindowsCapture(self.pipe, self.events.append, pa_factory=lambda: FakePA(self.state))
        self.b.RETRY_DELAYS = (0.1,)
        self.addCleanup(self.b.stop)

    def test_discovery_wasapi_only_default_first_and_virtual_cable_flagged(self):
        srcs = self.b.list_sources()
        self.assertEqual(srcs[0].id, "loopback:default")
        names = [s.name for s in srcs]
        self.assertFalse(any("MME" in n for n in names))
        self.assertTrue(any("virtual cable" in n for n in names))
        self.assertEqual([s.kind for s in srcs if s.id.startswith("input:")], [SourceKind.INPUT])

    def test_loopback_capture_native_rate_and_stereo(self):
        self.b.start("loopback:Headphones (USB) [Loopback]")
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING))
        self.assertTrue(wait_for(lambda: self.pipe.health.frames_in > 44100))
        self.assertEqual(self.pipe.health.input_rate, 44100)
        y = np.concatenate([(time.sleep(0.02), self.pipe.read_frame())[1] for _ in range(30)])
        self.assertGreater(rms_db(y[:, 0]), -12)
        self.assertLess(rms_db(y[:, 1]), -100)

    def test_device_removal_recovers_when_it_returns(self):
        self.b.start("loopback:Headphones (USB) [Loopback]")
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING))
        removed = self.state.devices.pop(2)
        for s in self.state.streams:
            s.live = False
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RECOVERING))
        self.assertTrue(wait_for(lambda: any(k in self.pipe.health.last_error for k in ("not available", "not connected", "removed"))))
        self.state.devices.insert(2, removed)
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING, 8))
        self.assertIn("recovered", [e.kind for e in self.events])

    def test_default_output_change_reattaches_following_source(self):
        self.b.start("loopback:default")
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING))
        self.state.default_idx = 2                           # user switches default playback device
        self.assertTrue(wait_for(lambda: self.pipe.health.restarts >= 1, 8))
        self.assertTrue(wait_for(lambda: self.pipe.health.input_rate == 44100 and self.b.state == CaptureState.RUNNING, 8))

    def test_virtual_cable_input_captures(self):
        self.b.start("input:CABLE Output (VB-Audio Virtual Cable)")
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING))

    def test_app_routing_reports_honest_limitation(self):
        ok, msg = self.b.route_application("spotify")
        self.assertFalse(ok)
        self.assertIn("virtual cable", msg.lower())

    def test_stop_closes_stream(self):
        self.b.start("loopback:default")
        self.assertTrue(wait_for(lambda: self.b.state == CaptureState.RUNNING))
        self.b.stop()
        self.assertEqual(self.b.state, CaptureState.STOPPED)
        self.assertTrue(all(s._stop.is_set() for s in self.state.streams))


if __name__ == "__main__":
    unittest.main()
