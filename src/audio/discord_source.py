"""discord.py audio sources fed by an AudioPipeline.

OpusFloatSource (default) hands float32 PCM straight to libopus (opus_encode_float)
and gives discord.py ready-made Opus packets, so there is no float->int16 step before
Discord's own lossy encode. discord.py's built-in encoder (128 kbps, FEC on, 15% loss
assumption, 'auto' signal) is bypassed so the encoder can be tuned for music.
PipelineSource is the s16 fallback used when float input is disabled or unavailable.
"""
from __future__ import annotations

import ctypes
import logging
from typing import Optional

import numpy as np

from .pipeline import AudioPipeline
from .quality_config import FRAME_SAMPLES, QualityConfig, OPUS_MAX_KBPS

log = logging.getLogger("DiscordSpeaker.opus")

OPUS_SET_COMPLEXITY = 4010
OPUS_SET_VBR = 4006
OPUS_SET_INBAND_FEC = 4012
OPUS_SET_PACKET_LOSS_PERC = 4014
OPUS_SET_BITRATE = 4002


class OpusFloatEncoder:
    """48 kHz stereo float32 -> Opus using libopus' float API."""

    def __init__(self, cfg: QualityConfig, channel_bitrate_bps: Optional[int] = None):
        import discord.opus as dopus
        from .opus_loader import ensure_opus_loaded
        if not ensure_opus_loaded():
            raise RuntimeError("libopus is not available")
        self._opus = dopus
        self._lib = dopus._lib
        fn = self._lib.opus_encode_float
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_int32]
        fn.restype = ctypes.c_int32
        self._fn = fn
        # reuse discord.py's encoder object only to create/own the libopus state and for its tested ctl wrappers
        self._enc = dopus.Encoder(application=cfg.opus_application, bitrate=64, fec=False,
                                  bandwidth=cfg.opus_bandwidth, signal_type=cfg.opus_signal)
        self.bitrate_bps = self._pick_bitrate(cfg, channel_bitrate_bps)
        ctl = self._lib.opus_encoder_ctl
        ctl(self._enc._state, OPUS_SET_BITRATE, self.bitrate_bps)
        ctl(self._enc._state, OPUS_SET_COMPLEXITY, cfg.opus_complexity)
        ctl(self._enc._state, OPUS_SET_VBR, 1 if cfg.opus_vbr else 0)
        self._enc.set_fec(cfg.opus_fec)
        ctl(self._enc._state, OPUS_SET_PACKET_LOSS_PERC, cfg.opus_packet_loss_pct if cfg.opus_fec else 0)
        self._buf = ctypes.create_string_buffer(4000)

    @staticmethod
    def _pick_bitrate(cfg: QualityConfig, channel_bps: Optional[int]) -> int:
        """Auto = the voice channel's bitrate (Discord will not carry more); never above Opus' 510 kbps."""
        cap = min(channel_bps or OPUS_MAX_KBPS * 1000, OPUS_MAX_KBPS * 1000)
        want = cfg.opus_bitrate_kbps * 1000 if cfg.opus_bitrate_kbps else cap
        return int(min(want, cap))

    def encode(self, frame: np.ndarray) -> bytes:
        pcm = np.ascontiguousarray(frame, dtype=np.float32)
        ptr = pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        n = self._fn(self._enc._state, ptr, FRAME_SAMPLES, self._buf, len(self._buf))
        if n < 0:
            raise RuntimeError(f"opus_encode_float failed: {n}")
        return self._buf.raw[:n]


def _base():
    import discord
    return discord.AudioSource


class OpusFloatSource(_base()):
    def __init__(self, pipeline: AudioPipeline, channel_bitrate_bps: Optional[int] = None):
        self.pipeline = pipeline
        self.encoder = OpusFloatEncoder(pipeline.config, channel_bitrate_bps)

    def is_opus(self) -> bool:
        return True

    def read(self) -> bytes:
        return self.encoder.encode(self.pipeline.read_frame())

    def cleanup(self) -> None:
        pass


class PipelineSource(_base()):
    """s16le fallback (discord.py encodes). Uses TPDF dither for the one float->int16 step."""

    def __init__(self, pipeline: AudioPipeline):
        self.pipeline = pipeline

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        return self.pipeline.to_int16(self.pipeline.read_frame())

    def cleanup(self) -> None:
        pass


def make_source(pipeline: AudioPipeline, channel_bitrate_bps: Optional[int] = None):
    if pipeline.config.opus_float_input:
        try:
            return OpusFloatSource(pipeline, channel_bitrate_bps)
        except Exception as e:  # libopus without float API etc.
            log.warning("Float Opus path unavailable (%s); using 16-bit fallback", e)
    return PipelineSource(pipeline)
