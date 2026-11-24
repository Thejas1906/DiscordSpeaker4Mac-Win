"""Platform-independent capture interface plus a shared supervision loop.

Backends implement discovery and one blocking `_run_capture()`; the base class
owns thread lifecycle, state, reconnect/back-off and error reporting, so every OS
behaves the same way for the bot and the UI.
"""
from __future__ import annotations

import abc
import logging
import threading
from typing import Callable, List, Optional, Tuple

from .models import (AudioError, CaptureEvent, CaptureSource, CaptureState,
                     BackendUnavailableError, DeviceInfo, DeviceUnavailableError, PermissionDeniedError)
from .pipeline import AudioPipeline
from . import spotify_connect

log = logging.getLogger("DiscordSpeaker.audio")


class CaptureBackend(abc.ABC):
    platform = "generic"
    supports_app_capture = False
    RETRY_DELAYS = (0.5, 1.0, 2.0, 5.0)

    def __init__(self, pipeline: AudioPipeline, on_event: Optional[Callable[[CaptureEvent], None]] = None):
        self.pipeline = pipeline
        self._on_event = on_event or (lambda e: None)
        self._state = CaptureState.STOPPED
        self._source_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._proc = None                       # helper process owned by the active capture (if any)
        # Spotify Connect receiver (available on every OS through the same pipeline)
        self.device_name = "DiscordSpeaker"
        self.spotify_cache_dir: Optional[str] = None
        self.spotify = spotify_connect.SpotifyConnectState()
        self.default_source_id: Optional[str] = None

    # ---- discovery (backend specific) ----------------------------------
    @abc.abstractmethod
    def list_devices(self) -> List[DeviceInfo]: ...

    @abc.abstractmethod
    def list_sources(self) -> List[CaptureSource]: ...

    def all_sources(self) -> List[CaptureSource]:
        """Spotify Connect first, then whatever the OS backend can capture."""
        try:
            os_sources = self.list_sources()
        except Exception as e:  # a missing OS audio library must not hide the Spotify source
            log.warning("listing OS sources failed: %s", e)
            os_sources = []
        return [spotify_connect.source()] + os_sources

    def default_source(self) -> Optional[CaptureSource]:
        srcs = [s for s in self.all_sources() if s.available]
        if self.default_source_id:
            for s in srcs:
                if s.id == self.default_source_id:
                    return s
        return srcs[0] if srcs else None

    def list_applications(self) -> List[str]:
        return []

    # ---- routing --------------------------------------------------------
    def route_application(self, application: str) -> Tuple[bool, str]:
        return False, "Application routing is not supported by this backend."

    # ---- capture body ----------------------------------------------------
    @abc.abstractmethod
    def _run_capture(self, source: CaptureSource, stop: threading.Event) -> None:
        """Open `source`, push frames into self.pipeline, call self._running() once data
        flows, return when `stop` is set, and raise AudioError on device loss."""

    def _prepare(self) -> None:
        """Hook run in start() before the thread launches (e.g. create a virtual sink)."""

    # ---- lifecycle -------------------------------------------------------
    @property
    def state(self) -> CaptureState:
        return self._state

    @property
    def source_id(self) -> Optional[str]:
        return self._source_id

    def _set_state(self, st: CaptureState, error: str = "") -> None:
        self._state = st
        self.pipeline.health.state = st
        if error:
            self.pipeline.health.last_error = error

    def _running(self) -> None:
        if self._state != CaptureState.RUNNING:
            recovered = self._state == CaptureState.RECOVERING
            self._set_state(CaptureState.RUNNING)
            if recovered:
                self._emit("recovered", "capture resumed")

    def _emit(self, kind: str, message: str = "", **detail) -> None:
        try:
            self._on_event(CaptureEvent(kind, message, detail))
        except Exception:  # never let a listener kill capture
            log.exception("event listener failed")

    def start(self, source_id: Optional[str] = None) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                if source_id and source_id != self._source_id:
                    self.select_source(source_id)
                return
            self._prepare()
            self._source_id = source_id or self._source_id
            if not self._source_id:
                d = self.default_source()
                if not d:
                    self._set_state(CaptureState.ERROR, "No capture source available")
                    raise DeviceUnavailableError("No capture source available")
                self._source_id = d.id
            self.pipeline.reset()
            self._stop = threading.Event()
            self._set_state(CaptureState.STARTING)
            self._thread = threading.Thread(target=self._supervise, args=(self._stop,),
                                            name=f"capture-{self.platform}", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            t = self._thread
            self._stop.set()
            p = self._proc
            if p is not None and p.poll() is None:      # unblock a reader waiting on a quiet helper process
                try:
                    p.terminate()
                except Exception:
                    pass
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        with self._lock:
            self._thread = None
            self._set_state(CaptureState.STOPPED)
            self.pipeline.reset()

    def select_source(self, source_id: str) -> None:
        """Switch capture source safely: stop old capture fully, then start the new one."""
        with self._lock:
            was_running = bool(self._thread and self._thread.is_alive())
        if was_running:
            self.stop()
        self._source_id = source_id
        if was_running:
            self.start(source_id)

    def _find(self, source_id: str) -> Optional[CaptureSource]:
        for s in self.all_sources():
            if s.id == source_id:
                return s
        return None

    def _supervise(self, stop: threading.Event) -> None:
        attempt = 0
        while not stop.is_set():
            src = None
            try:
                src = self._find(self._source_id)
                if src is not None and not src.available and src.id == spotify_connect.SPOTIFY_SOURCE_ID:
                    raise BackendUnavailableError(spotify_connect.MISSING_MESSAGE)
                if src is None or not src.available:
                    raise DeviceUnavailableError(f"Source '{self._source_id}' is not available")
                if src.id == spotify_connect.SPOTIFY_SOURCE_ID:
                    spotify_connect.run(self, src, stop)
                else:
                    self._run_capture(src, stop)
                if stop.is_set():
                    break
                raise DeviceUnavailableError("capture ended unexpectedly")
            except (PermissionDeniedError, BackendUnavailableError) as e:   # retrying cannot fix these
                self._set_state(CaptureState.ERROR, str(e))
                self._emit("error", str(e), fatal=True)
                return
            except AudioError as e:
                attempt += 1
                self.pipeline.health.restarts += 1
                self._set_state(CaptureState.RECOVERING, str(e))
                self._emit("disconnected", str(e), attempt=attempt)
            except Exception as e:  # unexpected library failure
                attempt += 1
                self.pipeline.health.restarts += 1
                log.exception("capture failure")
                self._set_state(CaptureState.RECOVERING, f"{type(e).__name__}: {e}")
                self._emit("error", f"{type(e).__name__}: {e}")
            delay = self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)]
            if stop.wait(delay):
                break
            if self._state == CaptureState.RUNNING:
                attempt = 0
