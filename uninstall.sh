#!/bin/sh
# Removes Keyboard Guard and turns the laptop keyboard back on.
# Usage: sudo ./uninstall.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run: sudo ./uninstall.sh" >&2
    exit 1
fi

systemctl disable --now keyboard-guard.service 2>/dev/null || true
rm -f /etc/systemd/system/keyboard-guard.service
rm -f /etc/xdg/autostart/keyboard-guard-tray.desktop
rm -f /usr/share/applications/keyboard-guard-tray.desktop
rm -rf /usr/local/lib/keyboard-guard /var/lib/keyboard-guard
systemctl daemon-reload
pkill -f kbguard-tray.py || true

echo "Keyboard Guard is removed and the laptop keyboard is on."
