#!/usr/bin/env python3
"""Third-party MotionPlus over hidraw -> uinput "Nintendo Wii Remote Motion Plus" (phys = the remote's address).

hid-wiimote drops these units: they stay mapped at the extension address and give ID a4 20 05 05, which it does not
know. Their gyro data is fine, so we read it from the raw reports, while the kernel keeps buttons, accelerometer, IR
and rumble. The output matches the kernel's own MotionPlus device, so the daemon finds it with find(MPLUS, address).
Run alone for tests (stop the daemon first), or as a thread in the daemon."""
import os
import select
import time
from pathlib import Path

from evdev import AbsInfo, UInput, ecodes as e

NAME = "Nintendo Wii Remote Motion Plus"
IDS = ("HID_ID=0005:0000057E:00000306", "HID_ID=0005:0000057E:00000330")
EXT_AT = {0x32: 3, 0x34: 3, 0x35: 6, 0x36: 13, 0x37: 16, 0x3D: 1}  # report -> first extension byte
WITH_EXT = {0x30: 0x32, 0x31: 0x35}  # the kernel's modes without extension bytes -> the same plus extension bytes
GYRO = AbsInfo(0, -16000, 16000, 4, 8, 0)  # as hid-wiimote


def gyro(x):
    """6 MotionPlus bytes -> ABS_RX, ABS_RY, ABS_RZ exactly as hid-wiimote (wiimod_mp_in_mp) reports them."""
    out = []
    for lo, hi, slow in ((x[0], x[3], x[3] & 2), (x[1], x[4], x[4] & 2), (x[2], x[5], x[3] & 1)):
        v = (lo | (hi << 6) & 0xFF00) - 8192
        out.append(v * 9 if slow else v * 2000 * 9 // 440)
    return out


def candidates():
    """(hidraw path, address) of every Wiimote whose kernel driver has no MotionPlus device."""
    for h in Path("/sys/class/hidraw").glob("hidraw*"):
        try:
            ev = (h / "device/uevent").read_text()
            names = [(p / "name").read_text().strip() for p in (h / "device/input").iterdir()]
        except OSError:
            continue
        if any(i in ev for i in IDS) and NAME not in names:
            yield f"/dev/{h.name}", next(l[9:] for l in ev.splitlines() if l.startswith("HID_UNIQ="))


class Remote:
    def __init__(self, path, address):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.address, self.dev, self.asked = address, None, 0

    def send(self, *data):
        os.write(self.fd, bytes(data))

    def start(self, now):
        # Map the MotionPlus in case it is not yet; a unit that maps itself refuses this, which is fine.
        self.send(0x16, 0x04, 0xA6, 0x00, 0xFE, 1, 0x04, *[0] * 15)
        self.ask(0x35, now)

    def ask(self, mode, now):
        if now - self.asked > 0.2:  # the kernel sets its mode again after status reports; don't flood the link
            self.asked = now
            self.send(0x12, 0x00, mode)

    def read(self, now):
        for r in iter(lambda: self.next_report(), None):
            if r[0] in WITH_EXT:
                self.ask(WITH_EXT[r[0]], now)
            elif r[0] in EXT_AT and len(r) >= EXT_AT[r[0]] + 6:
                x = r[EXT_AT[r[0]]:EXT_AT[r[0]] + 6]
                if x[5] & 2 and x != b"\xff" * 6:  # MotionPlus frame (not pass-through, not empty)
                    if self.dev is None:
                        self.dev = UInput({e.EV_ABS: [(c, GYRO) for c in (e.ABS_RX, e.ABS_RY, e.ABS_RZ)]},
                                          name=NAME, phys=self.address)
                    for c, v in zip((e.ABS_RX, e.ABS_RY, e.ABS_RZ), gyro(x)):
                        self.dev.write(e.EV_ABS, c, v)
                    self.dev.syn()

    def next_report(self):
        try:
            return os.read(self.fd, 32)
        except BlockingIOError:
            return None

    def close(self):
        os.close(self.fd)
        if self.dev:
            self.dev.close()


def run(paused=lambda: False):
    remotes, next_scan = {}, 0
    while True:
        now = time.monotonic()
        if paused():
            for r in remotes.values():
                r.close()
            remotes.clear()
            time.sleep(1)
            continue
        if now >= next_scan:
            next_scan = now + 1
            for path, address in candidates():
                if path not in remotes:
                    try:
                        remotes[path] = Remote(path, address)
                        remotes[path].start(now)
                    except OSError:
                        remotes.pop(path, None)
        for fd in select.select([r.fd for r in remotes.values()], [], [], 1)[0]:
            path = next(p for p, r in remotes.items() if r.fd == fd)
            try:
                remotes[path].read(time.monotonic())
            except OSError:  # the remote went away
                remotes.pop(path).close()


if __name__ == "__main__":
    x = bytes([0x7F, 0x7A, 0x74, 0x7F, 0x7E, 0x7E])  # a real frame at rest, both slow: (8063-8192)*9 = -1161
    assert gyro(x) == [-1161, -1206, -1260]
    assert gyro(bytes([0x00, 0x00, 0x00, 0x80, 0x80, 0x80]))[0] == 0  # fast mode at center
    try:
        run()
    except KeyboardInterrupt:
        pass
