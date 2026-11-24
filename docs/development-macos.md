# Development: macOS (Apple Silicon and Intel)

Prerequisites: Xcode command line tools (`swiftc`, `lipo`), Python 3.12+, `brew install opus` (libopus is bundled into the app from here).

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements-desktop.txt
src/audio/platforms/macos/tap/build.sh          # universal Core Audio helper → tap/build/dsaudiotap
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
python src/app.py
DS_HARDWARE_TESTS=1 python -m unittest tests.test_hardware -v   # real tap test: plays a tone with afplay
```

## How capture works

`dsaudiotap` (Swift, `src/audio/platforms/macos/tap/dsaudiotap.swift`) creates a private **Core Audio process tap**
(`CATapDescription`, macOS 14.2+) plus a private aggregate device on the default output, runs an IOProc, and writes interleaved little-endian float32 stereo to stdout at the device rate
after a JSON header on stderr. `capture --pid N…` taps only those processes; `list-apps` lists audio processes. Exit codes: 2 permission/unsupported, 3 no such app, 75 default output changed (the Python side restarts it).
`muteBehavior = unmuted`: you keep hearing your audio.

### Permissions (important)

* The tap needs **System Audio Recording** (System Settings → Privacy & Security → Screen & System Audio Recording → *System Audio Recording Only*). The packaged app declares `NSAudioCaptureUsageDescription`.
  **Without permission macOS delivers digital silence, not an error**: the app detects >8 s of silence and shows a hint.
* Input devices (BlackHole etc.) need **Microphone** (`NSMicrophoneUsageDescription`).
* When developing from a terminal or IDE, the *terminal/IDE app* is the process that needs the permission.
* The app never tries to bypass or pre-approve permissions.

### Verification status

On an Apple Silicon Mac (macOS 27, Bluetooth headphones at 44.1 kHz) the hardware test passes: the system tap captured `afplay` output with 440 Hz on the left and 880 Hz on the right. That test exposed a real bug
(the tap advertised 48 kHz while the device ran at 44.1 kHz, pitch-shifting audio), now fixed by reporting the device's real rate. The packaged app launches, its engine child starts and is reaped when the app is killed.
Not verified on hardware: per-app taps, BlackHole/input capture, sleep/wake, Intel Macs, Developer ID signing and notarization.

Run it yourself (from a terminal that has System Audio Recording permission):
`DS_HARDWARE_TESTS=1 .venv/bin/python -m unittest tests.test_hardware -v`

## BlackHole (optional)

Install [BlackHole](https://existential.audio/blackhole/) yourself (GPL-3.0 for the 2ch build; see their site for redistribution terms; not bundled here). Select *BlackHole 2ch* as the source and route apps to a Multi-Output Device (BlackHole + your speakers) to keep hearing them.
Sleep/wake and device changes: the input path has a 3 s data watchdog and the supervisor restarts the stream; the tap helper restarts on default-output changes.

## Build, sign, notarize

```bash
packaging/build_macos.sh     # current arch → dist/DiscordSpeaker.app and dist/DiscordSpeaker-<ver>-macos-<arch>.dmg
```

Run it once on an Apple Silicon Mac and once on an Intel Mac (or the `macos-15-intel` CI runner): libopus and Python are per-architecture. The default signature is **ad-hoc** (runs locally; other Macs need right-click → Open).
For distribution: `DS_SIGN_IDENTITY="Developer ID Application: …" packaging/build_macos.sh` (hardened runtime + `packaging/macos/entitlements.plist`), then
`xcrun notarytool submit dist/*.dmg --keychain-profile <profile> --wait && xcrun stapler staple dist/*.dmg`. Signing with a Developer ID and notarization have **not** been exercised (no certificate available).

An Intel build has not been produced or run by the author.
