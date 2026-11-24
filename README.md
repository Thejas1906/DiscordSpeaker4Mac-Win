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

Install the app, press **Start**, and your bot shows up in Spotify as a **Connect device**. Pick it in Spotify and the
music plays straight into your Discord voice channel. You control everything (songs, queue, volume) in the Spotify app.

```text
  Spotify app ──► "Connect to a device" ──► Music Speaker (your bot)
                                                   │  librespot (Spotify Connect receiver, runs inside DiscordSpeaker)
                                                   ▼  decoded PCM, float32
                                   resample once → jitter buffer → libopus (float32)
                                                   ▼
                                   Discord bot ──► your voice channel (/join)
```

1. **Install** DiscordSpeaker and add your bot token (Settings, once).
2. **Click Start.** The bot logs in on your machine and the Spotify device appears.
3. **Add the bot to your server** (the app has an *Add to server* button) and type `/join` in a voice channel.
4. **Open Spotify → Connect to a device → pick the bot's name.**
5. Play and control music in Spotify.

Nothing is captured from your speakers, so there is no system-audio permission, no virtual audio driver, and a Bluetooth headset
switching to call mode cannot degrade the sound. You hear the music in Discord (you are in the channel); your Mac's own speakers stay silent.

> **Needs Spotify Premium.** The receiver is [librespot](https://github.com/librespot-org/librespot), an unofficial client that
> Spotify's terms do not formally permit. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
> The older "capture any app / the whole system" mode is still available under **Devices → advanced sources**, and the Linux Docker deployment is unchanged.

## Compatibility and verification

| Platform | Spotify Connect receiver | Advanced: system/app capture | Verified here |
| :--- | :--- | :--- | :--- |
| **macOS** (Apple Silicon & Intel) | ✅ bundled `librespot` | Core Audio tap (14.2+), BlackHole inputs | ✅ Apple Silicon, real hardware: bot online, voice streaming, device advertised via Bonjour; system-tap capture verified. Intel not built here |
| **Windows 10/11 x64** | ✅ (needs `librespot.exe` at build time) | WASAPI loopback, virtual cables | ⚠️ unit tests on real Windows CI runners, mocked devices; **not run on a Windows desktop** |
| **Linux** | ✅ if `librespot` is installed | PulseAudio/PipeWire sink (Docker deployment) | ✅ Docker + real PulseAudio daemon; Spotify path covered by fake-receiver tests |

Hardware-dependent tests live in [`tests/test_hardware.py`](tests/test_hardware.py) (skipped unless `DS_HARDWARE_TESTS=1`).

## Requirements

* **Spotify Premium** (for the Spotify Connect mode).
* A Discord bot token and permission to add the bot to your server.
* Your computer and the device running Spotify must be on the **same network** (Spotify finds the bot over mDNS/Bonjour). VPNs, guest Wi-Fi and "client isolation" block this.
* macOS 12+ / Windows 10+ / Linux. macOS may ask to allow **Local Network** access: allow it.
* Windows: allow DiscordSpeaker through the firewall on **private networks** when asked.

## Quick start

### 1. Set up your Discord bot

1. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications).
   Use [logo.png](.github/assets/logo.png) as the application icon and bot avatar.
2. **Bot → Reset Token → Copy** (it is a long three-part string with dots: not the Public Key or Client Secret). Keep it private.
3. Enable **Message Content Intent** on the Bot page: this bot enables that intent for its fallback `!` commands.
4. You add the bot to your server from the app (**Add to server**), or generate an installation URL with `bot` and `applications.commands`
   scopes and **View Channels**, **Send Messages**, **Embed Links**, **Connect**, **Speak**.

### 2. Install

* **macOS:** open `DiscordSpeaker-<version>-macos-<arch>.dmg`, drag the app to **Applications**. Unsigned builds: right-click → **Open** the first time.
  macOS may ask for your login password once to let the app read the token it saved in the Keychain: choose **Always Allow**.
* **Windows:** run `DiscordSpeaker-<version>-windows-x64-setup.exe` (per-user, no admin). Optionally start with Windows.
* **Linux:** `packaging/linux/install-desktop-launcher.sh` (needs `librespot`), or the Docker deployment below.

Build instructions for each platform: [macOS](docs/development-macos.md) · [Windows](docs/development-windows.md) · [Linux](docs/development-linux.md).

### 3. Use it

1. Open DiscordSpeaker → **Settings → paste the bot token → Save token** (stored in Keychain / Credential Manager / Secret Service, never in a file).
2. **Dashboard → Start.** Step 1 turns green when the bot is online.
3. **Add to server** → choose your server → Authorize. In Discord, join a voice channel and type **`/join`** (or pick the channel in the app and press *Join*).
4. In Spotify (desktop, phone or web) press the **Connect to a device** icon and choose the bot's name, e.g. *Music Speaker*.
5. Press play. Control songs in Spotify. Press **Stop** to take the bot offline.

The first time you select the device Spotify signs it in with your account (this works over the local network, no password is typed anywhere);
later sessions reuse it.

### Linux headless (Docker, unchanged)

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
In Docker mode audio comes from the PulseAudio sink: route apps to **Discord Speaker** in pavucontrol, or use `/route_spotify`.

## Advanced: capturing other audio (not Spotify Connect)

| | What `/route_app <name>` does |
| :--- | :--- |
| **Linux** | Moves every active playback stream whose application or process name matches into the virtual sink (unchanged). |
| **macOS** | Switches capture to that app's audio only (Core Audio per-process tap). The app must be playing. |
| **Windows** | Not available: see [limitations](#known-limitations). The reply explains the virtual-cable alternative. |

### Listen locally

* **Windows / macOS (system or loopback capture, Devices tab):** you keep hearing your audio normally: capture does not mute playback.
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

- **The bot is not in Spotify's device list:** your Mac and the Spotify device must be on the same network (no VPN / guest Wi-Fi), the bot must be Started, and on macOS *Local Network* must be allowed for DiscordSpeaker. Premium is required.
- **Selecting the device does nothing / Spotify says it can't play:** you need Spotify Premium; try selecting the device again.
- **Friends hear nothing:** make sure they are in the same voice channel as the bot, and that you did not mute the bot *for them*. Check `~/Library/Logs/DiscordSpeaker/engine.jsonl` for `can_encrypt`.
- **macOS (advanced system capture only): silence:** grant *System Audio Recording*. Input devices need *Microphone*.
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
