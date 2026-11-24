#!/usr/bin/env python3
"""
DiscordSpeaker - cross-platform desktop audio -> Discord voice bot.

Linux:   captures the PipeWire/PulseAudio virtual sink 'DiscordSpeaker.monitor' (unchanged Docker workflow).
Windows: WASAPI loopback / input endpoints.      macOS: Core Audio process tap / BlackHole inputs.
All capture, conversion and Opus encoding lives in the `audio` package; this module owns Discord.
"""

import asyncio
import json
import logging
import math
import os
import signal
import subprocess
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from audio.discord_source import make_source
from audio.models import CaptureState
from audio.opus_loader import ensure_opus_loaded
from audio.platforms.linux import pulse
from audio.quality_config import QualityConfig
from audio.service import AudioService

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("DiscordSpeaker")

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SINK_NAME = os.getenv("SINK_NAME", "DiscordSpeaker")
AUTO_CREATE_SINK = os.getenv("AUTO_CREATE_SINK", "true").lower() in ("true", "1", "yes")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
LOGO_URL = "https://raw.githubusercontent.com/lucid-codr/DiscordSpeaker/main/.github/assets/logo.png"

IS_LINUX = sys.platform.startswith("linux")


# Ensure libopus is loaded (bundled copy in packaged builds, system library otherwise)
if not ensure_opus_loaded():
    logger.warning("libopus not found: voice playback will fail. Install libopus (see README).")


def pulse_list(kind: str) -> list[dict]:
    """Read structured PulseAudio data, independent of locale and numeric sink IDs."""
    return pulse.pulse_list(kind)


def is_sink_active(sink_name: str = SINK_NAME) -> bool:
    """Check for an exact sink name on the host audio server."""
    return pulse.is_sink_active(sink_name, pulse_list_fn=lambda k: pulse_list(k))


def ensure_sink_active(sink_name: str = SINK_NAME) -> bool:
    """Ensure the null sink is created."""
    script_path = os.path.join(os.path.dirname(__file__), "audio_ctl.sh")
    return pulse.ensure_sink_active(sink_name, script_path, is_active=lambda n: is_sink_active(n))


def get_routed_apps(sink_name: str = SINK_NAME) -> list[str]:
    """Resolve numeric sink IDs to show all apps routed to this speaker."""
    return pulse.get_routed_apps(sink_name, pulse_list_fn=lambda k: pulse_list(k))


def route_app_stream(application: str, sink_name: str = SINK_NAME) -> tuple[bool, str]:
    """Move all active streams matching an app name or process name to the speaker."""
    return pulse.route_app_stream(
        application, sink_name,
        pulse_list_fn=lambda k: pulse_list(k),
        is_active=lambda n: is_sink_active(n),
        ensure=lambda n: ensure_sink_active(n),
    )


def route_spotify_stream(sink_name: str = SINK_NAME) -> tuple[bool, str]:
    """Convenience shortcut; audio from every routed application is supported."""
    return route_app_stream("spotify", sink_name)


class AudioBot(commands.Bot):
    def __init__(self, audio: Optional[AudioService] = None, stop_capture_on_leave: bool = IS_LINUX):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix=COMMAND_PREFIX,
            intents=intents,
            help_command=None,
        )
        self._audio = audio
        # Docker/Linux keeps the original behaviour (capture only while in a channel). The desktop
        # controller keeps capture alive across Discord reconnects and /leave.
        self.stop_capture_on_leave = stop_capture_on_leave
        self.manage_sink = IS_LINUX
        self.on_state_change = None   # optional callback(dict) used by the engine process

    @property
    def audio(self) -> AudioService:
        if self._audio is None:
            self._audio = AudioService(
                QualityConfig.from_env(),
                sink_name=SINK_NAME, auto_create_sink=AUTO_CREATE_SINK,
                script_path=os.path.join(os.path.dirname(__file__), "audio_ctl.sh"),
            ) if IS_LINUX else AudioService(QualityConfig.from_env())
        return self._audio

    @audio.setter
    def audio(self, value: AudioService) -> None:
        self._audio = value

    @property
    def current_volume(self) -> float:
        return self.audio.pipeline.volume

    async def setup_hook(self):
        # Sync slash commands on startup
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} application slash commands.")
        except Exception as e:
            logger.error(f"Failed to sync application commands: {e}")

    async def on_ready(self):
        logger.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s).")

        if IS_LINUX and AUTO_CREATE_SINK:
            if ensure_sink_active(SINK_NAME):
                logger.info(f"Audio sink '{SINK_NAME}' is ready.")
            else:
                logger.warning(f"Could not verify audio sink '{SINK_NAME}'. Check PipeWire / pactl.")

        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"desktop audio ({SINK_NAME})" if IS_LINUX else "desktop audio",
        ))
        self._notify()

    async def on_resumed(self):
        logger.info("Discord gateway session resumed; audio capture untouched.")
        self._notify()

    async def on_disconnect(self):
        self._notify()

    def _notify(self):
        if self.on_state_change:
            try:
                self.on_state_change(self.discord_status())
            except Exception:
                logger.exception("state callback failed")

    def discord_status(self) -> dict:
        vcs = [vc for vc in self.voice_clients if vc.is_connected()]
        return {
            "logged_in": bool(self.user) and self.is_ready(),
            "user": str(self.user) if self.user else "",
            "latency_ms": round(self.latency * 1000) if self.is_ready() and math.isfinite(self.latency) else None,
            "voice": [{"guild": vc.guild.name, "channel": vc.channel.name, "playing": vc.is_playing()} for vc in vcs],
        }

    def start_streaming(self, voice_client: discord.VoiceClient):
        """Start (or restart) feeding the audio pipeline into Discord voice."""
        if voice_client.is_playing():
            voice_client.stop()
        if not self.audio.running:
            self.audio.start()
        channel_bps = getattr(voice_client.channel, "bitrate", None)
        source = make_source(self.audio.pipeline, channel_bps)
        enc = getattr(source, "encoder", None)
        logger.info("Streaming %s -> '%s' (opus %s)", self.audio.status().get("selected"), voice_client.channel.name,
                    f"{enc.bitrate_bps // 1000} kbps float32" if enc else "16-bit fallback")

        def after_playback(err):
            if err:
                logger.error(f"Stream playback error: {err}")
            else:
                logger.info("Stream playback stopped.")
            self._notify()

        voice_client.play(source, after=after_playback)
        logger.info(f"Audio stream active in channel: {voice_client.channel.name}")
        self._notify()

    async def connect_and_stream(self, guild: discord.Guild, target: discord.VoiceChannel):
        """Shared by /join, !join and the desktop controller. Returns (voice_client, error_message)."""
        me = guild.me or await guild.fetch_member(self.user.id)
        perms = target.permissions_for(me)
        if not perms.connect:
            return None, (f"❌ I don't have permission to **Connect** to `{target.name}`.\n"
                          f"Please grant the bot the **Connect** permission (or appropriate role) for this channel, or test in another channel.")
        if not perms.speak:
            return None, (f"❌ I don't have permission to **Speak** in `{target.name}`.\n"
                          f"Please grant the bot the **Speak** permission for this channel.")

        if IS_LINUX and AUTO_CREATE_SINK and not is_sink_active(SINK_NAME):
            ensure_sink_active(SINK_NAME)

        voice_client: Optional[discord.VoiceClient] = guild.voice_client
        if voice_client:
            if voice_client.is_connected():
                if voice_client.channel.id != target.id:
                    await voice_client.move_to(target)
            else:
                await voice_client.disconnect(force=True)
                voice_client = None

        if not voice_client:
            try:
                voice_client = await asyncio.wait_for(target.connect(self_deaf=True, timeout=10.0), timeout=12.0)
            except asyncio.TimeoutError:
                return None, (f"❌ Timed out connecting to `{target.name}`.\n"
                              f"Discord did not complete the voice handshake. Please verify channel permissions or try another voice channel.")
            except Exception as e:
                return None, f"❌ Failed to connect to `{target.name}`: {e}"

        if not voice_client or not voice_client.is_connected():
            return None, f"❌ Failed to establish connection with `{target.name}`."

        try:
            self.start_streaming(voice_client)
        except Exception as e:
            logger.error(f"Could not start streaming: {e}")
            return None, f"❌ Connected, but audio capture could not start: {e}"
        return voice_client, None

    async def leave_guild(self, guild: discord.Guild) -> Optional[str]:
        voice_client: Optional[discord.VoiceClient] = guild.voice_client
        if not voice_client:
            return None
        name = voice_client.channel.name
        if voice_client.is_playing():
            voice_client.stop()
        await voice_client.disconnect(force=True)
        if self.stop_capture_on_leave and not any(vc.is_connected() for vc in self.voice_clients):
            await asyncio.get_running_loop().run_in_executor(None, self.audio.stop)
        self._notify()
        return name


bot = AudioBot()


def route_any(application: str) -> tuple[bool, str]:
    """Linux keeps the original pactl routing; Windows/macOS use their native backend."""
    if IS_LINUX:
        return route_app_stream(application)
    return bot.audio.route_application(application)


def route_spotify_any() -> tuple[bool, str]:
    return route_spotify_stream(SINK_NAME) if IS_LINUX else route_any("spotify")


def routed_apps() -> list[str]:
    if IS_LINUX:
        return get_routed_apps(SINK_NAME)
    return bot.audio.applications()


def capture_label() -> str:
    st = bot.audio.status()
    if IS_LINUX:
        sink_ok = is_sink_active(SINK_NAME)
        return f"✅ `{SINK_NAME}` (Active)" if sink_ok else f"❌ `{SINK_NAME}` (Inactive)"
    state = st["state"]
    icon = "✅" if state == "running" else ("⚠️" if state in ("starting", "recovering") else "⏹️")
    return f"{icon} {state.capitalize()} · `{st.get('selected') or 'no source'}`"


@bot.tree.command(name="join", description="Join your voice channel and begin streaming audio")
@app_commands.describe(channel="Optional voice channel to join (defaults to your current channel)")
async def slash_join(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
):
    await interaction.response.defer()

    target_channel = channel
    if not target_channel:
        if interaction.user.voice and interaction.user.voice.channel:
            target_channel = interaction.user.voice.channel
        else:
            await interaction.followup.send(
                "❌ You must either be in a voice channel or specify one in the command.",
                ephemeral=True,
            )
            return

    voice_client, error = await bot.connect_and_stream(interaction.guild, target_channel)
    if error:
        await interaction.followup.send(error, ephemeral=True)
        return

    apps = routed_apps()
    apps_text = ", ".join(apps) if apps else "None detected yet"

    embed = discord.Embed(
        title="🔊 Audio Stream Active",
        description=f"Streaming desktop audio into **{target_channel.name}**",
        color=0x5865F2,
    )
    embed.set_thumbnail(url=LOGO_URL)
    embed.add_field(name="Connected Channel", value=target_channel.name, inline=True)
    embed.add_field(name="Volume", value=f"{int(bot.current_volume * 100)}%", inline=True)
    embed.add_field(name="Active App(s)", value=apps_text, inline=False)
    embed.set_footer(text="Route any app to Discord Speaker in pavucontrol, or use /route_app" if IS_LINUX
                     else "Pick the capture source in the DiscordSpeaker app, or use /route_app")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="leave", description="Stop streaming and leave the voice channel")
async def slash_leave(interaction: discord.Interaction):
    await interaction.response.defer()
    name = await bot.leave_guild(interaction.guild)
    if name is None:
        await interaction.followup.send("❌ Bot is not currently in a voice channel.", ephemeral=True)
        return
    await interaction.followup.send(f"👋 Disconnected from **{name}**.")


@bot.tree.command(name="status", description="Show stream and audio capture status")
async def slash_status(interaction: discord.Interaction):
    guild = interaction.guild
    voice_client: Optional[discord.VoiceClient] = guild.voice_client

    apps = routed_apps()
    apps_text = "\n".join([f"• {a}" for a in apps]) if apps else "None"
    health = bot.audio.status()

    embed = discord.Embed(
        title="📊 Discord Speaker Status",
        color=0x3498DB,
    )
    embed.set_thumbnail(url=LOGO_URL)
    embed.add_field(name="PipeWire Sink" if IS_LINUX else "Audio Capture", value=capture_label(), inline=True)
    embed.add_field(
        name="Voice Connection",
        value=f"Connected to **{voice_client.channel.name}**" if voice_client else "Disconnected",
        inline=True,
    )
    embed.add_field(
        name="Streaming",
        value="▶️ Playing" if (voice_client and voice_client.is_playing()) else "⏹️ Idle",
        inline=True,
    )
    embed.add_field(name="Volume", value=f"{int(bot.current_volume * 100)}%", inline=True)
    embed.add_field(name="Gateway Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    embed.add_field(name="Audio Format",
                    value=f"{health['input_rate'] or '—'} Hz → 48 kHz stereo · {health['latency_ms']:.0f} ms buffer",
                    inline=True)
    embed.add_field(name="Apps Routed to Sink" if IS_LINUX else "Apps Playing Audio", value=apps_text, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="route_app", description="Route an active app's playback to Discord Speaker")
@app_commands.describe(application="App or process name, e.g. spotify, firefox, or vlc")
async def slash_route_app(interaction: discord.Interaction, application: str):
    await interaction.response.defer()
    success, msg = await asyncio.get_running_loop().run_in_executor(None, route_any, application)
    await interaction.followup.send(f"{'✅' if success else '⚠️'} {msg}")


@bot.tree.command(name="route_spotify", description="Automatically move active Spotify playback to DiscordSpeaker")
async def slash_route_spotify(interaction: discord.Interaction):
    await interaction.response.defer()
    success, msg = await asyncio.get_running_loop().run_in_executor(None, route_spotify_any)
    if success:
        await interaction.followup.send(f"✅ {msg}")
    else:
        await interaction.followup.send(f"⚠️ {msg}")


@bot.tree.command(name="volume", description="Set streaming output volume (1% - 200%)")
@app_commands.describe(level="Volume level percentage (e.g. 100 for default, 50 for half)")
async def slash_volume(interaction: discord.Interaction, level: app_commands.Range[int, 1, 200]):
    bot.audio.set_volume(level)
    bot._notify()
    await interaction.response.send_message(f"🔊 Bot stream volume set to **{level}%**.")


# Legacy prefix commands for convenience (!join, !leave, !status, etc.)
@bot.command(name="join")
async def prefix_join(ctx: commands.Context, *, channel: Optional[discord.VoiceChannel] = None):
    target_channel = channel
    if not target_channel:
        if ctx.author.voice and ctx.author.voice.channel:
            target_channel = ctx.author.voice.channel
        else:
            await ctx.send("❌ You must be in a voice channel or specify one.")
            return

    voice_client, error = await bot.connect_and_stream(ctx.guild, target_channel)
    if error:
        await ctx.send(error)
        return
    await ctx.send(f"🔊 Streaming desktop audio in **{target_channel.name}**.")


@bot.command(name="leave")
async def prefix_leave(ctx: commands.Context):
    name = await bot.leave_guild(ctx.guild)
    if name is None:
        await ctx.send("❌ Bot is not connected to a voice channel.")
        return
    await ctx.send("👋 Disconnected.")


@bot.command(name="status")
async def prefix_status(ctx: commands.Context):
    vc = ctx.guild.voice_client
    status_str = f"Audio capture: {capture_label().replace('`', '')}\n"
    status_str += f"Voice: {'Connected to ' + vc.channel.name if vc else 'Disconnected'}\n"
    status_str += f"Volume: {int(bot.current_volume * 100)}%"
    await ctx.send(f"```{status_str}```")


@bot.command(name="route_app")
async def prefix_route_app(ctx: commands.Context, *, application: str):
    success, msg = await asyncio.get_running_loop().run_in_executor(None, route_any, application)
    await ctx.send(f"{'✅' if success else '⚠️'} {msg}")


@bot.command(name="route")
async def prefix_route(ctx: commands.Context):
    success, msg = await asyncio.get_running_loop().run_in_executor(None, route_spotify_any)
    prefix = "✅" if success else "⚠️"
    await ctx.send(f"{prefix} {msg}")


@bot.command(name="volume")
async def prefix_volume(ctx: commands.Context, level: int):
    if not 1 <= level <= 200:
        await ctx.send("❌ Volume must be between 1 and 200.")
        return
    bot.audio.set_volume(level)
    bot._notify()
    await ctx.send(f"🔊 Volume set to **{level}%**.")


def handle_shutdown(signum, frame):
    logger.info("Received termination signal. Shutting down...")
    try:
        if bot._audio is not None:
            bot._audio.stop()
    finally:
        sys.exit(0)


def main():
    """Headless entry point (Docker / Linux CLI). The desktop app uses service.engine instead."""
    if not TOKEN or TOKEN.startswith("YOUR_"):
        logger.error("DISCORD_BOT_TOKEN is missing or not configured in .env!")
        sys.exit(1)
    from service.instance import InstanceLock, AlreadyRunning
    try:
        lock = InstanceLock.for_token(TOKEN)
        lock.acquire()
    except AlreadyRunning:
        logger.error("Another DiscordSpeaker instance is already running with this bot token. Exiting.")
        sys.exit(1)
    from service import logsetup
    logsetup.register_secret(TOKEN)
    for handler in logging.getLogger().handlers:
        handler.addFilter(logsetup.RedactFilter())
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.error("Discord rejected the bot token (DISCORD_BOT_TOKEN). Check it in the Developer Portal.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        logger.error("Enable 'Message Content Intent' for the bot in the Discord Developer Portal.")
        sys.exit(1)


if __name__ == "__main__":
    main()
