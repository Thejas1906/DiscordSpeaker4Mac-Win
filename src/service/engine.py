"""The DiscordSpeaker engine process: Discord bot + audio capture, controlled over stdin/stdout JSON lines.

Why stdio and not a socket: no network port, nothing for other local users to connect to, and the
engine ends automatically when the controller dies (stdin EOF), so no orphaned bot/capture processes.
The bot token is delivered over this private pipe (never argv/env) and is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional

from . import logsetup
from .paths import config_dir

log = logging.getLogger("DiscordSpeaker.engine")
PROTOCOL = 1


class Engine:
    def __init__(self, out=None, inp=None):
        self.out = out or sys.stdout
        self.inp = inp or sys.stdin
        self._out_lock = threading.Lock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.bot = None
        self.bot_task: Optional[asyncio.Task] = None
        self.lock = None
        self.audio = None
        self.discord_error = ""
        self._stop: Optional[asyncio.Event] = None
        self.preferred_source: Optional[str] = None
        self._auto_started = False

    # ---- protocol -------------------------------------------------------------------
    def emit(self, event: str, **data) -> None:
        line = json.dumps({"event": event, **data}, default=str)
        with self._out_lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (BrokenPipeError, ValueError):
                if self.loop and self._stop:
                    self.loop.call_soon_threadsafe(self._stop.set)

    def reply(self, rid, ok: bool, **data) -> None:
        line = json.dumps({"id": rid, "ok": ok, **data}, default=str)
        with self._out_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def _reader(self) -> None:
        for line in self.inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            asyncio.run_coroutine_threadsafe(self._handle(msg), self.loop)
        self.loop.call_soon_threadsafe(self._stop.set)     # controller closed the pipe

    async def _handle(self, msg: Dict[str, Any]) -> None:
        rid, cmd = msg.get("id"), msg.get("cmd")
        try:
            fn = getattr(self, f"cmd_{cmd}", None)
            if fn is None:
                raise ValueError(f"unknown command '{cmd}'")
            res = fn(**{k: v for k, v in msg.items() if k not in ("id", "cmd")})
            if asyncio.iscoroutine(res):
                res = await res
            self.reply(rid, True, result=res)
        except Exception as e:
            log.warning("command %s failed: %s", cmd, e)
            self.reply(rid, False, error=logsetup.redact(f"{type(e).__name__}: {e}"))

    # ---- commands ---------------------------------------------------------------------
    def cmd_hello(self):
        return {"protocol": PROTOCOL, "platform": self.audio.platform}

    def cmd_state(self):
        return self._snapshot()

    async def cmd_list_sources(self):
        return await self.loop.run_in_executor(None, self.audio.sources)

    async def cmd_list_devices(self):
        return await self.loop.run_in_executor(None, self.audio.devices_list)

    async def cmd_list_channels(self):
        out = []
        if self.bot:
            for g in self.bot.guilds:
                for ch in g.voice_channels + list(g.stage_channels):
                    out.append({"guild_id": g.id, "guild": g.name, "channel_id": ch.id, "channel": ch.name})
        return out

    async def cmd_select_source(self, source_id: str):
        self.preferred_source = source_id
        # (named source_id: a keyword called "id" would collide with the request id in the JSON envelope)
        await self.loop.run_in_executor(None, self.audio.select_source, source_id)
        return True

    def cmd_set_volume(self, percent: int):
        self.audio.set_volume(int(percent))
        return True

    async def cmd_apply_quality(self, quality: dict):
        from audio.quality_config import QualityConfig
        cfg = QualityConfig.from_dict(quality)
        was = self.audio.running
        self.audio.apply_config(cfg)
        return {"restart_required": was}

    async def cmd_start_capture(self, source_id: Optional[str] = None):
        await self.loop.run_in_executor(None, self.audio.start, source_id or None)
        return True

    async def cmd_stop_capture(self):
        await self.loop.run_in_executor(None, self.audio.stop)
        return True

    async def cmd_route_app(self, app: str):
        ok, msg = await self.loop.run_in_executor(None, self.audio.route_application, app)
        return {"ok": ok, "message": msg}

    async def cmd_login(self, token: str, source_id: Optional[str] = None):
        """Start the Discord bot. Rejects a second process using the same token."""
        from service.instance import AlreadyRunning, InstanceLock
        import bot as botmod
        if self.bot_task and not self.bot_task.done():
            return {"already": True}
        logsetup.register_secret(token)
        lock = InstanceLock.for_token(token)
        try:
            lock.acquire()
        except AlreadyRunning:
            raise RuntimeError("Another DiscordSpeaker process is already using this bot token.")
        self.lock = lock
        self.discord_error = ""
        self.bot = botmod.bot
        self.bot.audio = self.audio
        self.bot.stop_capture_on_leave = False
        self.preferred_source = source_id or None
        self._auto_started = False
        self.bot.on_state_change = self._on_discord_state
        self.bot_task = asyncio.create_task(self._run_bot(token))
        return {"starting": True}

    def _on_discord_state(self, s: dict) -> None:
        self.emit("discord", **s)
        if s.get("logged_in") and not self._auto_started:
            self._auto_started = True
            self.loop.create_task(self._auto_start(s.get("name") or "DiscordSpeaker"))

    async def _auto_start(self, bot_name: str) -> None:
        """Bot is online: expose it in Spotify as '<bot name>' (Spotify Connect) and start receiving."""
        self.audio.configure_spotify(device_name=bot_name)
        try:
            await self.loop.run_in_executor(None, self.audio.start, self.preferred_source or None)
        except Exception as e:
            log.warning("auto-start capture failed: %s", e)
            self.emit("audio_event", kind="error", message=logsetup.redact(str(e)))

    async def _run_bot(self, token: str) -> None:
        import discord
        try:
            await self.bot.start(token)
        except discord.LoginFailure:
            self.discord_error = "Login failed: the bot token was rejected by Discord."
        except discord.PrivilegedIntentsRequired:
            self.discord_error = "Enable 'Message Content Intent' for the bot in the Discord Developer Portal."
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.discord_error = logsetup.redact(f"{type(e).__name__}: {e}")
        if self.discord_error:
            log.error("discord: %s", self.discord_error)
            self.emit("discord_error", message=self.discord_error)

    async def cmd_join(self, guild_id: int, channel_id: int):
        if not self.bot or not self.bot.is_ready():
            raise RuntimeError("Discord is not connected yet")
        guild = self.bot.get_guild(int(guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if channel is None:
            raise RuntimeError("Voice channel not found")
        vc, err = await self.bot.connect_and_stream(guild, channel)
        if err:
            raise RuntimeError(err.replace("❌ ", "").replace("`", ""))
        return True

    async def cmd_leave(self):
        if self.bot:
            for g in list(self.bot.guilds):
                await self.bot.leave_guild(g)
        return True

    async def cmd_logout(self):
        await self._close_bot()
        return True

    async def cmd_shutdown(self):
        self.loop.call_soon(self._stop.set)
        return True

    # ---- lifecycle ---------------------------------------------------------------------
    def _snapshot(self) -> dict:
        d = self.bot.discord_status() if self.bot else {"logged_in": False, "voice": []}
        if self.discord_error:
            d["error"] = self.discord_error
        return {"audio": self.audio.status(), "discord": d}

    async def _ticker(self) -> None:
        while True:
            self.emit("state", **self._snapshot())
            await asyncio.sleep(0.1)

    async def _close_bot(self) -> None:
        if self.bot:
            try:
                for g in list(self.bot.guilds):
                    await self.bot.leave_guild(g)
                await asyncio.wait_for(self.bot.close(), 5)
            except Exception as e:
                log.warning("bot close: %s", e)
        if self.bot_task:
            self.bot_task.cancel()
            try:
                await self.bot_task
            except BaseException:
                pass
            self.bot_task = None
        if self.lock:
            self.lock.release()
            self.lock = None

    async def run(self) -> int:
        from audio.quality_config import QualityConfig
        from audio.service import AudioService
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.audio = AudioService(QualityConfig.from_env())
        self.audio.configure_spotify(cache_dir=str(config_dir() / "spotify"))
        self.audio.add_listener(lambda ev: self.emit("audio_event", kind=ev.kind, message=logsetup.redact(ev.message)))
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):   # Windows
                signal.signal(sig, lambda *_: self.loop.call_soon_threadsafe(self._stop.set))
        threading.Thread(target=self._reader, name="ipc-reader", daemon=True).start()
        ticker = asyncio.create_task(self._ticker())
        self.emit("ready", protocol=PROTOCOL, platform=self.audio.platform)
        await self._stop.wait()
        log.info("engine shutting down")
        ticker.cancel()
        await self._close_bot()
        await self.loop.run_in_executor(None, self.audio.stop)
        self.emit("stopped")
        return 0


def main() -> int:
    logsetup.setup("engine")
    real_out = sys.stdout
    sys.stdout = sys.stderr           # stray prints can never corrupt the protocol stream
    eng = Engine(out=real_out)
    try:
        return asyncio.run(eng.run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
