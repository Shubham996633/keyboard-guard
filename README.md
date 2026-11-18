# keyboard-guard

Turn off a faulty laptop keyboard while you use an external one, and get it back when you
unplug.

A failing built-in keyboard can type keys on its own or get in the way of the keyboard you
actually use. Linux tools like `xinput` don't work on Wayland, so `keyboard-guard` uses the
kernel's own per-device switch. It works on Wayland, on X11 and at the login screen, and your
choice survives a reboot.

## What you get

| | |
|---|---|
| **Auto mode** | The laptop keyboard is off while an external keyboard is connected, and comes back when you unplug it. This is the default. |
| **Always off / Always on** | Stays the way you set it, whatever is plugged in. |
| **Turn on for 10 minutes** | Need the laptop keyboard for a moment? It turns on, then goes back off by itself. |
| **Ignore list** | Stop a security key or barcode scanner from counting as a keyboard. Tick **Ignore** in the app window. |
| **Popups** | A notification tells you when the laptop keyboard turns off or on. |
| **Tray icon** | Green when the laptop keyboard is on, red when it is off. Switch modes from the menu. |
| **App window** | Shows the current state, the mode and every detected keyboard. |
| **No password to switch** | After installing, the user who installed it can change modes without sudo. |

Only the built-in keyboard is switched. The power button and external keyboards are separate
devices and are never touched. Some Fn keys may stop working if your laptop sends them through
the main keyboard.

## Requirements

- Linux with systemd and kernel 5.11 or newer
- Python 3 with PyGObject and GTK 3
- A tray that supports AppIndicator. GNOME needs an extension for this
  (`gnome-shell-extension-appindicator`). KDE, Cinnamon, XFCE and Zorin have it built in.

Debian/Ubuntu dependencies:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
```

## Install

```sh
git clone https://github.com/Shubham996633/keyboard-guard.git
cd keyboard-guard
sudo ./install.sh
```

Then open **Keyboard Guard** from your app menu. The tray icon starts by itself at each login.

**In Auto mode, your laptop keyboard turns off as soon as the install finishes if an external
keyboard is connected.**

To apply your own code changes, run `sudo ./install.sh` again. To remove everything and turn
the laptop keyboard back on, run `sudo ./uninstall.sh`.

## How keyboards are detected

| Device | Treated as |
|---|---|
| USB or Bluetooth keyboard | External |
| USB keyboard wired inside the laptop | Laptop |
| Other hardware keyboard with letter keys, including PS/2 | Laptop |
| Mouse, number pad, macro pad, hotkey device | Not a keyboard, so ignored |
| Software keyboard, such as a remapper | Ignored |

## Good to know

- **A wireless keyboard that is switched off can still look connected.** Its USB receiver may
  keep it listed, so Auto can leave the laptop keyboard off. Use the touchpad to pick
  **Always on** from the tray.
- **Always off with no external keyboard** means you can't type on a physical keyboard at the
  login screen. Use the on-screen keyboard from the accessibility menu.
- **Disk encryption passwords and the boot menu are not covered.** The service starts after
  them, so the laptop keyboard still works there, and a faulty one can still interfere.
- **Barcode scanners and security keys** that act as USB keyboards count as external until you
  tick **Ignore** for them in the app window. Devices are matched by name, so this also ignores
  another device with the same name.

## If you get stuck

Stopping the service always turns the laptop keyboard back on:

```sh
sudo systemctl stop keyboard-guard
```

To check what it's doing:

```sh
systemctl status keyboard-guard
journalctl -u keyboard-guard
```

## How it works

`kbguardd.py` runs as a root service and writes to `/sys/class/input/inputN/inhibited` for the
built-in keyboard. It watches for devices being plugged in or removed, and keeps your mode in
`/var/lib/keyboard-guard/state.json`.

`kbguard-tray.py` is the tray icon and window. It runs as your user and talks to the service
over a Unix socket that only the installing user can open. It can only ask for the status,
change the mode, start or cancel the timer, and change the ignore list.

Run the tests with `python3 test_kbguard.py`. They use a fake device tree, so they need no root
and don't touch your hardware.

## Tested on

Zorin OS 18 on GNOME Wayland, on a Xiaomi Mi Notebook Horizon Edition 14 with a Logitech
wireless keyboard. Reports from other laptops are welcome.

## License

MIT
