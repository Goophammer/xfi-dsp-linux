#!/usr/bin/env python3
"""xfi_tray.py - tray icon for the X-Fi DSP: switches and sliders of the driver's DSP controls, and your settings kept across reboots.

It reads the controls from the card, so it shows what the loaded mode has. Nothing here touches the DSP directly: every change goes through
`amixer`, the recording source through PipeWire's ports. Settings: ~/.config/xfi-dsp/settings.json.

Run as your normal user: python3 src/xfi_tray.py   (needs PyQt6, alsa-utils, pactl)
"""
import json
import os
import re
import subprocess
import sys

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDial, QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel,
                             QMenu, QPushButton, QRadioButton, QSlider, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

CARD = 'XFi'
CONF = os.path.expanduser('~/.config/xfi-dsp/settings.json')
SOURCE_CTL = 'DSP Recording Source'
LEVEL_CTL = 'DSP Mic Capture Volume'	# the mic's level, also PipeWire's input volume
BOOST_CTL = 'DSP Mic Boost Capture Volume'	# the input chip's gain, in dB
MASTER_CTL = 'DSP Master Playback Volume'	# the taskbar volume
NOT_SAVED = (LEVEL_CTL, MASTER_CTL)		# PipeWire remembers these
PORTS = {'Mic': 'xfi-dsp-mic', 'What U Hear': 'xfi-dsp-wuh'}
# (tab, control, label, slider that belongs to it). CMSS-3D first: switching it resets EAX and Mic Environment.
# Master is the knob below the tabs, Output sits next to it.
LAYOUT = [
    ('Master', MASTER_CTL, 'Master Volume', None),
    ('Output', 'DSP Auto Mute Speakers Playback Switch', 'Mute speakers when headphones are plugged in', None),
    ('Effects', 'DSP CMSS-3D Playback Switch', 'CMSS-3D', 'DSP CMSS-3D Level Playback Volume'),
    ('Effects', 'DSP Crystalizer Playback Switch', 'Crystalizer', 'DSP Crystalizer Level Playback Volume'),
    ('Effects', 'DSP SVM Playback Switch', 'SVM', None),
    ('Effects', 'DSP Bass Boost Playback Switch', 'Bass Boost', 'DSP Bass Boost Level Playback Volume'),
    ('EQ', 'DSP EQ Preset', 'Preset', 'DSP EQ Level Playback Volume'),
    ('Microphone', LEVEL_CTL, 'Mic Level', None),
    ('Microphone', BOOST_CTL, 'Mic Boost', None),
    ('Microphone', 'DSP Mic Monitor Playback Switch', 'Mic Monitor', None),
    ('Microphone', 'DSP EAX Playback Volume', 'EAX', None),
    ('Microphone', 'DSP Mic Environment Playback Volume', 'Mic FX', None),
]
TABS = ['Effects', 'EQ', 'Microphone']

# The X-Fi look: dark navy glass with glowing cyan edges (drawn by Qt, no images)
STYLE = '''
QWidget#Panel { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #050b1c, stop:1 #0a1630);
    border: 1px solid #1c8fe8; }
QLabel#Title { color: #6fe3ff; font-weight: bold; letter-spacing: 1px; }
QPushButton#Close { border: none; border-radius: 4px; color: #8fb8d8; font-size: 13pt; padding: 0px 8px; background: transparent; }
QPushButton#Close:hover { color: #ffffff; background: #c4161c; }
QWidget { color: #bfe6ff; font-size: 10pt; }
QFrame#Glass { border: 2px solid #1c8fe8; border-radius: 12px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0d2a5c, stop:0.5 #08193b, stop:1 #061230); }
QLabel#PageTitle { color: #6fe3ff; font-size: 12pt; font-weight: bold; letter-spacing: 1px; }
QLabel#Status { color: #8fb8d8; }
QLabel#KnobValue { color: #6fe3ff; font-weight: bold; }
QPushButton[tab="true"] { color: #9fd8ff; font-weight: bold; text-align: left; padding: 10px 14px; min-width: 120px;
    border: 1px solid #2a6fb8; border-radius: 9px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0f2d63, stop:1 #091b40); }
QPushButton[tab="true"]:hover { border-color: #4fb8ff; }
QPushButton[tab="true"]:checked { color: #ffffff; border: 2px solid #6fe3ff;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1d9cf0, stop:1 #0b4f9a); }
QCheckBox, QRadioButton { spacing: 8px; }
QCheckBox::indicator, QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #3fa9f5; background: #061230; }
QCheckBox::indicator { border-radius: 3px; }
QRadioButton::indicator { border-radius: 8px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.6, fx:0.5, fy:0.5, stop:0 #ffffff, stop:0.45 #6fe3ff, stop:1 #1c8fe8); }
QCheckBox:disabled, QLabel:disabled { color: #4d6a85; }
QSlider::groove:horizontal { height: 6px; border-radius: 3px; background: #04102a; border: 1px solid #1c5f9e; }
QSlider::sub-page:horizontal { border-radius: 3px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0b5fb0, stop:1 #4fd4ff); }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; border: 1px solid #9feaff;
    background: qradialgradient(cx:0.4, cy:0.35, radius:0.7, fx:0.4, fy:0.35, stop:0 #ffffff, stop:0.5 #b8c8d8, stop:1 #6a7c90); }
QSlider::handle:horizontal:disabled { border-color: #3a5470; }
QComboBox { border: 1px solid #3fa9f5; border-radius: 6px; padding: 4px 8px; background: #0a2350; color: #d8f1ff; min-width: 160px; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: url(@ARROW@); width: 10px; height: 6px; }
QComboBox QAbstractItemView { background: #0a1f4a; color: #d8f1ff; selection-background-color: #1d8fe0; border: 1px solid #3fa9f5; }
QToolTip { background: #0a1f4a; color: #d8f1ff; border: 1px solid #3fa9f5; }
'''

# The default: the same layout in the desktop theme's own colours (accent = the theme's highlight); native checkboxes
# and sliders. The tray menu's 'Use system colors' unticked = STYLE, the X-Fi look (settings.json "theme": "xfi").
STYLE_SYSTEM = '''
QWidget#Panel { border: 1px solid palette(mid); }
QFrame#Glass { border: 1px solid palette(mid); border-radius: 10px; background: palette(base); }
QLabel#PageTitle { color: palette(highlight); font-size: 12pt; font-weight: bold; letter-spacing: 1px; }
QLabel#Title { font-weight: bold; }
QLabel#KnobValue { color: palette(highlight); font-weight: bold; }
QPushButton[tab="true"] { font-weight: bold; text-align: left; padding: 10px 14px; min-width: 120px;
    border: 1px solid palette(mid); border-radius: 8px; background: palette(button); color: palette(button-text); }
QPushButton[tab="true"]:hover { border-color: palette(highlight); }
QPushButton[tab="true"]:checked { background: palette(highlight); color: palette(highlighted-text); border-color: palette(highlight); }
QPushButton#Close { border: none; border-radius: 4px; font-size: 13pt; padding: 0px 8px; background: transparent; }
QPushButton#Close:hover { color: #ffffff; background: #c4161c; }
'''


class Knob(QDial):
    """a brushed-metal volume knob with a glowing ring that shows the level"""
    def __init__(self, system=False):
        super().__init__()
        self.system = system		# theme colours instead of the X-Fi look
        self.setFixedSize(96, 96)
        self.setNotchesVisible(False)
        self.setWrapping(False)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        c = r.center()
        span = self.maximum() - self.minimum()
        f = (self.value() - self.minimum()) / span if span else 0
        # the ring: dim track, lit part up to the level (270 degrees, from bottom left round to bottom right)
        pal = self.palette()
        track, lit = (pal.mid().color(), pal.highlight().color()) if self.system else (QColor('#0f2a55'), QColor('#4fd4ff'))
        p.setPen(QPen(track, 5, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(r, -45 * 16, 270 * 16)
        p.setPen(QPen(lit if self.isEnabled() else track, 5, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(r, 225 * 16, -int(270 * f) * 16)
        # the metal cap
        k = r.adjusted(9, 9, -9, -9)
        g = QRadialGradient(QPointF(k.left() + k.width() * 0.35, k.top() + k.height() * 0.3), k.width() * 0.8)
        g.setColorAt(0, QColor('#f4f8fc'))
        g.setColorAt(0.55, QColor('#aab7c6'))
        g.setColorAt(1, QColor('#56657a'))
        p.setPen(QPen(QColor('#1c2a3c'), 1.5))
        p.setBrush(g)
        p.drawEllipse(k)
        # the pointer dot
        a = math.radians(225 - 270 * f)
        d = k.width() / 2 - 9
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.highlight().color() if self.system else QColor('#1c8fe8'))
        p.drawEllipse(QPointF(c.x() + d * math.cos(a), c.y() - d * math.sin(a)), 4, 4)
        p.end()


def arrow_file():
    """the dropdown arrow as a small image (style sheets can only show arrows from files)"""
    path = os.path.expanduser('~/.cache/xfi-dsp/arrow.png')
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pm = QPixmap(20, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#6fe3ff'))
        p.drawPolygon(QPolygonF([QPointF(1, 1), QPointF(19, 1), QPointF(10, 11)]))
        p.end()
        pm.save(path)
    return path


def glass():
    f = QFrame()
    f.setObjectName('Glass')
    return f


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_contents(text):
    """`amixer contents` -> {name: {'type', 'value', 'min', 'max', 'items'}}"""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"numid=\d+,iface=\w+,name='(.*)'", line)
        if m:
            cur = {'type': None, 'value': None, 'min': 0, 'max': 0, 'items': []}
            out[m.group(1)] = cur
        elif cur is None:
            continue
        elif line.startswith('  ; type='):
            f = dict(p.split('=', 1) for p in line[4:].split(',') if '=' in p)
            cur['type'] = f.get('type')
            cur['min'], cur['max'] = int(f.get('min', 0)), int(f.get('max', 0))
        elif line.startswith('  ; Item #'):
            cur['items'].append(line.split("'", 1)[1].rsplit("'", 1)[0])
        elif line.startswith('  : values='):
            v = line.split('=', 1)[1].split(',')[0]
            if cur['type'] == 'BOOLEAN':
                cur['value'] = v == 'on'
            elif cur['type'] in ('INTEGER', 'ENUMERATED'):
                cur['value'] = int(v)
    return out


def xfi_sink():
    r = run('pactl', 'list', 'short', 'sinks')
    for line in (r.stdout.splitlines() if r else []):
        f = line.split('\t')
        if len(f) > 1 and f[1].endswith('.xfi-dsp'):
            return f[1]
    return None


WM_STATE = '/sys/kernel/xfi_dsp/winmode'	# the driver's own word: idle, loading, running, failed
STATE_TEXT = {'loading': 'The X-Fi DSP is starting...',
              'failed': 'The X-Fi DSP failed to start (journalctl -b -u xfi-dsp.service)'}


def dsp_state():
    try:
        with open(WM_STATE) as f:
            return (f.read().split() or ['idle'])[0].rstrip(':')
    except OSError:
        return 'not loaded'		# our driver isn't loaded, e.g. another kernel


BASS_LEVEL = 'DSP Bass Boost Level Playback Volume'
MONITOR_CTL = 'DSP Mic Monitor Playback Switch'
# the reverb on the voice sits after Mic Monitor: these do nothing unless it's on, so they only show then
MONITOR_ONLY = ('DSP EAX Playback Volume', 'DSP Mic Environment Playback Volume')


def value_text(name, v, c):
    """what a slider's position means: Bass Boost in dB (3 dB + 0.5 dB per step), Mic Boost in dB, the rest in % of its range"""
    if name == BASS_LEVEL:
        return f'{3 + v / 2:g} dB'
    if name == BOOST_CTL:
        return f'+{v} dB'
    span = c['max'] - c['min']
    return f'{round(100 * (v - c["min"]) / span) if span else 0}%'


def read_controls():
    r = run('amixer', '-c', CARD, 'contents')
    if r is None or r.returncode:
        return {}
    ctls = {k: v for k, v in parse_contents(r.stdout).items() if k.startswith('DSP ')}
    # Master in PipeWire's percent, as the taskbar shows it: below the DSP's -50 dB PipeWire goes on in software
    sink, c = xfi_sink(), ctls.get(MASTER_CTL)
    if sink and c:
        r = run('pactl', 'get-sink-volume', sink)
        m = re.search(r'(\d+)%', r.stdout) if r else None
        if m:
            c.update(min=0, max=100, value=min(int(m.group(1)), 100))
    return ctls


def write_control(name, value):
    sink = xfi_sink() if name == MASTER_CTL else None
    if sink:
        run('pactl', 'set-sink-volume', sink, f'{value}%')
        return
    if isinstance(value, bool):
        value = 'on' if value else 'off'
    run('amixer', '-q', '-c', CARD, 'cset', f'name={name}', str(value))


def nudge_level():
    """What U Hear's input volume is PipeWire's software volume. Back on the mic port PipeWire moves the volume to the
    card but keeps that software volume (turned down by Discord's auto gain or a volume slider, the mic seems dead) until
    the card's level control changes. So change it one step and back."""
    r = run('amixer', '-c', CARD, 'cget', f'name={LEVEL_CTL}')
    m = re.search(r': values=(\d+)', r.stdout) if r else None
    if not m:
        return
    v = int(m.group(1))
    write_control(LEVEL_CTL, v - 1 if v > 0 else v + 1)
    write_control(LEVEL_CTL, v)


def set_source(item, ctl):
    """Through PipeWire when it has the port, so that it knows; straight to the control otherwise."""
    r = run('pactl', 'list', 'short', 'sources')
    for line in (r.stdout.splitlines() if r else []):
        name = line.split('\t')[1] if '\t' in line else ''
        if 'xfi-dsp-mic' in name and '.monitor' not in name:
            r = run('pactl', 'set-source-port', name, PORTS.get(item, ''))
            if r is not None and r.returncode == 0:
                if item == 'Mic':
                    nudge_level()
                return
    write_control(SOURCE_CTL, ctl['items'].index(item))


def load_settings():
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    tmp = CONF + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, CONF)


def restore(ctls, saved):
    """Your settings, in LAYOUT order, where the card differs."""
    order = [n for _, c, _, l in LAYOUT for n in (c, l) if n]
    for name in order:
        if name in saved and name in ctls and ctls[name]['value'] != saved[name]:
            write_control(name, saved[name])


def make_icon(active):
    """Equalizer bars on a dark tile: red while the DSP runs, grey when it does not. Five bars stay readable at tray size."""
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        k = size / 64
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#20242b'))
        p.drawRoundedRect(QRectF(1 * k, 1 * k, 62 * k, 62 * k), 14 * k, 14 * k)
        top, bottom = (QColor('#ff5a4d'), QColor('#c4161c')) if active else (QColor('#a9afb8'), QColor('#6d737c'))
        g = QLinearGradient(0, 10 * k, 0, 54 * k)
        g.setColorAt(0, top)
        g.setColorAt(1, bottom)
        p.setBrush(g)
        for n, h in enumerate((18, 34, 46, 28, 14)):
            x = (9 + n * 10) * k
            p.drawRoundedRect(QRectF(x, (32 - h / 2) * k, 7 * k, h * k), 3.5 * k, 3.5 * k)
        p.end()
        icon.addPixmap(pm)
    return icon


class Panel(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('X-Fi DSP')
        self.setWindowIcon(make_icon(True))
        self.setObjectName('Panel')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.saved = load_settings()
        self.apply_style(self.saved.get('theme') != 'xfi')		# default: the desktop theme's colours
        # no system frame: our own title strip with a close button, dragged to move the window
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.tab = TABS[0]		# the tab shown, kept across rebuilds
        self.ctls = {}
        self.state = "idle"
        self.widgets = {}
        self.layout_key = None
        self.restored = False
        self.pending = {}
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(12, 6, 12, 12)
        self.titlebar = QWidget()
        tb = QHBoxLayout(self.titlebar)
        tb.setContentsMargins(4, 0, 0, 0)
        t = QLabel('X-Fi DSP')
        t.setObjectName('Title')
        tb.addWidget(t)
        tb.addStretch()
        x = QPushButton('\u2715')
        x.setObjectName('Close')
        x.setToolTip('Close (the tray icon stays)')
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.clicked.connect(self.hide)
        tb.addWidget(x)
        self.outer.addWidget(self.titlebar)
        self.status = QLabel()
        self.status.setObjectName('Status')
        self.outer.addWidget(self.status)
        self.body = None
        self.debounce = QTimer(self, singleShot=True, interval=150)
        self.debounce.timeout.connect(self.flush)
        self.poll = QTimer(self, interval=3000)
        self.poll.timeout.connect(self.refresh)
        self.poll.start()
        self.refresh()

    def apply_style(self, system):
        """the X-Fi look, or the same layout in the desktop theme's colours"""
        self.system = system
        self.setStyleSheet(STYLE_SYSTEM if system else STYLE.replace('@ARROW@', arrow_file()))

    def set_theme(self, system):
        self.apply_style(system)
        if system:
            self.saved.pop('theme', None)
        else:
            self.saved['theme'] = 'xfi'
        save_settings(self.saved)
        self.layout_key = None		# rebuild: the knob takes its colours when it is made
        self.refresh()

    def mousePressEvent(self, e):
        # drag by the title strip (the window manager moves it, which also works on Wayland)
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() <= self.titlebar.geometry().bottom():
            self.windowHandle().startSystemMove()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)

    def active(self):
        return self.state == 'running' and bool(self.ctls)

    def refresh(self):
        if self.pending:
            return
        # DSP Master exists as soon as the driver loads: only the driver's state says the DSP is up. Restoring before
        # that would find the effects missing and never try again
        self.state = dsp_state()
        self.ctls = read_controls() if self.state == 'running' else {}
        if self.ctls and not self.restored:
            restore(self.ctls, self.saved)
            self.restored = True
            self.ctls = read_controls()
        elif not self.ctls:
            self.restored = False
        key = tuple(sorted(self.ctls))
        if key != self.layout_key:
            self.layout_key = key
            self.build()
        self.show_values()
        self.status.setText(STATE_TEXT.get(self.state, 'The X-Fi DSP is not running'))
        self.status.setVisible(not self.ctls)

    def build(self):
        if self.body is not None:
            self.outer.removeWidget(self.body)
            self.body.deleteLater()
        self.widgets = {}
        self.values = {}		# slider name -> its value label
        self.rows = {}			# slider name -> the widget to dim while its effect is off
        self.labels = {}		# slider name -> its name label, for rows that can be hidden
        self.body = QWidget()
        v = QVBoxLayout(self.body)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self.outer.addWidget(self.body)
        self.status.setText('The X-Fi DSP is not running')
        if not self.ctls:
            self.body.hide()
            self.adjustSize()
            return
        # top: tab buttons on the left, the tab's controls on the right
        top = glass()
        th = QHBoxLayout(top)
        th.setContentsMargins(14, 14, 14, 14)
        th.setSpacing(14)
        tabs = QVBoxLayout()
        tabs.setSpacing(8)
        self.stack = QStackedWidget()
        self.stack.setMinimumSize(440, 230)
        group = QButtonGroup(self.body)
        shown = None
        for tab in TABS:
            items = [(n, l, lv) for t, n, l, lv in LAYOUT if t == tab and n in self.ctls]
            if tab == 'Microphone' and SOURCE_CTL in self.ctls:
                items.append((SOURCE_CTL, None, None))
            if not items:
                continue
            page = QWidget()
            g = QGridLayout(page)
            g.setContentsMargins(4, 0, 4, 0)
            g.setVerticalSpacing(12)
            g.setColumnStretch(1, 1)
            g.setColumnMinimumWidth(1, 220)
            t = QLabel(tab.upper())
            t.setObjectName('PageTitle')
            g.addWidget(t, 0, 0, 1, 2)
            row = 1
            for name, label, level in items:
                row = self.add_control(g, row, name, label, level)
            g.setRowStretch(row, 1)
            idx = self.stack.addWidget(page)
            b = QPushButton(tab)
            b.setCheckable(True)
            b.setProperty('tab', True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, i=idx, n=tab: self.pick(i, n))
            group.addButton(b)
            tabs.addWidget(b)
            if tab == self.tab or shown is None:
                shown = (idx, b)
        tabs.addStretch()
        th.addLayout(tabs)
        th.addWidget(self.stack, 1)
        v.addWidget(top)
        if shown:
            self.stack.setCurrentIndex(shown[0])
            shown[1].setChecked(True)
        # bottom: what is running, and the master knob
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        info = glass()
        ih = QVBoxLayout(info)
        ih.setContentsMargins(14, 10, 14, 10)
        t = QLabel('STATUS')
        t.setObjectName('PageTitle')
        ih.addWidget(t)
        try:
            with open('/etc/xfi-dsp-mode') as f:
                mode = f.read().strip()
        except OSError:
            mode = ''
        ih.addWidget(QLabel('DSP running' + (f' \u00b7 mode {mode}' if mode else '')))
        ih.addStretch()
        og = QGridLayout()
        og.setColumnStretch(1, 1)
        row = 0
        for t, n, l, lv in LAYOUT:
            if t == 'Output' and n in self.ctls:
                row = self.add_control(og, row, n, l, lv)
        ih.addLayout(og)
        bottom.addWidget(info, 1)
        c = self.ctls.get(MASTER_CTL)
        if c:
            knob = glass()
            kv = QVBoxLayout(knob)
            kv.setContentsMargins(14, 8, 14, 8)
            kv.setSpacing(2)
            kv.addWidget(QLabel('Master Volume'), 0, Qt.AlignmentFlag.AlignHCenter)
            d = Knob(self.system)
            d.setRange(c['min'], c['max'])
            lab = QLabel()
            lab.setObjectName('KnobValue')
            d.valueChanged.connect(lambda val, n=MASTER_CTL, cc=c, l=lab: (l.setText(value_text(n, val, cc)), self.change(n, val)))
            kv.addWidget(d, 0, Qt.AlignmentFlag.AlignHCenter)
            kv.addWidget(lab, 0, Qt.AlignmentFlag.AlignHCenter)
            self.widgets[MASTER_CTL] = d
            self.values[MASTER_CTL] = lab
            bottom.addWidget(knob)
        v.addLayout(bottom)
        self.adjustSize()

    def pick(self, idx, name):
        self.stack.setCurrentIndex(idx)
        self.tab = name

    def add_control(self, g, row, name, label, level):
        """one control (and the level slider that belongs to it) on a tab page"""
        if name == SOURCE_CTL:
            g.addWidget(QLabel('Record from'), row, 0)
            box = QHBoxLayout()
            box.setSpacing(18)
            self.widgets[SOURCE_CTL] = []
            for item in self.ctls[SOURCE_CTL]['items']:
                b = QRadioButton(item)
                b.clicked.connect(lambda _, i=item: self.source(i))
                box.addWidget(b)
                self.widgets[SOURCE_CTL].append(b)
            box.addStretch()
            g.addLayout(box, row, 1)
            return row + 1
        c = self.ctls[name]
        if c['type'] == 'BOOLEAN':
            w = QCheckBox(label)
            w.toggled.connect(lambda val, n=name: self.change(n, val))
            self.widgets[name] = w
            g.addWidget(w, row, 0, 1, 2)
            row += 1
            if level in self.ctls:
                self.labels[level] = QLabel('Level')
                g.addWidget(self.labels[level], row, 0)
                g.addWidget(self.slider(level, self.ctls[level]), row, 1)
                row += 1
            return row
        if c['type'] == 'ENUMERATED':
            g.addWidget(QLabel(label), row, 0)
            w = QComboBox()
            w.addItems(c['items'])
            w.currentIndexChanged.connect(lambda val, n=name: self.change(n, val))
            g.addWidget(w, row, 1, Qt.AlignmentFlag.AlignLeft)
            self.widgets[name] = w
            row += 1
            if level in self.ctls:
                self.labels[level] = QLabel('Level')
                g.addWidget(self.labels[level], row, 0)
                g.addWidget(self.slider(level, self.ctls[level]), row, 1)
                row += 1
            return row
        self.labels[name] = QLabel(label)
        g.addWidget(self.labels[name], row, 0)
        g.addWidget(self.slider(name, c), row, 1)
        return row + 1

    def slider(self, name, c):
        """a slider with its value next to it"""
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(c['min'], c['max'])
        lab = QLabel()
        lab.setMinimumWidth(lab.fontMetrics().horizontalAdvance('12.5 dB') + 4)
        lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        s.valueChanged.connect(lambda v, n=name, cc=c, l=lab: (l.setText(value_text(n, v, cc)), self.change(n, v)))
        h.addWidget(s, 1)
        h.addWidget(lab)
        self.widgets[name] = s
        self.values[name] = lab
        self.rows[name] = box
        return box

    def show_values(self):
        for name, w in self.widgets.items():
            c = self.ctls.get(name)
            if c is None or c['value'] is None:
                continue
            if name == SOURCE_CTL:
                for i, b in enumerate(w):
                    b.setChecked(i == c['value'])
                continue
            w.blockSignals(True)
            if isinstance(w, QCheckBox):
                w.setChecked(c['value'])
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(c['value'])
            elif not w.isSliderDown():
                w.setValue(c['value'])
            w.blockSignals(False)
            if name in self.values:
                self.values[name].setText(value_text(name, w.value(), c))
        mon = self.ctls.get(MONITOR_CTL)
        show = bool(mon and mon['value'])
        changed = False
        for name in MONITOR_ONLY:
            for w in (self.labels.get(name), self.rows.get(name)):
                if w is not None and w.isVisibleTo(self) != show:
                    w.setVisible(show)
                    changed = True
        if changed:
            QTimer.singleShot(0, self.adjustSize)
        # a level whose effect is off still works (it applies when the effect comes on), but shows dimmed
        for _, sw, _, level in LAYOUT:
            row, c = self.rows.get(level), self.ctls.get(sw)
            if row is None or c is None or c['value'] is None:
                continue
            on = c['items'][c['value']] != 'Off' if c['type'] == 'ENUMERATED' else bool(c['value'])
            fx = row.graphicsEffect()
            if fx is None:
                fx = QGraphicsOpacityEffect(row)
                row.setGraphicsEffect(fx)
            fx.setOpacity(1.0 if on else 0.4)
        src, lvl = self.ctls.get(SOURCE_CTL), self.widgets.get(LEVEL_CTL)
        if src and lvl:		# What U Hear has its own level: the taskbar slider, in software
            mic = src['items'][src['value']] == 'Mic'
            lvl.setEnabled(mic)
            lvl.setToolTip('' if mic else 'What U Hear: use the taskbar input volume')

    def change(self, name, value):
        self.pending[name] = value
        self.debounce.start()

    def flush(self):
        order = [n for _, c, _, l in LAYOUT for n in (c, l) if n]
        for name in sorted(self.pending, key=lambda n: order.index(n) if n in order else 99):
            write_control(name, self.pending[name])
            if name not in NOT_SAVED:
                self.saved[name] = self.pending[name]
        self.pending = {}
        save_settings(self.saved)
        self.refresh()

    def source(self, item):
        set_source(item, self.ctls[SOURCE_CTL])
        QTimer.singleShot(400, self.refresh)


class Tray:
    def __init__(self, app):
        self.app = app
        self.panel = Panel()
        self.tray = QSystemTrayIcon(make_icon(self.panel.active()))
        self.tray.setToolTip('X-Fi DSP')
        self.menu = QMenu()
        a = QAction('Open', self.menu)
        a.triggered.connect(self.show)
        self.menu.addAction(a)
        a = QAction('Use system colors', self.menu, checkable=True)
        a.setChecked(self.panel.system)
        a.toggled.connect(self.panel.set_theme)
        self.menu.addAction(a)
        a = QAction('Quit', self.menu)
        a.triggered.connect(app.quit)
        self.menu.addAction(a)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.clicked)
        self.tray.show()
        self.last = None
        self.timer = QTimer(interval=3000)
        self.timer.timeout.connect(self.update_icon)
        self.timer.start()
        self.update_icon()

    def update_icon(self):
        a = self.panel.active()
        if (a, self.panel.state) != self.last:
            self.last = (a, self.panel.state)
            self.tray.setIcon(make_icon(a))
            self.tray.setToolTip('X-Fi DSP' if a else 'X-Fi DSP: starting' if self.panel.state == 'loading'
                                 else 'X-Fi DSP: not running')

    def clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.panel.isVisible():
                self.panel.hide()
            else:
                self.show()

    def show(self):
        self.panel.refresh()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('xfi-tray')
    app.setQuitOnLastWindowClosed(False)
    tray = Tray(app)		# keep the reference: without it the icon is gone at once
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
