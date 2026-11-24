"""Persistent user settings (JSON, no secrets) and the OS credential-store wrapper for the bot token."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from audio.quality_config import QualityConfig
from .paths import config_dir

KEYRING_SERVICE = "DiscordSpeaker"
KEYRING_USER = "bot-token"


class SecretStoreError(Exception):
    pass


class SecretStore:
    """Bot token lives only in the OS credential store (Keychain / Credential Manager / Secret Service).
    There is deliberately NO plaintext fallback. Headless setups use the DISCORD_BOT_TOKEN env var."""

    def __init__(self, keyring_module=None):
        self._kr = keyring_module

    def _keyring(self):
        if self._kr is None:
            try:
                import keyring
                from keyring.backends import fail
            except Exception as e:
                raise SecretStoreError(f"keyring is not installed: {e}")
            if isinstance(keyring.get_keyring(), fail.Keyring):
                raise SecretStoreError("No OS credential store is available (is a Secret Service / keyring daemon running?)")
            self._kr = keyring
        return self._kr

    def get(self) -> Optional[str]:
        env = os.environ.get("DISCORD_BOT_TOKEN")
        if env and not env.startswith("YOUR_"):
            return env
        try:
            return self._keyring().get_password(KEYRING_SERVICE, KEYRING_USER)
        except SecretStoreError:
            return None

    def set(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise SecretStoreError("Token is empty")
        self._keyring().set_password(KEYRING_SERVICE, KEYRING_USER, token)

    def delete(self) -> None:
        try:
            self._keyring().delete_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception:
            pass

    def has(self) -> bool:
        return bool(self.get())


@dataclass
class AppSettings:
    schema: int = 2                     # 2: Spotify Connect is the default source
    source_id: str = ""
    monitor_device: str = ""            # optional local monitoring output (informational on Win/mac)
    local_monitoring: bool = False
    guild_id: int = 0
    channel_id: int = 0
    volume_percent: int = 100
    minimize_to_tray: bool = True
    start_minimized: bool = False
    launch_at_login: bool = False
    auto_stream_on_launch: bool = False
    quality: dict = field(default_factory=lambda: QualityConfig().to_dict())

    def quality_config(self) -> QualityConfig:
        return QualityConfig.from_dict(self.quality)

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppSettings":
        p = Path(path) if path else cls.path()
        try:
            data = json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known})
        # Boost above 100% only clips loud masters (Spotify has its own volume), so never restore one.
        s.volume_percent = max(1, min(int(s.volume_percent), 100))
        if int(data.get("schema", 1)) < 2:
            s.source_id = ""        # older versions saved a system-capture source; the new default is Spotify Connect
            s.schema = 2
        try:
            s.quality_config()
        except ValueError:
            s.quality = QualityConfig().to_dict()
        return s

    def save(self, path: Optional[Path] = None) -> None:
        p = Path(path) if path else self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".settings-")
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, p)
