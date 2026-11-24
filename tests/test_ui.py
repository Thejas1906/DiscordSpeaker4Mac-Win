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
                 overruns=0, drift_corrections=0, clipped_samples=0, frames_out=96000, selected="a", last_error="", silent_s=0)
        a.update(extra)
        return {"event": "state", "audio": a, "discord": {"logged_in": logged, "user": "bot#1", "latency_ms": 30, "voice": voice or []}}

    def state(self, audio="running", voice=None, logged=True, guilds=1, spotify=None, **extra):
        a = dict(state=audio, peak=0.5, rms=0.2, input_rate=44100, input_channels=2, latency_ms=140.0, underruns=0,
                 overruns=0, drift_corrections=0, clipped_samples=0, frames_out=96000, selected="spotify:connect",
                 last_error="", silent_s=0,
                 spotify=spotify or {"device_name": "Music Speaker", "now_playing": "", "user": "", "connected": False})
        a.update(extra)
        return {"event": "state", "audio": a, "discord": {"logged_in": logged, "name": "Music Speaker", "user": "Music Speaker#1",
                                                          "user_id": 42, "latency_ms": 30, "guilds": guilds, "voice": voice or []}}

    def names(self):
        return [c[0] for i in FakeClient.instances for c in i.calls]

    def test_nothing_starts_until_the_user_presses_start(self):
        self.pump()
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(self.w.b_main.text().strip("▶ ").strip(), "Start")

    def test_start_logs_in_and_loads_channels_and_there_is_no_devices_tab(self):
        self.w.start_bot(); self.pump(400)
        self.assertIn("login", self.names())
        login = next(c for c in FakeClient.instances[0].calls if c[0] == "login")
        self.assertEqual(login[1]["token"], "x")
        self.c._on_event(self.state()); self.pump(300)
        self.assertEqual(self.w.channel_box.count(), 1)
        self.assertEqual(self.w.tabs.count(), 2)
        self.assertEqual([self.w.tabs.tabText(i) for i in range(2)], ["Dashboard", "Settings"])

    def test_setup_steps_follow_engine_state(self):
        self.w._started = True
        self.c._on_event(self.state(guilds=0, audio="running")); self.pump(50)
        self.assertIn("Not in any server", self.w.s_server.detail.text())
        self.assertTrue(self.w.s_server.button.isVisibleTo(self.w) or self.w.s_server.button.isEnabled())
        self.c._on_event(self.state(guilds=1)); self.pump(50)
        self.assertIn("In 1 server", self.w.s_server.detail.text())
        self.assertIn("Connect to a device", self.w.s_spotify.detail.text())
        self.assertIn("“Music Speaker”", self.w.s_spotify.detail.text())
        self.c._on_event(self.state(spotify={"device_name": "Music Speaker", "now_playing": "Blinding Lights", "user": "me", "connected": True}))
        self.pump(50)
        self.assertIn("Now playing: Blinding Lights", self.w.s_spotify.detail.text())
        self.c._on_event(self.state(voice=[{"guild": "G", "channel": "Music", "playing": True}])); self.pump(50)
        self.assertIn("Streaming in Music", self.w.s_voice.detail.text())
        self.assertEqual(self.w.b_main.text().strip("■ ").strip(), "Stop")
        self.assertAlmostEqual(self.w.meter.peak, 0.5)

    def test_receiver_errors_are_visible(self):
        self.w._started = True
        self.c._on_event(self.state(audio="error", last_error="Spotify Connect needs 'librespot'")); self.pump(50)
        self.assertIn("librespot", self.w.s_spotify.detail.text())

    def test_stop_shuts_the_engine_down(self):
        self.w.start_bot(); self.pump(400)
        self.w.stop_bot(); self.pump(400)
        self.assertEqual(FakeClient.instances[0].calls[-1], ("stop",))
        self.assertFalse(self.w._started)

    def test_missing_token_blocks_start_and_points_to_settings(self):
        self.c.secrets.tok = None
        self.w.start_bot(); self.pump(100)
        self.assertEqual(self.w.tabs.currentIndex(), 1)
        self.assertEqual(FakeClient.instances, [])

    def test_join_button_sends_join(self):
        self.w.start_bot(); self.pump(300); self.c._on_event(self.state()); self.pump(300)
        self.w.join_channel(); self.pump(300)
        self.assertIn(("join", {"guild_id": 1, "channel_id": 2}), [(c[0], c[1]) for c in FakeClient.instances[0].calls if c[0] == "join"])

    def test_channel_refresh_triggered_from_event_does_not_deadlock(self):
        """Regression: refresh_channels() ran on the engine-reader thread and timed out waiting for its own reply."""
        import threading
        self.w.start_bot(); self.pump(300)
        seen = {}
        orig = FakeClient.request
        def request(self, cmd, timeout=0, **a):
            if cmd == "list_channels":
                seen["thread"] = threading.current_thread()
            return orig(self, cmd, timeout, **a)
        with patch.object(FakeClient, "request", request):
            self.c._logged_in_once = False
            self.c._on_event(self.state()); self.pump(300)
        self.assertIn("thread", seen)
        self.assertIsNot(seen["thread"], threading.current_thread(), "must not run inline on the event thread")
        self.assertEqual(self.w.channel_box.count(), 1)

    def test_close_while_running_hides_to_tray_else_quits_and_stops_engine(self):
        self.w.start_bot(); self.pump(300)
        if self.w.tray is None:
            self.skipTest("no system tray in this environment")
        self.w.show(); self.w.close(); self.pump(50)
        self.assertFalse(self.w.isVisible()); self.assertFalse(self.w._quitting)
        self.w._started = False; self.w.show(); self.w.close(); self.pump(50)
        self.assertTrue(self.w._quitting)
        self.assertEqual(FakeClient.instances[0].calls[-1], ("stop",))

    def test_quit_shuts_engine_down(self):
        self.w.start_bot(); self.pump(300); self.w.quit_app()
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

    @unittest.skipIf(sys.platform == "win32", "writes the real HKCU Run key; covered by manual Windows testing")
    def test_launch_at_login_files_written_and_removed(self):
        from ui import autostart
        with patch.object(autostart.Path, "home", return_value=__import__("pathlib").Path(self.tmp.name)), \
             patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            self.assertFalse(autostart.is_enabled())
            autostart.set_enabled(True); self.assertTrue(autostart.is_enabled())
            autostart.set_enabled(False); self.assertFalse(autostart.is_enabled())


if __name__ == "__main__":
    unittest.main()
