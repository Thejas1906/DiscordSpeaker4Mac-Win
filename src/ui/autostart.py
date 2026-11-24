"""Launch-at-login for each OS, with no extra dependencies."""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

APP_NAME = "DiscordSpeaker"
LABEL = "com.discordspeaker.app"


def launch_command() -> list:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        if sys.platform == "darwin" and ".app/Contents/MacOS" in exe:
            return ["/usr/bin/open", "-a", exe.split(".app/Contents/MacOS")[0] + ".app", "--args", "--minimized"]
        return [exe, "--minimized"]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "app.py"), "--minimized"]


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _desktop_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "discordspeaker.desktop"


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _plist_path().exists()
    return _desktop_path().exists()


def set_enabled(enabled: bool) -> None:
    cmd = launch_command()
    if sys.platform == "win32":
        import subprocess, winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
    elif sys.platform == "darwin":
        p = _plist_path()
        if enabled:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(plistlib.dumps({"Label": LABEL, "ProgramArguments": cmd, "RunAtLoad": True,
                                          "ProcessType": "Interactive"}))
        else:
            p.unlink(missing_ok=True)
    else:
        p = _desktop_path()
        if enabled:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("[Desktop Entry]\nType=Application\nName=DiscordSpeaker\nExec="
                         + " ".join(f'"{c}"' if " " in c else c for c in cmd) + "\nX-GNOME-Autostart-enabled=true\n")
        else:
            p.unlink(missing_ok=True)
