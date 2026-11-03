#!/usr/bin/env python3
"""Wiimote + Nunchuk -> virtual Xbox 360 pad (uinput). Usage: wiimote2xbox.py [config.toml]
SIGHUP reloads the config without recreating the pad.
Raw Wiimote/Nunchuk presses are also sent to MONITOR, where the GUI listens.
Mouse mode sends the buttons and either the aim of the Wiimote or the Nunchuk stick to a virtual mouse instead of the pad."""
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
MOUSE_DEFAULT = {"enabled": False, "toggle": "BTN_MODE", "speed": 1000, "curve": 2.0, "pointer": "WIIMOTE",
                 "use_motionplus": False, "use_ir": False, "aim_speed": 40, "tilt_deadzone": 6, "tilt_max": 40,
                 "cutoff": 1.0, "beta": 0.1, "tighten": 3.0, "click_lock": 0.12,
                 "invert_x": False, "invert_y": False, "ir_invert_x": False, "ir_invert_y": False}
GAMEPLAY_DEFAULT = {"filter": True, "cutoff": 1.0, "beta": 0.01, "shake_limit": 50, "button_hold": 0.2,
                    "steer_curve": 1.5, "recenter": "BTN_2"}
GRAVITY_OF = {ACCEL: GRAVITY, ACCEL2: GRAVITY, NUNCHUK: 190}  # accelerometer length at rest; the Nunchuk reads about 190
SHAKE_HOLD = 0.15  # s the tilt stays held after a shake; a shake crosses 1 g on its way, and those moments are not tilt
MOUSE_CLICKS = ("BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE")
IR_CENTER = (512, 384)
IR_DEGREES = 33 / 1024  # the camera sees about 33 degrees across its 1024 units
IR_TIME = 0.5           # s for the IR camera to take out the gyro's drift


class Axis:
    """Raw value -> -32767..32767 with center, deadzone and (optionally self-growing) range per side.

    anti (percent): output starts at this value just past the deadzone, so it clears the game's own deadzone
    (x360ce AntiDeadZone).
    curve: above 1, small moves give less output and the full range stays the same."""

    def __init__(self, center, deadzone, maximum, invert, grow=True, anti=0, curve=1):
        self.center, self.deadzone, self.invert, self.grow, self.curve = center, deadzone, invert, grow, curve
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
        frac = min(1, (a - self.deadzone) / (self.max[side] - self.deadzone)) ** self.curve
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


def pod_turn(left, right, span, curve):
    """Engine difference -> steering -1..1, + = right (left engine harder). span: the difference that gives full lock."""
    turn = max(-1, min(1, (left - right) / span))
    return math.copysign(abs(turn) ** curve, turn)


def ir_seen(st):
    """IR dots in sight. Hidden dots read 1023 (x) or 767+ (y)."""
    dots = [(st.get(x, 1023), st.get(y, 1023)) for x, y in IR_DOTS]
    return [d for d in dots if d[0] < 1023 and d[1] < 767]


def motion_raw(how, st):
    """Raw motion value from a device's latest ABS values; None when unknown (no data, IR dots out of sight)."""
    if how == "SHAKE":
        if not st:
            return None
        return abs(math.hypot(st.get(e.ABS_RX, 0), st.get(e.ABS_RY, 0), st.get(e.ABS_RZ, 0)) - GRAVITY)
    if how in ("IR_X", "IR_Y", "IR_DIST"):
        # Two dots: use their middle, so dot order does not matter.
        seen = ir_seen(st)
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
            # AIM: turn rates as the screen sees them (+ = right, up), so a Wiimote held at an angle still aims true.
            gyro.update(YAW=self.yaw, RATE_PITCH=p, RATE_ROLL=r, RATE_YAW=w,
                        AIM_X=w * z + r * y - p * x, AIM_Y=(p * z + w * x) / (math.hypot(x, z) or 1))


def lowpass(cutoff, dt):
    return dt / (dt + 1 / (2 * math.pi * cutoff))


class OneEuro:
    """1 euro filter (Casiez, Roussel, Vogel, CHI 2012): a low-pass whose cutoff rises with speed.
    Slow hand: heavy smoothing, no jitter. Fast hand: light smoothing, no lag."""

    def __init__(self, cutoff, beta):
        self.cutoff, self.beta = cutoff, beta
        self.x, self.speed = None, 0.0

    def __call__(self, x, dt):
        if self.x is None:
            self.x = x
        if dt > 0:
            self.speed += lowpass(1.0, dt) * ((x - self.x) / dt - self.speed)
            self.x += lowpass(self.cutoff + self.beta * abs(self.speed), dt) * (x - self.x)
        return self.x


class Aim:
    """Wiimote sensors -> pointer movement.

    Gyro and IR: angle (gyro, with IR taking out its drift) -> 1 euro filter -> tightening -> click lock -> degrees (+ = right, up).
    Tilt only: angle from where the Wiimote was when mouse mode began -> 1 euro filter -> Axis -> pointer speed.
    The virtual mouse is relative, so a lost IR dot or a new one moves the reference, and the pointer never jumps."""

    def __init__(self):
        self.config(MOUSE_DEFAULT)

    def config(self, m):
        self.m = m
        self.tilt_axis = Axis(0, m["tilt_deadzone"], m["tilt_max"], False, False)
        self.reset()

    def reset(self):
        self.filters = [OneEuro(self.m["cutoff"], self.m["beta"]) for _ in "xy"]
        self.angle, self.out = [0.0, 0.0], [0.0, 0.0]
        self.last = None
        self.lock = 0
        self.ir_offset, self.ir_dots, self.ir_roll = None, 0, None
        self.center, self.speed = None, [0, 0]
        self.shake_start = None

    def tick(self, now):
        dt = min(now - self.last, 0.1) if self.last else 0
        self.last = now
        return dt

    def click(self, now):
        """A press jerks the hand (the "Heisenberg effect"), so the pointer holds still for a moment."""
        self.lock = now + self.m["click_lock"]

    def camera(self, st, upside_down):
        """IR dots -> (x, y, dots in sight), with x, y in degrees of aim and the Wiimote's roll taken out; None if hidden."""
        seen = ir_seen(st)
        if not seen:
            return None
        if self.ir_roll is None:
            self.ir_roll = math.pi if upside_down else 0.0
        if len(seen) == 2:
            (ax, ay), (bx, by) = seen
            roll = math.atan2(by - ay, bx - ax)
            # The line between the dots has two directions: the one nearest the last roll is the right one.
            if abs((roll - self.ir_roll + math.pi) % math.tau - math.pi) > math.pi / 2:
                roll += math.pi
            self.ir_roll = roll
        x = sum(d[0] for d in seen) / len(seen) - IR_CENTER[0]
        y = sum(d[1] for d in seen) / len(seen) - IR_CENTER[1]
        c, s = math.cos(self.ir_roll), math.sin(self.ir_roll)
        # The dots move against the aim: aiming right moves them left.
        return ((-1 if not self.m["ir_invert_x"] else 1) * (x * c + y * s) * IR_DEGREES,
                (1 if not self.m["ir_invert_y"] else -1) * (y * c - x * s) * IR_DEGREES, len(seen))

    def step(self, now, rate=None, ir=None):
        """One sensor report: rate = gyro (x, y) in deg/s or None, ir = camera() result. Returns (x, y) in degrees."""
        dt = self.tick(now)
        if ir is None or ir[2] != self.ir_dots:
            self.ir_offset = None
        self.ir_dots = ir[2] if ir else 0
        angle = [a + r * dt for a, r in zip(self.angle, rate or (0, 0))]
        if ir:
            if self.ir_offset is None:
                self.ir_offset = [a - c for a, c in zip(angle, ir)]
            pull = min(1, dt / IR_TIME) if rate else 1
            angle = [a + (c + o - a) * pull for a, c, o in zip(angle, ir, self.ir_offset)]
        self.angle = angle
        smooth = [f(a, dt) for f, a in zip(self.filters, angle)]
        move = [s - o for s, o in zip(smooth, self.out)]
        self.out = smooth
        if now < self.lock or not dt:
            return 0, 0
        # Tightening (GyroWiki): slow turns count for less, so a shaking hand holds still. It adds no lag.
        t = self.m["tighten"]
        scale = min(1, math.hypot(*move) / dt / t) if t else 1
        return move[0] * scale, move[1] * scale

    def tilt(self, st, now):
        """Accelerometer -> pointer speed (x, y) as -32767..32767, + = right, down. A shake keeps the last speed."""
        x, y, z = (st.get(c, 0) for c in (e.ABS_RX, e.ABS_RY, e.ABS_RZ))
        if abs(math.hypot(x, y, z) - GRAVITY) >= 25:
            # A sensor with an offset stays past the limit at rest, so only a short time counts as a shake.
            self.shake_start = self.shake_start or now
            if now - self.shake_start < SHAKE_HOLD:
                return self.speed
        else:
            self.shake_start = None
        dt = self.tick(now)
        angles =math.degrees(math.atan2(x, z)), math.degrees(math.atan2(y, math.hypot(x, z)))
        if self.center is None:
            self.center = angles
        self.speed = [self.tilt_axis(f(a - c, dt)) * s for f, a, c, s in zip(self.filters, angles, self.center, (1, -1))]
        return (0, 0) if now < self.lock else self.speed


class Motion:
    def __init__(self, m, play):
        self.device, self.how = MOTION_SOURCES[m["source"]]
        self.target = m["target"]
        if self.target not in MOTION_AXES + POD and e.ecodes[self.target] not in BUTTONS:
            sys.exit(f"config.toml: unknown motion target {self.target}")
        self.tilt = self.device in GRAVITY_OF and self.how != "SHAKE"
        self.axis = Axis(m["center"], m["deadzone"], m["max"], m["invert"], False, m.get("anti_deadzone", 0),
                         play["steer_curve"] if self.target == "ABS_X" else 1)
        self.smooth = m["smoothing"]
        # Smoothing 0 means a quick strike or shake, so no filter there either.
        self.filter = OneEuro(play["cutoff"], play["beta"]) if play["filter"] and self.smooth else None
        self.value = None
        self.last = None    # time of the last value
        self.pressed = False
        self.active = 0     # button targets: last time the motion was past its threshold


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
        self.aim = Aim()
        self.next_scan = 0
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
        self.play = GAMEPLAY_DEFAULT | cfg.get("gameplay", {})
        self.recenter = e.ecodes[self.play["recenter"]] if self.play["recenter"] else None
        self.shaken = {}  # device -> time until its tilt counts again
        self.build_motions()
        # latest ABS values of motion devices; read again from open ones, because a still device sends nothing
        self.state = {n: abs_values(self.devs[n]) if n in self.devs else {} for n in (ACCEL, ACCEL2, NUNCHUK, IR, MPLUS)}
        self.imu.config(cfg.get("motionplus", {}))
        self.rumble = cfg["rumble"]["enabled"]
        m = MOUSE_DEFAULT | cfg.get("mouse", {})
        self.mouse_mode = m["enabled"]
        self.toggle = e.ecodes[m["toggle"]] if m["toggle"] else None
        self.speed, self.curve = m["speed"], m["curve"]
        self.aiming = m["pointer"] == "WIIMOTE"
        self.use_mplus, self.use_ir = m["use_motionplus"], m["use_ir"]
        self.aim_speed = m["aim_speed"]
        self.flip = (-1 if m["invert_x"] else 1, -1 if m["invert_y"] else 1)
        self.aim.config(m)
        self.mouse_buttons = {e.ecodes[src]: dst for src, dst in cfg.get("mouse_buttons", {}).items() if dst}
        for dst in self.mouse_buttons.values():
            if dst not in WHEEL and e.ecodes[dst] not in MOUSE_KEYS:
                sys.exit(f"{self.path}: unknown mouse target {dst}")
        self.vals = {}  # pad axis -> {source: value}; the largest wins
        self.dpad = set()
        self.dirty = False  # skip SYN for reports that changed nothing
        self.released = cfg.get("mode", {}).get("active") == "DOLPHIN"  # hands the Wiimotes over to Dolphin
        self.close_unused()
        if self.released:
            for name in list(self.devs):
                self.devs.pop(name).close()
        self.reset()

    def close_unused(self):
        needed = self.motion_devices()
        for name in [n for n in self.devs if n in (ACCEL, ACCEL2, WIIMOTE2, IR, MPLUS) and n not in needed]:
            self.devs.pop(name).close()

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
        self.motions = [Motion(m, self.play) for m in cfg]
        self.pod = {m.target: 0.0 for m in self.motions if m.target in POD}

    def motion_devices(self):
        needed = {m.device for m in self.motions}
        if self.mouse_mode and self.aiming:  # only while aiming: an open sensor stays on and uses the battery
            needed |= {ACCEL} | ({MPLUS} if self.use_mplus else set()) | ({IR} if self.use_ir else set())
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
        if self.released:
            return
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
            if self.mouse_mode:
                if self.aiming:
                    self.update_aim(name, ev.timestamp())
            elif name in self.state:
                self.update_motion(name, ev.timestamp())
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
                self.aim.reset()
                self.close_unused()
                self.next_scan = 0  # open the sensors for aiming now
            return
        if ev.type == e.EV_KEY and ev.code == self.recenter and not self.mouse_mode and name != WIIMOTE2:  # no Nunchuk shake with two remotes: the left one keeps its Y
            if ev.value == 1:  # the hand's position now is the new center, until the next reload
                for m in self.motions:
                    if (m.tilt or m.how in ("PITCH", "ROLL")) and m.value is not None:
                        m.axis.center = m.value
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

    def update_motion(self, name, now):
        limit, st = self.play["shake_limit"], self.state[name]
        if limit and st and (g := GRAVITY_OF.get(name)):
            if abs(math.hypot(st.get(e.ABS_RX, 0), st.get(e.ABS_RY, 0), st.get(e.ABS_RZ, 0)) - g) > g * limit / 100:
                self.shaken[name] = now + SHAKE_HOLD
        calm = now >= self.shaken.get(name, 0)
        for i, m in enumerate(self.motions):
            if m.device != name:
                continue
            raw = motion_raw(m.how, st)
            if raw is None:
                m.value, out = None, 0
                if m.filter:
                    m.filter.x = None
            else:
                if calm or not m.tilt:  # during a shake a tilt keeps its last value
                    dt = min(now - m.last, 0.1) if m.last else 0
                    m.last = now
                    if m.filter:
                        m.value = m.filter(raw, dt)
                    else:
                        m.value = raw if m.value is None else m.value * m.smooth + raw * (1 - m.smooth)
                out = m.axis(m.value) if m.value is not None else 0
            if m.target in POD:
                self.pod[m.target] = out / 32767
            elif m.target in MOTION_AXES:
                self.axis(e.ecodes[m.target], f"motion{i}", out)
            elif self.play["button_hold"]:
                # One shake crosses the threshold many times, so the button stays down until the motion ends.
                if abs(out) > 16384:
                    m.active = now
                    if not m.pressed:
                        m.pressed = True
                        self.pad.write(e.EV_KEY, e.ecodes[m.target], 1)
            elif (abs(out) > 16384) != m.pressed:
                # ponytail: shares the key with any real button on the same target; whichever releases last wins.
                m.pressed = not m.pressed
                self.pad.write(e.EV_KEY, e.ecodes[m.target], int(m.pressed))
            self.dirty = True
        self.release_motions(now)
        if self.pod:
            left, right = self.pod.get("POD_LEFT", 0), self.pod.get("POD_RIGHT", 0)
            drive = pod_drive(left, right)
            self.axis(e.ABS_RZ, "pod", round(max(0, drive) * 32767))
            self.axis(e.ABS_Z, "pod", round(max(0, -drive) * 32767))
            # Left engine harder = turn right. With one engine lost there is no difference to read, so no turn.
            both = all(m.device in self.devs for m in self.motions if m.target in POD)
            span = self.two.get("steer_range", 1) if self.levers else 1
            self.axis(e.ABS_X, "pod", round(pod_turn(left, right, span, self.play["steer_curve"]) * 32767) if both else 0)
            if self.levers:
                for m in self.motions:
                    if m.target in POD:
                        side = POD.index(m.target)
                        self.tell_gui(ENGINES, e.EV_ABS, side, round(self.pod[m.target] * 100))
                        self.tell_gui(ENGINES, e.EV_ABS, side + 2, round(m.value or 0))

    def release_motions(self, now):
        hold = self.play["button_hold"]
        for m in self.motions:
            if hold and m.pressed and now - m.active >= hold:
                m.pressed = False
                self.pad.write(e.EV_KEY, e.ecodes[m.target], 0)
                self.dirty = True

    def update_aim(self, name, now):
        """The best sensor that is switched on moves the pointer: gyro (IR corrects it), else IR, else tilt."""
        gyro = self.use_mplus and MPLUS in self.devs
        if gyro and name == MPLUS:
            st = self.state[MPLUS]
            if "AIM_X" not in st:  # the gyro does not know its rest value yet
                return
            moved = self.aim.step(now, (st["AIM_X"], st["AIM_Y"]), self.read_ir())
        elif not gyro and self.use_ir and name == IR:
            moved = self.aim.step(now, ir=self.read_ir())
        elif not gyro and not self.use_ir and name == ACCEL:
            speed = self.aim.tilt(self.state[ACCEL], now)
            self.pointer = [v * s for v, s in zip(speed, self.flip)]
            return
        else:
            return
        x, y = moved
        if self.nudge(0, x * self.flip[0] * self.aim_speed) | self.nudge(1, -y * self.flip[1] * self.aim_speed):
            self.mouse.syn()

    def read_ir(self):
        if self.use_ir and IR in self.devs:
            return self.aim.camera(self.state[IR], self.state[ACCEL].get(e.ABS_RZ, 1) < 0)

    def handle_mouse(self, ev):
        if ev.type == e.EV_ABS and ev.code in self.stick:
            if not self.aiming:
                self.pointer[ev.code == e.ABS_HAT0Y] = self.stick[ev.code][1](ev.value)
        elif ev.type == e.EV_KEY and ev.code in self.mouse_buttons and ev.value != 2:
            dst = self.mouse_buttons[ev.code]
            if dst in MOUSE_CLICKS:
                self.aim.click(ev.timestamp())
            if dst in WHEEL:
                if ev.value:
                    self.wheel[dst] = 0  # 0 = first scroll, due now
                else:
                    self.wheel.pop(dst, None)
            else:
                self.mouse.write(e.EV_KEY, e.ecodes[dst], ev.value)
                self.mouse.syn()

    def nudge(self, i, pixels):
        """Moves the pointer on axis i; the part below one pixel waits for the next move. True if it moved."""
        self.rest[i] += pixels
        step = int(self.rest[i])
        self.rest[i] -= step
        if step:
            self.mouse.write(e.EV_REL, (e.REL_X, e.REL_Y)[i], step)
        return bool(step)

    def move(self, now):
        dt = min(now - self.last_tick, 0.05)
        self.last_tick = now
        moved = False
        for i in range(2):
            if self.pointer[i]:
                f = self.pointer[i] / 32767
                moved |= self.nudge(i, math.copysign(abs(f) ** self.curve, f) * self.speed * dt)
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
        while True:
            if self.reload:
                self.reload = False
                self.load()
            now = time.monotonic()
            if now >= self.next_scan:
                self.scan()
                self.next_scan = now + 1
            wake = min([self.next_scan, *self.playing.values()])
            if self.mouse_mode and (any(self.pointer) or self.wheel):
                wake = min(wake, now + TICK)
            if any(m.pressed for m in self.motions):  # a still sensor sends nothing, but the button must still let go
                wake = min(wake, now + 5 * TICK)
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
            self.release_motions(time.time())  # event times use the wall clock
            if self.dirty:
                self.pad.syn()
                self.dirty = False
            self.update_rumble(time.monotonic())


if __name__ == "__main__":
    try:
        Bridge(Path(sys.argv[1]) if len(sys.argv) > 1 else CONFIG).run()
    except KeyboardInterrupt:
        pass
