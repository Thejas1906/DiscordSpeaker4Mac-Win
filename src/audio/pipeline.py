"""Capture -> 48 kHz float32 stereo pipeline with clear ownership of every conversion.

Producer side (capture thread/callback):  push(frames, rate)
    channel mapping -> (only if rate != 48 kHz) polyphase resample -> jitter buffer
Consumer side (Discord packet clock, 20 ms): read_frame() -> float32 (960, 2)

Underflow, overflow, clock drift, source format changes and clipping are handled
here and counted in StreamHealth. Nothing here applies noise suppression, AGC or
normalisation. At volume 1.0 in-range audio passes through unmodified.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

from .models import CaptureEvent, StreamHealth
from .quality_config import FRAME_SAMPLES, QualityConfig, TARGET_CHANNELS, TARGET_RATE
from .resample import StreamingResampler
from .ringbuffer import FloatRing

_SQ = 0.70710678


def downmix_matrix(channels: int) -> np.ndarray:
    """ITU-R BS.775-style downmix to stereo, normalised so a full-scale in-phase
    signal cannot exceed full scale. LFE is dropped (as FFmpeg does by default)."""
    if channels == 1:
        m = np.array([[1.0, 1.0]])
    elif channels == 2:
        return np.eye(2)
    elif channels == 3:      # FL FR FC
        m = np.array([[1, 0], [0, 1], [_SQ, _SQ]])
    elif channels == 4:      # FL FR BL BR
        m = np.array([[1, 0], [0, 1], [_SQ, 0], [0, _SQ]])
    elif channels == 5:      # FL FR FC BL BR
        m = np.array([[1, 0], [0, 1], [_SQ, _SQ], [_SQ, 0], [0, _SQ]])
    elif channels == 6:      # FL FR FC LFE BL BR
        m = np.array([[1, 0], [0, 1], [_SQ, _SQ], [0, 0], [_SQ, 0], [0, _SQ]])
    elif channels == 8:      # FL FR FC LFE BL BR SL SR
        m = np.array([[1, 0], [0, 1], [_SQ, _SQ], [0, 0], [_SQ, 0], [0, _SQ], [_SQ, 0], [0, _SQ]])
    else:                    # unknown layout: take first two channels
        m = np.zeros((channels, 2)); m[0, 0] = 1; m[1, 1] = 1
        return m
    if channels > 2:
        m = m / max(m.sum(axis=0).max(), 1.0)
    return m


def soft_limit(x: np.ndarray, knee: float) -> np.ndarray:
    """Smooth saturation above `knee` (asymptote 1.0). Samples within the knee are untouched."""
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    y = x.copy()
    span = 1.0 - knee
    y[over] = np.sign(x[over]) * (knee + span * np.tanh((a[over] - knee) / span))
    return y


class AudioPipeline:
    PRIMING, PLAYING = "priming", "playing"

    def __init__(self, config: Optional[QualityConfig] = None,
                 on_event: Optional[Callable[[CaptureEvent], None]] = None):
        self.config = (config or QualityConfig()).validate()
        self.on_event = on_event or (lambda e: None)
        self.health = StreamHealth()
        self._cap_frames = TARGET_RATE * self.config.max_buffer_ms // 1000
        self._ring = FloatRing(self._cap_frames, TARGET_CHANNELS)
        self._target = TARGET_RATE * self.config.buffer_ms // 1000
        self._band = TARGET_RATE * 40 // 1000
        self._resampler: Optional[StreamingResampler] = None
        self._mix: Optional[np.ndarray] = None
        self._fmt = (0, 0)
        self._state = self.PRIMING
        self._correcting = 0              # -1 stretch, +1 squeeze, 0 none
        self._last_push = 0.0
        self._last_signal = 0.0
        self._last_read = 0.0             # 0 = consumer (Discord) has not started reading yet
        self._volume = float(self.config.volume)
        self._applied_volume = self._volume
        self._tail = np.zeros((0, 2), dtype=np.float32)
        self._lock = threading.Lock()
        self._peak_hold = 0.0
        self._rng = np.random.default_rng(0x5EED)

    # ---- control -------------------------------------------------------
    def set_volume(self, v: float) -> None:
        self._volume = float(min(max(v, 0.0), 2.0))

    @property
    def volume(self) -> float:
        return self._volume

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()
            self._state = self.PRIMING
            self._correcting = 0
            self._fmt = (0, 0)
            self._resampler = None

    # ---- producer ------------------------------------------------------
    def push(self, frames: np.ndarray, sample_rate: int) -> None:
        """frames: float32 array (n, channels) at `sample_rate`."""
        if frames.size == 0:
            return
        if frames.ndim == 1:
            frames = frames[:, None]
        frames = frames.astype(np.float32, copy=False)
        ch = frames.shape[1]
        with self._lock:
            if (sample_rate, ch) != self._fmt:
                if self._fmt != (0, 0):
                    self.on_event(CaptureEvent("format-changed",
                                               f"source changed {self._fmt} -> {(sample_rate, ch)}",
                                               {"rate": sample_rate, "channels": ch}))
                    self._ring.clear()
                    self._state = self.PRIMING
                self._fmt = (sample_rate, ch)
                self._mix = downmix_matrix(ch)
                self._resampler = StreamingResampler(sample_rate, TARGET_RATE, TARGET_CHANNELS)
                self.health.input_rate, self.health.input_channels = sample_rate, ch
            stereo = frames if ch == 2 else (frames @ self._mix).astype(np.float32)
            out = self._resampler.process(stereo)
            now_r = time.monotonic()
            if now_r - self._last_read > 0.5 and len(self._ring) > self._target:
                # Nobody is consuming (capture runs ahead of /join): keep only the freshest `target` of
                # audio so playback starts with low latency. This is not an overrun and is not counted.
                self._ring.read(len(self._ring) - self._target)
            dropped = self._ring.write(out)
            now = time.monotonic()
            if not self._last_signal or (frames.shape[0] and float(np.max(np.abs(frames))) > 1e-7):
                self._last_signal = now
            self._last_push = now
            self.health.frames_in += frames.shape[0]
            self.health.silent_s = round(now - self._last_signal, 1) if self._last_signal else 0.0
            if dropped:
                self.health.overruns += 1

    # ---- consumer ------------------------------------------------------
    def source_active(self) -> bool:
        return (time.monotonic() - self._last_push) < 0.5

    def read_frame(self) -> np.ndarray:
        """Always returns (960, 2) float32 (silence when nothing is available)."""
        n = FRAME_SAMPLES
        with self._lock:
            self._last_read = time.monotonic()
            fill = len(self._ring)
            if self._state == self.PRIMING:
                if fill >= self._target:
                    self._state = self.PLAYING
                    frame = self._take(n, fill)
                    frame = self._fade(frame, fade_in=True)
                else:
                    frame = np.zeros((n, 2), dtype=np.float32)
            else:
                if fill < n:
                    # underflow: play what's left with a fade-out, count only if the source is live
                    part = self._ring.read(fill)
                    frame = np.zeros((n, 2), dtype=np.float32)
                    if fill:
                        frame[:fill] = self._fade(part, fade_in=False)
                    self._state = self.PRIMING
                    if self.source_active():
                        self.health.underruns += 1
                else:
                    frame = self._take(n, fill)
            self.health.buffer_ms = len(self._ring) * 1000.0 / TARGET_RATE
            self.health.frames_out += n
        return self._finish(frame)

    def _take(self, n: int, fill: int) -> np.ndarray:
        if self.config.drift_correction:
            hi, lo = self._target + self._band, max(self._target - self._band, n)
            if self._correcting == 0:
                if fill > hi:
                    self._correcting = 1
                elif fill < lo:
                    self._correcting = -1
            elif (self._correcting == 1 and fill <= self._target + self._band // 4) or \
                 (self._correcting == -1 and fill >= self._target - self._band // 4):
                self._correcting = 0
            if self._correcting and fill > n + 1:
                n_in = n + self._correcting
                src = self._ring.read(n_in)
                pos = np.arange(n) * (n_in / n)
                xp = np.arange(n_in)
                self.health.drift_corrections += 1
                return np.stack([np.interp(pos, xp, src[:, c]) for c in range(2)], axis=1).astype(np.float32)
        return self._ring.read(n)

    @staticmethod
    def _fade(x: np.ndarray, fade_in: bool) -> np.ndarray:
        """fade_in: 5 ms ramp-up at the start of a block. fade-out: ramp the whole (short) tail to zero."""
        if x.shape[0] == 0:
            return x
        if fade_in:
            n = min(x.shape[0], 240)
            y = x.copy()
            y[:n] *= np.linspace(0.0, 1.0, n, dtype=np.float32)[:, None]
            return y
        return x * np.linspace(1.0, 0.0, x.shape[0], dtype=np.float32)[:, None]

    def _finish(self, frame: np.ndarray) -> np.ndarray:
        """Volume (click-free ramp) + transparent limiting + metering."""
        target = self._volume
        prev = self._applied_volume
        if target != 1.0 or prev != 1.0:
            if target != prev:
                ramp = np.linspace(prev, target, frame.shape[0], dtype=np.float32)[:, None]
                frame = frame * ramp
            else:
                frame = frame * np.float32(target)
            self._applied_volume = target
        peak = float(np.max(np.abs(frame))) if frame.size else 0.0
        if peak > 1.0 or (self.config.limiter and target > 1.0 and peak > 0.9):
            knee = 0.9 if target > 1.0 else 1.0
            clipped = int(np.count_nonzero(np.abs(frame) > 1.0))
            self.health.clipped_samples += clipped
            if knee < 1.0:
                frame = soft_limit(frame, knee)
            frame = np.clip(frame, -1.0, 1.0)
        self._meter(frame)
        return frame

    def _meter(self, frame: np.ndarray) -> None:
        pk = float(np.max(np.abs(frame))) if frame.size else 0.0
        self._peak_hold = max(pk, self._peak_hold * 0.92)
        self.health.peak = self._peak_hold
        self.health.rms = float(np.sqrt(np.mean(np.square(frame)))) if frame.size else 0.0
        delay_in = (self._resampler.delay_samples_in / max(self._fmt[0], 1) * 1000.0) if self._resampler else 0.0
        self.health.latency_ms = self.health.buffer_ms + delay_in + 20.0

    # ---- conversion for the 16-bit fallback ------------------------------
    def to_int16(self, frame: np.ndarray) -> bytes:
        """float32 -> interleaved s16le, with optional TPDF dither (the ONLY lossy step on this path)."""
        x = frame * 32767.0
        if self.config.dither:
            x = x + (self._rng.random(x.shape) - self._rng.random(x.shape)).astype(np.float32)
        return np.clip(np.rint(x), -32768, 32767).astype("<i2").tobytes()
