"""Audio-fidelity tests on generated reference signals (deterministic; no hardware, no network)."""
import unittest

import numpy as np

import _path  # noqa: F401
from _path import drain, fit, play, rms_db, sine
from audio.pipeline import AudioPipeline, downmix_matrix, soft_limit
from audio.quality_config import FRAME_SAMPLES, QualityConfig
from audio.resample import StreamingResampler


def run_through(rate, x, chunk_ms=10):
    r = StreamingResampler(rate, 48000)
    n = rate * chunk_ms // 1000
    return np.concatenate([r.process(x[i:i + n]) for i in range(0, len(x), n)])


class ResamplerTests(unittest.TestCase):
    def test_48k_is_bit_exact_passthrough(self):
        x = sine(1000, 0.2, 48000)
        self.assertTrue(np.array_equal(StreamingResampler(48000, 48000).process(x), x))

    def test_sine_snr_and_gain_for_common_rates(self):
        for rate in (44100, 88200, 96000, 32000, 22050, 192000):
            with self.subTest(rate=rate):
                y = run_through(rate, sine(1000, 1.5, rate))
                seg = y[9600:-9600, 0]
                amp, snr = fit(seg, 1000, 48000, 9600)
                self.assertAlmostEqual(amp, 0.5, delta=0.0005, msg="gain error > 0.01 dB")
                self.assertGreater(snr, 100, f"SNR {snr:.1f} dB")

    def test_frequency_response_flat_to_20k(self):
        for f in (100, 1000, 5000, 10000, 15000, 18000, 20000):
            with self.subTest(f=f):
                y = run_through(44100, sine(f, 1.0, 44100))[8000:-8000, 0]
                amp, _ = fit(y, f, 48000, 8000)
                self.assertAlmostEqual(20 * np.log10(amp / 0.5), 0.0, delta=0.1)

    def test_aliasing_rejection_when_downsampling(self):
        y = run_through(96000, sine(30000, 1.0, 96000))[8000:-8000, 0]   # above 24 kHz Nyquist of the output
        self.assertLess(rms_db(y), -85)

    def test_chunk_size_invariance(self):
        x = sine(1234, 0.5, 44100)
        a, b = run_through(44100, x, 10), run_through(44100, x, 37)
        n = min(len(a), len(b))
        self.assertLess(np.max(np.abs(a[:n] - b[:n])), 1e-5)

    def test_stereo_channel_separation(self):
        y = run_through(44100, sine(1000, 1.0, 44100, channels=(1.0, 0.0)))[8000:-8000]
        self.assertLess(rms_db(y[:, 1]), -120)      # right channel stays silent
        self.assertGreater(rms_db(y[:, 0]), -10)


class PipelineTests(unittest.TestCase):
    def feed(self, p, x, rate, chunk_ms=10):
        n = rate * chunk_ms // 1000
        for i in range(0, len(x), n):
            p.push(x[i:i + n], rate)

    def test_unity_volume_is_bit_transparent_at_48k(self):
        p = AudioPipeline(QualityConfig(buffer_ms=60))
        x = sine(997, 1.0, 48000, amp=0.8, channels=(1.0, 0.5))
        y = play(p, x, 48000)
        # priming discards nothing: output is the input delayed by whole samples of silence + fade-in
        tail = y[12000:24000]
        pos = int(np.argmax(np.all(x[:, :] == tail[0], axis=1)))
        self.assertTrue(np.array_equal(tail, x[pos:pos + len(tail)]), "48 kHz stereo data was altered by the pipeline")

    def test_44k_source_is_resampled_exactly_once(self):
        p = AudioPipeline(QualityConfig(buffer_ms=100))
        y = play(p, sine(1000, 2.0, 44100), 44100)[9600:-4800, 0]
        amp, snr = fit(y, 1000, 48000, 9600)
        self.assertGreater(snr, 95)
        self.assertAlmostEqual(amp, 0.5, delta=0.001)

    def test_mono_becomes_dual_mono_not_half_gain(self):
        p = AudioPipeline(QualityConfig(buffer_ms=60))
        y = play(p, sine(1000, 1.0, 48000, channels=(1.0,)), 48000)[6000:]
        self.assertTrue(np.array_equal(y[:, 0], y[:, 1]))
        self.assertAlmostEqual(float(np.max(np.abs(y))), 0.5, delta=0.01)

    def test_51_downmix_never_exceeds_full_scale_and_keeps_both_sides(self):
        m = downmix_matrix(6)
        self.assertLessEqual(float(np.abs(m).sum(axis=0).max()), 1.0 + 1e-9)
        x = np.zeros((4800, 6), np.float32); x[:, 0] = 0.5  # front-left only
        out = x @ m
        self.assertGreater(float(out[:, 0].max()), 0.0)
        self.assertEqual(float(out[:, 1].max()), 0.0)

    def test_underflow_is_counted_only_when_source_is_live_and_fades(self):
        p = AudioPipeline(QualityConfig(buffer_ms=60))
        self.feed(p, sine(1000, 0.2, 48000), 48000)      # exactly 200 ms then the source stalls
        frames = [p.read_frame() for _ in range(14)]
        self.assertEqual(p.health.underruns, 1)
        self.assertEqual(float(np.max(np.abs(frames[-1]))), 0.0)           # silence, no garbage
        peak_jump = max(float(np.max(np.abs(np.diff(f, axis=0)))) for f in frames)
        self.assertLess(peak_jump, 0.2, "click at underflow edge")        # faded, not truncated

    def test_idle_source_is_silence_not_an_error(self):
        p = AudioPipeline()
        for _ in range(20):
            self.assertEqual(float(np.max(np.abs(p.read_frame()))), 0.0)
        self.assertEqual(p.health.underruns, 0)

    def test_capture_running_ahead_of_playback_is_not_an_overrun(self):
        p = AudioPipeline(QualityConfig(buffer_ms=100))
        self.feed(p, sine(1000, 5.0, 48000), 48000)      # capture on, nobody reading for 5 s of audio
        self.assertEqual(p.health.overruns, 0)
        self.assertLessEqual(len(p._ring), 48000 * 110 // 1000)
        self.assertGreater(float(np.max(np.abs(p.read_frame()))), 0.0)   # playback starts immediately, fresh audio

    def test_overflow_drops_oldest_and_is_counted(self):
        p = AudioPipeline(QualityConfig(buffer_ms=100, max_buffer_ms=200))
        p.read_frame()                                  # consumer is active
        self.feed(p, sine(1000, 1.0, 48000), 48000)
        self.assertGreater(p.health.overruns, 0)
        self.assertLessEqual(p.health.buffer_ms if p.health.buffer_ms else 0, 200)
        self.assertLessEqual(len(p._ring), 48000 * 200 // 1000)

    def _drift_run(self, ppm, seconds=300):
        p = AudioPipeline(QualityConfig(buffer_ms=100, max_buffer_ms=400))
        rate, pushed, level = 48000, 0, []
        for k in range(seconds * 50):                      # one 20 ms read per iteration
            total = int((k + 1) * FRAME_SAMPLES * (1 + ppm * 1e-6))
            n, t = total - pushed, (pushed + np.arange(total - pushed)) / rate
            pushed = total
            p.push(np.repeat(0.3 * np.sin(2 * np.pi * 440 * t)[:, None], 2, 1).astype(np.float32), rate)
            p.read_frame()
            level.append(len(p._ring) * 1000 / rate)
        return p, level

    def test_realistic_clock_drift_is_compensated_in_both_directions(self):
        for ppm in (+500, -500):                           # 10x worse than typical audio-clock skew
            with self.subTest(ppm=ppm):
                p, level = self._drift_run(ppm)
                self.assertLess(max(level[300:]), 220)
                self.assertGreater(min(level[300:]), 20)
                self.assertEqual(p.health.underruns, 0)
                self.assertGreater(p.health.drift_corrections, 0)

    def test_gross_rate_mismatch_stays_bounded_via_overflow_policy(self):
        p, level = self._drift_run(+10000, seconds=120)     # 1% fast: beyond the micro-correction range
        self.assertLessEqual(max(level), 400 + 1)
        self.assertGreater(p.health.overruns, 0)

    def test_long_run_stability_no_dropouts(self):
        p = AudioPipeline(QualityConfig(buffer_ms=100))
        rate, pos, dropouts = 44100, 0, 0
        out = []
        for k in range(1500):                              # 30 s of 44.1 kHz delivered in 10 ms blocks
            for _ in range(2):
                n = rate // 100
                t = (pos + np.arange(n)) / rate; pos += n
                p.push(np.repeat(0.4 * np.sin(2 * np.pi * 523 * t)[:, None], 2, 1).astype(np.float32), rate)
            out.append(p.read_frame())
        y = np.concatenate(out)[24000:]
        self.assertEqual(p.health.underruns, 0)
        # dropout detector: no 5 ms window of digital silence inside the tone
        w = np.abs(y[:, 0]).reshape(-1, 240).max(axis=1)
        self.assertGreater(float(w.min()), 0.05)

    def test_format_change_flushes_and_reports(self):
        events = []
        p = AudioPipeline(on_event=events.append)
        self.feed(p, sine(1000, 0.3, 44100), 44100)
        self.feed(p, sine(1000, 0.3, 96000), 96000)
        self.assertEqual([e.kind for e in events], ["format-changed"])
        self.assertEqual(p.health.input_rate, 96000)

    def test_volume_ramps_without_click_and_unity_after(self):
        p = AudioPipeline(QualityConfig(buffer_ms=60))
        p.read_frame()                                   # consumer active, so the buffer is kept intact
        self.feed(p, sine(200, 0.4, 48000, amp=0.5), 48000)
        drain(p, 3)
        p.set_volume(0.25)
        f = p.read_frame()
        self.assertLess(float(np.max(np.abs(np.diff(f[:, 0])))), 0.05)
        self.assertAlmostEqual(float(np.max(np.abs(drain(p, 4)))), 0.125, delta=0.01)

    def test_clipping_is_counted_and_output_never_exceeds_full_scale(self):
        p = AudioPipeline(QualityConfig(buffer_ms=60))
        p.set_volume(2.0)
        self.feed(p, sine(300, 0.5, 48000, amp=0.9), 48000)
        y = drain(p, 20)
        self.assertLessEqual(float(np.max(np.abs(y))), 1.0)
        self.assertGreater(p.health.clipped_samples, 0)

    def test_soft_limit_is_identity_below_knee(self):
        x = np.linspace(-0.9, 0.9, 1000, dtype=np.float32)
        self.assertTrue(np.array_equal(soft_limit(x, 0.9), x))

    def test_int16_fallback_dither_is_tpdf_and_tiny(self):
        p = AudioPipeline(QualityConfig(dither=True))
        x = sine(1000, 1.0, 48000, amp=0.5)[:FRAME_SAMPLES * 20]
        pcm = np.frombuffer(p.to_int16(x), dtype="<i2").reshape(-1, 2) / 32768.0
        amp, snr = fit(pcm[:, 0], 1000, 48000)
        self.assertGreater(snr, 85)
        self.assertAlmostEqual(amp, 0.5, delta=0.001)


if __name__ == "__main__":
    unittest.main()
