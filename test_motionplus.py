"""Real MotionPlus check: which invert_pitch/roll/yaw are right for each Wiimote.

Stop the daemon first. Hold both Wiimotes still for 2 s, then move them slowly in all directions for the rest.
The accelerometer shows how "up" really turns; the gyro must predict the same turn. A fitted sign of -1 = invert."""
import select
import sys
import threading
import time

import numpy as np
from evdev import ecodes as e

import rawmplus
from wiimote2xbox import ACCEL, GRAVITY, GYRO_UNITS, MPLUS, find, remotes

SECONDS, STILL = 25, 2.0


def record():
    threading.Thread(target=rawmplus.run, daemon=True).start()  # third-party units
    time.sleep(2)
    devs = {}
    for who in remotes():
        mp, acc = find(MPLUS, who), find(ACCEL, who)
        if mp and acc:
            devs[mp.fd] = (who, "g", mp)
            devs[acc.fd] = (who, "a", acc)
    if not devs:
        sys.exit("No Wiimote with MotionPlus found.")
    state = {who: {"g": {}, "a": {}} for who, _, _ in devs.values()}
    rows = {who: [] for who in state}
    start = time.monotonic()
    print("Hold still...", flush=True)
    moved = False
    while (now := time.monotonic() - start) < SECONDS:
        if not moved and now > STILL:
            print(f"MOVE slowly in all directions for {SECONDS - STILL:.0f} s. Also point the tip up and swing it left/right.",
                  flush=True)
            moved = True
        for fd in select.select(list(devs), [], [], 0.1)[0]:
            who, kind, dev = devs[fd]
            try:
                events = list(dev.read())
            except OSError:  # the remote lost its MotionPlus or went away: keep testing the other one
                print(f"{who}: lost its {kind == 'g' and 'MotionPlus' or 'accelerometer'}", flush=True)
                for f in [f for f, d in devs.items() if d[0] == who]:
                    del devs[f]
                continue
            for ev in events:
                if ev.type == e.EV_ABS:
                    state[who][kind][ev.code] = ev.value
                elif ev.type == e.EV_SYN and kind == "g" and state[who]["a"]:
                    g, a = state[who]["g"], state[who]["a"]
                    rows[who].append([now] + [g.get(c, 0) for c in (e.ABS_RZ, e.ABS_RY, e.ABS_RX)]
                                     + [a.get(c, 0) for c in (e.ABS_RX, e.ABS_RY, e.ABS_RZ)])
    print("Done.\n")
    return {who: np.array(r) for who, r in rows.items()}


def fit(r):
    """Least squares d(up)/dt = sp*P + sr*R + sw*W (the Imu model). Returns the three multipliers and R²."""
    t, gyro, acc = r[:, 0], r[:, 1:4], r[:, 4:7]
    still = t < STILL
    rate = np.radians((gyro - np.median(gyro[still], axis=0)) / GYRO_UNITS)  # p, r, w in rad/s
    k = np.ones(9) / 9
    acc = np.column_stack([np.convolve(acc[:, i], k, "same") for i in range(3)])
    n = np.linalg.norm(acc, axis=1)
    up = acc / n[:, None]
    step = 5
    sl = slice(step, -step)
    dup = (up[2 * step:] - up[:-2 * step]) / (t[2 * step:] - t[:-2 * step])[:, None]
    x, y, z = up[sl].T
    p, ro, w = rate[sl].T
    zero = np.zeros_like(x)
    cols = [np.concatenate(c) for c in ((zero, p * z, -p * y), (ro * z, zero, -ro * x), (-w * y, w * x, zero))]
    ok = np.tile((np.abs(n[sl] - GRAVITY) < 15) & (t[sl] > STILL), 3)
    A, b = np.column_stack(cols)[ok], dup.T.reshape(-1)[ok]
    coef = np.linalg.lstsq(A, b, rcond=None)[0]
    r2 = 1 - np.sum((A @ coef - b) ** 2) / np.sum((b - b.mean()) ** 2)
    return coef, r2, ok.sum() // 3


if __name__ == "__main__":
    for who, r in record().items():
        if len(r) < 200:
            print(f"{who}: too little data ({len(r)} reports)")
            continue
        coef, r2, used = fit(r)
        print(f"{who}: {used} samples, fit R² = {r2:.2f} (good > 0.6)")
        for name, c in zip(("pitch", "roll", "yaw"), coef):
            print(f"  {name}: x{c:+.2f} -> invert_{name} = {'true' if c < 0 else 'false'}")
