import math

from evdev import ecodes as e

from wiimote2xbox import GYRO_UNITS, Axis, Imu, pod_drive

a = Axis(center=6, deadzone=8, maximum=82, invert=False)
assert a(6) == 0 and a(14) == 0           # rest and inside deadzone
assert a(6 + 82) == 32767 and a(6 - 82) == -32767
assert 0 < a(6 + 45) < 32767
assert a(6 + 100) == 32767 and a(6 + 82) < 32767  # range grows past max
assert a(6 - 82) == -32767                # other side unchanged
assert Axis(-4, 8, 82, True)(-4 + 82) == -32767  # invert
b = Axis(0, 10, 110, False, anti=25)
assert b(10) == 0 and 8191 < b(11) < 8600 and b(110) == 32767 and b(-60) < -20000  # anti-deadzone
assert pod_drive(1, 0.4) == 1 and pod_drive(1, -0.25) == 0.75 and pod_drive(-1, 1) == 0  # podracer mix
assert pod_drive(-1, -0.5) == -0.75 and pod_drive(0, 0) == 0


def feed(imu, t, seconds, accel, pitch=0, roll=0, yaw=0, offset=1000):
    """Run the Imu at 100 Hz with these rates (deg/s) and a gyro that reads `offset` at rest. Returns (state, end time)."""
    state = {}
    for i in range(int(seconds * 100)):
        t += 0.01
        hand = 8 * math.sin(i / 3) if pitch or roll or yaw else 0  # a moving hand is never perfectly steady
        state = {e.ABS_RZ: (pitch + hand) * GYRO_UNITS + offset, e.ABS_RY: (roll + hand) * GYRO_UNITS + offset,
                 e.ABS_RX: (yaw + hand) * GYRO_UNITS + offset}
        imu.update(state, accel, t)
    return state, t


FLAT = {e.ABS_RX: 0, e.ABS_RY: 0, e.ABS_RZ: 100}
imu = Imu()
s, t = feed(imu, 1000, 0.3, FLAT)
assert "RATE_PITCH" not in s and "YAW" not in s and abs(s["PITCH"]) < 1  # no gyro output before it knows its rest value
s, t = feed(imu, t, 1, FLAT)
assert imu.bias == [1000, 1000, 1000] and abs(s["RATE_PITCH"]) < 0.01 and abs(s["YAW"]) < 0.01
s, t = feed(imu, t, 1, {e.ABS_RX: 0, e.ABS_RY: 0, e.ABS_RZ: 0}, pitch=30)  # free fall: accelerometer ignored, gyro alone
assert 27 < s["PITCH"] < 33 and abs(s["ROLL"]) < 3 and abs(s["RATE_PITCH"] - 30) < 9
s, t = feed(imu, t, 1, {e.ABS_RX: 0, e.ABS_RY: 400, e.ABS_RZ: 0})  # a shake reading 4 g does not change the angle
assert 27 < s["PITCH"] < 33
s, t = feed(imu, t, 8, FLAT)  # gyro drift goes away: the accelerometer brings it back
assert abs(s["PITCH"]) < 1
s, t = feed(imu, t, 1, FLAT, yaw=60)
assert s["YAW"] > 35 and s["RATE_YAW"] > 50 and imu.bias == [1000, 1000, 1000]  # turning sets yaw, not the rest value
s, t = feed(imu, t, 15, FLAT)
assert abs(s["YAW"]) < 2  # yaw returns to 0 without a reference
for i in range(100):  # steady slow tilt: the gyro alone looks still, but the accelerometer moves
    t += 0.01
    tilt = math.radians(30 * i / 100)
    imu.update({e.ABS_RZ: 30 * GYRO_UNITS + 1000, e.ABS_RY: 1000, e.ABS_RX: 1000},
               {e.ABS_RX: 0, e.ABS_RY: 100 * math.sin(tilt), e.ABS_RZ: 100 * math.cos(tilt)}, t)
assert imu.bias == [1000, 1000, 1000]
imu.config({"invert_pitch": True})
assert imu.signs == [-1, 1, 1]
print("ok")
