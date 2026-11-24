#!/usr/bin/env python3
"""DiscordSpeaker desktop entry point.

  python app.py              launch the controller
  python app.py --minimized  start hidden in the tray (used by launch-at-login)
  python app.py --engine     run the engine process (the controller spawns this itself in packaged builds)
"""
from __future__ import annotations

import sys


def selftest() -> int:
    """Used to verify packaged builds: Qt plugins load, the engine process starts, libopus resolves."""
    import json
    from audio.opus_loader import ensure_opus_loaded
    from PySide6.QtWidgets import QApplication
    import ui.main_window  # noqa: F401  (pulls in every UI module)
    from service.client import EngineClient
    app = QApplication.instance() or QApplication(sys.argv)
    c = EngineClient()
    c.start()
    try:
        info = c.request("hello")
        sources = c.request("list_sources", timeout=30)
    finally:
        c.stop()
    print(json.dumps({"ok": True, "platform": info["platform"], "sources": len(sources),
                      "opus": ensure_opus_loaded(), "qt": app.platformName()}))
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--engine" in sys.argv:
        from service.engine import main as engine_main
        return engine_main()

    from PySide6.QtWidgets import QApplication, QMessageBox
    from service import logsetup
    from service.instance import AlreadyRunning, InstanceLock
    from service.settings import AppSettings
    from ui.controller import Controller
    from ui.main_window import MainWindow

    logsetup.setup("ui")
    app = QApplication(sys.argv)
    app.setApplicationName("DiscordSpeaker")
    app.setQuitOnLastWindowClosed(False)
    lock = InstanceLock.named("ui")
    try:
        lock.acquire()
    except AlreadyRunning:
        QMessageBox.information(None, "DiscordSpeaker", "DiscordSpeaker is already running. Look for it in the tray / menu bar.")
        return 0
    settings = AppSettings.load()
    win = MainWindow(Controller(settings))
    if not ("--minimized" in sys.argv or settings.start_minimized) or win.tray is None:
        win.show()
    code = app.exec()
    lock.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
