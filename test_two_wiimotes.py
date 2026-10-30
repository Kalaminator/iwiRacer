#!/usr/bin/env python3
"""End-to-end check of two Wiimotes as levers, with fake remotes and a private daemon.
Stop the service and disconnect real remotes first: systemctl --user stop wiimote2xbox"""
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e, ff

from wiimote2xbox import ACCEL, CONFIG, WIIMOTE, find

HERE = Path(__file__).parent
FLAT, FORWARD = (0, 100), (-71, 71)


class Remote:
    def __init__(self, owner):
        big = AbsInfo(0, -500, 500, 0, 0, 0)
        # The sticks are only here so udev calls it a joystick and lets this user open it.
        caps = {e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST], e.EV_FF: [e.FF_RUMBLE],
                e.EV_ABS: [(e.ABS_X, big), (e.ABS_Y, big)]}
        self.main = UInput(caps, name=WIIMOTE, phys=owner, max_effects=4)
        self.accel = UInput({e.EV_ABS: [(a, big) for a in (e.ABS_RX, e.ABS_RY, e.ABS_RZ)]}, name=ACCEL, phys=owner)
        self.buzz = 0
        threading.Thread(target=self.serve_rumble, daemon=True).start()

    def serve_rumble(self):
        for ev in self.main.read_loop():
            if ev.type == e.EV_UINPUT and ev.code == e.UI_FF_UPLOAD:
                up = self.main.begin_upload(ev.value)
                up.retval = 0
                self.main.end_upload(up)
            elif ev.type == e.EV_UINPUT and ev.code == e.UI_FF_ERASE:
                er = self.main.begin_erase(ev.value)
                er.retval = 0
                self.main.end_erase(er)
            elif ev.type == e.EV_FF:
                self.buzz = ev.value

    def tilt(self, pose):
        for i in range(2):
            self.accel.write(e.EV_ABS, e.ABS_RY, pose[0] + i)
            self.accel.write(e.EV_ABS, e.ABS_RZ, pose[1])
            self.accel.syn()


def run(left, right, seconds=0.6):
    for _ in range(int(seconds * 50)):
        low.tilt(left if swap else right)
        high.tilt(right if swap else left)
        time.sleep(0.02)
    return pad.absinfo(e.ABS_RZ).value, pad.absinfo(e.ABS_X).value


text = CONFIG.read_text().replace("[two_wiimotes]\nenabled = false", "[two_wiimotes]\nenabled = true")
assert "enabled = true\nswap" in text
with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "config.toml"
    cfg.write_text(text)
    daemon = subprocess.Popen([sys.executable, str(HERE / "wiimote2xbox.py"), str(cfg)])
    try:
        time.sleep(1)
        pad = find("Microsoft X-Box 360 pad")
        assert pad, "no pad"
        high = Remote("bb:bb")  # connects first but sorts second
        time.sleep(2.2)
        for _ in range(30):  # one remote, no Nunchuk: it still drives, and a missing lever does not steer
            high.tilt(FORWARD)
            time.sleep(0.02)
        assert pad.absinfo(e.ABS_RZ).value > 200 and pad.absinfo(e.ABS_X).value == 0
        swap = False
        low = Remote("aa:aa")
        time.sleep(2.2)
        # aa:aa is the first place = right lever, bb:bb the second = left lever.
        assert run(FLAT, FLAT) == (0, 0)
        rt, x = run(FORWARD, FORWARD)
        assert rt > 200 and abs(x) < 3000, (rt, x)  # both forward: thrust, straight on
        rt, x = run(FORWARD, FLAT)
        assert rt > 200 and x > 20000, (rt, x)  # left harder: thrust, turn right
        rt, x = run(FLAT, FORWARD)
        assert rt > 200 and x < -20000, (rt, x)  # right harder: thrust, turn left

        def press(remote, key):
            remote.main.write(e.EV_KEY, e.BTN_SOUTH, 1)
            remote.main.syn()
            time.sleep(0.1)
            assert key in pad.active_keys(), pad.active_keys()
            remote.main.write(e.EV_KEY, e.BTN_SOUTH, 0)
            remote.main.syn()
            time.sleep(0.1)
            assert key not in pad.active_keys()

        press(low, e.BTN_SOUTH)  # A on the right lever = Xbox A
        press(high, e.BTN_EAST)  # A on the left lever = Xbox B

        effect = pad.upload_effect(ff.Effect(e.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(0, 0),
                                             ff.EffectType(ff_rumble_effect=ff.Rumble(0xFFFF, 0xFFFF))))
        pad.write(e.EV_FF, effect, 1)
        time.sleep(0.3)
        assert low.buzz == 1 and high.buzz == 1, "rumble on both"
        pad.write(e.EV_FF, effect, 0)
        time.sleep(0.3)
        assert low.buzz == 0 and high.buzz == 0

        cfg.write_text(text.replace("swap = false", "swap = true"))  # swap, live
        daemon.send_signal(1)
        time.sleep(2.2)
        swap = True
        rt, x = run(FORWARD, FLAT)
        assert rt > 200 and x > 20000, (rt, x)  # the other remote now is the left lever

        high.main.close()
        high.accel.close()  # lose a lever while pushing: no stuck turn or stuck thrust
        time.sleep(2.2)
        for _ in range(30):
            low.tilt(FLAT)
            time.sleep(0.02)
        assert pad.absinfo(e.ABS_RZ).value == 0 and pad.absinfo(e.ABS_X).value == 0
        assert daemon.poll() is None, "daemon died"
    finally:
        daemon.terminate()
        daemon.wait()
print("ok")
