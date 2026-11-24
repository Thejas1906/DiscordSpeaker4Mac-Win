"""Shared data models for the cross-platform audio layer."""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Optional


class AudioError(Exception):
    """Base class for audio-layer failures. Messages never contain secrets."""


class DeviceUnavailableError(AudioError):
    """The requested device disappeared or cannot be opened."""


class PermissionDeniedError(AudioError):
    """The OS refused audio capture (e.g. macOS audio-capture permission)."""


class BackendUnavailableError(AudioError):
    """A required OS facility, library or helper binary is missing."""


class CaptureState(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    RECOVERING = "recovering"  # device vanished / helper died; retrying
    ERROR = "error"


class SourceKind(str, enum.Enum):
    LOOPBACK = "loopback"      # capture of an output device's mix (WASAPI loopback, Pulse monitor)
    INPUT = "input"            # ordinary input device (incl. BlackHole / VB-Cable endpoints)
    SYSTEM = "system"          # whole-system tap (macOS process tap)
    APPLICATION = "application"  # one application's audio


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int = 48000
    channels: int = 2


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    name: str
    kind: str                      # "output" | "input"
    default: bool = False
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    available: bool = True
    loopback: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CaptureSource:
    """Something that can be captured. `id` is stable and backend-specific."""
    id: str
    name: str
    kind: SourceKind
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    available: bool = True
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class StreamHealth:
    state: CaptureState = CaptureState.STOPPED
    source: str = ""
    input_rate: int = 0
    input_channels: int = 0
    output_rate: int = 48000
    output_channels: int = 2
    underruns: int = 0
    overruns: int = 0
    drift_corrections: int = 0
    restarts: int = 0
    clipped_samples: int = 0
    frames_in: int = 0
    frames_out: int = 0
    buffer_ms: float = 0.0
    latency_ms: float = 0.0
    peak: float = 0.0   # 0..1 linear, output side, decays
    rms: float = 0.0
    silent_s: float = 0.0   # seconds of continuous digital silence at the input
    last_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class CaptureEvent:
    kind: str                  # "disconnected" | "error" | "format-changed" | "recovered"
    message: str = ""
    detail: dict = field(default_factory=dict)
