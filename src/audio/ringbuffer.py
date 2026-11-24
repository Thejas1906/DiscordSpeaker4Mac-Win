"""Thread-safe float32 stereo FIFO."""
from __future__ import annotations

import threading

import numpy as np


class FloatRing:
    def __init__(self, capacity_frames: int, channels: int = 2):
        self._buf = np.zeros((capacity_frames, channels), dtype=np.float32)
        self._cap = capacity_frames
        self._r = 0
        self._count = 0
        self._lock = threading.Lock()
        self.channels = channels

    def __len__(self) -> int:
        return self._count

    @property
    def capacity(self) -> int:
        return self._cap

    def write(self, data: np.ndarray) -> int:
        """Append frames; if full, drop the *oldest* audio. Returns frames dropped."""
        n = data.shape[0]
        dropped = 0
        with self._lock:
            if n >= self._cap:
                dropped = self._count + (n - self._cap)
                data = data[-self._cap:]
                n = self._cap
                self._r, self._count = 0, 0
            elif self._count + n > self._cap:
                dropped = self._count + n - self._cap
                self._r = (self._r + dropped) % self._cap
                self._count -= dropped
            w = (self._r + self._count) % self._cap
            first = min(n, self._cap - w)
            self._buf[w:w + first] = data[:first]
            if first < n:
                self._buf[:n - first] = data[first:]
            self._count += n
        return dropped

    def read(self, n: int) -> np.ndarray:
        """Read exactly n frames (caller checks len() first)."""
        with self._lock:
            n = min(n, self._count)
            first = min(n, self._cap - self._r)
            out = np.empty((n, self.channels), dtype=np.float32)
            out[:first] = self._buf[self._r:self._r + first]
            if first < n:
                out[first:] = self._buf[:n - first]
            self._r = (self._r + n) % self._cap
            self._count -= n
        return out

    def clear(self) -> None:
        with self._lock:
            self._r = self._count = 0
