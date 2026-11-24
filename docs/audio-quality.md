# Audio quality

Goal: **no avoidable degradation before Discord's own Opus encode.** Discord voice is lossy; this project never claims otherwise.

## Pipeline and targets

| Stage | Behaviour |
| :--- | :--- |
| Capture | Native device rate and channel count, **float32** where the API provides it (parec f32le, WASAPI float32, Core Audio tap float32, PortAudio float32). No capture-time conversion to 16-bit. |
| Channel map | Stereo untouched. Mono → dual mono (same level). 3/4/5/6/8-channel layouts → stereo with ITU-R BS.775-style coefficients, **normalised so full-scale input cannot exceed full scale**; LFE dropped (as FFmpeg's default). |
| Sample-rate conversion | **Once**, only if the source ≠ 48 kHz: streaming polyphase windowed-sinc (Kaiser β 9.65, 128 taps/phase, ~96 dB stopband). 48 kHz sources bypass the filter entirely (bit-exact). |
| Buffering | 120 ms jitter-buffer target (configurable 40–2000 ms), 500 ms hard cap. Reported latency = buffer + filter delay + one 20 ms frame. |
| Clock drift | Capture and Discord clocks differ by tens of ppm. When fill leaves a ±40 ms band, a ±1-sample-per-frame (0.1 %, ~1.7 cents) linear micro-adjust runs until it recovers. Tested at ±500 ppm for 5 simulated minutes. Larger mismatches fall back to the overflow policy. |
| Underflow | Remaining audio fades out over the frame, silence is emitted, the buffer re-primes to target and fades in. Counted only if the source was live (an idle loopback is silence, not an error). |
| Overflow | Oldest audio is dropped and counted. If capture runs before anyone is playing (e.g. before `/join`) the buffer is kept at target silently, so playback starts fresh. |
| Format change | A source switching rate/channels rebuilds the converter and re-primes; event `format-changed`. |
| Volume | Click-free ramp. 100 % = unity (**bit-transparent**). Above 100 %, a soft limiter (knee 0.9) prevents hard clipping; clipped samples are counted. |
| Not applied | Noise suppression, AGC, loudness normalisation, EQ, compression. |
| Encode | `opus_encode_float` from float32. `audio` application, `music` signal, fullband, complexity 10, VBR, FEC off, bitrate = the voice channel's bitrate (≤ 510 kbps; override in Settings). 16-bit fallback (`opus_float_input=false`) uses TPDF dither for the single float→int16 step. |

discord.py's stock encoder is 128 kbps, FEC on, 15 % packet-loss assumption (lower quality for music at a fixed bitrate) and takes 16-bit PCM; both are bypassed.

## Measured (automated, deterministic)

| Measurement | Result | Test |
| :--- | :--- | :--- |
| 44.1→48 kHz, 1 kHz sine: SNR / gain error | **123 dB / < 0.01 dB** | `ResamplerTests.test_sine_snr_and_gain_for_common_rates` (also 22.05, 32, 88.2, 96, 192 kHz: SNR > 100 dB) |
| Frequency response 100 Hz–20 kHz | within ±0.1 dB | `test_frequency_response_flat_to_20k` |
| Alias rejection (30 kHz in, 96→48) | < −85 dBFS | `test_aliasing_rejection_when_downsampling` |
| Left-only tone: right-channel level | < −120 dBFS after resampling; **119.7 dB separation** through real PulseAudio | resampler test; `tests/docker/linux_integration.py` |
| 48 kHz stereo through the pipeline | bit-exact | `test_unity_volume_is_bit_transparent_at_48k` |
| 30 s of 44.1 kHz delivered in 10 ms blocks | 0 underruns, no 5 ms dropout | `test_long_run_stability_no_dropouts` |
| Opus round trip (128 kbps): level / SNR floor / channel separation | within 0.01 / > 25 dB / > 40 dB | `tests/test_opus.py` (a *floor* for a lossy codec, not a fidelity claim) |
| Real discord.py `AudioPlayer` driving the Opus source | 150 packets / 3 s, no underruns | `DiscordPlayerIntegration` |

## Unavoidable limitations

1. Discord transmits **Opus** at the channel bitrate (96 kbps default; up to 384 kbps with server boosts). That is the dominant quality limit.
2. Device formats are whatever the OS exposes. WASAPI shared mode and Core Audio taps deliver the OS mix format (usually 48 kHz float, sometimes 44.1/96 kHz).
3. Bluetooth headsets switching to a hands-free profile change the capture format mid-stream; the pipeline adapts but the source quality drops.
4. Latency is typically 120–200 ms (buffer + filter + Discord). Lowering the buffer risks underruns on busy systems.
5. Real-time clocks, USB audio and Bluetooth can exceed the ±0.1 % drift correction range; such mismatches are handled by dropping audio (counted), not by hidden pitch shifts.
6. Discord's server and clients may apply their own processing; nothing here controls it.
