"""Real-hardware tests. SKIPPED unless DS_HARDWARE_TESTS=1 and running on the matching OS.

These are the only tests that prove a native OS capture path works. CI never runs them.
macOS needs the 'System Audio Recording' permission granted to the terminal/app running the tests.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
import wave

import numpy as np

import _path  # noqa: F401
from _path import fit
from audio.models import CaptureState
from audio.pipeline import AudioPipeline

ENABLED = os.environ.get("DS_HARDWARE_TESTS") == "1"


def tone_wav(path, rate=48000, seconds=8):
    t = np.arange(rate * seconds) / rate
    x = np.stack([0.3 * np.sin(2 * np.pi * 440 * t), 0.3 * np.sin(2 * np.pi * 880 * t)], 1)
    w = wave.open(path, "wb"); w.setnchannels(2); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes((x * 32767).astype("<i2").tobytes()); w.close()


@unittest.skipUnless(ENABLED and sys.platform == "darwin", "macOS hardware test (DS_HARDWARE_TESTS=1)")
class MacTapHardware(unittest.TestCase):
    def test_system_tap_hears_afplay_with_stereo_separation(self):
        from audio.platforms.macos.capture import MacOSCapture
        p = AudioPipeline(); b = MacOSCapture(p)
        self.assertTrue(b.tap_supported(), "build the helper: src/audio/platforms/macos/tap/build.sh")
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "t.wav"); tone_wav(wav)
            b.start("tap:system")
            pl = subprocess.Popen(["afplay", "-v", "0.2", wav]); time.sleep(1.5)
            out = []
            end = time.time() + 3
            while time.time() < end:
                time.sleep(0.02); out.append(p.read_frame())
            pl.terminate(); pl.wait(); b.stop()
        y = np.concatenate(out)
        self.assertEqual(p.health.input_rate in (44100, 48000, 96000), True)
        self.assertGreater(float(np.abs(y).max()), 0.01,
                           "digital silence: grant System Audio Recording permission to this terminal/app")
        seg = y[24000:]
        spec = lambda c: np.abs(np.fft.rfft(seg[:, c] * np.hanning(len(seg))))
        f = np.fft.rfftfreq(len(seg), 1 / 48000)
        self.assertAlmostEqual(f[spec(0).argmax()], 440, delta=5)
        self.assertAlmostEqual(f[spec(1).argmax()], 880, delta=5)


@unittest.skipUnless(ENABLED and sys.platform == "win32", "Windows hardware test (DS_HARDWARE_TESTS=1)")
class WindowsLoopbackHardware(unittest.TestCase):
    def test_default_loopback_opens_and_runs(self):
        from audio.platforms.windows.capture import WindowsCapture
        p = AudioPipeline(); b = WindowsCapture(p)
        b.start("loopback:default"); time.sleep(3)
        self.assertIn(b.state, (CaptureState.RUNNING,))
        b.stop()


@unittest.skipUnless(ENABLED and sys.platform.startswith("linux"), "Linux hardware test (DS_HARDWARE_TESTS=1)")
class LinuxPulseHardware(unittest.TestCase):
    def test_monitor_capture_of_null_sink(self):
        from audio.platforms.linux.capture import LinuxCapture
        p = AudioPipeline(); b = LinuxCapture(p, auto_create_sink=True)
        b.start(); time.sleep(2)
        self.assertEqual(b.state, CaptureState.RUNNING)
        b.stop()


if __name__ == "__main__":
    unittest.main()
