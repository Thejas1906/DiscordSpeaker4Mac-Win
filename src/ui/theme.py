from __future__ import annotations

ACCENT = "#5865F2"
GOOD = "#3BA55D"
WARN = "#FAA61A"
BAD = "#ED4245"

QSS = f"""
* {{ font-size: 13px; }}
QWidget {{ background: #1e1f22; color: #e3e5e8; }}
QLabel#title {{ font-size: 20px; font-weight: 700; }}
QLabel#sub {{ color: #949ba4; }}
QLabel#h {{ color: #949ba4; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QFrame#card {{ background: #2b2d31; border-radius: 12px; }}
QFrame#card QLabel {{ background: transparent; }}
QPushButton {{ background: #3a3c42; border: none; border-radius: 8px; padding: 9px 16px; }}
QPushButton:hover {{ background: #45474e; }}
QPushButton:disabled {{ color: #6d6f78; background: #2f3136; }}
QPushButton#start {{ background: {ACCENT}; color: white; font-weight: 700; font-size: 14px; padding: 12px 20px; }}
QPushButton#start:hover {{ background: #6d79f6; }}
QPushButton#start:disabled {{ background: #3b3f6e; color: #8c91c4; }}
QPushButton#stop {{ background: {BAD}; color: white; font-weight: 700; font-size: 14px; padding: 12px 20px; }}
QPushButton#stop:hover {{ background: #f0585a; }}
QPushButton#stop:disabled {{ background: #5c2f31; color: #b08587; }}
QComboBox, QLineEdit, QSpinBox {{ background: #1e1f22; border: 1px solid #3f4147; border-radius: 8px; padding: 7px 10px; }}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #2b2d31; selection-background-color: {ACCENT}; border: 1px solid #3f4147; }}
QSlider::groove:horizontal {{ height: 6px; background: #3f4147; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -5px 0; background: white; border-radius: 8px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: #949ba4; padding: 8px 16px; border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: white; border-bottom: 2px solid {ACCENT}; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QToolTip {{ background: #111214; color: #e3e5e8; border: 1px solid #3f4147; }}
"""
