#!/bin/sh
# Installs iwiRacer for this user: Python environment, device permissions, background service and menu entry.
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR"

python3 -m venv --system-site-packages .venv
.venv/bin/pip install --quiet -r requirements.txt

echo "Device permissions need sudo: udev rule and uinput module."
sudo install -m 644 70-iwiracer.rules /etc/udev/rules.d/70-iwiracer.rules
echo uinput | sudo tee /etc/modules-load.d/uinput.conf > /dev/null
sudo modprobe uinput
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=misc --subsystem-match=input --subsystem-match=hidraw

mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/applications"
sed "s|@DIR@|$DIR|g" wiimote2xbox.service > "$HOME/.config/systemd/user/wiimote2xbox.service"
sed "s|@DIR@|$DIR|g" wiimote2xbox.desktop > "$HOME/.local/share/applications/wiimote2xbox.desktop"
systemctl --user daemon-reload
systemctl --user enable wiimote2xbox
systemctl --user restart wiimote2xbox

echo "Done. Open iwiRacer from the application menu."
