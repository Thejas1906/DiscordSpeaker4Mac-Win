"""Streaming polyphase windowed-sinc resampler (the single owner of rate conversion).

Kaiser window (beta ~ 9.65, ~96 dB stopband), 128 taps per phase. For 44.1 kHz ->
48 kHz the passband is flat to ~20 kHz. Equal rates bypass the filter entirely, so
48 kHz sources are bit-exact.
"""
from __future__ import annotations

from math import gcd

import numpy as np

TAPS_PER_PHASE = 128
KAISER_BETA = 9.65


class StreamingResampler:
    def __init__(self, in_rate: int, out_rate: int, channels: int = 2):
        if in_rate <= 0 or out_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.in_rate, self.out_rate, self.channels = in_rate, out_rate, channels
        g = gcd(in_rate, out_rate)
        self.up, self.down = out_rate // g, in_rate // g       # L, M
        self.passthrough = self.up == self.down
        if self.passthrough:
            return
        K = TAPS_PER_PHASE
        n = np.arange(K * self.up, dtype=np.float64)
        centre = (K * self.up - 1) / 2.0
        # transition width (Hz) of the Kaiser design, used to place the stop-band edge at Nyquist
        width = (96 - 8) / (2.285 * 2 * np.pi * K) * min(in_rate, out_rate)
        cutoff_hz = max(min(in_rate, out_rate) / 2.0 - width / 2.0, 1000.0)
        fc = cutoff_hz / (in_rate * self.up)                    # cycles/sample at the L-times rate
        h = 2 * fc * np.sinc(2 * fc * (n - centre)) * np.kaiser(K * self.up, KAISER_BETA)
        h *= self.up                                            # unity passband gain after zero-stuffing
        # polyphase table hp[phase, k] = h[phase + k*L]
        self._hp = h.reshape(K, self.up).T.copy()               # (L, K)
        self._k = np.arange(K)
        self._hist = np.zeros((K - 1, channels), dtype=np.float64)
        self._consumed = 0          # global input samples already folded into history
        self._next_out = 0          # next global output index

    @property
    def delay_samples_in(self) -> float:
        return 0.0 if self.passthrough else TAPS_PER_PHASE / 2.0

    def process(self, x: np.ndarray) -> np.ndarray:
        """x: float32 (n, channels) -> float32 (m, channels)."""
        if self.passthrough:
            return x
        if x.shape[0] == 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        K = TAPS_PER_PHASE
        xs = np.concatenate([self._hist, x.astype(np.float64, copy=False)], axis=0)
        base = self._consumed - (K - 1)                         # global index of xs[0]
        total = self._consumed + x.shape[0]
        # outputs computable while i0 = floor(m*M/L) <= total-1
        m_end = ((total - 1) * self.up) // self.down + 1
        m = np.arange(self._next_out, m_end)
        if m.size == 0:
            out = np.zeros((0, self.channels))
        else:
            pos = m * self.down
            i0 = pos // self.up
            ph = pos % self.up
            idx = (i0[:, None] - self._k[None, :]) - base       # (n, K)
            idx = np.clip(idx, 0, xs.shape[0] - 1)
            X = xs[idx]                                         # (n, K, ch)
            out = np.einsum("nk,nkc->nc", self._hp[ph], X, optimize=True)
        self._next_out = m_end
        self._hist = xs[-(K - 1):].copy()
        self._consumed = total
        return out.astype(np.float32)
