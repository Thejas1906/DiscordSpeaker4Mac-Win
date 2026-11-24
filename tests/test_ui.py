"""UI lifecycle smoke tests (offscreen Qt, fake engine). Skipped when PySide6 is unavailable."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import _path  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except Exception:  # pragma: no cover
    HAVE_QT = False


class FakeClient:
    instances = []

    def __init__(self, on_event=None, **kw):
        self.on_event, self.calls, self._alive = on_event, [], False
        FakeClient.instances.append(self)

    @property
    def alive(self): return self._alive
    def start(self, timeout=0): self._alive = True
    def stop(self, **kw): self._alive = False; self.calls.append(("stop",))

    def request(self, cmd, timeout=0, **args):
        self.calls.append((cmd, args))
        return {"hello": {"protocol": 1, "platform": "test"}, "list_sources": [
            {"id": "a", "name": "System audio", "kind": "system", "available": True, "detail": ""},
            {"id": "b", "name": "Gone", "kind": "input", "available": False, "detail": ""}],
            "list_channels": [{"guild_id": 1, "guild": "G", "channel_id": 2, "channel": "General"}],
            "apply_quality": {"restart_required": False}}.get(cmd, True)


class FakeSecrets:
    def __init__(self, tok="x"): self.tok = tok
    def get(self): return self.tok
    def has(self): return bool(self.tok)
    def set(self, t): self.tok = t
    def delete(self): self.tok = None


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from service.settings import AppSettings
        from ui.controller import Controller
        from ui.main_window import MainWindow
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        p = patch.dict(os.environ, {"DS_CONFIG_DIR": self.tmp.name}); p.start(); self.addCleanup(p.stop)
        FakeClient.instances.clear()
        self.c = Controller(AppSettings(), FakeSecrets(), client_factory=FakeClient)
        self.w = MainWindow(self.c)
        self.addCleanup(self.w.deleteLater)

    def pump(self, ms=300):
        import time
        end = time.time() + ms / 1000
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def state(self, audio="stopped", voice=None, logged=True, **extra):
        a = dict(state=audio, peak=0.5, rms=0.2, input_rate=44100, input_channels=2, latency_ms=140.0, underruns=0,
                 overruns=0, drift_corrections=0, clipped_samples=0, selected="a", last_error="", silent_s=0)
        a.update(extra)
        return {"event": "state", "audio": a, "discord": {"logged_in": logged, "user": "bot#1", "latency_ms": 30, "voice": voice or []}}

    def test_boot_connects_loads_sources_and_channels(self):
        self.pump()
        calls = [c[0] for c in FakeClient.instances[0].calls]
        self.assertIn("login", calls); self.assertIn("list_sources", calls)
        self.c._on_event(self.state()); self.pump(100)
        self.assertEqual(self.w.src_list.count(), 2)
        self.assertEqual(self.w.channel_box.count(), 1)
        self.assertIn("unavailable", self.w.src_list.item(1).text())

    def test_status_widgets_and_button_states_follow_engine_state(self):
        self.pump(); self.c._on_event(self.state()); self.pump(50)
        self.assertTrue(self.w.b_start.isEnabled()); self.assertFalse(self.w.b_stop.isEnabled())
        self.c._on_event(self.state("running", voice=[{"guild": "G", "channel": "General", "playing": True}])); self.pump(50)
        self.assertFalse(self.w.b_start.isEnabled()); self.assertTrue(self.w.b_stop.isEnabled())
        self.assertIn("44,100 Hz →", self.w.l_fmt.text())
        self.assertAlmostEqual(self.w.meter.peak, 0.5)

    def test_start_and_stop_send_expected_engine_commands(self):
        self.pump(); self.c._on_event(self.state()); self.pump(100)
        self.w.start_stream(); self.pump(500)
        calls = FakeClient.instances[0].calls
        self.assertIn("start_capture", [c[0] for c in calls])
        self.assertIn(("join", {"guild_id": 1, "channel_id": 2}), [(c[0], c[1]) for c in calls if c[0] == "join"])
        self.c._on_event(self.state("running")); self.pump(50)
        self.w.stop_stream(); self.pump(500)
        names = [c[0] for c in FakeClient.instances[0].calls]
        self.assertEqual(names[-2:], ["leave", "stop_capture"])

    def test_missing_token_blocks_start_and_points_to_settings(self):
        self.c.secrets.tok = None
        self.w.start_stream(); self.pump(100)
        self.assertEqual(self.w.tabs.currentIndex(), 2)
        self.assertNotIn("start_capture", [c[0] for i in FakeClient.instances for c in i.calls])

    def test_silence_hint_and_error_states(self):
        self.pump()
        self.c._on_event(self.state("running", silent_s=12.0)); self.pump(50)
        self.assertIn("No signal", self.w.l_hint.text())
        self.c._on_event(self.state("error", last_error="macOS blocked audio capture")); self.pump(50)
        self.assertIn("macOS blocked", self.w.p_capture._val.text())

    def test_close_while_streaming_hides_to_tray_else_quits_and_stops_engine(self):
        self.pump(); self.c._on_event(self.state("running")); self.pump(50)
        if self.w.tray is None:
            self.skipTest("no system tray in this environment")
        self.w.show(); self.w.close(); self.pump(50)
        self.assertFalse(self.w.isVisible()); self.assertFalse(self.w._quitting)
        self.c._on_event(self.state("stopped")); self.w.show(); self.w.close(); self.pump(50)
        self.assertTrue(self.w._quitting)
        self.assertEqual(FakeClient.instances[0].calls[-1], ("stop",))

    def test_quit_shuts_engine_down(self):
        self.pump(); self.w.quit_app()
        self.assertEqual(FakeClient.instances[0].calls[-1], ("stop",))

    def test_token_save_never_displayed_back(self):
        self.pump()
        self.w.token.setText("secret-token-value"); self.w.save_token(); self.pump(100)
        self.assertEqual(self.w.token.text(), "")
        self.assertNotIn("secret-token-value", self.w.token_state.text() + self.w.msg.text())

    def test_apply_settings_persists_quality(self):
        self.pump()
        self.w.buf.setValue(250); self.w.apply_settings(); self.pump(300)
        self.assertEqual(self.c.settings.quality["buffer_ms"], 250)

    def test_launch_at_login_files_written_and_removed(self):
        from ui import autostart
        with patch.object(autostart.Path, "home", return_value=__import__("pathlib").Path(self.tmp.name)), \
             patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            self.assertFalse(autostart.is_enabled())
            autostart.set_enabled(True); self.assertTrue(autostart.is_enabled())
            autostart.set_enabled(False); self.assertFalse(autostart.is_enabled())


if __name__ == "__main__":
    unittest.main()
