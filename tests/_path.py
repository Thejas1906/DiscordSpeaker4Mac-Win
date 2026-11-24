"""Test helpers: import path + reference-signal generation and measurement."""
import os
import sys
from pathlib import Path

import numpy as np

SRC = str(Path(__file__).resolve().parents[1] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-no-network")


def pid_alive(pid):
    """Cross-platform liveness check. (os.kill(pid, 0) would TERMINATE the process on Windows.)"""
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sine(freq, seconds, rate, amp=0.5, channels=(1.0, 1.0)):
    """Stereo (or n-channel) sine; `channels` are per-channel gains (0 mutes a channel)."""
    t = np.arange(int(seconds * rate)) / rate
    base = amp * np.sin(2 * np.pi * freq * t)
    return np.stack([base * g for g in channels], axis=1).astype(np.float32)


def fit(y, freq, rate, start_index=0):
    """Least-squares sine fit. Returns (amplitude, snr_db) of the fundamental vs everything else."""
    y = np.asarray(y, dtype=np.float64)
    n = np.arange(len(y)) + start_index
    A = np.stack([np.sin(2 * np.pi * freq * n / rate), np.cos(2 * np.pi * freq * n / rate)], axis=1)
    c, *_ = np.linalg.lstsq(A, y, rcond=None)
    sig, res = A @ c, y - A @ c
    return float(np.hypot(*c)), 10 * np.log10(np.mean(sig ** 2) / max(np.mean(res ** 2), 1e-30))


def rms_db(x):
    return 20 * np.log10(max(np.sqrt(np.mean(np.square(x.astype(np.float64)))), 1e-12))


def drain(pipeline, frames):
    """Read `frames` 20 ms frames from a pipeline and concatenate."""
    return np.concatenate([pipeline.read_frame() for _ in range(frames)])


def fit_multi(y, freqs, rate, start_index=0):
    """Joint LS fit of several tones; returns SNR (dB) of the tones vs. residual."""
    y = np.asarray(y, dtype=np.float64)
    n = np.arange(len(y)) + start_index
    cols = []
    for f in freqs:
        cols += [np.sin(2 * np.pi * f * n / rate), np.cos(2 * np.pi * f * n / rate)]
    A = np.stack(cols, axis=1)
    c, *_ = np.linalg.lstsq(A, y, rcond=None)
    sig, res = A @ c, y - A @ c
    return 10 * np.log10(np.mean(sig ** 2) / max(np.mean(res ** 2), 1e-30))


def play(pipeline, x, rate, chunk_ms=10, frames_per_read=None):
    """Real-time-like interleave: push `chunk_ms` of source audio, read 20 ms frames as they become due."""
    n = rate * chunk_ms // 1000
    out, owed = [], 0.0
    for i in range(0, len(x), n):
        pipeline.push(x[i:i + n], rate)
        owed += chunk_ms / 20.0
        while owed >= 1.0:
            out.append(pipeline.read_frame()); owed -= 1.0
    return np.concatenate(out)
