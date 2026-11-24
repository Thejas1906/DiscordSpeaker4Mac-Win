# Linux integration rig: the production image plus a PulseAudio *daemon* with a dummy device so the real
# pactl/parec/audio_ctl.sh code paths run with no sound hardware. Build from the repo root:
#   docker build -t discordspeaker:test . && docker build -t discordspeaker:linux-it -f tests/docker/Dockerfile .
FROM discordspeaker:test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio && rm -rf /var/lib/apt/lists/*
USER bot
ENV XDG_RUNTIME_DIR=/tmp/xdg PULSE_SERVER=unix:/tmp/xdg/pulse/native
ENTRYPOINT ["/bin/bash", "-c"]
