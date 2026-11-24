# Development: Linux

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements-desktop.txt      # desktop app + tests
python -m unittest discover -s tests -v
bash -n src/audio_ctl.sh
python src/app.py                                           # desktop controller
```

* Runtime tools: `pactl`, `parec` (pulseaudio-utils), optional `ffmpeg` fallback, libopus (`libopus0`), a Secret Service keyring for the token.
* Qt on a headless box: `QT_QPA_PLATFORM=offscreen`.

## Real PulseAudio without sound hardware (what CI uses)

```bash
docker build -t discordspeaker:test .
docker build -t discordspeaker:linux-it -f tests/docker/Dockerfile .
docker run --rm -v "$PWD:/work:ro" discordspeaker:linux-it \
  "mkdir -p /tmp/xdg/pulse && python /work/tests/docker/linux_integration.py"
```

It starts a PulseAudio daemon with a dummy sink and exercises the original routing code paths plus the new backend end to end (sink creation,
route-app, a 440/880 Hz stereo tone, channel separation, recovery after the sink disappears, process cleanup, teardown). PipeWire-pulse itself is not in this rig;
the code only uses the PulseAudio protocol and `pactl --format=json`.

## Docker deployment

Unchanged: `docker compose up -d --build`. The image installs only `src/requirements.txt` (no Qt/keyring). `.dockerignore` is an allow-list.

## Desktop launcher

`packaging/linux/install-desktop-launcher.sh` adds an application-menu entry. A packaged Linux app is intentionally not provided: Docker and the venv launcher cover Linux.
