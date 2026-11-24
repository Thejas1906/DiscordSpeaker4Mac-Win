"""Process lifecycle, settings/secret handling, logging redaction, single-instance locks, bot wiring."""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import _path  # noqa: F401
from _path import pid_alive
from audio.models import CaptureState
from audio.quality_config import QualityConfig
from service import logsetup
from service.client import EngineClient, EngineError
from service.instance import AlreadyRunning, InstanceLock
from service.settings import AppSettings, SecretStore, SecretStoreError

# Assembled at runtime: a literal token-shaped string trips secret scanners (GitHub push protection)
FAKE_TOKEN = ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ"[:20] + "ABCDEFGH", "Gabc" + "DE", "x" * 27 + "0123456789"])


class FakeKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, s, u):
        return self.d.get((s, u))

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def delete_password(self, s, u):
        self.d.pop((s, u), None)


class SettingsAndSecrets(unittest.TestCase):
    def test_token_goes_to_credential_store_never_to_disk(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DS_CONFIG_DIR": d}):
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            kr = FakeKeyring()
            store = SecretStore(kr)
            store.set(FAKE_TOKEN)
            s = AppSettings(source_id="x"); s.save()
            self.assertEqual(store.get(), FAKE_TOKEN)
            for p in Path(d).rglob("*"):
                if p.is_file():
                    self.assertNotIn(FAKE_TOKEN, p.read_text(errors="ignore"), f"token leaked into {p}")
            store.delete()
            self.assertIsNone(store.get())

    def test_no_plaintext_fallback_when_no_credential_store(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_BOT_TOKEN", None)
            class Broken(SecretStore):
                def _keyring(self):
                    raise SecretStoreError("No OS credential store is available")
            with self.assertRaises(SecretStoreError):
                Broken().set(FAKE_TOKEN)
            self.assertIsNone(Broken().get())

    def test_env_var_token_supported_for_headless_use(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": FAKE_TOKEN}):
            self.assertEqual(SecretStore(FakeKeyring()).get(), FAKE_TOKEN)
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "YOUR_BOT_TOKEN_HERE"}):
            self.assertIsNone(SecretStore(FakeKeyring()).get())

    def test_settings_roundtrip_and_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            s = AppSettings(source_id="tap:system", volume_percent=80, minimize_to_tray=False)
            s.quality["buffer_ms"] = 200
            s.save(p)
            r = AppSettings.load(p)
            self.assertEqual((r.source_id, r.volume_percent, r.quality["buffer_ms"]), ("tap:system", 80, 200))
            p.write_text("{not json")
            self.assertEqual(AppSettings.load(p).volume_percent, 100)
            p.write_text(json.dumps({"quality": {"buffer_ms": 5}}))
            self.assertEqual(AppSettings.load(p).quality_config().buffer_ms, 120)   # invalid -> defaults

    def test_old_settings_migrate_to_spotify_connect_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"source_id": "tap:app:Spotify", "volume_percent": 90}))   # v1 file, no schema
            s = AppSettings.load(p)
            self.assertEqual((s.source_id, s.volume_percent, s.schema), ("", 90, 2))
            s.source_id = "tap:system"; s.save(p)
            self.assertEqual(AppSettings.load(p).source_id, "tap:system")                      # explicit v2 choice kept

    def test_saved_boost_above_100_is_not_restored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps({"schema": 2, "volume_percent": 200}))
            self.assertEqual(AppSettings.load(p).volume_percent, 100)

    def test_quality_defaults_are_music_safe(self):
        c = QualityConfig()
        self.assertEqual((c.volume, c.limiter), (1.0, True))
        with self.assertRaises(ValueError):
            QualityConfig(volume=3.0).validate()
        with self.assertRaises(ValueError):
            QualityConfig(buffer_ms=10).validate()
        self.assertEqual(QualityConfig.from_env({"DS_OPUS_BITRATE_KBPS": "256", "DS_DITHER": "false"}).opus_bitrate_kbps, 256)


class LoggingRedaction(unittest.TestCase):
    def test_tokens_are_redacted_in_files_and_streams(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DS_LOG_DIR": d}):
            import io
            buf = io.StringIO()
            path = logsetup.setup("t", stream=buf)
            logsetup.register_secret("exact-secret-value")
            import logging
            log = logging.getLogger("t")
            log.info("login with %s failed", FAKE_TOKEN)
            log.error("secret exact-secret-value leaked?")
            try:
                raise RuntimeError(f"boom {FAKE_TOKEN}")
            except RuntimeError:
                log.exception("failed")
            for h in logging.getLogger().handlers:
                h.flush()
            blob = path.read_text() + buf.getvalue()
            self.assertNotIn(FAKE_TOKEN, blob)
            self.assertNotIn("exact-secret-value", blob)
            self.assertIn("[REDACTED]", blob)
            for line in path.read_text().splitlines():
                json.loads(line)                         # structured: every line is JSON
            logging.getLogger().handlers.clear()
            logsetup._secrets.clear()


class InstanceLocks(unittest.TestCase):
    def test_second_holder_is_rejected_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = InstanceLock(Path(d) / "x.lock"), InstanceLock(Path(d) / "x.lock")
            a.acquire()
            with self.assertRaises(AlreadyRunning):
                b.acquire()
            a.release()
            b.acquire(); b.release()

    def test_lock_is_released_when_holder_process_dies(self):
        with tempfile.TemporaryDirectory() as d:
            code = ("import sys,time;sys.path.insert(0,%r);from service.instance import InstanceLock;"
                    "l=InstanceLock(%r);l.acquire();print('held',flush=True);time.sleep(60)") % (_path.SRC, str(Path(d) / "k.lock"))
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            self.assertEqual(p.stdout.readline().strip(), "held")
            with self.assertRaises(AlreadyRunning):
                InstanceLock(Path(d) / "k.lock").acquire()
            p.kill(); p.wait()
            lk = InstanceLock(Path(d) / "k.lock"); lk.acquire(); lk.release()

    def test_token_lock_is_keyed_by_hash_and_never_contains_token(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"DS_CONFIG_DIR": d}):
            l = InstanceLock.for_token(FAKE_TOKEN)
            self.assertNotIn(FAKE_TOKEN[:12], str(l.path))
            l.acquire()
            with self.assertRaises(AlreadyRunning):
                InstanceLock.for_token(FAKE_TOKEN).acquire()
            InstanceLock.for_token(FAKE_TOKEN + "x").acquire()   # different token is independent
            l.release()


class EngineProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        env = dict(os.environ, DS_CONFIG_DIR=self.tmp.name, DS_LOG_DIR=self.tmp.name)
        env.pop("DISCORD_BOT_TOKEN", None)
        self.events = []
        self.client = EngineClient(on_event=self.events.append, env=env)
        self.addCleanup(lambda: self.client.stop(force=True))

    def test_start_stop_leaves_no_orphan(self):
        self.client.start()
        self.assertEqual(self.client.request("hello")["protocol"], 1)
        pid = self.client.pid
        self.client.stop()
        time.sleep(0.3)
        self.assertFalse(pid_alive(pid))
        self.assertIn("stopped", [e["event"] for e in self.events])

    def test_engine_exits_when_controller_vanishes(self):
        """Controller crash == stdin EOF: the engine must shut down by itself."""
        self.client.start()
        pid = self.client.pid
        proc = self.client._proc
        proc.stdin.close()
        try:
            proc.wait(timeout=10)            # also reaps the child (an un-reaped zombie still "exists")
        except subprocess.TimeoutExpired:
            self.fail("engine did not exit after the controller closed its pipe")
        self.assertFalse(pid_alive(pid))

    def test_state_stream_and_commands(self):
        self.client.start()
        for _ in range(50):
            if any(e["event"] == "state" for e in self.events):
                break
            time.sleep(0.05)
        st = next(e for e in self.events if e["event"] == "state")
        self.assertEqual(st["audio"]["state"], "stopped")
        self.assertEqual(st["audio"]["output_rate"], 48000)
        self.client.request("set_volume", percent=50)
        self.assertEqual(self.client.request("state")["audio"]["volume"], 50)
        with self.assertRaises(EngineError):
            self.client.request("nope")
        self.assertEqual(self.client.request("apply_quality", quality=QualityConfig().to_dict()), {"restart_required": False})
        with self.assertRaises(EngineError):
            self.client.request("apply_quality", quality={"buffer_ms": 1})

    def test_select_source_roundtrip_and_reserved_names(self):
        """Regression: an argument named 'id' overwrote the request id (select_source never got a reply)."""
        self.client.start()
        with self.assertRaises(ValueError):
            self.client.request("select_source", id="x")
        self.assertTrue(self.client.request("select_source", source_id="tap:system", timeout=20) is True)

    def test_join_without_discord_is_a_clean_error(self):
        self.client.start()
        with self.assertRaises(EngineError) as cm:
            self.client.request("join", guild_id=1, channel_id=2)
        self.assertIn("not connected", str(cm.exception))

    def test_second_engine_with_same_token_is_refused(self):
        """Two engines, one token: the second login fails before touching the network."""
        env = dict(os.environ, DS_CONFIG_DIR=self.tmp.name, DS_LOG_DIR=self.tmp.name)
        other = EngineClient(env=env); self.addCleanup(lambda: other.stop(force=True))
        self.client.start(); other.start()
        lock = InstanceLock.for_token(FAKE_TOKEN)
        with patch.dict(os.environ, {"DS_CONFIG_DIR": self.tmp.name}):
            lock = InstanceLock.for_token(FAKE_TOKEN)
            lock.acquire()
            try:
                with self.assertRaises(EngineError) as cm:
                    other.request("login", token=FAKE_TOKEN)
                self.assertIn("already using this bot token", str(cm.exception))
                self.assertNotIn(FAKE_TOKEN, str(cm.exception))
            finally:
                lock.release()


class BotWiringTests(unittest.TestCase):
    def setUp(self):
        import bot as botmod
        self.m = botmod

    def test_all_commands_registered_with_prefix_twins(self):
        for name in ("join", "leave", "status", "route_app", "route_spotify", "volume"):
            self.assertIsNotNone(self.m.bot.tree.get_command(name), name)
        for name in ("join", "leave", "status", "route_app", "route", "volume"):
            self.assertIsNotNone(self.m.bot.get_command(name), name)

    def test_volume_command_drives_pipeline_not_a_transformer(self):
        from audio.service import AudioService
        from test_backends import pulse_fake
        from audio.pipeline import AudioPipeline
        from audio.base import CaptureBackend

        class Null(CaptureBackend):
            platform = "test"
            def list_devices(self): return []
            def list_sources(self): return []
            def _run_capture(self, s, stop): stop.wait()
        svc = AudioService(QualityConfig(), backend=Null(AudioPipeline()))
        self.m.bot.audio = svc
        self.m.bot.audio.set_volume(150)
        self.assertAlmostEqual(self.m.bot.current_volume, 1.5)
        self.m.bot._audio = None

    def test_route_any_uses_native_backend_off_linux(self):
        class B:
            def route_application(self, a): return True, f"native:{a}"
        class S:
            backend = B()
            def route_application(self, a): return self.backend.route_application(a)
        self.m.bot.audio = S()
        with patch.object(self.m, "IS_LINUX", False):
            self.assertEqual(self.m.route_any("x"), (True, "native:x"))
            self.assertEqual(self.m.route_spotify_any(), (True, "native:spotify"))
        self.m.bot._audio = None

    def test_missing_token_exits_cleanly_in_main_not_at_import(self):
        with patch.object(self.m, "TOKEN", None), self.assertRaises(SystemExit) as cm:
            self.m.main()
        self.assertEqual(cm.exception.code, 1)

    def test_bot_status_reports_without_login(self):
        d = self.m.bot.discord_status()
        self.assertFalse(d["logged_in"])
        self.assertEqual(d["voice"], [])


if __name__ == "__main__":
    unittest.main()
