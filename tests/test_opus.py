"""Float32 -> libopus -> decode round trips. Requires libopus (bundled with discord.py wheels / Docker image)."""
import unittest

import numpy as np

import _path  # noqa: F401
from _path import fit, fit_multi, rms_db, sine
from audio.quality_config import FRAME_SAMPLES, QualityConfig

try:
    import discord.opus as dopus
    from audio.discord_source import OpusFloatEncoder
    OpusFloatEncoder(QualityConfig())
    HAVE_OPUS = True
except Exception:  # pragma: no cover
    HAVE_OPUS = False


@unittest.skipUnless(HAVE_OPUS, "libopus not available")
class OpusTests(unittest.TestCase):
    def roundtrip(self, x, cfg=None, channel_bps=96000):
        enc = OpusFloatEncoder(cfg or QualityConfig(), channel_bps)
        dec = dopus.Decoder()
        out = []
        for i in range(0, len(x) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            out.append(np.frombuffer(dec.decode(enc.encode(x[i:i + FRAME_SAMPLES]), fec=False), "<i2").reshape(-1, 2) / 32768.0)
        return np.concatenate(out), enc

    def test_tone_survives_with_correct_level_and_high_snr(self):
        y, enc = self.roundtrip(sine(1000, 2.0, 48000, amp=0.5), channel_bps=128000)
        amp, snr = fit(y[9600:, 0], 1000, 48000, 9600)
        self.assertAlmostEqual(amp, 0.5, delta=0.01)
        self.assertGreater(snr, 25)       # lossy codec: this is a floor, not a lossless claim

    def test_stereo_separation_after_opus(self):
        y, _ = self.roundtrip(sine(1000, 2.0, 48000, channels=(1.0, 0.0)), channel_bps=128000)
        sep = rms_db(y[9600:, 0]) - rms_db(y[9600:, 1])
        self.assertGreater(sep, 40)

    def test_bitrate_selection_follows_channel_and_config(self):
        self.assertEqual(OpusFloatEncoder._pick_bitrate(QualityConfig(), 96000), 96000)
        self.assertEqual(OpusFloatEncoder._pick_bitrate(QualityConfig(), 384000), 384000)
        self.assertEqual(OpusFloatEncoder._pick_bitrate(QualityConfig(opus_bitrate_kbps=64), 96000), 64000)
        self.assertEqual(OpusFloatEncoder._pick_bitrate(QualityConfig(opus_bitrate_kbps=510), 96000), 96000)
        self.assertEqual(OpusFloatEncoder._pick_bitrate(QualityConfig(), None), 510000)

    def test_music_defaults_disable_fec(self):
        c = QualityConfig()
        self.assertFalse(c.opus_fec)
        self.assertEqual((c.opus_signal, c.opus_application, c.opus_bandwidth, c.opus_complexity),
                         ("music", "audio", "full", 10))

    def test_higher_bitrate_is_not_worse(self):
        x = sine(3000, 2.0, 48000, amp=0.4) + sine(7000, 2.0, 48000, amp=0.2)
        snr = {}
        for kbps in (24, 128, 384):
            y, _ = self.roundtrip(x, QualityConfig(opus_bitrate_kbps=kbps), channel_bps=510000)
            snr[kbps] = fit_multi(y[9600:, 0], (3000, 7000), 48000, 9600)
        self.assertGreaterEqual(snr[384], snr[24])
        self.assertGreater(snr[128], 20)


@unittest.skipUnless(HAVE_OPUS, "libopus not available")
class DiscordPlayerIntegration(unittest.TestCase):
    """Drive discord.py's real AudioPlayer thread with our Opus source, using a fake voice client."""

    def test_player_paces_pipeline_into_opus_packets(self):
        import threading, time
        from discord.player import AudioPlayer
        from audio.discord_source import make_source
        from audio.pipeline import AudioPipeline

        pipe = AudioPipeline(QualityConfig(buffer_ms=80))
        src = make_source(pipe, 128000)
        self.assertTrue(src.is_opus())
        packets, flags = [], []

        class Client:
            timeout = 1
            def is_connected(self): return True
            def send_audio_packet(self, data, *, encode=True):
                flags.append(encode); packets.append((time.perf_counter(), data))

        class Player(AudioPlayer):
            def _speak(self, *_): pass
            def send_silence(self, *_): pass

        stop = threading.Event()
        def producer():                                   # 44.1 kHz capture, 10 ms blocks, real-time paced
            n, t0 = 0, time.perf_counter()
            while not stop.is_set():
                tt = (n + np.arange(441)) / 44100
                pipe.push(np.stack([0.5 * np.sin(2 * np.pi * 440 * tt), 0.5 * np.sin(2 * np.pi * 880 * tt)], 1).astype(np.float32), 44100)
                n += 441
                time.sleep(max(0, t0 + n / 44100 - time.perf_counter()))
        th = threading.Thread(target=producer, daemon=True); th.start()
        time.sleep(0.3)
        pl = Player(src, Client()); pl.start()
        time.sleep(3.0)
        pl.stop(); stop.set(); pl.join(2); th.join(2)

        self.assertFalse(any(flags), "Opus source must bypass discord.py's int16 encoder")
        self.assertTrue(145 <= len(packets) <= 155, f"{len(packets)} packets in ~3 s")
        gaps = np.diff([t for t, _ in packets]) * 1000     # gaps[0] is discord.py running one frame ahead by design
        # Shared CI runners can stall a thread for ~100 ms; judge typical pacing strictly, outliers loosely.
        self.assertLess(float(np.percentile(gaps[1:], 90)), 30.0, "typical scheduling gap")
        self.assertLess(float(np.max(gaps[1:])), 250.0, "worst scheduling gap")
        self.assertEqual(pipe.health.underruns, 0)
        dec = dopus.Decoder()
        y = np.concatenate([np.frombuffer(dec.decode(d, fec=False), "<i2").reshape(-1, 2) / 32768.0 for _, d in packets])[24000:]
        f = np.fft.rfftfreq(len(y), 1 / 48000)
        sp = lambda c: np.abs(np.fft.rfft(y[:, c] * np.hanning(len(y))))
        self.assertAlmostEqual(f[sp(0).argmax()], 440, delta=3)
        self.assertAlmostEqual(f[sp(1).argmax()], 880, delta=3)


if __name__ == "__main__":
    unittest.main()
