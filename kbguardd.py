#!/usr/bin/env python3
"""Keyboard Guard service: runs as root and turns the laptop keyboard on or off.

It uses the kernel's per-device switch, /sys/class/input/inputN/inhibited, which
works the same on Wayland, X11, the login screen and text consoles.

Modes:
  auto  laptop keyboard is off while an external keyboard is connected
  off   laptop keyboard is always off
  on    laptop keyboard is always on

The laptop keyboard can also be turned on for a set number of minutes, and
external devices can be ignored so they don't count as a keyboard in auto mode.

The tray app talks to this service over a Unix socket that only one user can open.
"""
import argparse
import glob
import json
import math
import os
import pwd
import select
import signal
import socket
import struct
import sys
import time

MODES = ("auto", "off", "on")
DEFAULT_SYSFS = "/sys/class/input"
DEFAULT_STATE = "/var/lib/keyboard-guard/state.json"
DEFAULT_SOCKET = "/run/keyboard-guard/control.sock"

BUS_USB = 0x03
BUS_BLUETOOTH = 0x05
EXTERNAL_BUSES = (BUS_USB, BUS_BLUETOOTH)

# Linux key codes for the three letter rows (Q-P, A-L, Z-M). Only real keyboards
# have all of them. Mice and hotkey devices also report "key" events, so this is
# what keeps a Logitech mouse or the Fn-key device from counting as a keyboard.
LETTER_KEYS = list(range(16, 26)) + list(range(30, 39)) + list(range(44, 51))

RESCAN_SECONDS = 5  # fallback in case a hotplug event is missed
SETTLE_SECONDS = 0.5  # let a burst of hotplug events finish before rescanning
NETLINK_KOBJECT_UEVENT = 15
MAX_TEMPORARY_MINUTES = 120
MAX_IGNORED = 32
MAX_NAME_LENGTH = 256


def log(message):
    print(message, file=sys.stderr, flush=True)


def read_text(path, default=""):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return default


def has_letter_keys(capabilities):
    """capabilities is the text of capabilities/key: hex words, highest first."""
    word_bits = struct.calcsize("l") * 8
    mask = 0
    for index, word in enumerate(reversed(capabilities.split())):
        mask |= int(word, 16) << (index * word_bits)
    return all(mask >> key & 1 for key in LETTER_KEYS)


def usb_port_is_fixed(device_path):
    """True when a USB device is wired in (some laptops have USB keyboards)."""
    path = device_path
    while path not in ("", "/") and path.startswith("/"):
        value = read_text(os.path.join(path, "removable"))
        if value:
            return value == "fixed"
        path = os.path.dirname(path)
    return False


def classify(device_path, bus):
    """Return 'external' or 'internal'."""
    if bus in EXTERNAL_BUSES and not (bus == BUS_USB and usb_port_is_fixed(device_path)):
        return "external"
    return "internal"


def scan_keyboards(sysfs):
    keyboards = []
    for event_path in sorted(glob.glob(os.path.join(sysfs, "event*"))):
        device = os.path.join(event_path, "device")
        real_path = os.path.realpath(device)
        # Software keyboards (remappers, ydotool) are not hardware to protect
        # against, and must not count as "an external keyboard is connected".
        if "/devices/virtual/" in real_path:
            continue
        try:
            capabilities = read_text(os.path.join(device, "capabilities", "key"))
            if not has_letter_keys(capabilities):
                continue
            bus = int(read_text(os.path.join(device, "id", "bustype"), "0"), 16)
        except ValueError:
            continue
        keyboards.append({
            "name": read_text(os.path.join(device, "name"), "Unknown keyboard"),
            "event": os.path.basename(event_path),
            "kind": classify(real_path, bus),
            "disabled": read_text(os.path.join(device, "inhibited"), "0") == "1",
            "_inhibit_path": os.path.join(device, "inhibited"),
        })
    return keyboards


def should_disable_internal(mode, keyboards):
    if mode == "off":
        return True
    if mode == "on":
        return False
    return any(keyboard["kind"] == "external" and not keyboard.get("ignored")
               for keyboard in keyboards)


def boot_clock():
    """Seconds since boot, including suspend, so a timer can't stretch across sleep."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class Guard:
    def __init__(self, sysfs, state_path, clock=boot_clock):
        self.sysfs = sysfs
        self.state_path = state_path
        self.clock = clock
        self.mode = "auto"
        self.ignored = []
        self.on_until = None  # end of a "turn on for a while"; deliberately not saved
        self.error = ""
        self.load_state()

    def load_state(self):
        try:
            with open(self.state_path) as handle:
                state = json.load(handle)
            if state.get("mode") in MODES:
                self.mode = state["mode"]
            ignored = state.get("ignored", [])
            if isinstance(ignored, list):
                self.ignored = [name for name in ignored if isinstance(name, str)][:MAX_IGNORED]
        except (OSError, ValueError, AttributeError):
            pass

    def save_state(self):
        temporary = self.state_path + ".tmp"
        with open(temporary, "w") as handle:
            json.dump({"mode": self.mode, "ignored": self.ignored}, handle)
        os.replace(temporary, self.state_path)

    def set_mode(self, mode):
        self.mode = mode
        self.on_until = None
        self.save_state()
        self.apply()

    def turn_on_for(self, minutes):
        self.on_until = self.clock() + minutes * 60
        self.apply()

    def cancel_temporary(self):
        self.on_until = None
        self.apply()

    def temporary_minutes_left(self):
        if self.on_until is None:
            return 0
        left = self.on_until - self.clock()
        if left <= 0:
            self.on_until = None
            return 0
        return math.ceil(left / 60)

    def set_ignored(self, name, ignored):
        """Return an error message, or '' on success."""
        if ignored and name not in self.ignored:
            external = [k["name"] for k in scan_keyboards(self.sysfs) if k["kind"] == "external"]
            if name not in external:
                return "not a connected external keyboard"
            if len(self.ignored) >= MAX_IGNORED:
                return "too many ignored devices"
            self.ignored.append(name)
        elif not ignored and name in self.ignored:
            self.ignored.remove(name)
        self.save_state()
        self.apply()
        return ""

    def write_inhibit(self, keyboard, disabled):
        if keyboard["disabled"] == disabled:
            return
        try:
            with open(keyboard["_inhibit_path"], "w") as handle:
                handle.write("1" if disabled else "0")
            keyboard["disabled"] = disabled
            log(f"{keyboard['name']}: {'disabled' if disabled else 'enabled'}")
        except OSError as error:
            self.error = f"Could not change {keyboard['name']}: {error}"
            log(self.error)

    def apply(self):
        """Make the hardware match the mode. External keyboards are never touched."""
        keyboards = scan_keyboards(self.sysfs)
        for keyboard in keyboards:
            keyboard["ignored"] = keyboard["kind"] == "external" and keyboard["name"] in self.ignored
        disable = (should_disable_internal(self.mode, keyboards)
                   and self.temporary_minutes_left() == 0)
        self.error = ""
        for keyboard in keyboards:
            if keyboard["kind"] == "internal":
                self.write_inhibit(keyboard, disable)
        return keyboards

    def enable_all_internal(self):
        for keyboard in scan_keyboards(self.sysfs):
            if keyboard["kind"] == "internal":
                self.write_inhibit(keyboard, False)

    def status(self):
        keyboards = self.apply()
        return {
            "mode": self.mode,
            "error": self.error,
            "temporary_minutes_left": self.temporary_minutes_left(),
            "keyboards": [
                {key: value for key, value in keyboard.items() if not key.startswith("_")}
                for keyboard in keyboards
            ],
        }


def handle_request(guard, raw):
    try:
        request = json.loads(raw)
        command = request["cmd"]
    except (ValueError, KeyError, TypeError):
        return {"error": "bad request"}
    if command == "set_mode":
        if request.get("mode") not in MODES:
            return {"error": "unknown mode"}
        guard.set_mode(request["mode"])
    elif command == "turn_on_for":
        minutes = request.get("minutes")
        if type(minutes) is not int or not 1 <= minutes <= MAX_TEMPORARY_MINUTES:
            return {"error": "bad minutes"}
        guard.turn_on_for(minutes)
    elif command == "cancel_temporary":
        guard.cancel_temporary()
    elif command == "set_ignored":
        name = request.get("name")
        ignored = request.get("ignored")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME_LENGTH \
                or not isinstance(ignored, bool):
            return {"error": "bad device"}
        problem = guard.set_ignored(name, ignored)
        if problem:
            return {"error": problem}
    elif command != "status":
        return {"error": "unknown command"}
    return guard.status()


def serve_client(guard, client):
    with client:
        client.settimeout(1)
        try:
            raw = b""
            while b"\n" not in raw and len(raw) < 4096:
                chunk = client.recv(4096)
                if not chunk:
                    break
                raw += chunk
            client.sendall(json.dumps(handle_request(guard, raw)).encode() + b"\n")
        except OSError as error:
            log(f"client error: {error}")


def open_control_socket(path, owner):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous_umask = os.umask(0o177)  # 0600 from the moment the socket exists
    try:
        server.bind(path)
    finally:
        os.umask(previous_umask)
    if owner:
        os.chown(path, pwd.getpwnam(owner).pw_uid, -1)
    server.listen(4)
    return server


def open_hotplug_socket():
    """Kernel notifications for devices being added or removed."""
    try:
        hotplug = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
        hotplug.bind((0, 1))
        return hotplug
    except OSError as error:
        log(f"no hotplug events, using the {RESCAN_SECONDS}s rescan only: {error}")
        return None


def run(args):
    guard = Guard(args.sysfs, args.state)
    server = open_control_socket(args.socket, args.user)
    hotplug = open_hotplug_socket()

    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sockets = [server] + ([hotplug] if hotplug else [])
    guard.apply()
    log(f"running, mode={guard.mode}")
    try:
        while True:
            ready, _, _ = select.select(sockets, [], [], RESCAN_SECONDS)
            if server in ready:
                client, _ = server.accept()
                serve_client(guard, client)
            if hotplug in ready:
                message = hotplug.recv(8192)
                if b"SUBSYSTEM=input" in message:
                    time.sleep(SETTLE_SECONDS)
                    guard.apply()
            if not ready:
                guard.apply()
    finally:
        # Whenever the service stops, hand the keyboard back.
        guard.enable_all_internal()
        server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="the only user allowed to control the service")
    parser.add_argument("--sysfs", default=DEFAULT_SYSFS)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--release", action="store_true",
                        help="enable the laptop keyboard and exit")
    args = parser.parse_args()
    if args.release:
        Guard(args.sysfs, args.state).enable_all_internal()
        return
    run(args)


if __name__ == "__main__":
    main()
