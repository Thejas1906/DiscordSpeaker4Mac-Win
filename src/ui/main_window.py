from __future__ import annotations

import logging
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSlider,
                               QSpinBox, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget)

from audio.quality_config import QualityConfig
from service.client import EngineError
from service.settings import AppSettings, SecretStoreError
from . import autostart
from .controller import Controller
from .theme import ACCENT, BAD, GOOD, QSS, WARN
from .widgets import LevelMeter, StatusPill

log = logging.getLogger("DiscordSpeaker.ui")
OS_NAMES = {"darwin": "macOS", "win32": "Windows", "linux": "Linux"}


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
        self._channels: List[dict] = []
        self._sources: List[dict] = []
        self._bridge = _Bridge()
        self._bridge.finished.connect(lambda err, cb: self._done(err, cb))
        self.setWindowTitle("DiscordSpeaker")
        self.setWindowIcon(QIcon(asset("logo.png")))
        self.resize(560, 700)
        self.setStyleSheet(QSS)

        tabs = QTabWidget()
        tabs.addTab(self._dashboard(), "Dashboard")
        tabs.addTab(self._devices(), "Devices")
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
        c.sources.connect(self.on_sources)
        c.channels.connect(self.on_channels)
        c.message.connect(self.say)
        c.engine_up.connect(lambda up: None if up else self.say("The audio engine stopped unexpectedly.", "error"))
        QTimer.singleShot(0, self.boot)

    # ---- layout ---------------------------------------------------------------------------
    def _header(self):
        row = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(asset("logo-nobg.png")).scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo.setStyleSheet("background: transparent;")
        col = QVBoxLayout()
        t = QLabel("DiscordSpeaker"); t.setObjectName("title")
        sub = QLabel(f"{OS_NAMES.get(sys.platform, platform.system())} · desktop audio → Discord voice")
        sub.setObjectName("sub")
        col.addWidget(t); col.addWidget(sub)
        row.addWidget(logo); row.addSpacing(8); row.addLayout(col); row.addStretch(1)
        return row

    def _dashboard(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setSpacing(12); lay.setContentsMargins(0, 12, 0, 0)
        st = card(); sl = QVBoxLayout(st)
        self.p_discord, self.p_capture, self.p_voice = StatusPill("Discord"), StatusPill("Capture"), StatusPill("Voice")
        for p in (self.p_discord, self.p_capture, self.p_voice):
            sl.addWidget(p)
        lay.addWidget(st)

        lv = card(); ll = QVBoxLayout(lv)
        ll.addWidget(header("Input level"))
        self.meter = LevelMeter(); ll.addWidget(self.meter)
        g = QGridLayout(); g.setHorizontalSpacing(18)
        self.l_source, self.l_fmt, self.l_lat, self.l_health = QLabel("—"), QLabel("—"), QLabel("—"), QLabel("—")
        for i, (k, v) in enumerate((("Source", self.l_source), ("Format", self.l_fmt),
                                    ("Latency", self.l_lat), ("Stream health", self.l_health))):
            kk = QLabel(k); kk.setObjectName("sub"); g.addWidget(kk, i, 0); v.setWordWrap(True); g.addWidget(v, i, 1)
        g.setColumnStretch(1, 1)
        ll.addLayout(g)
        self.l_hint = QLabel(""); self.l_hint.setWordWrap(True); self.l_hint.setStyleSheet(f"color: {WARN};")
        ll.addWidget(self.l_hint)
        lay.addWidget(lv)

        ctl = card(); cl = QVBoxLayout(ctl)
        cl.addWidget(header("Voice channel"))
        row = QHBoxLayout()
        self.channel_box = QComboBox(); self.channel_box.setAccessibleName("Voice channel")
        rb = QPushButton("↻"); rb.setToolTip("Refresh channels"); rb.setFixedWidth(44)
        rb.clicked.connect(lambda: self.run(self.c.refresh_channels))
        row.addWidget(self.channel_box, 1); row.addWidget(rb)
        cl.addLayout(row)
        cl.addWidget(header("Volume"))
        vr = QHBoxLayout()
        self.vol = QSlider(Qt.Horizontal); self.vol.setRange(1, 200); self.vol.setValue(self.s.volume_percent)
        self.vol.setAccessibleName("Stream volume")
        self.vol_l = QLabel(f"{self.s.volume_percent}%"); self.vol_l.setFixedWidth(48)
        self.vol.valueChanged.connect(self._vol)
        vr.addWidget(self.vol, 1); vr.addWidget(self.vol_l)
        cl.addLayout(vr)
        lay.addWidget(ctl)

        br = QHBoxLayout()
        self.b_start = QPushButton("▶  Start Streaming"); self.b_start.setObjectName("start")
        self.b_stop = QPushButton("■  Stop Streaming"); self.b_stop.setObjectName("stop")
        self.b_start.clicked.connect(self.start_stream); self.b_stop.clicked.connect(self.stop_stream)
        br.addWidget(self.b_start, 1); br.addWidget(self.b_stop, 1)
        lay.addLayout(br)
        lay.addStretch(1)
        self.b_stop.setEnabled(False)
        return w

    def _devices(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w); lay.setContentsMargins(0, 12, 0, 0)
        c = card(); cl = QVBoxLayout(c)
        cl.addWidget(header("Capture source"))
        self.src_list = QListWidget()
        self.src_list.setStyleSheet("QListWidget{background:#1e1f22;border:1px solid #3f4147;border-radius:8px;}"
                                    "QListWidget::item{padding:8px;} QListWidget::item:selected{background:%s;}" % ACCENT)
        self.src_list.setAccessibleName("Capture sources")
        cl.addWidget(self.src_list, 1)
        row = QHBoxLayout()
        rf = QPushButton("↻  Refresh devices"); rf.clicked.connect(lambda: self.run(self.c.refresh_sources))
        use = QPushButton("Use selected source"); use.clicked.connect(self.use_source)
        row.addWidget(rf); row.addStretch(1); row.addWidget(use)
        cl.addLayout(row)
        self.l_out = QLabel(""); self.l_out.setObjectName("sub"); self.l_out.setWordWrap(True)
        cl.addWidget(self.l_out)
        lay.addWidget(c, 1)
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
        self.cb_auto = QCheckBox("Start streaming automatically on launch"); self.cb_auto.setChecked(self.s.auto_stream_on_launch)
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
        self.a_toggle = QAction("Start Streaming", self); self.a_toggle.triggered.connect(self.toggle_stream)
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
        if self.c.secrets.has():
            self.run(self.c.connect_discord, then=self._after_connect)
        else:
            self.say("Add your Discord bot token in Settings to get started.", "warn")
            self.tabs.setCurrentIndex(2)
            self.run(self.c.ensure_engine)

    def _after_connect(self) -> None:
        if self.s.auto_stream_on_launch:
            QTimer.singleShot(2500, self.start_stream)

    def _selected_channel(self):
        i = self.channel_box.currentIndex()
        return self._channels[i] if 0 <= i < len(self._channels) else None

    def start_stream(self) -> None:
        if self._busy:
            return
        if not self.c.secrets.has():
            self.say("Add your Discord bot token in Settings first.", "warn"); self.tabs.setCurrentIndex(2); return
        ch = self._selected_channel()

        def go():
            if not (self.c.last_state.get("discord", {}).get("logged_in")):
                self.c.connect_discord()
                for _ in range(100):
                    if self.c.last_state.get("discord", {}).get("logged_in"):
                        break
                    err = self.c.last_state.get("discord", {}).get("error")
                    if err:
                        raise EngineError(err)
                    time.sleep(0.2)
                else:
                    raise EngineError("Timed out waiting for Discord to connect.")
            self.c.start_streaming(*( (ch["guild_id"], ch["channel_id"]) if ch else (0, 0)))
        self.run(go, busy=True, then=lambda: self.say(
            "Streaming." if ch else "Capturing audio. Join a voice channel with /join, or pick one above and press Start.", "info"))

    def stop_stream(self) -> None:
        self.run(self.c.stop_streaming, busy=True, then=lambda: self.say("Stopped.", "info"))

    def toggle_stream(self) -> None:
        (self.stop_stream if self.c.streaming else self.start_stream)()

    def _vol(self, v: int) -> None:
        self.vol_l.setText(f"{v}%")
        self.c.set_volume(v)

    def use_source(self) -> None:
        it = self.src_list.currentItem()
        if not it:
            return
        sid = it.data(Qt.UserRole)
        if not it.data(Qt.UserRole + 1):
            self.say("That source is unavailable right now.", "warn"); return
        self.run(self.c.select_source, sid, then=lambda: self.say("Source switched.", "info"))

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

    def on_sources(self, srcs: list) -> None:
        self._sources = srcs
        self.src_list.clear()
        chosen = self.s.source_id
        for s in srcs:
            label = s["name"] + ("" if s["available"] else "  — unavailable")
            if s.get("detail"):
                label += f"\n{s['detail']}"
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, s["id"]); it.setData(Qt.UserRole + 1, s["available"])
            if not s["available"]:
                it.setForeground(Qt.gray)
            self.src_list.addItem(it)
            if s["id"] == chosen:
                self.src_list.setCurrentItem(it)
        if self.src_list.currentRow() < 0 and self.src_list.count():
            self.src_list.setCurrentRow(0)

    def on_state(self, st: dict) -> None:
        a, d = st["audio"], st["discord"]
        self.meter.set_levels(a["peak"], a["rms"])
        if d.get("error"):
            self.p_discord.set(d["error"], BAD)
        elif d.get("logged_in"):
            lat = f" · {d['latency_ms']} ms" if d.get("latency_ms") is not None else ""
            self.p_discord.set(f"Online as {d.get('user', '')}{lat}", GOOD)
        else:
            self.p_discord.set("Connecting…" if self.c.secrets.has() else "No token", WARN)
        cs = a["state"]
        self.p_capture.set({"running": "Capturing", "starting": "Starting…", "recovering": f"Recovering — {a['last_error'][:60]}",
                            "stopped": "Idle", "error": f"Error — {a['last_error'][:80]}"}.get(cs, cs),
                           {"running": GOOD, "starting": WARN, "recovering": WARN, "stopped": "#6d6f78", "error": BAD}.get(cs, WARN))
        v = d.get("voice") or []
        self.p_voice.set(("Streaming in " + v[0]["channel"]) if v and v[0]["playing"] else (f"Connected to {v[0]['channel']}" if v else "Not in a channel"),
                         GOOD if v and v[0]["playing"] else ("#6d6f78"))
        self.l_source.setText(a.get("selected") or "—")
        if a["input_rate"]:
            conv = "" if a["input_rate"] == 48000 else f"{a['input_rate']:,} Hz → "
            self.l_fmt.setText(f"{conv}48,000 Hz · stereo · {a['input_channels']} ch in")
        else:
            self.l_fmt.setText("48,000 Hz · stereo")
        self.l_lat.setText(f"{a['latency_ms']:.0f} ms" if cs == "running" else "—")
        self.l_health.setText(f"{a['underruns']} underruns · {a['overruns']} overruns · {a['drift_corrections']} drift fixes · {a['clipped_samples']} clipped · {a['frames_out'] // 48000} s sent")
        self.l_hint.setText(
            "No signal reaching the app. If audio is playing, check the capture source"
            + (" and the macOS ‘System Audio Recording’ permission (README → macOS)." if sys.platform == "darwin" else ".")
            if cs == "running" and a.get("silent_s", 0) > 8 and not v else "")
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        streaming = self.c.streaming
        voice = bool(self.c.last_state.get("discord", {}).get("voice"))
        self.b_start.setEnabled(not self._busy and not (streaming and voice))
        self.b_stop.setEnabled(not self._busy and (streaming or voice))
        if self.tray:
            self.a_toggle.setText("Stop Streaming" if streaming else "Start Streaming")

    # ---- window / tray lifecycle ---------------------------------------------------------------------------
    def bring_front(self) -> None:
        self.showNormal(); self.raise_(); self.activateWindow()

    def closeEvent(self, ev) -> None:
        if (not self._quitting and self.tray and self.s.minimize_to_tray and self.c.streaming):
            ev.ignore()
            self.hide()
            self.tray.showMessage("DiscordSpeaker", "Still streaming in the background. Use the tray icon to stop or quit.",
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
