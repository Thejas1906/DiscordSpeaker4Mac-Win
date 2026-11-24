"""Audio quality configuration with validated, high-quality defaults.

Defaults deliberately avoid any processing that alters the programme:
no noise suppression, no AGC, no normalisation. Volume 100% is bit-transparent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict, fields
from typing import Any, Mapping

TARGET_RATE = 48000       # Discord voice native rate (Opus fullband)
TARGET_CHANNELS = 2
FRAME_MS = 20             # Discord/Opus frame duration
FRAME_SAMPLES = TARGET_RATE * FRAME_MS // 1000  # 960

OPUS_MAX_KBPS = 510


@dataclass
class QualityConfig:
    # Capture / pipeline
    preferred_sample_rate: int = 0      # 0 = use the device's native rate (no pre-capture conversion)
    buffer_ms: int = 120                # target jitter buffer fill
    max_buffer_ms: int = 500            # overflow threshold
    drift_correction: bool = True       # +/-0.1% micro-adjust only when the buffer drifts out of band
    dither: bool = True                 # TPDF dither (only used by the 16-bit fallback path)
    limiter: bool = True                # transparent soft limiter, active only above 0 dBFS after gain
    volume: float = 1.0                 # 1.0 == unity, bit-transparent
    # Opus encoder
    opus_bitrate_kbps: int = 0          # 0 = auto: the voice channel's bitrate, capped at 510
    opus_application: str = "audio"     # music-tuned (not "voip")
    opus_signal: str = "music"
    opus_bandwidth: str = "full"
    opus_complexity: int = 10
    opus_fec: bool = False              # FEC at a fixed bitrate steals bits from music
    opus_packet_loss_pct: int = 0
    opus_vbr: bool = True
    opus_float_input: bool = True       # feed float32 directly to libopus (opus_encode_float)

    def validate(self) -> "QualityConfig":
        if self.preferred_sample_rate not in (0, 44100, 48000, 88200, 96000):
            raise ValueError("preferred_sample_rate must be 0, 44100, 48000, 88200 or 96000")
        if not 40 <= self.buffer_ms <= 2000:
            raise ValueError("buffer_ms must be between 40 and 2000")
        if self.max_buffer_ms < self.buffer_ms * 2:
            self.max_buffer_ms = self.buffer_ms * 2
        if not 0.0 <= self.volume <= 2.0:
            raise ValueError("volume must be between 0.0 and 2.0")
        if self.opus_bitrate_kbps and not 16 <= self.opus_bitrate_kbps <= OPUS_MAX_KBPS:
            raise ValueError("opus_bitrate_kbps must be 0 (auto) or 16-510")
        if self.opus_application not in ("audio", "voip", "lowdelay"):
            raise ValueError("opus_application invalid")
        if self.opus_signal not in ("auto", "voice", "music"):
            raise ValueError("opus_signal invalid")
        if self.opus_bandwidth not in ("narrow", "medium", "wide", "superwide", "full"):
            raise ValueError("opus_bandwidth invalid")
        if not 0 <= self.opus_complexity <= 10:
            raise ValueError("opus_complexity must be 0-10")
        if not 0 <= self.opus_packet_loss_pct <= 100:
            raise ValueError("opus_packet_loss_pct must be 0-100")
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QualityConfig":
        known = {f.name: f for f in fields(cls)}
        kwargs = {}
        for k, v in (data or {}).items():
            if k in known:
                kwargs[k] = type(getattr(cls(), k))(v)
        return cls(**kwargs).validate()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "QualityConfig":
        """Optional DS_* overrides (used by the Docker deployment)."""
        raw = {}
        for f in fields(cls):
            key = "DS_" + f.name.upper()
            if key in env and env[key] != "":
                val = env[key]
                if isinstance(getattr(cls(), f.name), bool):
                    val = val.lower() in ("1", "true", "yes")
                raw[f.name] = val
        return cls.from_dict(raw)
