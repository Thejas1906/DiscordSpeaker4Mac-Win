"""Backend selection and device/source bookkeeping shared by bot, UI and CLI."""
from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

from .base import CaptureBackend
from .models import CaptureEvent, CaptureSource, SourceKind
from .pipeline import AudioPipeline
from .quality_config import QualityConfig


def current_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def create_backend(pipeline: AudioPipeline, on_event=None, platform: Optional[str] = None, **kw) -> CaptureBackend:
    """Import only the backend for the running OS (others need OS-specific libraries)."""
    platform = platform or current_platform()
    if platform == "linux":
        from .platforms.linux.capture import LinuxCapture as B
    elif platform == "windows":
        from .platforms.windows.capture import WindowsCapture as B
    elif platform == "macos":
        from .platforms.macos.capture import MacOSCapture as B
    else:
        raise ValueError(platform)
    return B(pipeline, on_event, **kw)


class DeviceManager:
    """Remembers the chosen source across refreshes and reports it as unavailable if it vanishes."""

    def __init__(self, backend: CaptureBackend):
        self.backend = backend
        self.selected: Optional[str] = None

    def refresh(self) -> List[Dict]:
        sources = self.backend.list_sources()
        out = [s.to_dict() for s in sources]
        if self.selected and not any(s.id == self.selected for s in sources):
            out.append(CaptureSource(self.selected, f"{self.selected} (disconnected)",
                                     kind=SourceKind.INPUT,
                                     available=False).to_dict())
        return out

    def select(self, source_id: str) -> None:
        self.selected = source_id
        self.backend.select_source(source_id)
