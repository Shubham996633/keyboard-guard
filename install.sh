#!/bin/sh
# Installs Keyboard Guard, or updates it after you edit the code here.
# The code is copied to a root-owned folder because the service runs as root.
# Usage: sudo ./install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run: sudo ./install.sh" >&2
    exit 1
fi
if [ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = "root" ]; then
    echo "Run this through sudo from your normal user, so the tray knows who may control it." >&2
    exit 1
fi

if ! ls /sys/class/input/input*/inhibited >/dev/null 2>&1; then
    echo "This kernel can't switch input devices off. Keyboard Guard needs Linux 5.11 or newer." >&2
    exit 1
fi
if ! python3 -c '
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
' 2>/dev/null; then
    echo "Warning: the tray and window need PyGObject, GTK 3 and an AppIndicator library." >&2
    echo "On Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1" >&2
fi

source_dir=$(dirname "$(readlink -f "$0")")
lib=/usr/local/lib/keyboard-guard

install -d "$lib/icons"
install -m 755 "$source_dir/kbguardd.py" "$source_dir/kbguard-tray.py" "$lib/"
install -m 644 "$source_dir"/icons/*.svg "$lib/icons/"
sed "s/@USER@/$SUDO_USER/" "$source_dir/keyboard-guard.service" > /etc/systemd/system/keyboard-guard.service
# At login only the tray icon starts. Opening it from the app menu shows the window.
sed 's|^Exec=.*|& --tray|' "$source_dir/keyboard-guard-tray.desktop" > /etc/xdg/autostart/keyboard-guard-tray.desktop
install -m 644 "$source_dir/keyboard-guard-tray.desktop" /usr/share/applications/keyboard-guard-tray.desktop
pkill -f kbguard-tray.py || true  # the old copy would keep running the old code

systemctl daemon-reload
systemctl enable keyboard-guard.service
systemctl restart keyboard-guard.service

echo "Keyboard Guard is installed for $SUDO_USER and is in Auto mode unless you changed it."
echo "The tray icon starts at login. To start now, open Keyboard Guard from your app menu."
