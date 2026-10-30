#!/usr/bin/env python3
"""Config GUI for wiimote2xbox: buttons, stick, motion, mouse, rumble and a live view of the virtual pad."""
import json
import os
import socket
import subprocess
import sys
import tomllib

from evdev import ecodes as e, ff
from PySide6.QtCore import QPointF, QRectF, QSocketNotifier, Qt, QTimer
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QMessageBox, QProgressBar, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from wiimote2xbox import (ACCEL, CONFIG, ENGINES, IR, MONITOR, MONITORED, MOTION_SOURCES, MPLUS, NUNCHUK, RAW, WIIMOTE,
                          WIIMOTE2, Imu, abs_values, find, motion_raw, remotes)

PAD = "Microsoft X-Box 360 pad"
SERVICE = "wiimote2xbox.service"
DAEMON = r"^\S*python\S* \S*wiimote2xbox\.py"  # pgrep pattern for the daemon, also when run by hand
LIT = "background: palette(highlight); color: palette(highlighted-text); font-weight: bold;"
SOURCES = [("BTN_SOUTH", "A"), ("BTN_EAST", "B"), ("BTN_1", "1"), ("BTN_2", "2"),
           ("KEY_NEXT", "+"), ("KEY_PREVIOUS", "−"), ("BTN_MODE", "Home"),
           ("KEY_UP", "Cruceta ↑"), ("KEY_DOWN", "Cruceta ↓"), ("KEY_LEFT", "Cruceta ←"), ("KEY_RIGHT", "Cruceta →"),
           ("BTN_C", "C (Nunchuk)"), ("BTN_Z", "Z (Nunchuk)")]
NOTHING = [("", "— nada —")]
PAD_BUTTONS = [("BTN_SOUTH", "A"), ("BTN_EAST", "B"), ("BTN_X", "X"), ("BTN_Y", "Y"),
               ("BTN_TL", "LB"), ("BTN_TR", "RB"), ("BTN_SELECT", "Back"), ("BTN_START", "Start"),
               ("BTN_MODE", "Guide"), ("BTN_THUMBL", "L3"), ("BTN_THUMBR", "R3")]
TARGETS = NOTHING + PAD_BUTTONS + [("LT", "LT"), ("RT", "RT"), ("DPAD_UP", "D-pad ↑"), ("DPAD_DOWN", "D-pad ↓"),
                                   ("DPAD_LEFT", "D-pad ←"), ("DPAD_RIGHT", "D-pad →")]
MOUSE_TARGETS = NOTHING + [("BTN_LEFT", "Clic izquierdo"), ("BTN_RIGHT", "Clic derecho"), ("BTN_MIDDLE", "Clic central"),
                           ("WHEEL_UP", "Rueda ↑"), ("WHEEL_DOWN", "Rueda ↓"),
                           ("KEY_ENTER", "Intro"), ("KEY_ESC", "Esc"), ("KEY_BACKSPACE", "Borrar"),
                           ("KEY_TAB", "Tab"), ("KEY_SPACE", "Espacio"), ("KEY_UP", "Flecha ↑"),
                           ("KEY_DOWN", "Flecha ↓"), ("KEY_LEFT", "Flecha ←"), ("KEY_RIGHT", "Flecha →"),
                           ("KEY_LEFTMETA", "Tecla Windows"), ("KEY_VOLUMEUP", "Volumen +"),
                           ("KEY_VOLUMEDOWN", "Volumen −"), ("KEY_PLAYPAUSE", "Play / Pausa")]
STICK_TARGETS = [("LEFT", "Stick izquierdo"), ("RIGHT", "Stick derecho")]
MOTION_LABELS = {"WIIMOTE_X": "Wiimote: girar (eje X)", "WIIMOTE_Y": "Wiimote: inclinar adelante/atrás (eje Y)",
                 "WIIMOTE_Z": "Wiimote: eje Z", "WIIMOTE_SHAKE": "Wiimote: agitar",
                 "NUNCHUK_X": "Nunchuk: girar (eje X)", "NUNCHUK_Y": "Nunchuk: inclinar adelante/atrás (eje Y)",
                 "NUNCHUK_Z": "Nunchuk: eje Z", "NUNCHUK_SHAKE": "Nunchuk: agitar",
                 "IR_X": "Puntero IR: apuntar izquierda/derecha", "IR_Y": "Puntero IR: apuntar arriba/abajo (altura)",
                 "IR_DIST": "Puntero IR: acercar/alejar de la pantalla",
                 "WIIMOTE_PITCH": "MotionPlus: ángulo adelante/atrás (grados)", "WIIMOTE_ROLL": "MotionPlus: ángulo de giro (grados)",
                 "WIIMOTE_YAW": "MotionPlus: apuntar izquierda/derecha (grados, vuelve a 0)",
                 "GYRO_PITCH": "MotionPlus: golpe adelante/atrás (grados/s)", "GYRO_ROLL": "MotionPlus: golpe de muñeca (grados/s)",
                 "GYRO_YAW": "MotionPlus: golpe izquierda/derecha (grados/s)"}
MOTION_TABS = {"WIIMOTE_X": "Wii girar", "WIIMOTE_Y": "Wii inclinar", "WIIMOTE_Z": "Wii Z", "WIIMOTE_SHAKE": "Wii agitar",
               "NUNCHUK_X": "Nun girar", "NUNCHUK_Y": "Nun inclinar", "NUNCHUK_Z": "Nun Z", "NUNCHUK_SHAKE": "Nun agitar",
               "IR_X": "IR ↔", "IR_Y": "IR ↕", "IR_DIST": "IR distancia",
               "WIIMOTE_PITCH": "M+ inclinar", "WIIMOTE_ROLL": "M+ girar", "WIIMOTE_YAW": "M+ apuntar",
               "GYRO_PITCH": "M+ golpe ↕", "GYRO_ROLL": "M+ golpe giro", "GYRO_YAW": "M+ golpe ↔"}
MOTION_TARGETS = [("ABS_X", "Stick izquierdo ↔ (dirección)"), ("ABS_Y", "Stick izquierdo ↕"),
                  ("ABS_RX", "Stick derecho ↔"), ("ABS_RY", "Stick derecho ↕"),
                  ("ABS_Z", "LT"), ("ABS_RZ", "RT"),
                  ("POD_LEFT", "Podracer: motor izquierdo"), ("POD_RIGHT", "Podracer: motor derecho")] + [(k, f"Botón {v}") for k, v in PAD_BUTTONS]
MOTION_DEFAULT = {"enabled": False, "source": "WIIMOTE_X", "target": "ABS_X", "center": 0,
                  "deadzone": 10, "max": 100, "anti_deadzone": 0, "invert": False, "smoothing": 0.7}
MOTION_SLOTS = len(MOTION_LABELS)
ANTI_LABEL = "Empuje inicial % (anti-zona muerta)"
MOUSE_DEFAULT = {"enabled": False, "toggle": "BTN_MODE", "speed": 1000, "curve": 2.0}
TWO_DEFAULT = {"enabled": False, "swap": False, "center_left": 0, "center_right": 0, "deadzone": 15, "max": 70, "invert": True}
MPLUS_DEFAULT = {"invert_pitch": False, "invert_roll": False, "invert_yaw": False, "accel_time": 1.0, "yaw_return": 3.0}


def dump(cfg):
    out = []
    for section, values in cfg.items():
        for table in values if isinstance(values, list) else [values]:
            out.append(f"[[{section}]]" if isinstance(values, list) else f"[{section}]")
            out += [f"{k} = {json.dumps(v)}" for k, v in table.items()]
            out.append("")
    return "# Written by config_gui.py.\n\n" + "\n".join(out)


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args, SERVICE], capture_output=True, text=True).stdout.strip()


def combo(items, current):
    box = QComboBox()
    for key, label in items:
        box.addItem(label, key)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


def spin(value, lo, hi):
    box = QSpinBox()
    box.setRange(lo, hi)
    box.setValue(value)
    return box


def dspin(value, lo, hi, step):
    box = QDoubleSpinBox()
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setValue(value)
    return box


def check(value, text=""):
    box = QCheckBox(text)
    box.setChecked(value)
    return box


def value(w):
    if isinstance(w, QComboBox):
        return w.currentData()
    if isinstance(w, QCheckBox):
        return w.isChecked()
    if isinstance(w, QDoubleSpinBox):
        return round(w.value(), 2)
    return w.value()


def form_page():
    page, form = QWidget(), QFormLayout()
    page.setLayout(form)
    return page, form


class Reader:
    """Reads an input device by name with a Qt notifier; reconnects every second."""

    def __init__(self, name, on_event, parent, on_connect=None):
        self.name, self.on_event, self.on_connect, self.dev, self.notifier = name, on_event, on_connect, None, None
        self.timer = QTimer(parent, timeout=self.connect)
        self.timer.start(1000)
        self.connect()

    def connect(self):
        if self.dev is None and (dev := find(self.name)):
            self.dev = dev
            self.notifier = QSocketNotifier(dev.fd, QSocketNotifier.Type.Read)
            self.notifier.activated.connect(self.read)
            if self.on_connect:
                self.on_connect(dev)

    def read(self):
        try:
            for ev in self.dev.read():
                self.on_event(ev)
        except BlockingIOError:
            pass
        except OSError:
            self.notifier.setEnabled(False)
            self.dev.close()
            self.dev = None


class PadView(QWidget):
    """Live drawing of the virtual Xbox pad."""
    BUTTONS = {e.BTN_TL: ("LB", 20, 30), e.BTN_TR: ("RB", 280, 30), e.BTN_SELECT: ("Back", 120, 70),
               e.BTN_MODE: ("Guide", 170, 45), e.BTN_START: ("Start", 220, 70),
               e.BTN_WEST: ("Y", 290, 70), e.BTN_NORTH: ("X", 260, 100), e.BTN_EAST: ("B", 320, 100),
               e.BTN_SOUTH: ("A", 290, 130)}

    def __init__(self):
        super().__init__()
        self.setFixedSize(360, 230)
        self.keys, self.abs = set(), {}

    def event_in(self, ev):
        if ev.type == e.EV_KEY:
            (self.keys.add if ev.value else self.keys.discard)(ev.code)
        elif ev.type == e.EV_ABS:
            self.abs[ev.code] = ev.value
        elif ev.type == e.EV_SYN:
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg, hi = pal.color(QPalette.ColorRole.WindowText), pal.color(QPalette.ColorRole.Highlight)
        p.setPen(fg)

        def box(rect, on, text):
            p.setBrush(hi if on else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, 5, 5)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

        for code, x, label in ((e.ABS_Z, 20, "LT"), (e.ABS_RZ, 280, "RT")):
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(x, 8, 60, 14))
            p.fillRect(QRectF(x, 8, 60 * self.abs.get(code, 0) / 255, 14), hi)
            p.drawText(QRectF(x, 8, 60, 14), Qt.AlignmentFlag.AlignCenter, label)
        for code, (label, x, y) in self.BUTTONS.items():
            w = 60 if code in (e.BTN_TL, e.BTN_TR) else 34
            box(QRectF(x, y, w, 24), code in self.keys, label)
        for (ax, ay, click), (cx, cy) in (((e.ABS_X, e.ABS_Y, e.BTN_THUMBL), (65, 120)),
                                         ((e.ABS_RX, e.ABS_RY, e.BTN_THUMBR), (230, 175))):
            p.setBrush(hi if click in self.keys else Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 40, 40)
            p.setBrush(fg)
            p.drawEllipse(QPointF(cx + 40 * self.abs.get(ax, 0) / 32767, cy + 40 * self.abs.get(ay, 0) / 32767), 7, 7)
        hx, hy = self.abs.get(e.ABS_HAT0X, 0), self.abs.get(e.ABS_HAT0Y, 0)
        for dx, dy, text in ((0, -1, "↑"), (0, 1, "↓"), (-1, 0, "←"), (1, 0, "→")):
            box(QRectF(130 + 26 * dx, 165 + 26 * dy, 24, 24), (dx and hx == dx) or (dy and hy == dy), text)


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Wiimote")
        cfg = tomllib.loads(CONFIG.read_text())
        self.raw = {WIIMOTE: {}, WIIMOTE2: {}, NUNCHUK: {}, ACCEL: {}, IR: {}, MPLUS: {}}  # latest raw ABS values per device
        self.imu = Imu()
        self.levers = [0, 0, 0, 0]  # from the daemon: left/right output %, left/right raw
        self.button_labels = {}  # source code -> labels to light up
        self.rumble_id = None
        # The daemon keeps the Wiimote and Nunchuk grabbed and copies their raw events to this socket.
        if os.path.exists(MONITOR):
            os.unlink(MONITOR)
        self.monitor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.monitor.bind(MONITOR)
        self.monitor.setblocking(False)
        self.monitor_notifier = QSocketNotifier(self.monitor.fileno(), QSocketNotifier.Type.Read, self)
        self.monitor_notifier.activated.connect(self.monitor_in)

        tabs = QTabWidget()
        tabs.addTab(self.buttons_tab(cfg["buttons"]), "Botones")
        tabs.addTab(self.stick_tab(cfg["stick"]), "Stick")
        motions = cfg.get("motion", [])
        tabs.addTab(self.motion_tab([MOTION_DEFAULT | (motions[i] if i < len(motions) else {})
                                     for i in range(MOTION_SLOTS)]), "Movimiento")
        tabs.addTab(self.two_tab(TWO_DEFAULT | cfg.get("two_wiimotes", {}), cfg.get("left_buttons", {})), "Dos Wiimotes")
        tabs.addTab(self.mplus_tab(MPLUS_DEFAULT | cfg.get("motionplus", {})), "MotionPlus")
        tabs.addTab(self.mouse_tab(MOUSE_DEFAULT | cfg.get("mouse", {}), cfg.get("mouse_buttons", {})), "Ratón")
        tabs.addTab(self.rumble_tab(cfg["rumble"]), "Vibración")

        self.view = PadView()
        self.pad = Reader(PAD, self.view.event_in, self)
        Reader(ACCEL, lambda ev: self.store(ACCEL, ev), self, lambda d: self.raw[ACCEL].update(abs_values(d)))
        Reader(IR, lambda ev: self.store(IR, ev), self, lambda d: self.raw[IR].update(abs_values(d)))
        Reader(MPLUS, self.store_gyro, self, lambda d: self.raw[MPLUS].update(abs_values(d)))
        # Grabbed by the daemon, so no events here (they come through the monitor); only the starting values.
        Reader(NUNCHUK, lambda ev: None, self, lambda d: self.raw[NUNCHUK].update(abs_values(d)))
        QTimer(self, timeout=self.refresh_motion).start(50)

        self.status = QLabel()
        self.autostart = QCheckBox("Arrancar al iniciar sesión")
        self.autostart.setChecked(systemctl("is-enabled") == "enabled")
        self.autostart.toggled.connect(lambda on: systemctl("enable" if on else "disable"))
        start, stop, save = QPushButton("Iniciar"), QPushButton("Parar"), QPushButton("Guardar y aplicar")
        start.clicked.connect(lambda: systemctl("start"))
        stop.clicked.connect(lambda: systemctl("stop"))
        save.clicked.connect(self.save)
        save.setDefault(True)
        QTimer(self, timeout=self.refresh_status).start(1000)
        self.refresh_status()

        live = QVBoxLayout()
        live.addWidget(QLabel("Mando virtual en directo (lo que ve el juego):"))
        live.addWidget(self.view)
        live.addStretch()
        top = QHBoxLayout()
        top.addWidget(tabs)
        top.addLayout(live)
        bottom = QHBoxLayout()
        for w in (self.status, start, stop, self.autostart):
            bottom.addWidget(w)
        bottom.addStretch()
        bottom.addWidget(save)
        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(bottom)

    def mapping(self, form, targets, current, sources=SOURCES):
        boxes = {}
        for src, label in sources:
            boxes[src] = combo(targets, current.get(src, ""))
            lab = QLabel(f" {label} ")
            self.button_labels.setdefault(e.ecodes[src], []).append(lab)
            form.addRow(lab, boxes[src])
        return boxes

    def buttons_tab(self, buttons):
        page, form = form_page()
        form.addRow(QLabel("Pulsa un botón del Wiimote: su fila se ilumina."))
        self.button_boxes = self.mapping(form, TARGETS, buttons)
        return page

    def stick_tab(self, s):
        page, form = form_page()
        self.stick = {"target": combo(STICK_TARGETS, s["target"]),
                      "center_x": spin(s["center_x"], -120, 120), "center_y": spin(s["center_y"], -120, 120),
                      "deadzone": spin(s["deadzone"], 0, 60), "max": spin(s["max"], 10, 120),
                      "anti_deadzone": spin(s.get("anti_deadzone", 0), 0, 90),
                      "invert_x": check(s["invert_x"]), "invert_y": check(s["invert_y"])}
        for key, label in (("target", "Stick del Nunchuk →"), ("center_x", "Centro X"), ("center_y", "Centro Y"),
                           ("deadzone", "Zona muerta"), ("max", "Recorrido máximo"),
                           ("anti_deadzone", ANTI_LABEL), ("invert_x", "Invertir X"), ("invert_y", "Invertir Y")):
            form.addRow(label, self.stick[key])
        self.raw_label = QLabel("Stick en bruto: —")
        center = QPushButton("Usar la posición actual como centro (stick suelto)")
        center.clicked.connect(self.center_stick)
        form.addRow(self.raw_label)
        form.addRow(center)
        form.addRow(QLabel("El recorrido máximo crece solo si el stick llega más lejos."))
        return page

    def motion_tab(self, slots):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(QLabel("Convierte el movimiento en stick o botones del mando.\n"
                             "Cada número es un movimiento. Mueve el mando y mira la barra."))
        sub = QTabWidget()
        lay.addWidget(sub)
        self.motions = []
        for i, m in enumerate(slots):
            p, form = form_page()
            w = {"enabled": check(m["enabled"], "Activado"),
                 "source": combo(MOTION_LABELS.items(), m["source"]), "target": combo(MOTION_TARGETS, m["target"]),
                 "center": spin(m["center"], -1023, 1023), "deadzone": spin(m["deadzone"], 0, 500),
                 "max": spin(m["max"], 1, 1023), "anti_deadzone": spin(m["anti_deadzone"], 0, 90),
                 "invert": check(m["invert"]),
                 "smoothing": dspin(m["smoothing"], 0, 0.95, 0.05)}
            form.addRow(w["enabled"])
            for key, label in (("source", "Movimiento"), ("target", "Controla"), ("center", "Centro"),
                               ("deadzone", "Zona muerta"), ("max", "Recorrido máximo"),
                               ("anti_deadzone", ANTI_LABEL), ("invert", "Invertir"), ("smoothing", "Suavizado (0 = nada)")):
                form.addRow(label, w[key])
            w["raw"] = QLabel("—")
            w["bar"] = QProgressBar()
            w["bar"].setRange(-100, 100)
            w["bar"].setFormat("%v %")
            center = QPushButton("Usar la posición actual como centro")
            center.clicked.connect(lambda _=False, w=w: self.center_motion(w))
            form.addRow("Valor en bruto", w["raw"])
            form.addRow("Salida", w["bar"])
            form.addRow(center)
            self.motions.append(w)
            sub.addTab(p, MOTION_TABS[m["source"]])
            w["source"].currentIndexChanged.connect(
                lambda _=0, i=i, w=w: sub.setTabText(i, MOTION_TABS[w["source"].currentData()]))
        lay.addWidget(QLabel("Puntero IR: necesita la barra de sensores encendida y el Wiimote apuntando a ella."))
        return page

    def two_tab(self, t, left_buttons):
        page, form, buttons = QWidget(), QFormLayout(), QFormLayout()
        row = QHBoxLayout(page)
        row.addLayout(form)
        row.addLayout(buttons)
        buttons.addRow(QLabel("Botones del mando izquierdo.\nEl derecho usa la pestaña Botones."))
        self.left_boxes = self.mapping(buttons, TARGETS, left_buttons, [s for s in SOURCES if s[0] not in ("BTN_C", "BTN_Z")])
        self.two = {"enabled": check(t["enabled"], "Usar dos Wiimotes como palancas"),
                    "swap": check(t["swap"], "Intercambiar izquierda y derecha"),
                    "center_left": spin(t["center_left"], -100, 100), "center_right": spin(t["center_right"], -100, 100),
                    "deadzone": spin(t["deadzone"], 0, 60), "max": spin(t["max"], 10, 120),
                    "invert": check(t["invert"], "Invertir adelante/atrás")}
        self.two_count = QLabel()
        self.lever_bars = [QProgressBar(), QProgressBar()]
        for bar in self.lever_bars:
            bar.setRange(-100, 100)
            bar.setFormat("%v %")
        center = QPushButton("Usar la posición actual como centro (los dos mandos)")
        center.clicked.connect(self.center_levers)
        form.addRow(QLabel("Dos Wiimotes sin Nunchuk, como un solo mando. Cada uno es una palanca:\n"
                           "los dos adelante aceleran, los dos atrás frenan y la diferencia gira.\n"
                           "Con un solo Wiimote todo sigue como antes."))
        form.addRow(self.two["enabled"])
        form.addRow(self.two_count)
        form.addRow("Palanca izquierda", self.lever_bars[0])
        form.addRow("Palanca derecha", self.lever_bars[1])
        form.addRow(QLabel("Inclina un mando y mira las barras. Si se mueve la barra equivocada,\n"
                           "marca «Intercambiar» y pulsa «Guardar y aplicar»."))
        form.addRow(self.two["swap"])
        form.addRow(center)
        for key, label in (("center_left", "Centro izquierda"), ("center_right", "Centro derecha"),
                           ("deadzone", "Zona muerta"), ("max", "Recorrido máximo")):
            form.addRow(label, self.two[key])
        form.addRow(self.two["invert"])
        return page

    def center_levers(self):
        self.two["center_left"].setValue(self.levers[2])
        self.two["center_right"].setValue(self.levers[3])

    def mplus_tab(self, m):
        page, form = form_page()
        self.imu.config(m)
        self.mplus = {"invert_pitch": check(m["invert_pitch"], "Invertir adelante/atrás"),
                      "invert_roll": check(m["invert_roll"], "Invertir giro"),
                      "invert_yaw": check(m["invert_yaw"], "Invertir izquierda/derecha"),
                      "accel_time": dspin(m["accel_time"], 0.2, 10, 0.1), "yaw_return": dspin(m["yaw_return"], 0.5, 60, 0.5)}
        form.addRow(QLabel("Usa estos valores en la pestaña Movimiento (fuentes «MotionPlus»).\n"
                           "Al conectar, deja el Wiimote quieto 1 segundo: así aprende su reposo."))
        for key in ("invert_pitch", "invert_roll", "invert_yaw"):
            form.addRow(self.mplus[key])
        form.addRow("Corrección del acelerómetro (s, menos = corrige antes)", self.mplus["accel_time"])
        form.addRow("Apuntar vuelve a 0 en (s)", self.mplus["yaw_return"])
        return page

    def mouse_tab(self, m, buttons):
        page, form = form_page()
        self.mouse = {"enabled": check(m["enabled"], "Usar el Wiimote como ratón (en vez de mando)"),
                      "toggle": combo(NOTHING + SOURCES, m["toggle"]),
                      "speed": spin(m["speed"], 100, 5000), "curve": dspin(m["curve"], 1, 3, 0.1)}
        form.addRow(self.mouse["enabled"])
        form.addRow("Botón para cambiar mando ↔ ratón", self.mouse["toggle"])
        form.addRow("Velocidad (píxeles/s)", self.mouse["speed"])
        form.addRow("Curva (1 = lineal)", self.mouse["curve"])
        form.addRow(QLabel("El stick del Nunchuk mueve el puntero. Botones en modo ratón:"))
        self.mouse_boxes = self.mapping(form, MOUSE_TARGETS, buttons)
        return page

    def rumble_tab(self, r):
        page, form = form_page()
        self.rumble = check(r["enabled"], "Enviar la vibración del juego al Wiimote")
        test = QPushButton("Probar vibración")
        test.clicked.connect(self.test_rumble)
        form.addRow(self.rumble)
        form.addRow(test)
        return page

    def store(self, name, ev):
        if ev.type == e.EV_ABS:
            self.raw[name][ev.code] = ev.value

    def store_gyro(self, ev):
        self.store(MPLUS, ev)
        if ev.type == e.EV_SYN:
            self.imu.update(self.raw[MPLUS], self.raw[ACCEL], ev.timestamp())

    def monitor_in(self):
        try:
            while True:
                dev, typ, code, val = RAW.unpack(self.monitor.recv(RAW.size))
                if dev == ENGINES:
                    self.levers[code] = val
                    continue
                name = MONITORED[dev]
                if typ == e.EV_KEY and code in self.button_labels and val != 2:
                    for lab in self.button_labels[code]:
                        lab.setStyleSheet(LIT if val else "")
                elif typ == e.EV_ABS:
                    self.raw[name][code] = val
        except BlockingIOError:
            pass

    def motion_input(self, w):
        device, how = MOTION_SOURCES[w["source"].currentData()]
        return motion_raw(how, self.raw[device])

    def refresh_motion(self):
        n = self.raw[NUNCHUK]
        self.raw_label.setText(f"Stick en bruto: X {n.get(e.ABS_HAT0X, '—')}   Y {n.get(e.ABS_HAT0Y, '—')}")
        for bar, pct in zip(self.lever_bars, self.levers):
            bar.setValue(pct)
        for w in self.motions:
            raw = self.motion_input(w)
            w["raw"].setText("— (sin datos)" if raw is None else str(round(raw)))
            if raw is None:
                w["bar"].setValue(0)
                continue
            v = raw - w["center"].value()
            a = max(0, abs(v) - w["deadzone"].value()) / max(1, w["max"].value() - w["deadzone"].value())
            anti = w["anti_deadzone"].value() if a > 0 else 0
            pct = round(anti + min(1, a) * (100 - anti)) * (-1 if (v < 0) != w["invert"].isChecked() else 1)
            w["bar"].setValue(pct)

    def center_stick(self):
        n = self.raw[NUNCHUK]
        self.stick["center_x"].setValue(n.get(e.ABS_HAT0X, self.stick["center_x"].value()))
        self.stick["center_y"].setValue(n.get(e.ABS_HAT0Y, self.stick["center_y"].value()))

    def center_motion(self, w):
        raw = self.motion_input(w)
        if raw is not None:
            w["center"].setValue(round(raw))

    def test_rumble(self):
        dev = self.pad.dev
        if dev is None:
            return QMessageBox.warning(self, "Vibración", "El mando virtual no está activo. Pulsa Iniciar.")
        if self.rumble_id is None:
            self.rumble_id = dev.upload_effect(ff.Effect(
                e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(500, 0),
                ff.EffectType(ff_rumble_effect=ff.Rumble(0xFFFF, 0xFFFF))))
        dev.write(e.EV_FF, self.rumble_id, 1)

    def refresh_status(self):
        state = systemctl("is-active")
        running = state == "active" or subprocess.run(["pgrep", "-f", DAEMON], capture_output=True).returncode == 0
        self.status.setText("Daemon: en marcha" if running else f"Daemon: parado ({state})")
        self.two_count.setText(f"Wiimotes conectados: {len(remotes())} de 2")

    def save(self):
        cfg = {"stick": {k: value(w) for k, w in self.stick.items()},
               "buttons": {k: value(w) for k, w in self.button_boxes.items()},
               "rumble": {"enabled": self.rumble.isChecked()},
               "motion": [{k: value(w[k]) for k in MOTION_DEFAULT} for w in self.motions],
               "two_wiimotes": {k: value(w) for k, w in self.two.items()},
               "left_buttons": {k: value(w) for k, w in self.left_boxes.items()},
               "motionplus": {k: value(w) for k, w in self.mplus.items()},
               "mouse": {k: value(w) for k, w in self.mouse.items()},
               "mouse_buttons": {k: value(w) for k, w in self.mouse_boxes.items()}}
        self.imu.config(cfg["motionplus"])
        CONFIG.write_text(dump(cfg))
        subprocess.run(["pkill", "-HUP", "-f", DAEMON])

    def closeEvent(self, event):
        os.unlink(MONITOR)
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setDesktopFileName("wiimote2xbox")
    w = Window()
    w.show()
    sys.exit(app.exec())
