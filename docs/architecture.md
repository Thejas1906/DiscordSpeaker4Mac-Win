# Architecture and repository audit

## 1. Original architecture (audit of the Linux-only project)

| Piece | What it did | Linux-specific assumption |
| :--- | :--- | :--- |
| `src/bot.py` (single module) | discord.py bot: 6 slash commands + prefix twins, voice join/leave, status embeds | Sink helpers called `pactl`; `/status`, `/join` text mentions PipeWire; process exits at import if no token |
| Capture | `discord.FFmpegPCMAudio("DiscordSpeaker.monitor", before_options="-f pulse", options="-vn")` | `-f pulse` input (PulseAudio protocol) |
| Format | FFmpeg default: **s16le, 48 kHz, stereo** (a 16-bit step, with FFmpeg/Pulse resampling when the sink wasn't 48 kHz) | |
| Encode | discord.py's `Encoder`: **128 kbps, FEC on, 15 % loss assumed, `auto` signal, fullband**, int16 input | |
| Volume | `PCMVolumeTransformer` on int16 (clips hard above 100 %) | |
| Routing | `pactl move-sink-input` by application name/binary (`pulse_list` JSON) | Pulse/PipeWire sink-inputs |
| `src/audio_ctl.sh` | create/teardown null sink, loopback monitoring, routing CLI | Bash + pactl |
| Deployment | Docker Compose mounting the host Pulse socket; non-root, read-only, caps dropped | Linux host sockets |
| Tests | 9 mocked unit tests of routing | |
| Buffering | None of our own: FFmpeg pipe + discord.py's 20 ms packet loop; no drift/underflow handling | |

### Preserve / abstract / replace

* **Preserved unchanged in behaviour:** all commands (slash and prefix), permission messages, embeds, the `pulse_list`/`is_sink_active`/
  `ensure_sink_active`/`get_routed_apps`/`route_app_stream`/`route_spotify_stream` functions and their signatures (the original 9
  tests pass untouched), `audio_ctl.sh` interface, Docker Compose deployment, `.env` variables.
* **Abstracted:** capture (a `CaptureBackend` interface), routing (`route_application`), status (an `AudioService`), volume (moved into the pipeline).
* **Replaced:** the FFmpeg capture pipe and `PCMVolumeTransformer` with the pipeline + float32 Opus path; discord.py's default encoder settings;
  import-time `sys.exit` with a `main()`.
* **Fixed along the way (found by real PulseAudio testing):** the original `sink_properties=device.description='Discord Speaker'`
  fails on a real PulseAudio daemon (works on pipewire-pulse); `audio_ctl.sh` and the Python fallback now try both quoting styles. The virtual sink is
  created at 48 kHz stereo so no server-side resampling is needed.

## 2. New architecture

```
src/
  app.py                       desktop entry (--engine / --minimized / --selftest)
  bot.py                       Discord layer (commands) + Linux legacy helpers; main() for Docker
  audio/
    models.py                  AudioFormat, DeviceInfo, CaptureSource, StreamHealth, errors
    quality_config.py          QualityConfig (validated; DS_* env overrides)
    base.py                    CaptureBackend ABC + supervision (retry/back-off/state/events)
    resample.py                streaming polyphase Kaiser resampler (sole owner of rate conversion)
    ringbuffer.py              thread-safe float32 FIFO
    pipeline.py                downmix → resample → jitter buffer → drift/underrun/overrun/volume/limiter/meters
    discord_source.py          OpusFloatSource (opus_encode_float) + s16 fallback; bitrate selection
    opus_loader.py             finds libopus (incl. bundled)
    device_manager.py          backend selection per OS; selected-device bookkeeping
    service.py                 AudioService: start/stop/select/route/volume/status
    platforms/
      linux/   pulse.py (pactl helpers, original behaviour) · capture.py (parec float32 / ffmpeg fallback)
      windows/ capture.py (WASAPI loopback + endpoints via PyAudioWPatch)
      macos/   capture.py · tap/dsaudiotap.swift (Core Audio process tap helper) · tap/build.sh
  service/
    engine.py                  engine process: bot + audio, JSON-lines over stdin/stdout
    client.py                  controller-side process handle
    settings.py                settings JSON + OS credential store wrapper (no plaintext fallback)
    instance.py                single-instance locks (UI; one bot per token, keyed by hash)
    logsetup.py                structured JSON logs, token redaction
  ui/                          PySide6: dashboard, devices, settings, meter, tray, autostart
packaging/                     PyInstaller spec; macOS .app/DMG; Windows Inno Setup; Linux launcher
tests/                         unit + fidelity + backend + lifecycle + UI; tests/docker = real PulseAudio rig
```

### Process model

The UI never talks to Discord or audio devices. It spawns the **engine** and exchanges one JSON object per line over the child's stdin/stdout.
This was chosen over sockets/HTTP because there is no listening port to secure, the OS tears the pipe down when either side dies
(engine sees EOF → leaves voice, stops capture, kills helper processes), and it works identically on all three OSes. The bot token is sent over this pipe,
never on a command line or in the environment, and is never logged.

### Spotify Connect (primary desktop source)

`audio/spotify_connect.py` runs **librespot** (`--backend pipe --format F32 --bitrate 320`, device name = the bot's username) as a child
process. It decodes Spotify's stream and writes float32 / 44.1 kHz / stereo PCM to stdout, which feeds the same pipeline as every other source
(one resample to 48 kHz, jitter buffer, `opus_encode_float`). The engine starts it as soon as the bot is online (`Engine._auto_start`), so the device is visible
in Spotify while the bot runs. It is just another capture *source* (`spotify:connect`) of every OS backend, so system/app capture remains available as an advanced option.
Requires Spotify Premium; librespot is MIT-licensed and unofficial.

### Audio data path

`backend thread` → `pipeline.push(float32 frames @ native rate)` → *(one polyphase resample if rate ≠ 48 kHz)* → ring buffer
→ `pipeline.read_frame()` every 20 ms from discord.py's player thread → `opus_encode_float` → Discord.

Capture lifecycle is independent of the Discord connection: voice reconnects/gateway resumes do not touch the pipeline; `/leave` stops playback only
in desktop mode (Docker mode keeps its original “capture only while in a channel” behaviour).

## 3. Platform technology decisions

| Platform | Choice | Why / alternatives considered |
| :--- | :--- | :--- |
| Windows | **WASAPI loopback via PyAudioWPatch** (Apache-2.0, maintained: 0.2.12.9, Oct 2026; wheels for cp39–cp315 win_amd64) | Native shared-mode loopback needs no driver. `soundcard` is stale; hand-written ctypes COM is high-risk without a Windows machine. VB-Audio Cable is supported as an *ordinary capture endpoint* if the user installs it; it is not bundled (its licence restricts redistribution). Per-process loopback needs `ActivateAudioInterfaceAsync`; not exposed by PyAudioWPatch → not implemented. |
| macOS | **Core Audio process taps** (14.2+) in a small Swift helper, plus **sounddevice** (PortAudio/Core Audio) input capture for BlackHole & co. | Taps are the only native route to system *and* per-app audio with no driver. ScreenCaptureKit evaluated: needs Screen Recording permission, audio-only use is awkward, no benefit over taps. Universal helper (`arm64`+`x86_64`) built with `swiftc`/`lipo`. BlackHole is optional and not bundled (GPL-3 / commercial licensing). |
| Linux | `parec` float32 on the existing virtual sink (FFmpeg fallback) | Preserves the sink/Docker design; avoids FFmpeg's 16-bit pulse capture. |
| UI | **PySide6** (LGPL) | Native tray/menu bar on all three OSes; no compelling reason to choose otherwise. |
| Secrets | **keyring** (Keychain / Credential Manager / Secret Service) | No plaintext fallback by design. |
| Packaging | **PyInstaller** + `hdiutil` DMG + **Inno Setup** | Standard, scriptable in CI. |
