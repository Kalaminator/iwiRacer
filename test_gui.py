#!/usr/bin/env python3
"""Off screen: every option has a place in the window, and a save changes no setting. The real config stays untouched."""
import os
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
import config_gui as g

real = g.subprocess.run
g.subprocess.run = lambda args, **k: None if args[0] == "pkill" else real(args, **k)
tmp = Path(tempfile.mkdtemp(dir=os.environ.get("XDG_RUNTIME_DIR")))
text = g.CONFIG.read_text()
before = tomllib.loads(text)
g.CONFIG = tmp / "config.toml"
g.CONFIG.write_text(text)
g.MONITOR = str(tmp / "gui.sock")

app = g.QApplication(sys.argv)
w = g.Window()

for options in (w.stick, w.play, w.two, w.mplus, w.mouse, *w.motions):
    for key, widget in options.items():
        assert widget.parent() is not None, f"{key} is not in the window"

# Each motion is once in the tabs, in the group of its own name.
sources = [m["source"].currentData() for m in w.motions]
assert sources == [s for names in g.MOTION_TABS.values() for s in names] and set(sources) == set(g.MOTION_LABELS)
assert all(not m["source"].isEnabled() for m in w.motions)

# The check marks follow the options.
first = w.motions[0]
sub = w.motion_groups.widget(0)
first["enabled"].setChecked(True)
assert sub.tabText(0).startswith("✓")
assert w.motion_groups.tabText(0).startswith("✓")
first["enabled"].setChecked(False)
assert not sub.tabText(0).startswith("✓")
first["enabled"].setChecked(next(m for m in before["motion"] if m["source"] == sources[0])["enabled"])

# Tabs in the order of use.
tabs = w.findChild(g.QTabWidget)
assert [tabs.tabText(i) for i in range(tabs.count())] == ["Botones", "Stick", "Movimiento", "Juego", "Vibración",
                                                          "Dos Wiimotes", "Ratón", "MotionPlus"]

w.save()
after = tomllib.loads(g.CONFIG.read_text())
by_source = lambda m: m["source"]
assert sorted(after.pop("motion"), key=by_source) == sorted(before.pop("motion"), key=by_source)
assert after == before, {k: (before.get(k), after.get(k)) for k in after if before.get(k) != after.get(k)}
shutil.rmtree(tmp)
print("ok")
os._exit(0)
