<div align="center">

<img src=".github/assets/logo-nobg.png" alt="Discord Speaker logo" width="180">

# Discord Speaker

**Your desktop audio, in your Discord voice channel.**

Stream Spotify, your browser, media players, and games to Discord from **Windows, macOS or Linux**.

**Desktop app** · **WASAPI / Core Audio / PipeWire** · **48 kHz stereo · float32 pipeline**

[Quick start](#quick-start) · [Audio routing](#audio-routing) · [Commands](#commands) · [Audio quality](#audio-quality) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Limitations](#known-limitations)

</div>

---

## How it works

Discord Speaker captures audio on your computer, converts it **once** to 48 kHz stereo float32, and hands it
straight to libopus, which Discord's voice channels use. A small desktop app (Windows, macOS, Linux) starts
and stops everything with one click; on Linux a headless Docker deployment is still available.

```text
                    ┌──────────────────────────────┐
  Desktop app  ───► │  Engine process (stdin/out)  │      no network port, dies with the app
  (PySide6 UI,      │                              │
   tray / menu bar) │  Discord bot (discord.py)    │ ◄──── slash + prefix commands from Discord
                    │        ▲                     │
                    │        │ float32 48 kHz      │
                    │  Audio pipeline              │  channel map → resample (once) → jitter buffer
                    │  (drift, underrun, volume)   │  → opus_encode_float → Discord voice
                    │        ▲                     │
                    │  Capture backend (per OS)    │
                    └────────┼─────────────────────┘
        Windows: WASAPI loopback / virtual cable │ macOS: Core Audio process tap / BlackHole │ Linux: PipeWire/Pulse monitor
```

- **Share any app.** Windows/macOS capture system audio directly; macOS can also capture a single app; Linux routes apps into a virtual sink.
- **No music-service credentials.** No Spotify API keys, track URLs, or downloaders.
- **Quality first.** Defaults avoid any processing that alters your music: no noise suppression, no AGC, no normalisation. See [Audio quality](#audio-quality).

> **Honest status.** Linux (Docker) is validated against a real PulseAudio daemon in CI-style containers. The
> macOS Core Audio helper and the Windows WASAPI backend are fully implemented and covered by mocked-device
> tests, but *real-audio verification on each OS needs a human with that hardware*: see
> [Compatibility and verification](#compatibility-and-verification).

## Compatibility and verification

| Platform | Capture method | Per-app capture | Implemented | Verified here |
| :--- | :--- | :--- | :---: | :--- |
| **Linux** (PipeWire / PulseAudio) | Virtual sink monitor via `parec` (float32) | Yes, move streams into the sink | ✅ | ✅ real PulseAudio daemon in Docker: routing, 440/880 Hz stereo fidelity (119 dB separation), recovery, clean shutdown |
| **Windows 10/11 x64** | WASAPI loopback of any output; any capture endpoint incl. VB-Audio Cable | No (see [limitations](#known-limitations)) | ✅ | ⚠️ mocked WASAPI only. **Not run on Windows.** |
| **macOS 14.2+** (Apple Silicon & Intel) | Core Audio **process tap** via bundled `dsaudiotap` helper (no driver) | Yes | ✅ | ✅ Apple Silicon, real hardware: system tap captured a 440 Hz (L) / 880 Hz (R) tone at the right pitch and channels (Bluetooth output at 44.1 kHz). App + DMG built and launched. Per-app capture, BlackHole, sleep/wake and Intel not run on hardware |
| **macOS 12–14.1** | Input device capture (BlackHole, Loopback…) | No | ✅ | ⚠️ mocked |

Nothing in this table is claimed beyond what is stated. Tests that need real hardware are in
[`tests/test_hardware.py`](tests/test_hardware.py) and are skipped unless `DS_HARDWARE_TESTS=1`.

## Requirements

| | Windows | macOS | Linux |
| :--- | :--- | :--- | :--- |
| OS | Windows 10/11, 64-bit | macOS 14.2+ for system/app capture (12+ with BlackHole) | PipeWire + pipewire-pulse (or PulseAudio) |
| Install | `DiscordSpeaker-…-setup.exe` | `DiscordSpeaker-…-macos-<arch>.dmg` | Docker Engine + Compose v2 **or** `python3` for the desktop app |
| Extras | None. Optional: [VB-Audio Cable](https://vb-audio.com/Cable/) for per-app isolation | None. Optional: [BlackHole](https://existential.audio/blackhole/) for older macOS or virtual routing | `pavucontrol` for per-app routing (optional) |
| Discord | A bot token and permission to add the bot to your server (all platforms) | | |

Third-party drivers are **never bundled or auto-installed**. They have their own licences and installers; this
project only recognises their devices when you have installed them yourself.

## Quick start

### 1. Set up your Discord bot

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Use [logo.png](.github/assets/logo.png) as the application icon and bot avatar.
2. Copy the bot token. Keep it private.
3. Enable **Message Content Intent** on the Bot page: this bot enables that intent
   for its fallback `!` commands.
4. Generate an installation URL with `bot` and `applications.commands` scopes.
   Grant **View Channels**, **Send Messages**, **Embed Links**, **Connect**, and
   **Speak**, then add the bot to your server. Channel overrides must permit it
   to view and connect to the intended voice channel.

### 2a. Windows

1. Run `DiscordSpeaker-<version>-windows-x64-setup.exe` (per-user install, no admin needed). Optionally tick
   *Start when I sign in*.
2. Open **DiscordSpeaker** from the Start menu / desktop. **Settings → Discord bot token → Save token.** The token is stored in
   Windows Credential Manager, never in a file.
3. **Dashboard:** pick the voice channel, press **Start Streaming**. The tray icon keeps it running when you close the window.
4. Default source is *System audio – default output* (WASAPI loopback). Whatever you hear is what Discord hears. To stream
   only one app, send that app to a virtual cable (Windows Settings → System → Sound → Volume mixer) and choose
   *CABLE Output* on the **Devices** tab.

### 2b. macOS

1. Open the DMG and drag **DiscordSpeaker** to **Applications**. First launch: if Gatekeeper blocks an unsigned/ad-hoc build,
   right-click the app → **Open** (a Developer-ID-signed, notarized build avoids this: see [packaging](docs/development-macos.md)).
2. **Settings → Save token** (stored in the macOS Keychain).
3. Press **Start Streaming**. macOS asks for **System Audio Recording**; allow it. If you missed the prompt:
   **System Settings → Privacy & Security → Screen & System Audio Recording → *System Audio Recording Only* → enable DiscordSpeaker**, then press Start again.
   Without permission macOS delivers *silence* (the app shows “No signal reaching the app”).
4. To stream a single app, use the **Devices** tab (apps that are playing audio are listed) or `/route_app spotify`.
5. Using BlackHole instead (older macOS or virtual routing): install it, select **BlackHole 2ch** as a source, and route apps to a
   Multi-Output Device that includes BlackHole so you can still hear them. macOS asks for Microphone permission for input devices.

### 2c. Linux: desktop app

```bash
packaging/linux/install-desktop-launcher.sh   # creates .venv, installs deps, adds "DiscordSpeaker" to your app menu
# or run directly:
python3 -m venv .venv && . .venv/bin/activate && pip install -r src/requirements-desktop.txt && python src/app.py
```

The token needs a Secret Service keyring (GNOME Keyring / KWallet). Headless? Use Docker or export `DISCORD_BOT_TOKEN`.

### 2d. Linux: Docker (unchanged workflow)

For a new setup, run from the repository root. Keep your existing `.env` if you have already configured it:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and set DISCORD_BOT_TOKEN.
# Match HOST_UID/HOST_GID to `id -u` / `id -g`.
# Set PULSE_RUNTIME_DIR to your desktop's $XDG_RUNTIME_DIR/pulse.
docker compose up -d --build
docker compose logs -f bot
```

The token is passed at runtime and excluded from Git and the Docker build
context. Avoid running a second copy of the bot with the same token (the bot refuses to).

### 3. Share your first audio

1. Join a Discord voice channel and run `/join` (or press **Start Streaming** in the desktop app).
2. Start playing music in any app. On Windows/macOS it is captured automatically. On Linux open **pavucontrol → Playback**, find the app and set its output to
   **Discord Speaker** (older sinks may appear as **Discord_Speaker**), or use `/route_spotify` / `/route_app application:firefox`.
3. Everyone in the voice channel hears the routed playback.

## Audio routing

| | What `/route_app <name>` does |
| :--- | :--- |
| **Linux** | Moves every active playback stream whose application or process name matches into the virtual sink (unchanged). |
| **macOS** | Switches capture to that app's audio only (Core Audio per-process tap). The app must be playing. |
| **Windows** | Not available: see [limitations](#known-limitations). The reply explains the virtual-cable alternative. |

### Listen locally

* **Windows / macOS (system or loopback capture):** you keep hearing your audio normally: capture does not mute playback.
* **Linux:** by default routed audio plays in Discord only. To hear it locally as well:

```bash
docker compose exec bot ./audio_ctl.sh enable-loopback
docker compose exec bot ./audio_ctl.sh disable-loopback   # turn monitoring back off
```

## Commands

| Slash command | Prefix command | Action |
| :--- | :--- | :--- |
| `/join [channel]` | `!join` | Join a voice channel and start streaming |
| `/leave` | `!leave` | Stop streaming and disconnect |
| `/status` | `!status` | Show voice and audio status (capture state, format, buffer, routed apps) |
| `/route_app <application>` | `!route_app <application>` | Route / capture a matching app (see table above) |
| `/route_spotify` | `!route` | Convenience shortcut for Spotify |
| `/volume <1-200>` | `!volume <1-200>` | Adjust the volume sent to Discord (100 = unity, bit-transparent) |

## Demos

<table>
  <tr>
    <th width="50%">Join a voice channel</th>
    <th width="50%">Stream audio</th>
  </tr>
  <tr>
    <td><img src=".github/assets/Join.gif" alt="Demo of joining a Discord voice channel" width="480"></td>
    <td><img src=".github/assets/Audio.gif" alt="Demo of streaming audio to Discord" width="480"></td>
  </tr>
  <tr>
    <th>Check status</th>
    <th>Leave a voice channel</th>
  </tr>
  <tr>
    <td><img src=".github/assets/Status.gif" alt="Demo of checking the bot's status" width="480"></td>
    <td><img src=".github/assets/Leave.gif" alt="Demo of leaving a Discord voice channel" width="480"></td>
  </tr>
</table>

## Audio quality

Full detail: [docs/audio-quality.md](docs/audio-quality.md). In short:

* One owner for every conversion. Capture at the device's **native rate** (44.1/48/96 kHz…), downmix to stereo if needed,
  resample **once** with a 128-tap-per-phase Kaiser polyphase filter (measured **123 dB SNR** at 1 kHz and ±0.1 dB flat to 20 kHz
  for 44.1→48 kHz, >100 dB across the other tested rates; a 48 kHz source is **bit-exact**), buffer, then feed **float32 straight into libopus**
  (`opus_encode_float`). There is no 16-bit step before Discord's encode.
* Music-tuned Opus: `audio` application, `music` signal, fullband, complexity 10, VBR, **FEC off** (at a fixed bitrate it takes
  bits from the music), bitrate = the voice channel's bitrate (up to Opus' 510 kbps). discord.py's default of 128 kbps, FEC on and a 15 % loss
  assumption is bypassed.
* Underruns fade out and re-prime (no clicks); overflows drop the oldest audio; clock drift between capture and Discord's clock is
  absorbed with a ±0.1 % micro-adjust only when the buffer leaves its band; format changes (device rate switches) rebuild the converter.
  All events are counted and shown in the app and `/status`.
* Volume 100 % is unity gain. Above 100 % a transparent soft limiter prevents clipping. No AGC, no noise suppression, no normalisation.

**Not lossless.** Discord voice is always Opus-compressed (and capped by the channel bitrate; the server and your friends' clients
add their own handling). The goal is to add *no avoidable degradation before* Discord's encoder: it cannot make Discord lossless.

## Configuration

Desktop app: **Settings** tab (stored in your user config directory; the token lives in the OS credential store).

Docker / headless (`.env`):

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | Required | Discord bot token; keep in local `.env` |
| `SINK_NAME` | `DiscordSpeaker` | Internal sink name; its monitor is `<name>.monitor` |
| `AUTO_CREATE_SINK` | `true` | Create the virtual output when the bot connects |
| `COMMAND_PREFIX` | `!` | Prefix for fallback text commands |
| `HOST_UID` / `HOST_GID` | `1000` / `1000` | Desktop user's numeric UID/GID |
| `PULSE_RUNTIME_DIR` | `/run/user/1000/pulse` | Host directory containing the `native` socket |
| `DS_OPUS_BITRATE_KBPS`, `DS_BUFFER_MS`, `DS_DITHER`, … | see [.env.example](.env.example) | Optional audio-quality overrides (`DS_<FIELD>` of `QualityConfig`) |

Docker sets `PULSE_SERVER=unix:/run/host/pulse/native` inside the container.
The socket directory is mounted so recreated sockets remain visible after a
host audio restart. The process runs without root privileges, with a read-only
root filesystem and a small writable `/tmp`.

## Starting, stopping and background running

* **One click:** Start/Stop Streaming in the window or tray menu. **Launch at login** and **start minimised** are in Settings.
* Closing the window while streaming keeps DiscordSpeaker in the tray / menu bar; closing it while stopped quits.
* The Discord bot and capture run in a separate **engine process** the app talks to over a private stdin/stdout pipe (no network port).
  If the app exits or crashes the engine shuts itself down and stops capture, and the bot refuses to start twice with the same token.
* Logs: structured JSON lines with the token redacted: Windows `%LOCALAPPDATA%\DiscordSpeaker\Logs`, macOS `~/Library/Logs/DiscordSpeaker`, Linux `~/.local/state/DiscordSpeaker`.

## Troubleshooting

- **macOS: “No signal reaching the app” / silence:** grant *System Audio Recording* (see 2b). Input devices need *Microphone*.
- **macOS: audio stops after sleep or when switching headphones:** the helper re-attaches automatically (default-output change and a 3 s watchdog); if it does not, press Stop/Start.
- **Windows: nothing is captured:** pick *System audio – default output* on the Devices tab; if your device is exclusive-mode only, disable “Allow applications to take exclusive control”.
- **Linux: audio connection refused:** verify pipewire-pulse is running in your desktop
  session and that the runtime directory and UID/GID match. This setup uses
  PipeWire's PulseAudio compatibility server; cookie-authenticated standalone
  PulseAudio may require additional cookie configuration.
- **No sound:** ensure the source is playing, the bot has joined
  a voice channel, and its Discord speaking permissions are allowed.
- **Voice handshake fails:** check bot logs, channel permissions, and outbound network access.
- **Privileged intents error:** enable Message Content Intent for the bot.
- **“Another DiscordSpeaker process is already using this bot token”:** stop the other instance (or remove the stale Docker container).
- **Host audio restarted (Linux):** capture re-attaches by itself; if it does not, use `/leave` then `/join`.
- **Crackling:** raise *Jitter buffer* (Settings) to 200–300 ms. The app shows underruns/overruns/drift fixes live.

Container health checks verify audio-server access only. Discord errors appear
in logs. Docker does not automatically restart a container just because it is
unhealthy. Stopping the container leaves the host's virtual output in place.
To remove it, first `/leave`, then run
`docker compose exec bot ./audio_ctl.sh teardown` before stopping the container.

The Linux audio socket allows the bot to record and control host audio routing, just
as the native application does. Read-only mounting does not restrict these
protocol operations. Desktop audio must remain available while streaming.

## Known limitations

* **Discord is lossy.** See [Audio quality](#audio-quality).
* **Windows per-app capture is not implemented.** The “process loopback” API needs COM interop that PyAudioWPatch does not expose. Use a virtual cable.
* **macOS system/app capture needs 14.2+** and the System Audio Recording permission. ScreenCaptureKit was evaluated and not used (it needs the broader Screen Recording permission and offers nothing the tap lacks).
* **Idle loopback streams** (Windows/macOS) deliver no samples while nothing plays; Discord receives silence.
* **Local monitoring of virtual-cable sources** is not provided on Windows/macOS (system/loopback capture is audible anyway); Linux monitoring uses PipeWire loopback.
* **Windows and macOS builds are not notarized/code-signed by default**; see the development guides to sign them.
* **Real-device verification** on Windows and per-app/BlackHole/Intel on macOS are pending: see [Compatibility](#compatibility-and-verification).
* The VPS case from the original project still needs its own audio source: the app captures a *desktop* session.

## Development

* [Architecture & audit](docs/architecture.md) · [Audio quality](docs/audio-quality.md)
* Platform guides: [Linux](docs/development-linux.md) · [Windows](docs/development-windows.md) · [macOS](docs/development-macos.md)

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements-desktop.txt
python -m unittest discover -s tests -v      # fake devices, no hardware, no Discord
bash -n src/audio_ctl.sh
```

CI uses a placeholder token and never connects to Discord or a sound card. Keep `.env`, virtual environments, build outputs and tokens out of commits.

References: [Discord app setup](https://github.com/discord/discord-api-docs/blob/main/developers/quick-start/getting-started.mdx),
[Compose services](https://docs.docker.com/reference/compose-file/services/)
and [Pulse server addresses](https://wiki.freedesktop.org/www/Software/PulseAudio/Documentation/User/ServerStrings/).
