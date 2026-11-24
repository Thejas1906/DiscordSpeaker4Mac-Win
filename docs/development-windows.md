# Development: Windows 10/11 x64

Prerequisites: Python 3.12 x64, PowerShell. Optional: Inno Setup 6 (`choco install innosetup`).

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -r src\requirements-desktop.txt
$env:QT_QPA_PLATFORM = "offscreen"; python -m unittest discover -s tests -v   # fake devices
python src\app.py                                                             # run the controller
$env:DS_HARDWARE_TESTS = "1"; python -m unittest tests.test_hardware -v       # real WASAPI loopback (needs an audio device)
```

* Capture: `src/audio/platforms/windows/capture.py` over **PyAudioWPatch** (`pyaudiowpatch`, WASAPI host API 13). Sources: `loopback:default` (follows the default output; re-attaches when it changes),
  `loopback:<device>`, `input:<device>` (e.g. **CABLE Output (VB-Audio Virtual Cable)**).
* Loopback delivers no samples while the system is silent; health is judged by endpoint presence, not by data arrival.
* VB-Audio Cable: install it yourself (donationware, not redistributable by this project). Route an app to *CABLE Input* in Volume mixer; capture *CABLE Output*.
* Token: Windows Credential Manager via `keyring`. Autostart: `HKCU\...\Run`. Logs: `%LOCALAPPDATA%\DiscordSpeaker\Logs`.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Produces `dist\DiscordSpeaker\` (PyInstaller folder, runs `--selftest`) and, with Inno Setup, `dist\DiscordSpeaker-<ver>-windows-x64-setup.exe`.
Code signing (recommended to avoid SmartScreen warnings) is not configured: sign `DiscordSpeaker.exe` and the installer with `signtool` after the build.

**Status: this code has been written and unit-tested with a mocked WASAPI layer on macOS. It has not been run on Windows by the author. Run the hardware test above on a Windows machine before relying on it.**
