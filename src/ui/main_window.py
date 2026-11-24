from __future__ import annotations

import logging
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSlider,
                               QSpinBox, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget)

from audio.quality_config import QualityConfig
from service.client import EngineError
from service.settings import AppSettings, SecretStoreError
from . import autostart
from .controller import Controller
from .theme import ACCENT, BAD, GOOD, QSS, WARN
from .widgets import LevelMeter, StatusPill, StepRow

log = logging.getLogger("DiscordSpeaker.ui")
OS_NAMES = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}
INVITE = "https://discord.com/oauth2/authorize?client_id={cid}&scope=bot%20applications.commands&permissions=3165184"


def asset(name: str) -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = base / "ui" / "assets" / name if hasattr(sys, "_MEIPASS") else base / "assets" / name
    return str(p)


class _Bridge(QObject):
    finished = Signal(object, object)   # error (or None), callback


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def header(text: str) -> QLabel:
    l = QLabel(text.upper())
    l.setObjectName("h")
    return l


class MainWindow(QMainWindow):
    def __init__(self, controller: Controller, start_hidden: bool = False):
        super().__init__()
        self.c = controller
        self.s = controller.settings
        self._quitting = False
        self._busy = False
        self._started = False
        self.tray = None
        self._channels: List[dict] = []
        self._bridge = _Bridge()
        self._bridge.finished.connect(lambda err, cb: self._done(err, cb))
        self.setWindowTitle("DiscordSpeaker")
        self.setWindowIcon(QIcon(asset("logo.png")))
        self.resize(560, 700)
        self.setStyleSheet(QSS)

        tabs = QTabWidget()
        tabs.addTab(self._dashboard(), "Dashboard")
        tabs.addTab(self._settings(), "Settings")
        self.tabs = tabs
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(20, 18, 20, 12)
        lay.addLayout(self._header())
        lay.addWidget(tabs, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setObjectName("sub")
        lay.addWidget(self.msg)
        self.setCentralWidget(root)
        self._tray()

        c = self.c
        c.state.connect(self.on_state)
        c.channels.connect(self.on_channels)
        c.message.connect(self.say)
        c.engine_up.connect(self._engine_up)
        QTimer.singleShot(0, self.boot)

    def _engine_up(self, up: bool) -> None:
        if not up and self._started:
            self._started = False
            self.say("The bot engine stopped unexpectedly. Press Start to try again.", "error")
            self._render_idle()

    # ---- layout ---------------------------------------------------------------------------
    def _header(self):
        row = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(asset("logo-nobg.png")).scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setStyleSheet("background: transparent;")
        col = QVBoxLayout()
        t = QLabel("DiscordSpeaker"); t.setObjectName("title")
        sub = QLabel("Play Spotify through your Discord bot")
        sub.setObjectName("sub")
        col.addWidget(t); col.addWidget(sub)
        row.addWidget(logo); row.addSpacing(8); row.addLayout(col); row.addStretch(1)
        return row

    def _dashboard(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(12); lay.setContentsMargins(0, 12, 0, 0)

        steps = card(); sl = QVBoxLayout(steps)
        sl.addWidget(header("Setup"))
        self.s_bot = StepRow(1, "Start the bot")
        self.s_server = StepRow(2, "Add the bot to your server", "Add to server")
        self.s_voice = StepRow(3, "Join your voice channel")
        self.s_spotify = StepRow(4, "Play from Spotify")
        for r in (self.s_bot, self.s_server, self.s_voice, self.s_spotify):
            sl.addWidget(r)
        self.s_server.button.clicked.connect(self.open_invite)
        self.s_server.button.setVisible(False)
        # optional manual join (the usual way is typing /join in Discord)
        jr = QHBoxLayout()
        self.channel_box = QComboBox(); self.channel_box.setAccessibleName("Voice channel")
        self.channel_box.setVisible(False)
        self.b_join = QPushButton("Join"); self.b_join.setVisible(False); self.b_join.clicked.connect(self.join_channel)
        self.b_leave = QPushButton("Leave"); self.b_leave.setVisible(False); self.b_leave.clicked.connect(self.leave_channel)
        rb = QPushButton("↻"); rb.setToolTip("Refresh channels"); rb.setFixedWidth(44); rb.setVisible(False)
        rb.clicked.connect(lambda: self.run(self.c.refresh_channels))
        self._refresh_btn = rb
        for x in (self.channel_box, rb, self.b_join, self.b_leave):
            jr.addWidget(x, 1 if x is self.channel_box else 0)
        sl.addLayout(jr)
        lay.addWidget(steps)

        lv = card(); ll = QVBoxLayout(lv)
        ll.addWidget(header("Audio"))
        self.meter = LevelMeter(); ll.addWidget(self.meter)
        self.l_health = QLabel("—"); self.l_health.setObjectName("sub"); self.l_health.setWordWrap(True)
        self.l_fmt = QLabel("—"); self.l_fmt.setObjectName("sub")
        ll.addWidget(self.l_fmt); ll.addWidget(self.l_health)
        self.l_hint = QLabel(""); self.l_hint.setWordWrap(True); self.l_hint.setStyleSheet(f"color: {WARN};")
        ll.addWidget(self.l_hint)
        vr = QHBoxLayout()
        vl = QLabel("Bot volume"); vl.setObjectName("sub")
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(1, 200); self.vol.setValue(self.s.volume_percent)
        self.vol.setAccessibleName("Bot volume")
        self.vol_l = QLabel(f"{self.s.volume_percent}%"); self.vol_l.setFixedWidth(48)
        self.vol.valueChanged.connect(self._vol)
        vr.addWidget(vl); vr.addWidget(self.vol, 1); vr.addWidget(self.vol_l)
        ll.addLayout(vr)
        vn = QLabel("Keep at 100% for untouched sound. Control the music itself in Spotify."); vn.setObjectName("sub"); vn.setWordWrap(True)
        ll.addWidget(vn)
        lay.addWidget(lv)

        self.b_main = QPushButton("▶  Start"); self.b_main.setObjectName("start")
        self.b_main.clicked.connect(self.toggle_bot)
        lay.addWidget(self.b_main)
        lay.addStretch(1)
        self._render_idle()
        return w

    def _settings(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 12, 0, 0); lay.setSpacing(12)
        t = card(); tl = QVBoxLayout(t)
        tl.addWidget(header("Discord bot token"))
        self.token = QLineEdit(); self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("Paste bot token (stored in the OS credential store)")
        self.token.setAccessibleName("Discord bot token")
        tl.addWidget(self.token)
        tr = QHBoxLayout()
        self.token_state = QLabel(""); self.token_state.setObjectName("sub")
        sv = QPushButton("Save token"); sv.clicked.connect(self.save_token)
        rm = QPushButton("Remove"); rm.clicked.connect(self.remove_token)
        tr.addWidget(self.token_state, 1); tr.addWidget(rm); tr.addWidget(sv)
        tl.addLayout(tr)
        lay.addWidget(t)

        q = card(); ql = QGridLayout(q); ql.setVerticalSpacing(10)
        ql.addWidget(header("Audio quality"), 0, 0, 1, 2)
        qc = self.s.quality_config()
        self.rate = QComboBox()
        for label, val in (("Auto (device native)", 0), ("44,100 Hz", 44100), ("48,000 Hz", 48000), ("96,000 Hz", 96000)):
            self.rate.addItem(label, val)
        self.rate.setCurrentIndex(max(0, self.rate.findData(qc.preferred_sample_rate)))
        self.buf = QSpinBox(); self.buf.setRange(40, 2000); self.buf.setSuffix(" ms"); self.buf.setValue(qc.buffer_ms)
        self.bitrate = QSpinBox(); self.bitrate.setRange(0, 510); self.bitrate.setSpecialValueText("Auto (channel max)")
        self.bitrate.setSuffix(" kbps"); self.bitrate.setValue(qc.opus_bitrate_kbps)
        self.dither = QCheckBox("Dither when 16-bit fallback is used"); self.dither.setChecked(qc.dither)
        for r, (k, wd) in enumerate((("Capture sample rate", self.rate), ("Jitter buffer", self.buf),
                                     ("Opus bitrate", self.bitrate)), 1):
            lab = QLabel(k); lab.setObjectName("sub"); ql.addWidget(lab, r, 0); ql.addWidget(wd, r, 1)
        ql.addWidget(self.dither, 4, 0, 1, 2)
        self.monitor = QCheckBox("Listen locally (Linux loopback)"); self.monitor.setChecked(self.s.local_monitoring)
        self.monitor.setVisible(sys.platform.startswith("linux"))
        ql.addWidget(self.monitor, 5, 0, 1, 2)
        note = QLabel("Quality changes apply the next time streaming starts. Discord transmission is lossy (Opus)."); note.setObjectName("sub"); note.setWordWrap(True)
        ql.addWidget(note, 6, 0, 1, 2)
        lay.addWidget(q)

        b = card(); bl = QVBoxLayout(b)
        bl.addWidget(header("Startup & behaviour"))
        self.cb_tray = QCheckBox("Keep running in the tray / menu bar when the window is closed"); self.cb_tray.setChecked(self.s.minimize_to_tray)
        self.cb_login = QCheckBox("Launch at login"); self.cb_login.setChecked(autostart.is_enabled())
        self.cb_min = QCheckBox("Start minimised"); self.cb_min.setChecked(self.s.start_minimized)
        self.cb_auto = QCheckBox("Start the bot automatically when the app opens"); self.cb_auto.setChecked(self.s.auto_stream_on_launch)
        for cb in (self.cb_tray, self.cb_login, self.cb_min, self.cb_auto):
            bl.addWidget(cb)
        lay.addWidget(b)
        ap = QPushButton("Apply settings"); ap.setObjectName("start"); ap.clicked.connect(self.apply_settings)
        lay.addWidget(ap)
        lay.addStretch(1)
        self._refresh_token_state()
        return w

    def _tray(self) -> None:
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(QIcon(asset("logo.png")), self)
        m = QMenu()
        self.a_show = QAction("Show DiscordSpeaker", self); self.a_show.triggered.connect(self.bring_front)
        self.a_toggle = QAction("Start", self); self.a_toggle.triggered.connect(self.toggle_bot)
        a_quit = QAction("Quit", self); a_quit.triggered.connect(self.quit_app)
        for a in (self.a_show, self.a_toggle): m.addAction(a)
        m.addSeparator(); m.addAction(a_quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.bring_front() if r == QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("DiscordSpeaker")
        self.tray.show()

    # ---- helpers -----------------------------------------------------------------------------
    def say(self, text: str, level: str = "info") -> None:
        color = {"info": "#949ba4", "warn": WARN, "error": BAD}.get(level, "#949ba4")
        self.msg.setStyleSheet(f"color: {color};")
        self.msg.setText(text)

    def run(self, fn: Callable, *args, then: Optional[Callable] = None, busy: bool = False) -> None:
        """Run blocking controller work off the UI thread."""
        if busy:
            self._busy = True; self._sync_buttons()
        def work():
            err = None
            try:
                fn(*args)
            except Exception as e:  # EngineError and friends: shown to the user, never contain the token
                err = e
            self._bridge.finished.emit(err, (then, busy))
        threading.Thread(target=work, daemon=True).start()

    def _done(self, err, cb) -> None:
        then, busy = cb
        if busy:
            self._busy = False
        if err:
            self.say(str(err), "error")
        elif then:
            then()
        self._sync_buttons()

    def _refresh_token_state(self) -> None:
        try:
            has = self.c.secrets.has()
            self.token_state.setText("● Token saved in the OS credential store" if has else "No token saved")
        except SecretStoreError as e:
            self.token_state.setText(str(e))

    # ---- boot / actions ---------------------------------------------------------------------------
    def boot(self) -> None:
        if not self.c.secrets.has():
            self.say("Add your Discord bot token in Settings to get started.", "warn")
            self.tabs.setCurrentIndex(1)
        elif self.s.auto_stream_on_launch:
            self.start_bot()
        else:
            self.say("Press Start to bring the bot online.", "info")

    def _selected_channel(self):
        i = self.channel_box.currentIndex()
        return self._channels[i] if 0 <= i < len(self._channels) else None

    @property
    def running(self) -> bool:
        return self._started

    def toggle_bot(self) -> None:
        (self.stop_bot if self._started else self.start_bot)()

    def start_bot(self) -> None:
        """Start = bot online + Spotify Connect device visible (the engine starts the receiver once the bot is up)."""
        if self._busy or self._started:
            return
        if not self.c.secrets.has():
            self.say("Add your Discord bot token in Settings first.", "warn"); self.tabs.setCurrentIndex(1); return
        self._started = True
        self.say("Starting…", "info")
        self.run(self.c.start_bot, busy=True)

    def stop_bot(self) -> None:
        self._started = False
        self.run(self.c.stop_bot, busy=True, then=self._render_idle)
        self.say("Stopped. The bot is offline and removed from Spotify's device list.", "info")

    def open_invite(self) -> None:
        cid = self.c.last_state.get("discord", {}).get("user_id")
        if cid:
            QDesktopServices.openUrl(QUrl(INVITE.format(cid=cid)))

    def join_channel(self) -> None:
        ch = self._selected_channel()
        if ch:
            self.run(self.c.join, ch["guild_id"], ch["channel_id"], busy=True)

    def leave_channel(self) -> None:
        self.run(self.c.leave, busy=True)

    def _vol(self, v: int) -> None:
        self.vol_l.setText(f"{v}%")
        self.c.set_volume(v)

    def save_token(self) -> None:
        tok = self.token.text().strip()
        try:
            self.c.save_token(tok)
        except SecretStoreError as e:
            self.say(str(e), "error"); return
        self.token.clear(); self._refresh_token_state()
        self.say("Token saved securely. Connecting to Discord…", "info")
        self.run(self.c.connect_discord)

    def remove_token(self) -> None:
        self.c.secrets.delete(); self._refresh_token_state(); self.say("Token removed.", "info")

    def apply_settings(self) -> None:
        try:
            cfg = QualityConfig.from_dict({**self.s.quality, "preferred_sample_rate": self.rate.currentData(),
                                           "buffer_ms": self.buf.value(), "opus_bitrate_kbps": self.bitrate.value(),
                                           "dither": self.dither.isChecked()})
        except ValueError as e:
            self.say(str(e), "error"); return
        self.s.minimize_to_tray = self.cb_tray.isChecked()
        self.s.start_minimized = self.cb_min.isChecked()
        self.s.auto_stream_on_launch = self.cb_auto.isChecked()
        self.s.local_monitoring = self.monitor.isChecked()
        try:
            autostart.set_enabled(self.cb_login.isChecked())
            self.s.launch_at_login = self.cb_login.isChecked()
        except Exception as e:
            self.say(f"Could not change launch-at-login: {e}", "warn")
        def go():
            restart = self.c.apply_quality(cfg.to_dict())
            self._restart_needed = restart
        self.run(go, then=lambda: self.say("Settings saved." + (" Restart streaming to apply quality changes."
                                                                 if getattr(self, "_restart_needed", False) else ""), "info"))

    # ---- signals from engine ------------------------------------------------------------------------
    def on_channels(self, chans: list) -> None:
        self._channels = chans
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        want = 0
        for i, ch in enumerate(chans):
            self.channel_box.addItem(f"{ch['guild']} › {ch['channel']}")
            if ch["channel_id"] == self.s.channel_id:
                want = i
        self.channel_box.setCurrentIndex(want)
        self.channel_box.blockSignals(False)

    def _render_idle(self) -> None:
        self.s_bot.set("todo", "Press Start. The bot logs in and appears in Spotify's device list.")
        self.s_server.set("todo", "After the bot is online, add it to your Discord server.")
        self.s_server.button.setVisible(False)
        self.s_voice.set("todo", "In Discord, join a voice channel and type /join.")
        self.s_spotify.set("todo", "Open Spotify → Connect to a device → pick the bot.")
        for x in (self.channel_box, self.b_join, self.b_leave, self._refresh_btn):
            x.setVisible(False)
        self.meter.set_levels(0.0, 0.0)
        self.l_fmt.setText("—"); self.l_health.setText("—"); self.l_hint.setText("")
        self.b_main.setText("▶  Start"); self.b_main.setObjectName("start"); self._restyle()
        if self.tray:
            self.a_toggle.setText("Start")

    def _restyle(self) -> None:
        self.b_main.style().unpolish(self.b_main); self.b_main.style().polish(self.b_main)

    def on_state(self, st: dict) -> None:
        a, d = st["audio"], st["discord"]
        sp = a.get("spotify") or {}
        voice = d.get("voice") or []
        self.meter.set_levels(a["peak"], a["rms"])

        # 1 bot
        if d.get("error"):
            self.s_bot.set("error", d["error"])
        elif d.get("logged_in"):
            lat = f" · {d['latency_ms']} ms" if d.get("latency_ms") is not None else ""
            self.s_bot.set("done", f"Online as {d.get('name') or d.get('user', '')}{lat}")
        else:
            self.s_bot.set("active", "Connecting to Discord…")
        online = bool(d.get("logged_in"))

        # 2 server
        n = d.get("guilds", 0)
        if not online:
            self.s_server.set("todo", "After the bot is online, add it to your Discord server."); self.s_server.button.setVisible(False)
        elif n:
            self.s_server.set("done", f"In {n} server{'s' if n != 1 else ''}"); self.s_server.button.setVisible(True)
            self.s_server.button.setText("Add to another")
        else:
            self.s_server.set("active", "Not in any server yet. Click the button, choose your server, Authorize.")
            self.s_server.button.setVisible(True); self.s_server.button.setText("Add to server")

        # 3 voice
        for x in (self.channel_box, self.b_join, self._refresh_btn):
            x.setVisible(online and n > 0 and not voice)
        self.b_leave.setVisible(bool(voice))
        if voice:
            self.s_voice.set("done", ("Streaming in " if voice[0]["playing"] else "Connected to ") + voice[0]["channel"])
        elif online and n:
            self.s_voice.set("active", "In Discord, join a voice channel and type /join, or pick one below.")
        else:
            self.s_voice.set("todo", "In Discord, join a voice channel and type /join.")

        # 4 spotify
        cs = a["state"]
        name = sp.get("device_name") or d.get("name") or "the bot"
        if cs == "error":
            self.s_spotify.set("error", a.get("last_error", "Spotify receiver error")[:160])
        elif cs == "recovering":
            self.s_spotify.set("active", "Reconnecting the Spotify receiver…")
        elif cs != "running":
            self.s_spotify.set("active" if online else "todo",
                               "Starting the Spotify receiver…" if online else "Open Spotify → Connect to a device → pick the bot.")
        elif sp.get("now_playing"):
            self.s_spotify.set("done", f"Now playing: {sp['now_playing']}")
        elif sp.get("connected"):
            self.s_spotify.set("done", f"Spotify connected as {sp.get('user') or 'your account'}. Press play in Spotify.")
        else:
            self.s_spotify.set("active", f"Open Spotify → Connect to a device → select “{name}”.")

        if a["input_rate"]:
            conv = "" if a["input_rate"] == 48000 else f"{a['input_rate']:,} Hz → "
            self.l_fmt.setText(f"{conv}48,000 Hz · stereo · {a['latency_ms']:.0f} ms buffer" if cs == "running" else "—")
        else:
            self.l_fmt.setText("Waiting for audio from Spotify")
        self.l_health.setText(f"{a['underruns']} underruns · {a['overruns']} overruns · {a['drift_corrections']} drift fixes · "
                              f"{a['clipped_samples']} clipped · {a['frames_out'] // 48000} s sent")
        self.l_hint.setText("")
        self.b_main.setText("■  Stop" if self._started else "▶  Start")
        self.b_main.setObjectName("stop" if self._started else "start"); self._restyle()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.b_main.setEnabled(not self._busy)
        if self.tray:
            self.a_toggle.setText("Stop" if self._started else "Start")

    # ---- window / tray lifecycle ---------------------------------------------------------------------------
    def bring_front(self) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()

    def closeEvent(self, ev) -> None:
        if (not self._quitting and self.tray and self.s.minimize_to_tray and self._started):
            ev.ignore()
            self.hide()
            self.tray.showMessage("DiscordSpeaker", "The bot is still running in the background. Use the tray icon to stop or quit.",
                                  QSystemTrayIcon.Information, 3000)
            return
        self.quit_app()
        ev.accept()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.s.save()
        self.c.shutdown()
        if self.tray:
            self.tray.hide()
        QApplication.quit()
