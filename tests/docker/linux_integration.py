"""Real PulseAudio integration test (run inside tests/docker image). Exits non-zero on any failure."""
import os
import subprocess
import sys
import time

sys.path.insert(0, "/work/src")
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-no-network")
import numpy as np

import bot
from audio.models import CaptureState
from audio.pipeline import AudioPipeline
from audio.platforms.linux.capture import LinuxCapture
from audio.quality_config import QualityConfig

FAILS = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        FAILS.append(name)


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


# 1. a real PulseAudio daemon with a dummy output
os.makedirs("/tmp/xdg/pulse", exist_ok=True)
subprocess.Popen(["pulseaudio", "-n", "--exit-idle-time=-1", "--daemonize=no", "--log-level=warning",
                  "-L", f"module-native-protocol-unix socket=/tmp/xdg/pulse/native",
                  "-L", "module-null-sink sink_name=dummy_out"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(50):
    if sh("pactl", "info").returncode == 0:
        break
    time.sleep(0.2)
check("pulseaudio daemon reachable", sh("pactl", "info").returncode == 0)

# 2. original code paths: sink creation through audio_ctl.sh, exact-name detection
check("sink absent before setup", not bot.is_sink_active("DiscordSpeaker"))
check("ensure_sink_active (audio_ctl.sh setup)", bot.ensure_sink_active("DiscordSpeaker"))
sinks = bot.pulse_list("sinks")
sink = next(s for s in sinks if s["name"] == "DiscordSpeaker")
spec = sink["sample_specification"]
check("virtual sink is 48 kHz stereo", "2ch" in spec and "48000Hz" in spec, str(spec))

# 3. a playing app routed with the original route_app_stream
app = subprocess.Popen(
    ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi", "-i",
     "aevalsrc=0.3*sin(2*PI*440*t)|0.3*sin(2*PI*880*t):s=48000:d=60", "-f", "pulse", "-name", "FakeSpotify",
     "-stream_name", "tone", "dummy_out"])
time.sleep(2)
ok, msg = bot.route_spotify_stream()
check("route_spotify moved the stream", ok, msg)
check("get_routed_apps sees it", bot.get_routed_apps() == ["FakeSpotify"], str(bot.get_routed_apps()))
ok, msg = bot.route_app_stream("nonexistent-app")
check("unknown app fails helpfully", (not ok) and "Start playback" in msg)

# 4. new capture backend against the real monitor source
pipe = AudioPipeline(QualityConfig(buffer_ms=80))
ev = []
cap = LinuxCapture(pipe, ev.append, auto_create_sink=False)
srcs = cap.list_sources()
check("monitor listed first as loopback", srcs and srcs[0].id == "DiscordSpeaker.monitor" and srcs[0].kind.value == "loopback",
      str([s.id for s in srcs][:3]))
cap.start()
for _ in range(50):
    if cap.state == CaptureState.RUNNING:
        break
    time.sleep(0.1)
check("capture RUNNING", cap.state == CaptureState.RUNNING, cap.state.value)
time.sleep(1.0)
frames = []
t0 = time.perf_counter()
for k in range(150):                      # 3 s, paced on absolute deadlines like discord.py's AudioPlayer
    time.sleep(max(0.0, t0 + (k + 1) * 0.02 - time.perf_counter()))
    frames.append(pipe.read_frame())
y = np.concatenate(frames)[24000:]
f = np.fft.rfftfreq(len(y), 1 / 48000)
spec_l = np.abs(np.fft.rfft(y[:, 0] * np.hanning(len(y)))); spec_r = np.abs(np.fft.rfft(y[:, 1] * np.hanning(len(y))))
check("left channel = 440 Hz", abs(f[spec_l.argmax()] - 440) < 3, f"{f[spec_l.argmax()]:.1f}")
check("right channel = 880 Hz", abs(f[spec_r.argmax()] - 880) < 3, f"{f[spec_r.argmax()]:.1f}")
def band_db(sp, f0): return 20 * np.log10(sp[(abs(f - f0) < 20)].max() + 1e-12)
sep = band_db(spec_l, 440) - band_db(spec_l, 880)
check("channel separation > 60 dB", sep > 60, f"{sep:.1f} dB")
rms_l = float(np.sqrt(np.mean(y[:, 0] ** 2)))
check("level preserved (0.3 amp -> rms 0.212)", abs(rms_l - 0.3 / np.sqrt(2)) < 0.01, f"{rms_l:.4f}")
check("native 48 kHz, no resampling", pipe.health.input_rate == 48000 and pipe.health.input_channels == 2)
check("no underruns / overruns in 3 s", pipe.health.underruns == 0 and pipe.health.overruns == 0,
      f"u={pipe.health.underruns} o={pipe.health.overruns}")

# 5. Opus float path on real captured audio
from audio.discord_source import OpusFloatEncoder
enc = OpusFloatEncoder(pipe.config, 96000)
pk = enc.encode(frames[-1])
check("opus packet produced", 10 < len(pk) < 1500, f"{len(pk)} bytes @ {enc.bitrate_bps}")

# 6. PulseAudio restart / source loss -> recovery
sh("pactl", "unload-module", "module-null-sink")  # drops dummy_out and DiscordSpeaker sinks
time.sleep(1.5)
check("source loss detected", pipe.health.restarts >= 1 or cap.state in (CaptureState.RECOVERING, CaptureState.ERROR),
      f"state={cap.state.value} restarts={pipe.health.restarts}")
bot.ensure_sink_active("DiscordSpeaker")
for _ in range(80):
    if cap.state == CaptureState.RUNNING:
        break
    time.sleep(0.1)
check("capture recovered after sink returned", cap.state == CaptureState.RUNNING, cap.state.value)

# 7. clean shutdown: no leftover parec/ffmpeg capture processes
cap.stop()
time.sleep(0.5)
def procs(name):
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].endswith(name.encode()):
                out.append(pid)
        except OSError:
            pass
    return out
left = procs("parec")
check("no orphaned parec after stop", not left, str(left))
check("capture state STOPPED", cap.state == CaptureState.STOPPED)
app.kill()

# 8. teardown through audio_ctl.sh
r = sh("/app/audio_ctl.sh", "teardown")
check("teardown removes sink", not bot.is_sink_active("DiscordSpeaker"), r.stdout.strip()[:80])

print(f"\n{'ALL PASSED' if not FAILS else 'FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
