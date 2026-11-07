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

from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QRadioButton, QSlider,
                             QSystemTrayIcon, QVBoxLayout, QWidget)

CARD = 'XFi'
CONF = os.path.expanduser('~/.config/xfi-dsp/settings.json')
SOURCE_CTL = 'DSP Recording Source'
LEVEL_CTL = 'DSP Mic Capture Volume'	# the mic's level, also PipeWire's input volume
MASTER_CTL = 'DSP Master Playback Volume'	# the taskbar volume
NOT_SAVED = (LEVEL_CTL, MASTER_CTL)		# PipeWire remembers these
PORTS = {'Mic': 'xfi-dsp-mic', 'What U Hear': 'xfi-dsp-wuh'}
# (section, control, label, slider that belongs to it). CMSS-3D first: switching it resets EAX and Mic Environment.
LAYOUT = [
    ('Output', MASTER_CTL, 'Master', None),
    ('Output', 'DSP Auto Mute Speakers Playback Switch', 'Mute speakers when headphones are in', None),
    ('Effects', 'DSP CMSS-3D Playback Switch', 'CMSS-3D', 'DSP CMSS-3D Level Playback Volume'),
    ('Effects', 'DSP SVM Playback Switch', 'SVM', None),
    ('Effects', 'DSP Bass Boost Playback Switch', 'Bass Boost', 'DSP Bass Boost Level Playback Volume'),
    ('Effects', 'DSP Crystalizer Playback Switch', 'Crystalizer', 'DSP Crystalizer Level Playback Volume'),
    ('Effects', 'DSP EQ Preset', 'EQ Preset', 'DSP EQ Level Playback Volume'),
    ('Microphone', LEVEL_CTL, 'Mic Level', None),
    ('Microphone', 'DSP Mic Monitor Playback Switch', 'Mic Monitor', None),
    ('Microphone', 'DSP EAX Playback Volume', 'EAX', None),
    ('Microphone', 'DSP Mic Environment Playback Volume', 'Mic FX', None),
]


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


def set_source(item, ctl):
    """Through PipeWire when it has the port, so that it knows; straight to the control otherwise."""
    r = run('pactl', 'list', 'short', 'sources')
    for line in (r.stdout.splitlines() if r else []):
        name = line.split('\t')[1] if '\t' in line else ''
        if 'xfi-dsp-mic' in name and '.monitor' not in name:
            r = run('pactl', 'set-source-port', name, PORTS.get(item, ''))
            if r is not None and r.returncode == 0:
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
        self.saved = load_settings()
        self.ctls = {}
        self.widgets = {}
        self.layout_key = None
        self.restored = False
        self.pending = {}
        self.outer = QVBoxLayout(self)
        self.status = QLabel()
        self.outer.addWidget(self.status)
        self.body = None
        self.debounce = QTimer(self, singleShot=True, interval=150)
        self.debounce.timeout.connect(self.flush)
        self.poll = QTimer(self, interval=3000)
        self.poll.timeout.connect(self.refresh)
        self.poll.start()
        self.refresh()

    def active(self):
        return bool(self.ctls)

    def refresh(self):
        if self.pending:
            return
        self.ctls = read_controls()
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
        self.status.setVisible(not self.ctls)

    def build(self):
        if self.body is not None:
            self.outer.removeWidget(self.body)
            self.body.deleteLater()
        self.widgets = {}
        self.body = QWidget()
        g = QGridLayout(self.body)
        g.setContentsMargins(0, 0, 0, 0)
        g.setColumnStretch(1, 1)
        g.setColumnMinimumWidth(1, 190)
        self.outer.addWidget(self.body)
        self.status.setText('The X-Fi DSP is not running')
        row, section = 0, None
        for sec, name, label, level in LAYOUT:
            c = self.ctls.get(name)
            if c is None:
                continue
            if sec != section:
                section = sec
                row = self.heading(g, row, sec)
            if c['type'] == 'BOOLEAN':
                w = QCheckBox(label)
                w.toggled.connect(lambda v, n=name: self.change(n, v))
            elif c['type'] == 'ENUMERATED':
                g.addWidget(QLabel(label), row, 0)
                w = QComboBox()
                w.addItems(c['items'])
                w.currentIndexChanged.connect(lambda v, n=name: self.change(n, v))
                g.addWidget(w, row, 1)
                self.widgets[name] = w
                row += 1
                row = self.level(g, row, level, 'EQ Level')
                continue
            else:
                g.addWidget(QLabel(label), row, 0)
                g.addWidget(self.slider(name, c), row, 1)
                row += 1
                continue
            self.widgets[name] = w
            g.addWidget(w, row, 0)
            if level in self.ctls:
                g.addWidget(self.slider(level, self.ctls[level]), row, 1)
            row += 1
        c = self.ctls.get(SOURCE_CTL)
        if c:
            row = self.heading(g, row, 'Recording')
            box = QHBoxLayout()
            self.widgets[SOURCE_CTL] = []
            for item in c['items']:
                b = QRadioButton(item)
                b.clicked.connect(lambda _, i=item: self.source(i))
                box.addWidget(b)
                self.widgets[SOURCE_CTL].append(b)
            box.addStretch()
            g.addLayout(box, row, 0, 1, 2)
        self.adjustSize()

    def heading(self, g, row, text):
        if row:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            g.addWidget(line, row, 0, 1, 2)
            row += 1
        h = QLabel(f'<b>{text}</b>')
        g.addWidget(h, row, 0, 1, 2)
        return row + 1

    def level(self, g, row, name, label):
        if name in self.ctls:
            g.addWidget(QLabel(label), row, 0)
            g.addWidget(self.slider(name, self.ctls[name]), row, 1)
            row += 1
        return row

    def slider(self, name, c):
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(c['min'], c['max'])
        s.valueChanged.connect(lambda v, n=name: self.change(n, v))
        self.widgets[name] = s
        return s

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
        if a != self.last:
            self.last = a
            self.tray.setIcon(make_icon(a))
            self.tray.setToolTip('X-Fi DSP' if a else 'X-Fi DSP: not running')

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
