# Third-party software

DiscordSpeaker bundles or depends on the following. Their licences apply to them, not to this project.

| Component | Use | Licence |
| :--- | :--- | :--- |
| [librespot](https://github.com/librespot-org/librespot) 0.8.0 | Spotify Connect receiver (bundled binary in packaged apps) | MIT |
| [libopus](https://opus-codec.org/) | Opus encoding for Discord voice (bundled on macOS) | BSD-3-Clause |
| [discord.py](https://github.com/Rapptz/discord.py), [davey](https://pypi.org/project/davey/) | Discord client library, voice end-to-end encryption | MIT, MIT |
| [PySide6 / Qt](https://www.qt.io/qt-for-python) | Desktop UI | LGPL-3.0 |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) (Windows) | WASAPI loopback capture | Apache-2.0 / MIT |
| [sounddevice](https://python-sounddevice.readthedocs.io/) / PortAudio (macOS) | Input-device capture | MIT |
| [keyring](https://github.com/jaraco/keyring) | OS credential store | MIT |

**Spotify:** librespot is an independent, reverse-engineered client. It requires a Spotify Premium account, and
Spotify's terms of service do not formally permit third-party clients or redistributing playback. You are responsible for
how you use it. DiscordSpeaker does not bundle, modify or circumvent anything beyond running librespot as published.
