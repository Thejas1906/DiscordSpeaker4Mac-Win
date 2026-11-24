"""AudioService: the one object the bot, the engine process and the CLI use to run capture.

It owns the pipeline and the OS backend and exposes a small, consistent control surface:
start / stop / select_source / sources / route_application / volume / status.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional, Tuple

from .base import CaptureBackend
from .device_manager import DeviceManager, create_backend, current_platform
from .models import CaptureEvent, CaptureState
from .pipeline import AudioPipeline
from .quality_config import QualityConfig

log = logging.getLogger("DiscordSpeaker.service")


class AudioService:
    def __init__(self, config: Optional[QualityConfig] = None, backend: Optional[CaptureBackend] = None,
                 on_event: Optional[Callable[[CaptureEvent], None]] = None, **backend_kwargs):
        self.config = (config or QualityConfig()).validate()
        self._listeners: List[Callable[[CaptureEvent], None]] = [on_event] if on_event else []
        self.pipeline = AudioPipeline(self.config, self._dispatch)
        self.backend = backend or create_backend(self.pipeline, self._dispatch, **backend_kwargs)
        if backend is not None:
            self.backend.pipeline = self.pipeline
            self.pipeline.on_event = self._dispatch
            self.backend._on_event = self._dispatch
        self.devices = DeviceManager(self.backend)
        self.platform = self.backend.platform
        self._lock = threading.RLock()

    def add_listener(self, fn: Callable[[CaptureEvent], None]) -> None:
        self._listeners.append(fn)

    def _dispatch(self, ev: CaptureEvent) -> None:
        log.info("audio event: %s %s", ev.kind, ev.message)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("listener failed")

    # ---- control --------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.backend.state in (CaptureState.STARTING, CaptureState.RUNNING, CaptureState.RECOVERING)

    def start(self, source_id: Optional[str] = None) -> None:
        with self._lock:
            if source_id:
                self.devices.selected = source_id
            self.backend.start(source_id or self.devices.selected)

    def stop(self) -> None:
        with self._lock:
            self.backend.stop()

    def select_source(self, source_id: str) -> None:
        with self._lock:
            self.devices.select(source_id)

    def sources(self) -> List[Dict]:
        return self.devices.refresh()

    def configure_spotify(self, device_name: Optional[str] = None, cache_dir: Optional[str] = None,
                          make_default: bool = True) -> None:
        """Name of the Spotify Connect device (the bot's name) and where librespot keeps its credentials."""
        if device_name:
            self.backend.device_name = device_name
        if cache_dir:
            self.backend.spotify_cache_dir = cache_dir
        if make_default:
            self.backend.default_source_id = "spotify:connect"

    def devices_list(self) -> List[Dict]:
        return [d.to_dict() for d in self.backend.list_devices()]

    def route_application(self, application: str) -> Tuple[bool, str]:
        return self.backend.route_application(application)

    def applications(self) -> List[str]:
        return self.backend.list_applications()

    def set_volume(self, percent: int) -> None:
        self.pipeline.set_volume(percent / 100.0)

    @property
    def volume_percent(self) -> int:
        return int(round(self.pipeline.volume * 100))

    def apply_config(self, config: QualityConfig) -> None:
        """Hot-apply settings that don't need a capture restart (volume); others take effect on next start."""
        self.config = config.validate()
        self.pipeline.config = self.config
        self.pipeline.set_volume(config.volume)

    def status(self) -> Dict:
        h = self.pipeline.health.to_dict()
        h.update(spotify=self.backend.spotify.as_dict(), platform=self.platform, selected=self.devices.selected or self.backend.source_id,
                 volume=self.volume_percent, supports_app_capture=self.backend.supports_app_capture)
        return h
