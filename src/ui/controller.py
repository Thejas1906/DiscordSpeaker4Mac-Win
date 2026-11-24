"""Qt-facing controller: owns the engine process and exposes simple actions + signals."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

from service.client import EngineClient, EngineError
from service.settings import AppSettings, SecretStore, SecretStoreError

log = logging.getLogger("DiscordSpeaker.ui")


class Controller(QObject):
    state = Signal(dict)          # {"audio": {...}, "discord": {...}}
    sources = Signal(list)
    channels = Signal(list)
    message = Signal(str, str)    # text, level (info|warn|error)
    engine_up = Signal(bool)
    busy = Signal(bool)

    def __init__(self, settings: AppSettings, secrets: Optional[SecretStore] = None, client_factory=EngineClient):
        super().__init__()
        self.settings = settings
        self.secrets = secrets or SecretStore()
        self._factory = client_factory
        self.client: Optional[EngineClient] = None
        self.last_state: dict = {}
        self.platform = ""
        self._op_lock = threading.Lock()
        self._logged_in_once = False

    # ---- engine lifecycle --------------------------------------------------------------
    def _on_event(self, ev: dict) -> None:
        name = ev.get("event")
        if name == "state":
            self.last_state = ev
            self.state.emit(ev)
            if ev["discord"].get("logged_in") and not self._logged_in_once:
                self._logged_in_once = True
                # Never call client.request() on the engine-reader thread: it would wait for a reply that
                # only this same thread can deliver (deadlock -> "list_channels timed out").
                threading.Thread(target=self.refresh_channels, daemon=True).start()
        elif name == "discord_error":
            self.message.emit(ev.get("message", "Discord error"), "error")
        elif name == "audio_event":
            lvl = "error" if ev.get("kind") in ("error",) else "warn"
            if ev.get("kind") != "recovered":
                self.message.emit(f"Audio: {ev.get('message', '')}", lvl)
            else:
                self.message.emit("Audio capture recovered.", "info")
        elif name == "engine_exit":
            self.engine_up.emit(False)

    def ensure_engine(self) -> None:
        if self.client and self.client.alive:
            return
        self.client = self._factory(on_event=self._on_event)
        self.client.start()
        info = self.client.request("hello")
        self.platform = info.get("platform", "")
        self.engine_up.emit(True)
        self.client.request("apply_quality", quality=self.settings.quality)
        self.client.request("set_volume", percent=self.settings.volume_percent)
        self.refresh_sources()

    def connect_discord(self) -> None:
        """Log the bot in (idempotent) using the token from the OS credential store."""
        self.ensure_engine()
        token = self.secrets.get()
        if not token:
            raise EngineError("No bot token saved. Open Settings and add your Discord bot token.")
        self._logged_in_once = False
        self.client.request("login", token=token)

    def shutdown(self) -> None:
        if self.client:
            self.client.stop()
            self.client = None

    # ---- actions (called from worker threads) --------------------------------------------
    def refresh_sources(self) -> None:
        try:
            self.sources.emit(self.client.request("list_sources", timeout=20))
        except EngineError as e:
            self.message.emit(str(e), "warn")

    def refresh_channels(self) -> None:
        try:
            self.channels.emit(self.client.request("list_channels"))
        except EngineError as e:
            self.message.emit(str(e), "warn")

    def select_source(self, source_id: str) -> None:
        self.settings.source_id = source_id
        self.settings.save()
        if self.client and self.client.alive:
            self.client.request("select_source", source_id=source_id, timeout=20)

    def set_volume(self, percent: int) -> None:
        self.settings.volume_percent = percent
        if self.client and self.client.alive:
            try:
                self.client.request("set_volume", percent=percent)
            except EngineError:
                pass

    def start_streaming(self, guild_id: int = 0, channel_id: int = 0) -> None:
        with self._op_lock:
            self.ensure_engine()
            self.client.request("start_capture", source_id=self.settings.source_id or None, timeout=30)
            if guild_id and channel_id:
                self.settings.guild_id, self.settings.channel_id = guild_id, channel_id
                self.settings.save()
                self.client.request("join", guild_id=guild_id, channel_id=channel_id, timeout=30)

    def stop_streaming(self) -> None:
        with self._op_lock:
            if self.client and self.client.alive:
                self.client.request("leave", timeout=15)
                self.client.request("stop_capture", timeout=15)

    def apply_quality(self, quality: dict) -> bool:
        self.settings.quality = quality
        self.settings.save()
        if self.client and self.client.alive:
            return bool(self.client.request("apply_quality", quality=quality).get("restart_required"))
        return False

    @property
    def streaming(self) -> bool:
        a = self.last_state.get("audio", {})
        return a.get("state") in ("starting", "running", "recovering")

    def save_token(self, token: str) -> None:
        self.secrets.set(token)
