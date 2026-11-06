"""Mouse mode with simulated sensors. Needs no real device and leaves the running daemon alone."""
import math
import re
import random
import tempfile
from pathlib import Path
from types import SimpleNamespace

from evdev import ecodes as e

import wiimote2xbox as w
from wiimote2xbox import ACCEL, GYRO_UNITS, IR, MPLUS, NUNCHUK, WIIMOTE, Aim, Bridge, OneEuro


class Fake:
    """Stands in for a UInput device and adds up what it receives."""
    fd = -1

    def __init__(self):
        self.rel = {e.REL_X: 0, e.REL_Y: 0}
        self.keys, self.abs, self.presses = {}, {}, 0

    def write(self, typ, code, value):
        if typ == e.EV_REL:
            self.rel[code] = self.rel.get(code, 0) + value
        elif typ == e.EV_KEY:
            self.presses += value and not self.keys.get(code, 0)
            self.keys[code] = value
        elif typ == e.EV_ABS:
            self.abs[code] = value

    def syn(self):
        pass


def bridge(**mouse):
    w.make_pad, w.make_mouse = Fake, Fake
    text = Path(w.CONFIG).read_text()
    base = re.sub(r"invert_(pitch|roll|yaw) = true", r"invert_\1 = false", text.split("[mouse]")[0]) + "[mouse]\nenabled = true\ntoggle = \"BTN_MODE\"\n"  # tests assume no invert
    extra = "".join(f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}\n".replace("'", '"') for k, v in mouse.items())
    tail = "\n[mouse_buttons]\nBTN_SOUTH = \"BTN_LEFT\"\nKEY_UP = \"WHEEL_UP\"\n"
    path = Path(tempfile.mkstemp(suffix=".toml")[1])
    path.write_text(base + extra + tail)
    b = Bridge(path)
    path.unlink()
    return b


def event(typ, code, value, t):
    return SimpleNamespace(type=typ, code=code, value=value, timestamp=lambda: t)


def report(b, name, values, t):
    for code, v in values.items():
        b.handle(event(e.EV_ABS, code, v, t), name)
    b.handle(event(e.EV_SYN, 0, 0, t), name)


def flat(pitch=0.0, roll=0.0):
    r, p = math.radians(roll), math.radians(pitch)
    return {e.ABS_RX: 100 * math.sin(r) * math.cos(p), e.ABS_RY: 100 * math.sin(p), e.ABS_RZ: 100 * math.cos(r) * math.cos(p)}


def gyro(p=0.0, r=0.0, y=0.0):
    return {e.ABS_RZ: p * GYRO_UNITS, e.ABS_RY: r * GYRO_UNITS, e.ABS_RX: y * GYRO_UNITS}


def dots(a, b=None):
    return {e.ABS_HAT0X: a[0], e.ABS_HAT0Y: a[1], e.ABS_HAT1X: b[0] if b else 1023, e.ABS_HAT1Y: b[1] if b else 1023}


# 1 euro filter: still jitter goes away, a fast move has little lag.
random.seed(1)
f, t = OneEuro(1.0, 0.1), []
for i in range(300):
    t.append(f(random.uniform(-0.5, 0.5), 0.01))
assert max(t[100:]) - min(t[100:]) < 0.3
f = OneEuro(1.0, 0.1)
for i in range(50):
    out = f(i * 3.0, 0.01)  # 300 deg/s
assert 147 - out < 6

# Aim: tightening stops a small tremor, a real turn passes.
a = Aim()
tremor = [a.step(i / 100, (1.0 * math.sin(i), 0)) for i in range(1, 100)]
a.reset()
turn = [a.step(i / 100, (100.0, 0)) for i in range(1, 100)]
assert sum(abs(m[0]) for m in tremor) < 0.05 and 95 < sum(m[0] for m in turn) < 98.5  # 100 deg/s x 0.98 s

# Aim: IR corrects gyro drift. The gyro says 2 deg/s, the camera says still.
a = Aim()
a.m = a.m | {"tighten": 0}
ir = (1.0, 2.0, 2)
moved = sum(a.step(i / 100, (2.0, 0), ir)[0] for i in range(1, 1000))
free = Aim()
free.m = free.m | {"tighten": 0}
assert abs(moved) < 2 and sum(free.step(i / 100, (2.0, 0))[0] for i in range(1, 1000)) > 19

# Aim: the camera undoes roll. A Wiimote rolled 90 degrees and turned right still moves right, by the same amount.
a = Aim()
level = a.camera(dots((412, 384), (612, 384)), False)
moved_level = a.camera(dots((312, 384), (512, 384)), False)
b_ = Aim()
rolled = b_.camera(dots((512, 284), (512, 484)), False)
moved_rolled = b_.camera(dots((512, 184), (512, 384)), False)
assert moved_level[0] > level[0] and abs((moved_level[0] - level[0]) - (moved_rolled[0] - rolled[0])) < 1e-9
assert abs(moved_rolled[1] - rolled[1]) < 1e-9
assert a.camera(dots((1023, 1023)), False) is None

# Stick mode: the stick moves the pointer and the Wiimote does not.
b = bridge(pointer="STICK")
assert ACCEL not in b.motion_devices() - {m.device for m in b.motions}
report(b, NUNCHUK, {e.ABS_HAT0X: 88}, 1.0)
assert b.pointer[0] == 32767
b.pointer = [0, 0]
report(b, ACCEL, flat(roll=30), 1.1)
assert b.pointer == [0, 0]

# Tilt: the first position is the center; tilting past the deadzone moves the pointer, a shake does not.
b = bridge(pointer="WIIMOTE")
assert b.aiming and ACCEL in b.motion_devices() and IR not in b.motion_devices() and MPLUS not in b.motion_devices()
b.devs[ACCEL] = None
report(b, NUNCHUK, {e.ABS_HAT0X: 88}, 0.5)
assert b.pointer == [0, 0]
report(b, ACCEL, flat(pitch=20), 1.0)
assert b.pointer == [0, 0]
t = 1.0
for i in range(100):
    t += 0.01
    report(b, ACCEL, flat(pitch=20, roll=30), t)
assert b.pointer[0] > 15000 and abs(b.pointer[1]) < 500
speed = list(b.pointer)
report(b, ACCEL, {e.ABS_RX: 0, e.ABS_RY: 400, e.ABS_RZ: 0}, t + 0.01)
assert b.pointer == speed
for i in range(100):
    t += 0.01
    report(b, ACCEL, flat(pitch=40), t)
assert b.pointer[1] < -10000  # nose up = pointer up

# Lever steering: span = the engine difference for full lock; a wider span gives finer control.
assert w.pod_turn(0.5, 0, 1, 1) == 0.5 and w.pod_turn(0, 0.5, 1, 1) == -0.5
assert w.pod_turn(0.75, 0, 1.5, 1) == 0.5 and w.pod_turn(1, -0.5, 1.5, 1) == 1 and w.pod_turn(1, -1, 1.5, 1) == 1
assert abs(w.pod_turn(0.25, 0, 1, 2) - 0.0625) < 1e-9 and w.pod_turn(0, 0, 1.5, 1) == 0

# A real remote (00:1A:E9:5F:FC:D8) reads about 125 at rest: a held tilt stays past the shake limit and must not freeze the pointer.
biased = lambda **pose: {c: v + o for (c, v), o in zip(sorted(flat(**pose).items()), (35, 19, 18))}
a, t = Aim(), 0.0
a.tilt(biased(), t)
for i in range(200):
    t += 0.01
    speed = a.tilt(biased(roll=20), t)
assert speed[0] > 5000
for i in range(200):
    t += 0.01
    speed = a.tilt(biased(), t)
assert abs(speed[0]) < 500

# Click lock: a press stops the pointer for a moment. Tilt returns to speed after it.
b.handle(event(e.EV_KEY, e.BTN_SOUTH, 1, t), WIIMOTE)
report(b, ACCEL, flat(pitch=40), t + 0.01)
assert b.pointer == [0, 0] and b.mouse.keys[e.BTN_LEFT] == 1
report(b, ACCEL, flat(pitch=40), t + 0.2)
assert b.pointer[1] < -10000

# MotionPlus: learns its rest, then a turn moves the pointer. Rolled 90 degrees, a turn still moves it the same way.
b = bridge(use_motionplus=True, tighten=0.0, click_lock=0.0)
assert MPLUS in b.motion_devices()
b.devs |= {MPLUS: None, ACCEL: None}
b.state[ACCEL] = flat()
t = 1.0
for i in range(100):
    t += 0.01
    report(b, MPLUS, gyro(), t)
assert b.mouse.rel[e.REL_X] == 0
for i in range(100):
    t += 0.01
    report(b, MPLUS, gyro(y=60 + 10 * math.sin(i)), t)
assert 1800 < b.mouse.rel[e.REL_X] < 2500 and abs(b.mouse.rel[e.REL_Y]) < 50  # 40 px/deg x about 60 deg
for i in range(100):
    t += 0.01
    report(b, MPLUS, gyro(p=30 + 10 * math.sin(i)), t)
assert b.mouse.rel[e.REL_Y] < -800  # nose up = pointer up
b = bridge(use_motionplus=True, tighten=0.0, click_lock=0.0, invert_x=True)
b.devs |= {MPLUS: None, ACCEL: None}
b.state[ACCEL] = flat(roll=90)
for i in range(100):
    t += 0.01
    report(b, MPLUS, gyro(), t)
for i in range(100):
    t += 0.01
    report(b, MPLUS, gyro(p=-60 + 10 * math.sin(i)), t)  # about gravity, with the Wiimote on its side
assert b.mouse.rel[e.REL_X] < -1800 and abs(b.mouse.rel[e.REL_Y]) < 50  # invert_x flips it

# IR alone: aiming right moves right; dots lost and back again do not jump; one dot lost does not jump.
b = bridge(use_ir=True, tighten=0.0, click_lock=0.0)
assert IR in b.motion_devices()
b.devs |= {IR: None, ACCEL: None}
b.state[ACCEL] = flat()
t = 1.0
x = 412.0
for i in range(100):
    t += 0.01
    report(b, IR, dots((x, 384), (x + 200, 384)), t)
assert b.mouse.rel[e.REL_X] == 0
for i in range(60):
    t += 0.01
    x -= 2
    report(b, IR, dots((x, 384), (x + 200, 384)), t)
for i in range(60):
    t += 0.01
    report(b, IR, dots((x, 384), (x + 200, 384)), t)
right = b.mouse.rel[e.REL_X]
assert 120 < right < 170  # 120 units x 33/1024 deg x 40 px/deg = about 155
report(b, IR, dots((1023, 1023)), t + 0.01)
report(b, IR, dots((100, 100), (300, 100)), t + 0.5)  # back in sight, far away
t += 0.5
for i in range(30):
    t += 0.01
    report(b, IR, dots((100, 100), (300, 100)), t)
report(b, IR, dots((100, 100)), t + 0.01)             # one dot lost: the middle moves 100 units, the pointer stays
assert abs(b.mouse.rel[e.REL_X] - right) < 4 and abs(b.mouse.rel[e.REL_Y]) < 4

# Toggle: leaving mouse mode closes the sensors only aiming needed. Reports then go to the pad, not the mouse.
b = bridge(use_ir=True)
closed = []
b.devs[IR] = SimpleNamespace(close=lambda: closed.append(IR))
b.handle(event(e.EV_KEY, b.toggle, 1, 1.0), WIIMOTE)
assert not b.mouse_mode and closed == [IR] and IR not in b.devs and b.next_scan == 0


# Gameplay. Steering by Nunchuk tilt, thrust by Wiimote tilt, and shake buttons.
def game(**play):
    w.make_pad, w.make_mouse = Fake, Fake
    motion = ('[[motion]]\nenabled = true\nsource = "{}"\ntarget = "{}"\ncenter = 0\ndeadzone = {}\nmax = {}\n'
              'anti_deadzone = {}\ninvert = false\nsmoothing = {}\n')
    text = ('[stick]\ntarget = "LEFT"\ncenter_x = 0\ncenter_y = 0\ndeadzone = 8\nmax = 82\ninvert_x = false\ninvert_y = false\n'
            '[buttons]\nBTN_2 = "BTN_Y"\nBTN_SOUTH = "BTN_SOUTH"\n[left_buttons]\nBTN_2 = "BTN_Y"\n[rumble]\nenabled = false\n'
            + motion.format("NUNCHUK_X", "ABS_X", 15, 120, 0, 0.7)
            + motion.format("WIIMOTE_Y", "POD_RIGHT", 15, 70, 0, 0.6)
            + motion.format("NUNCHUK_Y", "POD_LEFT", 30, 135, 0, 0.6)
            + motion.format("WIIMOTE_SHAKE", "BTN_X", 100, 300, 0, 0.0)
            + "[gameplay]\n" + "".join(f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}\n".replace("'", '"')
                                       for k, v in (w.GAMEPLAY_DEFAULT | play).items()))
    path = Path(tempfile.mkstemp(suffix=".toml")[1])
    path.write_text(text)
    b = Bridge(path)
    path.unlink()
    b.devs |= {NUNCHUK: None, ACCEL: None}
    return b


def nunchuk(x=0.0, y=0.0):
    """Raw Nunchuk axes at 1 g (about 190)."""
    return {e.ABS_RX: round(x), e.ABS_RY: round(y), e.ABS_RZ: round(math.sqrt(max(0, 190 ** 2 - x * x - y * y)))}


def hold(b, name, values, t, seconds):
    for i in range(int(seconds * 100)):
        t += 0.01
        report(b, name, values, t)
    return t


# Filter: a steady hand with noise moves the output less than with the old smoothing. A fast turn still gets there soon.
def spread(**play):
    b, seen, t = game(**play), [], 1.0
    random.seed(2)
    for i in range(300):
        t += 0.01
        report(b, NUNCHUK, nunchuk(40 + random.uniform(-6, 6)), t)
        if i > 100:
            seen.append(b.pad.abs[e.ABS_X])
    return max(seen) - min(seen), b, t


old_jitter, _, _ = spread(filter=False)
new_jitter, b, t = spread()
assert new_jitter < old_jitter * 0.7, (new_jitter, old_jitter)
t = hold(b, NUNCHUK, nunchuk(100), t, 0.15)
assert b.pad.abs[e.ABS_X] > 18000, b.pad.abs[e.ABS_X]

# Shake: the steering keeps its value during a shake and for a moment after it. The option turns this off.
b = game()
t = hold(b, NUNCHUK, nunchuk(60), 1.0, 1.0)
steer = b.pad.abs[e.ABS_X]
assert steer > 5000
for i in range(40):
    t += 0.01
    report(b, NUNCHUK, {e.ABS_RX: -400 if i % 2 else 400, e.ABS_RY: 300, e.ABS_RZ: 300}, t)
    assert b.pad.abs[e.ABS_X] == steer
report(b, NUNCHUK, nunchuk(-60), t + 0.05)
assert b.pad.abs[e.ABS_X] == steer  # still in the hold
t = hold(b, NUNCHUK, nunchuk(-60), t + 0.2, 1.0)
assert b.pad.abs[e.ABS_X] < -5000
b = game(shake_limit=0)
t = hold(b, NUNCHUK, nunchuk(60), 1.0, 1.0)
report(b, NUNCHUK, {e.ABS_RX: -400, e.ABS_RY: 300, e.ABS_RZ: 300}, t + 0.01)
assert b.pad.abs[e.ABS_X] < 0

# Shake button: one shake is one press, held down until it ends. The option turns this off.
def shake(b):
    t = 1.0
    for i in range(60):  # 60 reports, past the threshold on every third
        t += 0.01
        report(b, ACCEL, {e.ABS_RX: 0, e.ABS_RY: 400 if i % 3 == 0 else 100, e.ABS_RZ: 0}, t)
    return t


b = game()
t = shake(b)
assert b.pad.presses == 1 and b.pad.keys[e.BTN_NORTH] == 1
b.release_motions(t + 0.1)
assert b.pad.keys[e.BTN_NORTH] == 1
b.release_motions(t + 0.3)  # a still sensor sends nothing, and the button lets go anyway
assert b.pad.keys[e.BTN_NORTH] == 0
b = game(button_hold=0)
shake(b)
assert b.pad.presses > 5

# Curve: half a turn gives less, and the full turn and the anti-deadzone stay the same.
a = w.Axis(0, 10, 110, False, False, curve=1.5)
assert a(110) == 32767 and a(60) == round(0.5 ** 1.5 * 32767)
a = w.Axis(0, 10, 110, False, False, anti=25, curve=1.5)
assert 8191 < a(11) < 8300 and a(110) == 32767
b = game()
t = hold(b, ACCEL, flat(pitch=0), 1.0, 0.2)
assert game().motions[1].axis.curve == 1 and b.motions[0].axis.curve == 1.5  # only steering
assert game(steer_curve=1.0).motions[0].axis.curve == 1.0

# Podracer steering uses the curve too.
b = game()
b.pod = {"POD_LEFT": 0.5, "POD_RIGHT": 0.0}
b.motions = [m for m in b.motions if m.target not in w.POD or m.device == ACCEL]
b.pod["POD_LEFT"] = 0.5
report(b, ACCEL, {e.ABS_RX: 0, e.ABS_RY: 0, e.ABS_RZ: 100}, 1.0)
assert b.pad.abs[e.ABS_X] == round(0.5 ** 1.5 * 32767)

# Recenter: the position now is the center, the button does not reach the pad, and in mouse mode it keeps its mouse job.
b = game()
t = hold(b, NUNCHUK, nunchuk(60), 1.0, 1.0)
assert b.pad.abs[e.ABS_X] > 5000
b.handle(event(e.EV_KEY, e.BTN_2, 1, t), WIIMOTE)
b.handle(event(e.EV_KEY, e.BTN_2, 0, t), WIIMOTE)
report(b, NUNCHUK, nunchuk(60), t + 0.01)
assert b.pad.abs[e.ABS_X] == 0 and b.pad.presses == 0
b = game(recenter="")
b.handle(event(e.EV_KEY, e.BTN_2, 1, 1.0), WIIMOTE)
assert b.pad.keys[e.BTN_Y] == 1
b = game()  # two remotes: there is no Nunchuk shake, so 2 on the left one stays Y
b.handle(event(e.EV_KEY, e.BTN_2, 1, 1.0), w.WIIMOTE2)
assert b.pad.keys[e.BTN_Y] == 1
b = game()
hold(b, NUNCHUK, nunchuk(60), 1.0, 1.0)
center = b.motions[0].axis.center
b.mouse_mode = True
b.handle(event(e.EV_KEY, e.BTN_2, 1, 3.0), WIIMOTE)
assert b.motions[0].axis.center == center

# Dolphin mode: every device is closed, so its grab ends, and a scan opens nothing. Leaving the mode allows scans again.
b = game()
closed = []
b.devs = {n: SimpleNamespace(close=lambda n=n: closed.append(n), capabilities=lambda absinfo=False: {}) for n in (WIIMOTE, NUNCHUK)}
path = Path(tempfile.mkstemp(suffix=".toml")[1])
path.write_text('[mode]\nactive = "DOLPHIN"\n[stick]\ntarget = "LEFT"\ncenter_x = 0\ncenter_y = 0\ndeadzone = 8\nmax = 82\n'
                'invert_x = false\ninvert_y = false\n[buttons]\n[rumble]\nenabled = false\n')
b.path = path
b.load()
order = b.order
assert not b.devs and sorted(closed) == sorted([WIIMOTE, NUNCHUK]) and b.released
b.scan()
assert not b.devs and b.order == order
path.write_text(path.read_text().replace("DOLPHIN", "RACER"))
b.load()
path.unlink()
assert not b.released
# Two levers with MotionPlus: the gyro's forward tilt drives the engine, in the accelerometer's units.
b = bridge()
b.two |= {"enabled": True, "use_motionplus": True, "center_right": 0, "center_left": 0}
b.order, b.mp_remotes, b.mouse_mode = ["r1", "r2"], {"r1"}, False
b.build_motions()
assert [m.how for m in b.motions if m.target in w.POD] == ["LEVER", e.ABS_RY]  # r2 has no MotionPlus: tilt as before
assert w.MPLUS in b.motion_devices() and w.ACCEL in b.motion_devices()
b.devs |= {w.MPLUS: None, w.ACCEL: None, w.ACCEL2: None}
b.state[w.ACCEL] = flat()
t = 1.0
for i in range(100):
    t += 0.01
    report(b, w.MPLUS, gyro(), t)
assert abs(b.pod["POD_RIGHT"]) < 0.01
for i in range(50):  # tip forward 40 degrees at 80 deg/s; the accelerometer is still flat, so only the gyro moves it
    t += 0.01
    report(b, w.MPLUS, gyro(p=80), t)
lever = b.state[w.MPLUS]["LEVER"]
assert 40 < lever < 70 and b.pod["POD_RIGHT"] != 0, lever  # 100 * sin(40) = 64, minus a little lean to the flat accelerometer
print("ok")
