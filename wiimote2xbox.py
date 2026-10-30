#!/usr/bin/env python3
"""Wiimote + Nunchuk -> virtual Xbox 360 pad (uinput). Usage: wiimote2xbox.py [config.toml]
SIGHUP reloads the config without recreating the pad.
Raw Wiimote/Nunchuk presses are also sent to MONITOR, where the GUI listens.
Mouse mode sends the stick and buttons to a virtual mouse instead of the pad."""
import math
import os
import select
import signal
import socket
import struct
import sys
import time
import tomllib
from pathlib import Path

from evdev import AbsInfo, InputDevice, UInput, ecodes as e, ff, list_devices

WIIMOTE = "Nintendo Wii Remote"
NUNCHUK = "Nintendo Wii Remote Nunchuk"
ACCEL = "Nintendo Wii Remote Accelerometer"
IR = "Nintendo Wii Remote IR"
MPLUS = "Nintendo Wii Remote Motion Plus"
SECOND = " #2"  # marks the second remote's devices; both remotes use the same device names
WIIMOTE2, ACCEL2 = WIIMOTE + SECOND, ACCEL + SECOND
MONITORED = (WIIMOTE, NUNCHUK, WIIMOTE2)  # devices whose raw events go to the GUI
ENGINES = 255  # MONITOR device number for lever values: code 0/1 = left/right output %, code 2/3 = left/right raw
LEVER_SMOOTHING = 0.6
BUTTONS = [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR,
           e.BTN_SELECT, e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR]
STICK_AXES = (e.ABS_X, e.ABS_Y, e.ABS_RX, e.ABS_RY)
ALL_AXES = STICK_AXES + (e.ABS_Z, e.ABS_RZ, e.ABS_HAT0X, e.ABS_HAT0Y)
STICKS = {"LEFT": (e.ABS_X, e.ABS_Y), "RIGHT": (e.ABS_RX, e.ABS_RY)}
TRIGGERS = {"LT": e.ABS_Z, "RT": e.ABS_RZ}
DPAD = {"DPAD_UP": (e.ABS_HAT0Y, -1), "DPAD_DOWN": (e.ABS_HAT0Y, 1),
        "DPAD_LEFT": (e.ABS_HAT0X, -1), "DPAD_RIGHT": (e.ABS_HAT0X, 1)}
MOUSE_KEYS = [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.KEY_ENTER, e.KEY_ESC, e.KEY_BACKSPACE, e.KEY_TAB,
              e.KEY_SPACE, e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT, e.KEY_LEFTMETA,
              e.KEY_VOLUMEUP, e.KEY_VOLUMEDOWN, e.KEY_PLAYPAUSE]
WHEEL = {"WHEEL_UP": 1, "WHEEL_DOWN": -1}
WHEEL_DELAY, WHEEL_REPEAT = 0.4, 0.1  # s before a held wheel button repeats, s between repeats
TICK = 0.01  # s between pointer moves
CONFIG = Path(__file__).with_name("config.toml")
MONITOR = str(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "wiimote2xbox.sock")
RAW = struct.Struct("BHHi")  # MONITORED index, type, code, value
GRAVITY = 100  # accelerometer length at rest (1 g)
GYRO_UNITS = 8192 * 9 / 440  # hid-wiimote MotionPlus units per deg/s
STILL_TIME, STILL_RANGE, STILL_ACCEL = 0.5, 6 * GYRO_UNITS, 10  # at rest: gyro and accelerometer move less than this
IR_DOTS = ((e.ABS_HAT0X, e.ABS_HAT0Y), (e.ABS_HAT1X, e.ABS_HAT1Y))
# Motion source -> (device, what to read): an accelerometer axis, "SHAKE", an IR coordinate or an Imu value.
MOTION_SOURCES = {"WIIMOTE_X": (ACCEL, e.ABS_RX), "WIIMOTE_Y": (ACCEL, e.ABS_RY), "WIIMOTE_Z": (ACCEL, e.ABS_RZ),
                  "WIIMOTE_SHAKE": (ACCEL, "SHAKE"), "WIIMOTE2_Y": (ACCEL2, e.ABS_RY),
                  "NUNCHUK_X": (NUNCHUK, e.ABS_RX), "NUNCHUK_Y": (NUNCHUK, e.ABS_RY), "NUNCHUK_Z": (NUNCHUK, e.ABS_RZ),
                  "NUNCHUK_SHAKE": (NUNCHUK, "SHAKE"),
                  "IR_X": (IR, "IR_X"), "IR_Y": (IR, "IR_Y"), "IR_DIST": (IR, "IR_DIST"),
                  "WIIMOTE_PITCH": (MPLUS, "PITCH"), "WIIMOTE_ROLL": (MPLUS, "ROLL"), "WIIMOTE_YAW": (MPLUS, "YAW"),
                  "GYRO_PITCH": (MPLUS, "RATE_PITCH"), "GYRO_ROLL": (MPLUS, "RATE_ROLL"), "GYRO_YAW": (MPLUS, "RATE_YAW")}
MOTION_AXES = ("ABS_X", "ABS_Y", "ABS_RX", "ABS_RY", "ABS_Z", "ABS_RZ")
POD = ("POD_LEFT", "POD_RIGHT")  # podracer engines: tilt each controller forward like a thrust lever


class Axis:
    """Raw value -> -32767..32767 with center, deadzone and (optionally self-growing) range per side.

    anti (percent): output starts at this value just past the deadzone, so it clears the game's own deadzone
    (x360ce AntiDeadZone)."""

    def __init__(self, center, deadzone, maximum, invert, grow=True, anti=0):
        self.center, self.deadzone, self.invert, self.grow = center, deadzone, invert, grow
        self.anti = anti * 32767 / 100
        self.max = [maximum, maximum]  # [positive side, negative side]

    def __call__(self, raw):
        v = raw - self.center
        side = v < 0
        a = abs(v)
        if self.grow:
            self.max[side] = max(self.max[side], a)
        if a <= self.deadzone:
            return 0
        frac = min(1, (a - self.deadzone) / (self.max[side] - self.deadzone))
        out = round(self.anti + frac * (32767 - self.anti))
        return -out if side != self.invert else out


def pod_drive(left, right):
    """Podracer mix, inputs and output -1..1, + = thrust, - = brake.

    Adapted from "Hybrid Preserve Thrust" in Galactic Racer HOSAS/HOTAS Binder, (C) 2026 JaffaQuake, GPLv3.

    Both forward: strongest wins, so steering costs no thrust. One back: it scrubs the other's thrust.
    Both back: average."""
    if left >= 0 and right >= 0:
        return max(left, right)
    if left <= 0 and right <= 0:
        return (left + right) / 2
    return max(left, right) * (1 - abs(min(left, right)))


def motion_raw(how, st):
    """Raw motion value from a device's latest ABS values; None when unknown (no data, IR dots out of sight)."""
    if how == "SHAKE":
        if not st:
            return None
        return abs(math.hypot(st.get(e.ABS_RX, 0), st.get(e.ABS_RY, 0), st.get(e.ABS_RZ, 0)) - GRAVITY)
    if how in ("IR_X", "IR_Y", "IR_DIST"):
        # Hidden dots read 1023 (x) or 767+ (y). Two dots: use their middle, so dot order does not matter.
        dots = [(st.get(x, 1023), st.get(y, 1023)) for x, y in IR_DOTS]
        seen = [d for d in dots if d[0] < 1023 and d[1] < 767]
        if how == "IR_DIST":  # dots grow apart as the Wiimote comes closer to the bar
            return math.dist(*seen) if len(seen) == 2 else None
        return sum(d[how == "IR_Y"] for d in seen) / len(seen) if seen else None
    return st.get(how)


class Imu:
    """MotionPlus + Wiimote accelerometer -> PITCH, ROLL, YAW (degrees) and RATE_* (deg/s), stored in the gyro's state.

    The gyro is clean but drifts. The accelerometer does not drift but reads every shake as tilt. So "up" follows the
    gyro and slowly leans to the accelerometer while that reads about 1 g. Yaw has no reference, so it slides back to 0."""

    def __init__(self):
        self.config({})
        self.bias = None    # gyro at rest as (pitch, roll, yaw); unknown until the first still moment
        self.up = None      # gravity direction in Wiimote axes (x, y = nose, z = top), length 1
        self.yaw = 0.0
        self.last = None
        self.window = []
        self.window_start = 0

    def config(self, c):
        self.signs = [-1 if c.get(f"invert_{k}", False) else 1 for k in ("pitch", "roll", "yaw")]
        self.accel_time, self.yaw_return = c.get("accel_time", 1.0), c.get("yaw_return", 3.0)

    def learn_bias(self, raw, a, now):
        if not self.window:
            self.window_start = now
        self.window.append(raw + a)
        if now - self.window_start >= STILL_TIME:
            cols = list(zip(*self.window))
            gyro, accel = cols[:3], cols[3:]
            # A steady turn can fool the gyro alone, but tilting moves the accelerometer.
            # ponytail: a perfectly steady flat turn still looks like rest; the next still moment fixes it.
            if (len(self.window) >= 10 and all(max(c) - min(c) < STILL_RANGE for c in gyro)
                    and all(max(c) - min(c) < STILL_ACCEL for c in accel)
                    and abs(math.hypot(*(c[-1] for c in accel)) - GRAVITY) < 25):
                self.bias = [sum(c) / len(c) for c in gyro]
            self.window = []

    def update(self, gyro, accel, now):
        raw = [gyro.get(c, 0) for c in (e.ABS_RZ, e.ABS_RY, e.ABS_RX)]
        a = [accel.get(c, 0) for c in (e.ABS_RX, e.ABS_RY, e.ABS_RZ)]
        self.learn_bias(raw, a, now)
        dt = min(now - self.last, 0.1) if self.last else 0
        self.last = now
        n = math.hypot(*a)
        if self.up is None:
            if not n:
                return
            self.up = [v / n for v in a]
        x, y, z = self.up
        p, r, w = [s * (v - b) / GYRO_UNITS for s, v, b in zip(self.signs, raw, self.bias)] if self.bias else (0, 0, 0)
        k = math.radians(dt)
        x, y, z = x + (r * z - w * y) * k, y + (p * z + w * x) * k, z - (r * x + p * y) * k
        if abs(n - GRAVITY) < 25:
            lean = dt / self.accel_time
            x, y, z = x + (a[0] / n - x) * lean, y + (a[1] / n - y) * lean, z + (a[2] / n - z) * lean
        length = math.hypot(x, y, z)
        self.up = [x / length, y / length, z / length]
        x, y, z = self.up
        gyro["PITCH"] = math.degrees(math.atan2(y, math.hypot(x, z)))
        gyro["ROLL"] = math.degrees(math.atan2(x, z))
        if self.bias:
            self.yaw += ((w * z + r * y - p * x) - self.yaw / self.yaw_return) * dt
            gyro.update(YAW=self.yaw, RATE_PITCH=p, RATE_ROLL=r, RATE_YAW=w)


class Motion:
    def __init__(self, m):
        self.device, self.how = MOTION_SOURCES[m["source"]]
        self.target = m["target"]
        if self.target not in MOTION_AXES + POD and e.ecodes[self.target] not in BUTTONS:
            sys.exit(f"config.toml: unknown motion target {self.target}")
        self.axis = Axis(m["center"], m["deadzone"], m["max"], m["invert"], False, m.get("anti_deadzone", 0))
        self.smooth = m["smoothing"]
        self.value = None
        self.pressed = False


def make_pad():
    stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
    trigger = AbsInfo(0, 0, 255, 0, 0, 0)
    hat = AbsInfo(0, -1, 1, 0, 0, 0)
    caps = {
        e.EV_KEY: BUTTONS,
        e.EV_ABS: [(a, stick) for a in STICK_AXES]
        + [(e.ABS_Z, trigger), (e.ABS_RZ, trigger), (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
        e.EV_FF: [e.FF_RUMBLE],
    }
    return UInput(caps, name="Microsoft X-Box 360 pad", bustype=e.BUS_USB,
                  vendor=0x045E, product=0x028E, version=0x0110, max_effects=16)


def make_mouse():
    return UInput({e.EV_KEY: MOUSE_KEYS, e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL]}, name="Wiimote Mouse")


def abs_values(dev):
    """Current value of every ABS axis; evdev only reports changes, so a still device sends nothing."""
    return {code: dev.absinfo(code).value for code in dev.capabilities(absinfo=False).get(e.EV_ABS, [])}


def inputs():
    """(path, name, owner) of every input device, read from sysfs because opening a Wiimote sensor switches it on.
    owner: Bluetooth address of the remote, or phys for test devices."""
    for node in Path("/sys/class/input").glob("event*"):
        try:
            name = (node / "device/name").read_text().strip()
            phys = (node / "device/phys").read_text().strip()
            parent = node / "device/device/uevent"
            uevent = parent.read_text() if parent.exists() else ""
        except OSError:
            continue
        owner = next((line[9:] for line in uevent.splitlines() if line.startswith("HID_UNIQ=")), phys)
        yield f"/dev/input/{node.name}", name, owner


def remotes():
    """Connected Wiimotes, sorted so each one keeps its place between sessions."""
    return sorted({owner for _, name, owner in inputs() if name == WIIMOTE})


def find(name, owner=None):
    for path, found, who in inputs():
        if found == name and owner in (None, who):
            try:
                return InputDevice(path)
            except OSError:
                continue


class Bridge:
    def __init__(self, path):
        self.reload = False
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "reload", True))  # first, so an early SIGHUP can't kill us
        self.path = path
        self.pad = make_pad()
        self.mouse = make_mouse()
        self.pointer = [0, 0]      # stick output, used as pointer speed in mouse mode
        self.rest = [0.0, 0.0]     # sub-pixel movement carried to the next tick
        self.wheel = {}            # held wheel target -> next scroll time
        self.last_tick = time.monotonic()
        self.devs = {}
        self.effects = {}   # virtual effect id -> (length s, has magnitude)
        self.playing = {}   # virtual effect id -> stop time
        self.wm_effects = {}  # remote -> its rumble effect id
        self.order = []     # connected remotes: first = WIIMOTE (right lever), second = WIIMOTE2 (left lever)
        self.buzzing = False
        self.imu = Imu()    # outlives reloads, so the gyro rest value stays known
        self.monitor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.monitor.setblocking(False)
        self.load()

    def load(self):
        cfg = tomllib.loads(self.path.read_text())
        s = cfg["stick"]
        self.stick = {
            e.ABS_HAT0X: (STICKS[s["target"]][0], Axis(s["center_x"], s["deadzone"], s["max"], s["invert_x"], anti=s.get("anti_deadzone", 0))),
            e.ABS_HAT0Y: (STICKS[s["target"]][1], Axis(s["center_y"], s["deadzone"], s["max"], s["invert_y"], anti=s.get("anti_deadzone", 0))),
        }
        self.buttons = {e.ecodes[src]: dst for src, dst in cfg["buttons"].items() if dst}
        self.left_buttons = {e.ecodes[src]: dst for src, dst in cfg.get("left_buttons", {}).items() if dst}
        for dst in [*self.buttons.values(), *self.left_buttons.values()]:
            if dst not in TRIGGERS and dst not in DPAD and e.ecodes[dst] not in BUTTONS:
                sys.exit(f"{self.path}: unknown target {dst}")
        self.motion_cfg = [m for m in cfg.get("motion", []) if m["enabled"]]
        self.two = cfg.get("two_wiimotes", {})
        self.build_motions()
        # latest ABS values of motion devices; read again from open ones, because a still device sends nothing
        self.state = {n: abs_values(self.devs[n]) if n in self.devs else {} for n in (ACCEL, ACCEL2, NUNCHUK, IR, MPLUS)}
        self.imu.config(cfg.get("motionplus", {}))
        self.rumble = cfg["rumble"]["enabled"]
        m = cfg.get("mouse", {})
        self.mouse_mode = m.get("enabled", False)
        self.toggle = e.ecodes[m["toggle"]] if m.get("toggle") else None
        self.speed, self.curve = m.get("speed", 1000), m.get("curve", 2.0)
        self.mouse_buttons = {e.ecodes[src]: dst for src, dst in cfg.get("mouse_buttons", {}).items() if dst}
        for dst in self.mouse_buttons.values():
            if dst not in WHEEL and e.ecodes[dst] not in MOUSE_KEYS:
                sys.exit(f"{self.path}: unknown mouse target {dst}")
        self.vals = {}  # pad axis -> {source: value}; the largest wins
        self.dpad = set()
        self.dirty = False  # skip SYN for reports that changed nothing
        needed = self.motion_devices()
        for name in [n for n in self.devs if n in (ACCEL, ACCEL2, WIIMOTE2, IR, MPLUS) and n not in needed]:
            self.devs.pop(name).close()
        self.reset()

    def build_motions(self):
        """Option on and two remotes connected: each remote is one lever, in place of the engines in the motion list."""
        cfg = self.motion_cfg
        self.levers = self.two.get("enabled", False) and len(self.order) >= 2
        if self.levers:
            t = self.two
            lever = {"deadzone": t["deadzone"], "max": t["max"], "invert": t["invert"], "smoothing": LEVER_SMOOTHING}
            cfg = [m for m in cfg if m["target"] not in POD] + [
                lever | {"source": "WIIMOTE_Y", "target": "POD_RIGHT", "center": t["center_right"]},
                lever | {"source": "WIIMOTE2_Y", "target": "POD_LEFT", "center": t["center_left"]}]
        self.motions = [Motion(m) for m in cfg]
        self.pod = {m.target: 0.0 for m in self.motions if m.target in POD}

    def motion_devices(self):
        needed = {m.device for m in self.motions}
        if MPLUS in needed:
            needed.add(ACCEL)  # the Imu also reads the accelerometer
        if ACCEL2 in needed:
            needed.add(WIIMOTE2)  # the left lever's buttons work too
        return needed

    def reset(self):
        for code in BUTTONS:
            self.pad.write(e.EV_KEY, code, 0)
        for code in ALL_AXES:
            self.pad.write(e.EV_ABS, code, 0)
        self.pad.syn()
        for code in MOUSE_KEYS:
            self.mouse.write(e.EV_KEY, code, 0)
        self.mouse.syn()
        self.vals.clear()
        self.dpad.clear()
        self.pointer = [0, 0]
        self.wheel.clear()
        self.pod = dict.fromkeys(self.pod, 0.0)  # a lost engine must not keep its last thrust
        for m in self.motions:
            m.pressed = False

    def scan(self):
        order = sorted(remotes(), reverse=self.two.get("swap", False))
        if order != self.order:  # a remote came or went, or left and right changed: every place is new
            for name in (WIIMOTE, WIIMOTE2, ACCEL, ACCEL2, IR, MPLUS):
                if name in self.devs:
                    self.drop(name)
            self.order = order
            self.build_motions()
        extra = tuple(self.motion_devices() & {ACCEL, ACCEL2, WIIMOTE2, IR, MPLUS})
        for name in (WIIMOTE, NUNCHUK) + extra:
            if name in self.devs:
                continue
            place = name.endswith(SECOND)
            if name != NUNCHUK and place >= len(self.order):
                continue
            # ponytail: the Nunchuk works on either remote, so any will do.
            if dev := find(name.removesuffix(SECOND), None if name == NUNCHUK else self.order[place]):
                # ponytail: accelerometer, IR and gyro not grabbed so the GUI can show them raw; they have no ABS_X
                # and no buttons, so games don't take them for a pad.
                if name in MONITORED:
                    dev.grab()
                self.devs[name] = dev
                if name in self.state:
                    self.state[name] = abs_values(dev)
                if name in (WIIMOTE, WIIMOTE2):
                    self.wm_effects[name] = dev.upload_effect(ff.Effect(
                        e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(0, 0),
                        ff.EffectType(ff_rumble_effect=ff.Rumble(0xFFFF, 0xFFFF))))
                    self.buzzing = False

    def drop(self, name):
        self.devs.pop(name).close()
        # ponytail: releases everything, not only what the lost device held; a held Wiimote button needs a re-press.
        self.reset()

    def axis(self, code, source, value):
        if code in TRIGGERS.values() and source != "button":
            value = max(0, value) * 255 // 32767
        self.vals.setdefault(code, {})[source] = value
        self.pad.write(e.EV_ABS, code, max(self.vals[code].values(), key=abs))

    def handle(self, ev, name):
        if ev.type == e.EV_SYN:
            if name == MPLUS:
                self.imu.update(self.state[MPLUS], self.state[ACCEL], ev.timestamp())
            if name in self.state and not self.mouse_mode:
                self.update_motion(name)
            if self.dirty:
                self.pad.syn()
                self.dirty = False
            return
        if ev.type == e.EV_ABS and name in self.state:
            self.state[name][ev.code] = ev.value
        if name not in MONITORED:
            return
        if ev.type in (e.EV_KEY, e.EV_ABS):
            self.tell_gui(MONITORED.index(name), ev.type, ev.code, ev.value)
        if ev.type == e.EV_KEY and ev.code == self.toggle:
            if ev.value == 1:
                self.mouse_mode = not self.mouse_mode
                self.reset()
            return
        if self.mouse_mode:
            return self.handle_mouse(ev)
        if ev.type == e.EV_ABS and ev.code in self.stick:
            code, axis = self.stick[ev.code]
            self.axis(code, "stick", axis(ev.value))
        elif ev.type == e.EV_KEY and ev.code in (buttons := self.left_buttons if name == WIIMOTE2 else self.buttons) and ev.value != 2:
            # ponytail: a target on both remotes is released by either one, even if the other still holds it.
            dst = buttons[ev.code]
            if dst in TRIGGERS:
                self.axis(TRIGGERS[dst], "button", 255 * ev.value)
            elif dst in DPAD:
                (self.dpad.add if ev.value else self.dpad.discard)(dst)
                code = DPAD[dst][0]
                self.pad.write(e.EV_ABS, code, sum(d for n, (c, d) in DPAD.items() if c == code and n in self.dpad))
            else:
                self.pad.write(e.EV_KEY, e.ecodes[dst], ev.value)
        else:
            return
        self.dirty = True

    def tell_gui(self, *values):
        try:
            self.monitor.sendto(RAW.pack(*values), MONITOR)
        except OSError:
            pass  # GUI not open

    def update_motion(self, name):
        for i, m in enumerate(self.motions):
            if m.device != name:
                continue
            raw = motion_raw(m.how, self.state[name])
            if raw is None:
                m.value, out = None, 0
            else:
                m.value = raw if m.value is None else m.value * m.smooth + raw * (1 - m.smooth)
                out = m.axis(m.value)
            if m.target in POD:
                self.pod[m.target] = out / 32767
            elif m.target in MOTION_AXES:
                self.axis(e.ecodes[m.target], f"motion{i}", out)
            elif (abs(out) > 16384) != m.pressed:
                # ponytail: shares the key with any real button on the same target; whichever releases last wins.
                m.pressed = not m.pressed
                self.pad.write(e.EV_KEY, e.ecodes[m.target], int(m.pressed))
            self.dirty = True
        if self.pod:
            left, right = self.pod.get("POD_LEFT", 0), self.pod.get("POD_RIGHT", 0)
            drive = pod_drive(left, right)
            self.axis(e.ABS_RZ, "pod", round(max(0, drive) * 32767))
            self.axis(e.ABS_Z, "pod", round(max(0, -drive) * 32767))
            # Left engine harder = turn right. With one engine lost there is no difference to read, so no turn.
            both = all(m.device in self.devs for m in self.motions if m.target in POD)
            self.axis(e.ABS_X, "pod", round(max(-1, min(1, left - right)) * 32767) if both else 0)
            if self.levers:
                for m in self.motions:
                    if m.target in POD:
                        side = POD.index(m.target)
                        self.tell_gui(ENGINES, e.EV_ABS, side, round(self.pod[m.target] * 100))
                        self.tell_gui(ENGINES, e.EV_ABS, side + 2, round(m.value or 0))

    def handle_mouse(self, ev):
        if ev.type == e.EV_ABS and ev.code in self.stick:
            self.pointer[ev.code == e.ABS_HAT0Y] = self.stick[ev.code][1](ev.value)
        elif ev.type == e.EV_KEY and ev.code in self.mouse_buttons and ev.value != 2:
            dst = self.mouse_buttons[ev.code]
            if dst in WHEEL:
                if ev.value:
                    self.wheel[dst] = 0  # 0 = first scroll, due now
                else:
                    self.wheel.pop(dst, None)
            else:
                self.mouse.write(e.EV_KEY, e.ecodes[dst], ev.value)
                self.mouse.syn()

    def move(self, now):
        dt = min(now - self.last_tick, 0.05)
        self.last_tick = now
        moved = False
        for i, rel in enumerate((e.REL_X, e.REL_Y)):
            if self.pointer[i]:
                f = self.pointer[i] / 32767
                self.rest[i] += math.copysign(abs(f) ** self.curve, f) * self.speed * dt
                step = int(self.rest[i])
                self.rest[i] -= step
                if step:
                    self.mouse.write(e.EV_REL, rel, step)
                    moved = True
        for dst, due in list(self.wheel.items()):
            if now >= due:
                self.mouse.write(e.EV_REL, e.REL_WHEEL, WHEEL[dst])
                self.wheel[dst] = now + (WHEEL_DELAY if due == 0 else WHEEL_REPEAT)
                moved = True
        if moved:
            self.mouse.syn()

    def handle_ff(self, ev):
        if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
            up = self.pad.begin_upload(ev.value)
            r = up.effect.u.ff_rumble_effect
            self.effects[up.effect.id] = (up.effect.ff_replay.length / 1000, bool(r.strong_magnitude or r.weak_magnitude))
            up.retval = 0
            self.pad.end_upload(up)
        elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
            er = self.pad.begin_erase(ev.value)
            self.effects.pop(er.effect_id, None)
            self.playing.pop(er.effect_id, None)
            er.retval = 0
            self.pad.end_erase(er)
        elif ev.type == e.EV_FF and ev.code in self.effects:
            length, _ = self.effects[ev.code]
            if ev.value:
                self.playing[ev.code] = time.monotonic() + length if length else float("inf")
            else:
                self.playing.pop(ev.code, None)

    def update_rumble(self, now):
        self.playing = {i: t for i, t in self.playing.items() if t > now}
        want = self.rumble and any(self.effects[i][1] for i in self.playing)
        if want != self.buzzing:
            for name, effect in self.wm_effects.items():
                if name in self.devs:
                    self.devs[name].write(e.EV_FF, effect, int(want))
            self.buzzing = want

    def run(self):
        next_scan = 0
        while True:
            if self.reload:
                self.reload = False
                self.load()
            now = time.monotonic()
            if now >= next_scan:
                self.scan()
                next_scan = now + 1
            wake = min([next_scan, *self.playing.values()])
            if self.mouse_mode and (any(self.pointer) or self.wheel):
                wake = min(wake, now + TICK)
            fds = {self.pad.fd: None} | {d.fd: name for name, d in self.devs.items()}
            ready, _, _ = select.select(list(fds), [], [], max(0, wake - now))
            for fd in ready:
                name = fds[fd]
                if name is None:
                    for ev in self.pad.read():
                        self.handle_ff(ev)
                    continue
                if name not in self.devs:
                    continue
                try:
                    for ev in self.devs[name].read():
                        self.handle(ev, name)
                except OSError:
                    self.drop(name)
            if self.mouse_mode:
                self.move(time.monotonic())
            self.update_rumble(time.monotonic())


if __name__ == "__main__":
    try:
        Bridge(Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG).run()
    except KeyboardInterrupt:
        pass
