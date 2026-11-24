# PyInstaller spec for DiscordSpeaker (macOS .app, Windows folder/exe, Linux folder).
# Build:  pyinstaller packaging/discordspeaker.spec --noconfirm --distpath dist --workpath build
import ctypes.util
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
IS_MAC, IS_WIN = sys.platform == "darwin", sys.platform == "win32"

datas = [(str(SRC / "ui" / "assets"), "ui/assets")]
binaries = []
hidden = ["bot", "service.engine", "audio.platforms.linux.capture", "audio.platforms.macos.capture",
          "audio.platforms.windows.capture", "dotenv"]

if IS_MAC:
    helper = SRC / "audio/platforms/macos/tap/build/dsaudiotap"
    if not helper.exists():
        raise SystemExit("Build the Core Audio helper first: src/audio/platforms/macos/tap/build.sh")
    binaries.append((str(helper), "."))
    opus = os.environ.get("DS_OPUS_LIBRARY") or ctypes.util.find_library("opus") or "/opt/homebrew/lib/libopus.dylib"
    if opus and Path(opus).exists():
        binaries.append((str(Path(opus).resolve()), "."))
    hidden += ["keyring.backends.macOS", "sounddevice"]
elif IS_WIN:
    hidden += ["keyring.backends.Windows", "pyaudiowpatch"]
else:
    hidden += ["keyring.backends.SecretService", "keyring.backends.libsecret"]

# Qt modules the controller never uses (keeps the bundle small)
excludes = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQuick", "PySide6.QtQml",
            "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts",
            "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
            "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtSensors", "tkinter"]

a = Analysis([str(SRC / "app.py")], pathex=[str(SRC)], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)

icon = None
for cand in (ROOT / "build" / ("DiscordSpeaker.icns" if IS_MAC else "DiscordSpeaker.ico"),):
    if cand.exists():
        icon = str(cand)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="DiscordSpeaker", console=False, icon=icon,
          disable_windowed_traceback=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DiscordSpeaker")

if IS_MAC:
    app = BUNDLE(coll, name="DiscordSpeaker.app", icon=icon, bundle_identifier="com.discordspeaker.app", info_plist={
        "CFBundleName": "DiscordSpeaker", "CFBundleDisplayName": "DiscordSpeaker",
        "CFBundleShortVersionString": os.environ.get("DS_VERSION", "2.0.0"),
        "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
        # Shown by macOS in the permission prompts:
        "NSAudioCaptureUsageDescription": "DiscordSpeaker captures your Mac's system audio so it can stream it to your Discord voice channel.",
        "NSMicrophoneUsageDescription": "DiscordSpeaker reads audio from the input device you choose (for example BlackHole) to stream it to Discord. It does not record your microphone unless you select it.",
    })
