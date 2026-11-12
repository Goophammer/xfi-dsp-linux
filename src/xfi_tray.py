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

import cmath
import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDial, QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLayout,
                             QMenu, QPushButton, QRadioButton, QSlider, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

CARD = 'XFi'
CONF = os.path.expanduser('~/.config/xfi-dsp/settings.json')
SOURCE_CTL = 'DSP Recording Source'
LEVEL_CTL = 'DSP Mic Capture Volume'	# the mic's level, also PipeWire's input volume
BOOST_CTL = 'DSP Mic Boost Capture Volume'	# the input chip's gain, in dB
MASTER_CTL = 'DSP Master Playback Volume'	# the taskbar volume
UPMIX_CTL = 'Stereo Upmix'			# not a card control: xfi_upmix.sh (stereo -> 7.1 in PipeWire, gamehp only); shown as
UPMIX_AMOUNT = 'Stereo Upmix Amount'		# "Stereo Envelopment" (the names stay: they're the saved settings' keys)
SHOW_UPMIX = True		# False hides the switch (nothing restored then); xfi_upmix.sh works either way
EQ_CTL = 'DSP EQ Preset'
BANK_CTL = 'DSP EQ Custom Bank'			# hidden (iface CARD): the EQ bank computed here, for Bass/Treble
TONE_ENTRIES = ('Bass -', 'Bass +', 'Treble -', 'Treble +', 'Bass + Treble +')	# old fixed entries (in firmwares of
# 2026-11-09 only), replaced by the Bass/Treble sliders: a saved one is converted
EQ_SETTINGS = 'EQ'				# settings.json: the EQ page (custom bands, Bass, Treble, level)
NOT_SAVED = (LEVEL_CTL, MASTER_CTL)		# PipeWire remembers these
PORTS = {'Mic': 'xfi-dsp-mic', 'What U Hear': 'xfi-dsp-wuh'}
# (tab, control, label, slider that belongs to it). CMSS-3D first: switching it resets EAX and Mic Environment.
# Master is the knob below the tabs, Output sits next to it.
LAYOUT = [
    ('Master', MASTER_CTL, 'Master Volume', None),
    ('Output', 'DSP Auto Mute Speakers Playback Switch', 'Mute speakers when headphones are plugged in', None),
    ('Effects', 'DSP CMSS-3D Playback Switch', 'CMSS-3D', 'DSP CMSS-3D Level Playback Volume'),
    ('Effects', UPMIX_CTL, 'Stereo Envelopment', UPMIX_AMOUNT),
    ('Effects', 'DSP Crystalizer Playback Switch', 'Crystalizer', 'DSP Crystalizer Level Playback Volume'),
    ('Effects', 'DSP SVM Playback Switch', 'SVM', None),
    ('Effects', 'DSP Bass Boost Playback Switch', 'Bass Boost', ('DSP Bass Boost Level Playback Volume',
                                                                 'DSP Bass Boost Cutoff Playback Volume')),
    ('EQ', EQ_CTL, 'Preset', 'DSP EQ Level Playback Volume'),
    ('Microphone', LEVEL_CTL, 'Mic Level', None),
    ('Microphone', BOOST_CTL, 'Mic Boost', None),
    ('Microphone', 'DSP Mic Monitor Playback Switch', 'Mic Monitor', None),
    ('Microphone', 'DSP EAX Playback Volume', 'EAX', None),
    ('Microphone', 'DSP Mic Environment Playback Volume', 'Mic FX', None),
]
TABS = ['Effects', 'EQ', 'Microphone']
BESIDE = {UPMIX_CTL: 'DSP CMSS-3D Playback Switch'}	# switches shown on the same row as another, to its right
SUB_LABEL = {'DSP Bass Boost Cutoff Playback Volume': 'Cutoff', UPMIX_AMOUNT: 'Envelopment'}	# a control's sliders: 'Level' unless named here


def subs(level):
    """the sliders that belong to a LAYOUT control: none, one, or a tuple"""
    return () if not level else (level,) if isinstance(level, str) else level

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
QSlider::groove:vertical { width: 6px; border-radius: 3px; background: #04102a; border: 1px solid #1c5f9e; }
QSlider::add-page:vertical { border-radius: 3px; background: qlineargradient(x1:0, y1:1, x2:0, y2:0, stop:0 #0b5fb0, stop:1 #4fd4ff); }
QSlider::handle:vertical { width: 16px; height: 16px; margin: 0 -6px; border-radius: 8px; border: 1px solid #9feaff;
    background: qradialgradient(cx:0.4, cy:0.35, radius:0.7, fx:0.4, fy:0.35, stop:0 #ffffff, stop:0.5 #b8c8d8, stop:1 #6a7c90); }
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


# ---- EQ: Bass/Treble on top of a preset, computed in Creative's bank format (analysis/eq/eqmath.py, presets.py) ----
# 10 bands (31 Hz low shelf, 62 Hz..8 kHz peaks, 16 kHz high shelf), each (a2, a1, b1, b2) normalized, 8.24 fixed point,
# fs 48 kHz, all zero = band off, then the make-up level; gamehp adds its per-preset gain. Creative's own design, fitted to
# its recorded banks: proportional-Q peaks, shelves at 72 Hz / 8.8 kHz. Presets rebuilt this way are within 0.3 dB.
EQ_FS = 48000
EQ_CENTERS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_Q0 = [None, 0.509, 0.518, 0.538, 0.495, 0.495, 0.480, 0.421, 0.433, None]
EQ_QG = [None, 0.0175, 0.0097, 0.0285, 0.0317, 0.0154, 0.0310, 0.0646, 0.0, None]
EQ_PRESET_GAINS = {		# band gains (dB) decoded from Creative's banks
    'Off': [0.0] * 10, 'Flat': [0.0] * 10,
    'Acoustic': [0.0, 0.38, 2.08, -1.32, 0.45, -0.13, -1.12, 2.05, 0.44, 1.46],
    'Classical': [0.0, 4.48, 2.9, 1.12, -1.38, 0.56, -0.15, -1.62, 3.07, 1.77],
    'Country': [-1.19, 0.15, 0.81, 0.43, 0.96, -0.65, -0.91, 1.45, 1.26, 2.99],
    'Dance': [-1.21, 1.66, 0.64, 4.74, -3.52, -0.13, 0.48, -2.28, 3.99, 2.27],
    'Jazz': [0.0, -0.57, -0.47, 2.89, 1.14, 3.52, -3.0, 0.65, 2.06, 1.98],
    'New Age': [0.0, 1.57, 1.38, -1.12, 0.37, -0.7, 0.55, 1.16, 0.67, 1.42],
    'Pop': [-1.6, -0.01, 1.36, 1.52, -0.47, -0.74, -0.88, -0.71, 1.83, 4.71],
    'Rock': [-1.21, -1.38, 1.2, 2.59, -2.37, -0.4, 0.57, -2.3, 3.99, 2.27],
    'Vocal': [-2.0, 0.73, -1.46, -0.83, 2.17, 2.11, 2.15, -1.68, 0.13, 1.0],
}
EQ_BASS_MAX = [12.0, 0.94, 8.14, 1.97, -2.45, 0.71, -0.21, 0.04, -0.01, 0.0]	# Windows' Bass slider at the top
EQ_TREBLE_MAX = [0.0, -0.03, -0.05, 0.01, 0.21, 0.18, -3.02, 2.16, 8.13, 7.58]	# Treble at the top
HP71_GAIN_2E4 = {'Acoustic': 0x14248f0, 'Classical': 0x16dcbff, 'Country': 0x195bb8f, 'Dance': 0x195bb8f, 'Jazz': 0x16dcbff,
                 'New Age': 0x14248f0, 'Pop': 0x1fec983, 'Rock': 0x195bb8f, 'Vocal': 0x1229012}


def eq_band(k, g):
    """(b, a) of band k at g dB"""
    A = 10 ** (g / 40)
    w = 2 * math.pi * EQ_CENTERS[k] / EQ_FS
    if 0 < k < 9:
        q = EQ_Q0[k] + EQ_QG[k] * abs(g)
        al, c = math.sin(w) / (2 * q), math.cos(w)
        return (1 + al * A, -2 * c, 1 - al * A), (1 + al / A, -2 * c, 1 - al / A)
    f0, sl = (72, 0.9) if k == 0 else (8800, 0.8)
    w = 2 * math.pi * f0 / EQ_FS
    c = math.cos(w)
    al = math.sin(w) / 2 * math.sqrt((A + 1 / A) * (1 / sl - 1) + 2)
    sq = 2 * math.sqrt(A) * al
    if k == 0:
        return ((A * ((A + 1) - (A - 1) * c + sq), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sq)),
                ((A + 1) + (A - 1) * c + sq, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sq))
    return ((A * ((A + 1) + (A - 1) * c + sq), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sq)),
            ((A + 1) - (A - 1) * c + sq, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sq))


def eq_bank(base, bass, treble, count, level_db=0.0, gain_2e4=0x1000000):
    """the values for BANK_CTL: 10 band gains + Bass + Treble (dB) -> 40 coefficients, the level (with the EQ Level, in
    dB), and for gamehp its per-preset gain"""
    fx = lambda x: int(round(x * 2 ** 24))
    vals, level = [], 10 ** (level_db / 20)
    for k in range(10):
        g = base[k] + bass / 12 * EQ_BASS_MAX[k] + treble / 12 * EQ_TREBLE_MAX[k]
        if abs(g) < 0.05:
            vals += [0, 0, 0, 0]
            continue
        b, a = eq_band(k, g)
        level *= b[0] / a[0]
        vals += [fx(a[2] / a[0]), fx(a[1] / a[0]), fx(b[1] / b[0]), fx(b[2] / b[0])]
    vals.append(fx(level))
    if count > 41:
        vals.append(gain_2e4)
    return vals


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
            cur = {'type': None, 'value': None, 'min': 0, 'max': 0, 'items': [], 'count': 1}
            out[m.group(1)] = cur
        elif cur is None:
            continue
        elif line.startswith('  ; type='):
            f = dict(p.split('=', 1) for p in line[4:].split(',') if '=' in p)
            cur['type'] = f.get('type')
            cur['min'], cur['max'] = int(f.get('min', 0)), int(f.get('max', 0))
            cur['count'] = int(f.get('values', 1))
        elif line.startswith('  ; Item #'):
            cur['items'].append(line.split("'", 1)[1].rsplit("'", 1)[0])
        elif line.startswith('  : values='):
            v = line.split('=', 1)[1].split(',')[0]
            if cur['type'] == 'BOOLEAN':
                cur['value'] = v == 'on'
            elif cur['type'] in ('INTEGER', 'ENUMERATED'):
                cur['value'] = int(v)
    return out


def xfi_sink(channels=False):
    """the X-Fi DSP output's name (and with channels=True: its channel count)"""
    r = run('pactl', 'list', 'short', 'sinks')
    for line in (r.stdout.splitlines() if r else []):
        f = line.split('\t')
        if len(f) > 1 and f[1].endswith('.xfi-dsp'):
            if channels:
                m = re.search(r'(\d+)ch', line)
                return f[1], int(m.group(1)) if m else 2
            return f[1]
    return (None, 0) if channels else None


def upmix_script():
    """Stereo Upmix: next to this file (the package's src/), or installed"""
    for p in (os.path.join(os.path.dirname(os.path.realpath(__file__)), 'xfi_upmix.sh'), '/usr/local/bin/xfi-upmix'):
        if os.access(p, os.X_OK):
            return p
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
BASS_CUTOFF = 'DSP Bass Boost Cutoff Playback Volume'
MONITOR_CTL = 'DSP Mic Monitor Playback Switch'
# the reverb on the voice sits after Mic Monitor: these do nothing unless it's on, so they only show then
MONITOR_ONLY = ('DSP EAX Playback Volume', 'DSP Mic Environment Playback Volume')


def value_text(name, v, c):
    """what a slider's position means: Bass Boost in dB (3 dB + 0.5 dB per step), Mic Boost in dB, the rest in % of its range"""
    if name == BASS_LEVEL:
        return f'{3 + v / 2:g} dB'
    if name == BASS_CUTOFF:
        return f'{v} Hz'
    if name == UPMIX_AMOUNT:
        return f'{v}%'
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
    (sink, ch), c = xfi_sink(channels=True), ctls.get(MASTER_CTL)
    # Stereo Upmix: only when the X-Fi takes 8 channels in (gamehp), where the card's CMSS-3D places the extra channels
    up = upmix_script() if SHOW_UPMIX else None
    if up and sink and ch == 8:
        r = run(up, 'status')
        ctls[UPMIX_CTL] = {'type': 'BOOLEAN', 'value': bool(r and r.stdout.strip() == 'on'), 'min': 0, 'max': 0, 'items': []}
        r = run(up, 'amount')
        a = r.stdout.strip() if r and not r.returncode else ''
        ctls[UPMIX_AMOUNT] = {'type': 'INTEGER', 'value': int(a) if a.isdigit() else 100, 'min': 0, 'max': 200, 'items': []}
    if sink and c:
        r = run('pactl', 'get-sink-volume', sink)
        m = re.search(r'(\d+)%', r.stdout) if r else None
        if m:
            c.update(min=0, max=100, value=min(int(m.group(1)), 100))
    return ctls


def eq_response(vals, freqs):
    """dB at each frequency of a bank from eq_bank() - the same numbers the card gets"""
    sx = lambda v: v / 2 ** 24
    out = []
    for f in freqs:
        z = cmath.exp(-2j * math.pi * f / EQ_FS)
        h = sx(vals[40])
        for k in range(10):
            a2, a1, b1, b2 = (sx(x) for x in vals[4 * k:4 * k + 4])
            if a2 or a1 or b1 or b2:
                h *= (1 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
        out.append(20 * math.log10(max(abs(h), 1e-6)))
    return out


def eq_solve(targets):
    """filter gains whose overlapping bands add up to these targets (dB) at the band centres: the knobs show where the
    curve goes. Newton steps: how each band moves the curve at every centre (a 10x10 table), then all bands corrected at
    once. A shape too sharp for the wide bands ends as close as it gets (the curve shows it)"""
    resp = lambda g: eq_response(eq_bank(g, 0, 0, 41), EQ_CENTERS)
    g = list(targets)
    for _ in range(8):
        r = resp(g)
        err = [t - x for t, x in zip(targets, r)]
        if max(abs(e) for e in err) < 0.02:
            break
        J = []
        for k in range(10):		# column k: the curve's change per +0.5 dB on band k
            gk = list(g)
            gk[k] += 0.5
            J.append([(a - b) / 0.5 for a, b in zip(resp(gk), r)])
        A = [[J[k][j] for k in range(10)] + [err[j]] for j in range(10)]
        for c in range(10):		# Gaussian elimination with partial pivoting
            piv = max(range(c, 10), key=lambda rr: abs(A[rr][c]))
            A[c], A[piv] = A[piv], A[c]
            if abs(A[c][c]) < 1e-9:
                break
            for rr in range(10):
                if rr != c:
                    f = A[rr][c] / A[c][c]
                    A[rr] = [a - f * b for a, b in zip(A[rr], A[c])]
        step = [A[k][10] / A[k][k] if abs(A[k][k]) > 1e-9 else 0.0 for k in range(10)]
        g = [max(-24.0, min(24.0, gi + d)) for gi, d in zip(g, step)]
    return g


def eq_targets(gains):
    """what the curve of these filter gains is at the band centres, in the knobs' half-dB steps"""
    return [max(-24, min(24, round(2 * x))) for x in eq_response(eq_bank(gains, 0, 0, 41), EQ_CENTERS)]


def eq_state(saved):
    """the EQ page's settings: custom (bands in use instead of the preset), bands and Bass/Treble in half dB, level 0..100"""
    e = saved.get(EQ_SETTINGS) or {}
    return {'custom': bool(e.get('custom')), 'bands': [int(x) for x in (e.get('bands') or [0] * 10)][:10],
            'bass': int(e.get('bass', 0)), 'treble': int(e.get('treble', 0)), 'level': int(e.get('level', 100))}


def eq_level_db(level):
    return -24 + 0.24 * level		# Windows' EQ level: 0 to -24 dB below the preset's own level, even in dB


def apply_eq(e):
    """the card's EQ from the page's state: a plain preset at full level without Bass/Treble = Creative's recorded bank
    (choosing the preset again makes the driver write it); anything else = a computed bank"""
    r = run('amixer', '-c', CARD, 'contents')
    ctls = parse_contents(r.stdout) if r and not r.returncode else {}
    eq, bank = ctls.get(EQ_CTL), ctls.get(BANK_CTL)
    if not eq or not bank or eq['value'] is None:
        return
    preset = eq['items'][eq['value']]
    if not e['custom'] and not e['bass'] and not e['treble'] and e['level'] >= 100:
        run('amixer', '-q', '-c', CARD, 'cset', f'name={EQ_CTL}', str(eq['value']))
        return
    base = eq_solve([b / 2 for b in e['bands']]) if e['custom'] else EQ_PRESET_GAINS.get(preset, [0.0] * 10)
    g2e4 = 0x1000000 if e['custom'] else HP71_GAIN_2E4.get(preset, 0x1000000)
    vals = eq_bank(base, e['bass'] / 2, e['treble'] / 2, bank['count'], eq_level_db(e['level']), g2e4)
    run('amixer', '-q', '-c', CARD, 'cset', f'iface=CARD,name={BANK_CTL}', ','.join(str(v) for v in vals))


def write_control(name, value):
    if name in (UPMIX_CTL, UPMIX_AMOUNT):
        up = upmix_script()
        if up:
            run(up, *(('on' if value else 'off',) if name == UPMIX_CTL else ('amount', str(value))))
        return
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
    order = [n for _, c, _, l in LAYOUT for n in (c, *subs(l))]
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


class EqBands(QWidget):
    """the 10 band sliders on a panel that draws the card's whole EQ curve behind them (preset or Custom + Bass + Treble
    + level). Each band's centre frequency sits on its slider, and the curve uses the sliders' scale: middle = 0 dB, the
    ends of the travel = +-12 dB, so a plain preset/Custom curve runs through the knobs. Beyond +-12 dB it bends softly
    into the space above and below (the controls reach +38 / -62 dB): nothing is cut off, nothing moves."""
    WARN = 12

    def __init__(self, system):
        super().__init__()
        self.system = system
        self.db = None
        self.sliders = []

    def set_bank(self, vals):
        self.vals = vals
        self.update()

    def geometry_map(self):
        """slider columns (x) and the 0 dB / +-12 dB heights (y), measured from the sliders themselves"""
        from PyQt6.QtWidgets import QStyle, QStyleOptionSlider
        xs, top, bot = [], None, None
        for sl in self.sliders:
            opt = QStyleOptionSlider()
            sl.initStyleOption(opt)
            g = sl.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, sl)
            h = sl.style().subControlRect(QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, sl)
            p0 = sl.mapTo(self, g.center())
            xs.append(p0.x())
            t = sl.mapTo(self, g.topLeft()).y() + h.height() / 2
            b = sl.mapTo(self, g.bottomLeft()).y() - h.height() / 2
            top, bot = (t, b) if top is None else (min(top, t), max(bot, b))
        return xs, top, bot

    def paintEvent(self, _):
        if len(self.sliders) != 10 or not hasattr(self, 'vals'):
            return
        xs, top, bot = self.geometry_map()
        mid, half = (top + bot) / 2, (bot - top) / 2		# 0 dB, 12 dB in pixels
        room_up, room_dn = 9, 9		# beyond +-12 dB: a slim band past the slider ends, the labels stay clear
        def y(d):
            if abs(d) <= 12:
                return mid - d / 12 * half
            room = room_up if d > 0 else room_dn		# softly into the space beyond the travel
            return mid - math.copysign(half + room * math.tanh((abs(d) - 12) / 12), d)
        lg = [math.log2(f) for f in EQ_CENTERS]
        def x(f):
            l = math.log2(f)
            k = 0 if l <= lg[1] else 8 if l >= lg[8] else max(i for i in range(9) if lg[i] <= l)
            return xs[k] + (xs[k + 1] - xs[k]) * (l - lg[k]) / (lg[k + 1] - lg[k])
        freqs = [2 ** (lg[0] - 0.5 + (lg[9] - lg[0] + 1) * i / 159) for i in range(160)]
        db = eq_response(self.vals, freqs)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        line = pal.highlight().color() if self.system else QColor('#4fd4ff')
        grid = pal.mid().color() if self.system else QColor('#1c5f9e')
        faint = QColor(grid)
        faint.setAlpha(110)
        p.setPen(QPen(faint, 1, Qt.PenStyle.DashLine))
        for d in (12, -12):
            p.drawLine(QPointF(xs[0] - 14, y(d)), QPointF(xs[9] + 14, y(d)))
        p.setPen(QPen(grid, 1))
        p.drawLine(QPointF(xs[0] - 14, mid), QPointF(xs[9] + 14, mid))
        path = QPainterPath()
        for i, (f, d) in enumerate(zip(freqs, db)):
            pt = QPointF(x(f), y(d))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        fill = QPainterPath(path)
        fill.lineTo(QPointF(x(freqs[-1]), mid))
        fill.lineTo(QPointF(x(freqs[0]), mid))
        fill.closeSubpath()
        glow = QColor(line)
        glow.setAlpha(45)
        p.fillPath(fill, glow)
        soft = QColor(line)
        soft.setAlpha(200)
        p.setPen(QPen(soft, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()
        self.peak = max(db)


class EqPage(QWidget):
    """The EQ tab with a computed EQ (gamehp, ent): preset or Custom, 10 band sliders, Bass, Treble, EQ Level. The band
    sliders are TARGETS: where the curve goes at each band (a preset shows its real curve there; Custom is solved by
    eq_solve so the overlapping bands add up to them)"""
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.e = eq_state(panel.saved)
        self.items = [i for i in panel.ctls[EQ_CTL]['items'] if i not in TONE_ENTRIES]
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 0, 4, 0)
        v.setSpacing(8)
        t = QLabel('EQ')
        t.setObjectName('PageTitle')
        v.addWidget(t)
        top = QHBoxLayout()
        top.addWidget(QLabel('Preset'))
        self.combo = QComboBox()
        self.combo.addItems(self.items + ['Custom'])
        self.combo.currentIndexChanged.connect(self.preset_chosen)
        top.addWidget(self.combo)
        top.addStretch()
        self.warn = QLabel()
        self.warn.setStyleSheet('color: #ff9f43;')
        top.addWidget(self.warn)
        v.addLayout(top)
        self.curve = EqBands(panel.system)
        grid = QGridLayout(self.curve)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        center = Qt.AlignmentFlag.AlignHCenter
        self.bands, self.band_vals = [], []
        for k, f in enumerate(EQ_CENTERS):
            val = QLabel()
            val.setMinimumWidth(val.fontMetrics().horizontalAdvance('+12.5') + 2)
            val.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sl = QSlider(Qt.Orientation.Vertical)
            sl.setRange(-24, 24)
            sl.setPageStep(2)
            sl.setMinimumHeight(150)
            sl.valueChanged.connect(lambda x, k=k: self.band_moved(k, x))
            name = QLabel(f'{f // 1000}k' if f >= 1000 else str(f))
            grid.addWidget(val, 0, k, center)
            grid.addWidget(sl, 1, k, center)
            grid.addWidget(name, 2, k, center)
            self.bands.append(sl)
            self.band_vals.append(val)
        grid.setContentsMargins(0, 0, 0, 0)
        self.curve.sliders = self.bands
        v.addWidget(self.curve)
        rows = QGridLayout()
        rows.setColumnStretch(1, 1)
        self.sliders = {}
        for r, (key, label, lo, hi) in enumerate((('bass', 'Bass', -24, 24), ('treble', 'Treble', -24, 24),
                                                  ('level', 'EQ Level', 0, 100))):
            rows.addWidget(QLabel(label), r, 0)
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(lo, hi)
            lab = QLabel()
            lab.setMinimumWidth(lab.fontMetrics().horizontalAdvance('-24.0 dB') + 4)
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            sl.valueChanged.connect(lambda x, key=key: self.slider_moved(key, x))
            rows.addWidget(sl, r, 1)
            rows.addWidget(lab, r, 2)
            self.sliders[key] = (sl, lab)
        v.addLayout(rows)
        v.addStretch()
        self.timer = QTimer(self, singleShot=True, interval=150)
        self.timer.timeout.connect(self.push)
        self.show_state()
        self.setMinimumHeight(self.sizeHint().height())	# the window grows to fit it, nothing gets squeezed

    def draw(self):
        """the curve from the same bank apply_eq() sends (a plain preset: its decoded bands, within 0.3 dB)"""
        e, label = self.e, self.preset_label()
        base = eq_solve([b / 2 for b in e['bands']]) if e['custom'] else EQ_PRESET_GAINS.get(label, [0.0] * 10)
        vals = eq_bank(base, e['bass'] / 2, e['treble'] / 2, 41, eq_level_db(e['level']))
        self.curve.set_bank(vals)
        peak = max(eq_response(vals, [20 * 1000 ** (i / 59) for i in range(60)]))
        self.warn.setText(f'+{peak:.0f} dB peak: may distort' if peak > EqBands.WARN else '')

    def preset_label(self):
        eq = self.panel.ctls.get(EQ_CTL)
        if not eq or eq['value'] is None:
            return 'Off'
        label = eq['items'][eq['value']]
        return 'Off' if label in TONE_ENTRIES else label

    def show_state(self):
        e = self.e
        for w in [self.combo] + self.bands + [s for s, _ in self.sliders.values()]:
            w.blockSignals(True)
        label = self.preset_label()
        self.combo.setCurrentIndex(len(self.items) if e['custom'] else
                                   self.items.index(label) if label in self.items else 0)
        vals = e['bands'] if e['custom'] else eq_targets(EQ_PRESET_GAINS.get(label, [0.0] * 10))
        for sl, lab, x in zip(self.bands, self.band_vals, vals):
            sl.setValue(x)
            lab.setText(f'{x / 2:+g}' if x else '0')
        for key, (sl, lab) in self.sliders.items():
            sl.setValue(e[key])
            lab.setText(self.text(key, e[key]))
        for w in [self.combo] + self.bands + [s for s, _ in self.sliders.values()]:
            w.blockSignals(False)
        self.draw()

    @staticmethod
    def text(key, x):
        if key == 'level':
            return '0 dB' if x >= 100 else f'{eq_level_db(x):.1f} dB'
        return f'{x / 2:+g} dB' if x else '0 dB'

    def preset_chosen(self, i):
        if i == len(self.items):			# Custom: start from what the sliders show
            self.e['custom'] = True
            self.e['bands'] = [sl.value() for sl in self.bands]
        else:
            self.e['custom'] = False
            idx = self.panel.ctls[EQ_CTL]['items'].index(self.items[i])
            write_control(EQ_CTL, idx)		# the driver writes Creative's recorded preset
            self.panel.saved[EQ_CTL] = idx
            self.panel.ctls[EQ_CTL]['value'] = idx
        self.show_state()
        self.push()

    def band_moved(self, k, x):
        if not self.e['custom']:
            self.e['custom'] = True
            self.e['bands'] = [sl.value() for sl in self.bands]
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(len(self.items))
            self.combo.blockSignals(False)
        self.e['bands'][k] = x
        self.band_vals[k].setText(f'{x / 2:+g}' if x else '0')
        self.draw()
        self.timer.start()

    def slider_moved(self, key, x):
        self.e[key] = x
        self.sliders[key][1].setText(self.text(key, x))
        self.draw()
        self.timer.start()

    def push(self):
        self.panel.saved[EQ_SETTINGS] = dict(self.e)
        save_settings(self.panel.saved)
        apply_eq(self.e)


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
        self.outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)	# never smaller than its contents
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

    def convert_tone_entry(self):
        """a saved old fixed entry (Bass +, Treble -, ...) becomes Off + the Bass/Treble sliders at that end"""
        eq = self.ctls.get(EQ_CTL)
        i = self.saved.get(EQ_CTL)
        if BANK_CTL not in self.ctls or not eq or not isinstance(i, int) or i < 0:
            return
        if i < len(eq['items']):
            label = eq['items'][i]
        elif 11 <= i < 11 + len(TONE_ENTRIES):	# saved by 2026-11-09's firmware: Off, 10 presets, then the 5 entries
            label = TONE_ENTRIES[i - 11]
        else:
            return
        if self.saved.pop('DSP EQ Level Playback Volume', None) is not None:
            save_settings(self.saved)		# the EQ page has its own level
        if label not in TONE_ENTRIES:
            return
        self.saved[EQ_CTL] = eq['items'].index('Off') if 'Off' in eq['items'] else 0
        self.saved.pop('DSP EQ Level Playback Volume', None)
        e = eq_state(self.saved)
        e['bass'] = 24 if 'Bass +' in label else -24 if 'Bass -' in label else 0
        e['treble'] = 24 if 'Treble +' in label else -24 if 'Treble -' in label else 0
        self.saved[EQ_SETTINGS] = e
        save_settings(self.saved)

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
            self.convert_tone_entry()
            restore(self.ctls, self.saved)
            if BANK_CTL in self.ctls:
                apply_eq(eq_state(self.saved))	# the driver starts with the recorded preset: the page's EQ on top
            self.restored = True
            self.ctls = read_controls()
        elif self.ctls and self.saved.get(UPMIX_CTL) and self.ctls.get(UPMIX_CTL, {}).get('value') is False:
            write_control(UPMIX_CTL, True)		# it was on, but PipeWire restarted: bring it back
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
        self.eqpage = None
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
        self.stack.setMinimumWidth(440)		# height: the tallest page decides (the EQ page)
        group = QButtonGroup(self.body)
        shown = None
        for tab in TABS:
            items = [(n, l, lv) for t, n, l, lv in LAYOUT if t == tab and n in self.ctls]
            if tab == 'Microphone' and SOURCE_CTL in self.ctls:
                items.append((SOURCE_CTL, None, None))
            if not items:
                continue
            if tab == 'EQ' and BANK_CTL in self.ctls and EQ_CTL in self.ctls:
                page = self.eqpage = EqPage(self)	# graphic EQ, Bass, Treble, level: computed banks
            else:
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
                    if BESIDE.get(name) not in [n for n, _, _ in items]:	# else its partner's row shows it
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
            pair = [n for n, b in BESIDE.items() if b == name and n in self.ctls]
            g.addWidget(self.switch(name, label), row, 0, 1, 1 if pair else 2)
            for n in pair:
                g.addWidget(self.switch(n, next(l for _, m, l, _ in LAYOUT if m == n)), row, 1)
            row += 1
            for lv in subs(level) + tuple(x for n in pair for _, m, _, l in LAYOUT if m == n for x in subs(l)):
                if lv in self.ctls:
                    self.labels[lv] = QLabel(SUB_LABEL.get(lv, 'Level'))
                    g.addWidget(self.labels[lv], row, 0)
                    g.addWidget(self.slider(lv, self.ctls[lv]), row, 1)
                    row += 1
            return row
        if c['type'] == 'ENUMERATED':
            g.addWidget(QLabel(label), row, 0)
            w = QComboBox()
            items = c['items']
            if name == EQ_CTL and BANK_CTL in self.ctls:
                items = [i for i in items if i not in TONE_ENTRIES]	# the Bass/Treble sliders do these (they come last)
            w.addItems(items)
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

    def switch(self, name, label):
        w = QCheckBox(label)
        if name == UPMIX_CTL:
            w.setToolTip('Software, not the X-Fi: done on your PC in PipeWire, before the sound reaches the card (on Windows,\n'
                         'Creative does this in software too). Spreads stereo music and videos to 7.1 (center from what both\n'
                         'sides share, rear and side from what differs), so CMSS-3D can place it around you.\n'
                         'Surround games keep their own 7.1. With CMSS-3D off it works as a stereo widener.')
        w.toggled.connect(lambda val, n=name: self.change(n, val))
        self.widgets[name] = w
        return w

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
            c = self.ctls.get(sw)
            if c is None or c['value'] is None:
                continue
            on = c['items'][c['value']] != 'Off' if c['type'] == 'ENUMERATED' else bool(c['value'])
            for row in filter(None, (self.rows.get(lv) for lv in subs(level))):
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
        order = [n for _, c, _, l in LAYOUT for n in (c, *subs(l))]
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
