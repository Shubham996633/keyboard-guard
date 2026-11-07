#!/usr/bin/env python3
"""Keyboard Guard desktop app: a tray icon and a window, both driving the service.

Starting it opens the window. With --tray (used at login) only the icon appears.
Only one copy runs: starting it again opens the window of the running copy.
"""
import json
import os
import socket
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

# Distros ship one of these two libraries, and they work the same way.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3  # noqa: E402

APP_ID = "local.KeyboardGuard"
SOCKET_PATH = os.environ.get("KBGUARD_SOCKET", "/run/keyboard-guard/control.sock")
HERE = os.path.dirname(os.path.realpath(__file__))
ICON_ON = os.path.join(HERE, "icons", "keyboard-on.svg")
ICON_OFF = os.path.join(HERE, "icons", "keyboard-off.svg")
REFRESH_SECONDS = 2
TEMPORARY_MINUTES = 10
SERVICE_DOWN = "Keyboard Guard service is not running"
NO_EXTERNAL_WARNING = "No external keyboard: use the touchpad to turn it back on"

MODES = (
    ("auto", "Auto", "Off while an external keyboard is connected"),
    ("off", "Always off", "Stays off, even with no external keyboard"),
    ("on", "Always on", "Stays on, even with an external keyboard"),
)


def ask_service(request):
    """Return the service's reply, or None when it can't be reached."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(SOCKET_PATH)
            client.sendall(json.dumps(request).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                reply += chunk
        return json.loads(reply)
    except (OSError, ValueError):
        return None


def service_is_up(status):
    return status is not None and "keyboards" in status


def split_keyboards(status):
    internal = [k for k in status["keyboards"] if k["kind"] == "internal"]
    external = [k for k in status["keyboards"] if k["kind"] == "external"]
    return internal, external


def summarize(status):
    """Return (icon path, headline, warning or '') for what both views display."""
    if not service_is_up(status):
        return ICON_ON, SERVICE_DOWN, ""
    internal, external = split_keyboards(status)
    if laptop_keyboard_off(status):
        return ICON_OFF, "Laptop keyboard is OFF", "" if external else NO_EXTERNAL_WARNING
    minutes = status.get("temporary_minutes_left", 0)
    if minutes:
        unit = "minute" if minutes == 1 else "minutes"
        return ICON_ON, f"Laptop keyboard is ON for {minutes} more {unit}", ""
    return ICON_ON, "Laptop keyboard is ON", ""


def laptop_keyboard_off(status):
    internal, _external = split_keyboards(status)
    return bool(internal) and all(k["disabled"] for k in internal)


def temporary_action(status):
    """The timer action that makes sense now, as (label, request), or None."""
    if status.get("temporary_minutes_left", 0):
        return "Turn it back off now", {"cmd": "cancel_temporary"}
    if laptop_keyboard_off(status):
        return (f"Turn on for {TEMPORARY_MINUTES} minutes",
                {"cmd": "turn_on_for", "minutes": TEMPORARY_MINUTES})
    return None


def keyboard_kind_label(keyboard):
    if keyboard["kind"] == "internal":
        return "Laptop"
    return "External, ignored" if keyboard.get("ignored") else "External"


def keyboard_state_label(keyboard):
    return "Off" if keyboard["disabled"] else "On"


def label_item(text):
    item = Gtk.MenuItem(label=text)
    item.set_sensitive(False)
    return item


def left_label(text, css_class=None):
    label = Gtk.Label(label=text, xalign=0)
    label.set_line_wrap(True)
    if css_class:
        label.get_style_context().add_class(css_class)
    return label


def section_title(text):
    label = Gtk.Label(xalign=0)
    label.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
    return label


class Tray:
    def __init__(self, app):
        self.app = app
        self.indicator = AppIndicator3.Indicator.new(
            "keyboard-guard", ICON_ON, AppIndicator3.IndicatorCategory.HARDWARE)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    def show(self, status):
        icon, headline, warning = summarize(status)
        self.indicator.set_icon_full(icon, headline)

        menu = Gtk.Menu()
        menu.append(label_item(headline))
        if warning:
            menu.append(label_item(warning))
        if service_is_up(status):
            if status.get("error"):
                menu.append(label_item(status["error"]))
            action = temporary_action(status)
            if action:
                item = Gtk.MenuItem(label=action[0])
                item.connect("activate", lambda _item: self.app.send(action[1]))
                menu.append(item)
            self.add_mode_items(menu, status)
            self.add_keyboard_items(menu, status)

        menu.append(Gtk.SeparatorMenuItem())
        open_item = Gtk.MenuItem(label="Open Keyboard Guard")
        open_item.connect("activate", lambda _item: self.app.open_window())
        menu.append(open_item)
        quit_item = Gtk.MenuItem(label="Close tray icon")
        quit_item.connect("activate", lambda _item: self.app.quit())
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def add_mode_items(self, menu, status):
        menu.append(Gtk.SeparatorMenuItem())
        group = None
        for mode, title, _description in MODES:
            item = Gtk.RadioMenuItem.new_with_label_from_widget(group, title)
            group = item
            item.set_active(mode == status["mode"])
            # Connected after set_active so that building the menu changes nothing.
            item.connect("toggled", self.on_mode_toggled, mode)
            menu.append(item)

    def add_keyboard_items(self, menu, status):
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(label_item("Detected keyboards"))
        internal, external = split_keyboards(status)
        if not internal + external:
            menu.append(label_item("  None found"))
        for keyboard in internal + external:
            menu.append(label_item(
                f"  {keyboard_kind_label(keyboard)}: {keyboard['name']} "
                f"({keyboard_state_label(keyboard).lower()})"))

    def on_mode_toggled(self, item, mode):
        if item.get_active():
            GLib.idle_add(self.app.set_mode, mode)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Keyboard Guard")
        self.app = app
        self.showing_status = False  # true while the code, not the user, moves the radios
        self.set_resizable(False)
        self.set_icon_from_file(ICON_ON)
        self.connect("delete-event", lambda window, _event: window.hide_on_delete())

        header = Gtk.HeaderBar(title="Keyboard Guard", show_close_button=True)
        self.set_titlebar(header)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(18)
        page.set_size_request(460, -1)  # a non-resizable window ignores its default size
        self.add(page)

        summary = Gtk.Box(spacing=14)
        self.status_icon = Gtk.Image()
        summary.pack_start(self.status_icon, False, False, 0)
        summary_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.headline = Gtk.Label(xalign=0)
        self.warning = left_label("", "dim-label")
        self.warning.set_no_show_all(True)
        summary_text.pack_start(self.headline, False, False, 0)
        summary_text.pack_start(self.warning, False, False, 0)
        summary.pack_start(summary_text, True, True, 0)
        page.pack_start(summary, False, False, 6)

        self.temporary_button = Gtk.Button()
        self.temporary_button.set_no_show_all(True)
        self.temporary_button.connect("clicked", self.on_temporary_clicked)
        self.temporary_request = None
        page.pack_start(self.temporary_button, False, False, 0)

        self.mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.mode_box.pack_start(section_title("Mode"), False, False, 0)
        self.radios = {}
        group = None
        for mode, title, description in MODES:
            radio = Gtk.RadioButton.new_from_widget(group)
            group = radio
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            text.pack_start(left_label(title), False, False, 0)
            text.pack_start(left_label(description, "dim-label"), False, False, 0)
            radio.add(text)
            radio.connect("toggled", self.on_mode_toggled, mode)
            self.radios[mode] = radio
            self.mode_box.pack_start(radio, False, False, 0)
        page.pack_start(self.mode_box, False, False, 6)

        page.pack_start(section_title("Detected keyboards"), False, False, 6)
        self.keyboard_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.keyboard_list.get_style_context().add_class("frame")
        page.pack_start(self.keyboard_list, False, False, 0)
        page.show_all()

    def show_status(self, status):
        icon, headline, warning = summarize(status)
        self.status_icon.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(icon, 56, 56))
        self.headline.set_markup(
            f"<span size='large'><b>{GLib.markup_escape_text(headline)}</b></span>")
        error = status.get("error", "") if service_is_up(status) else ""
        message = error or warning
        self.warning.set_text(message)
        self.warning.set_visible(bool(message))

        up = service_is_up(status)
        self.mode_box.set_sensitive(up)
        action = temporary_action(status) if up else None
        self.temporary_button.set_visible(bool(action))
        if action:
            self.temporary_button.set_label(action[0])
            self.temporary_request = action[1]
        if up:
            self.showing_status = True
            self.radios[status["mode"]].set_active(True)
            self.showing_status = False
        self.show_keyboards(status if up else None)

    def show_keyboards(self, status):
        for row in self.keyboard_list.get_children():
            self.keyboard_list.remove(row)
        internal, external = split_keyboards(status) if status else ([], [])
        for keyboard in internal + external:
            row = Gtk.Box(spacing=12)
            row.set_border_width(10)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            text.pack_start(left_label(keyboard["name"]), False, False, 0)
            text.pack_start(left_label(keyboard_kind_label(keyboard), "dim-label"), False, False, 0)
            row.pack_start(text, True, True, 0)
            row.pack_end(Gtk.Label(label=keyboard_state_label(keyboard)), False, False, 0)
            if keyboard["kind"] == "external":
                ignore = Gtk.CheckButton(label="Ignore")
                ignore.set_tooltip_text(
                    "Don't count this device as a keyboard, for example a security key")
                ignore.set_active(keyboard.get("ignored", False))
                # Connected after set_active so that drawing the row changes nothing.
                ignore.connect("toggled", self.on_ignore_toggled, keyboard["name"])
                row.pack_end(ignore, False, False, 0)
            self.keyboard_list.add(row)
        if not internal + external:
            empty = left_label("None found", "dim-label")
            empty.set_margin_top(10)
            empty.set_margin_bottom(10)
            empty.set_margin_start(10)
            self.keyboard_list.add(empty)
        self.keyboard_list.show_all()

    def on_mode_toggled(self, radio, mode):
        if radio.get_active() and not self.showing_status:
            GLib.idle_add(self.app.set_mode, mode)

    def on_temporary_clicked(self, _button):
        if self.temporary_request:
            self.app.send(self.temporary_request)

    def on_ignore_toggled(self, check, name):
        # Deferred: the reply rebuilds the list, which destroys this very button.
        GLib.idle_add(self.app.send, {"cmd": "set_ignored", "name": name, "ignored": check.get_active()})


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.tray = None
        self.window = None
        self.last_status = object()
        self.announced_off = None
        self.previous_off = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # keeps the tray icon alive while the window is closed
        self.tray = Tray(self)
        self.refresh()
        GLib.timeout_add_seconds(REFRESH_SECONDS, self.refresh)

    def do_command_line(self, command_line):
        if "--tray" not in command_line.get_arguments():
            self.open_window()
        return 0

    def open_window(self):
        if self.window is None:
            self.window = Window(self)
            self.window.show_status(self.last_status)
        self.window.present()

    def refresh(self):
        status = ask_service({"cmd": "status"})
        # Rebuilding closes an open tray menu, so only do it when something changed.
        if status != self.last_status:
            self.last_status = status
            self.tray.show(status)
            if self.window:
                self.window.show_status(status)
        self.announce_change(status)
        return True

    def announce_change(self, status):
        """Popup when the keyboard flips. It must hold for two checks, so a USB
        blip while resuming from sleep doesn't produce a pair of popups."""
        if not service_is_up(status):
            return
        off = laptop_keyboard_off(status)
        if self.announced_off is None:
            self.announced_off = off  # what it was when the app started is not news
        elif off != self.announced_off and off == self.previous_off:
            self.announced_off = off
            self.notify(off)
        self.previous_off = off

    def notify(self, off):
        title = "Laptop keyboard is off" if off else "Laptop keyboard is on"
        try:
            subprocess.Popen(
                ["notify-send", "-a", "Keyboard Guard", "-i", ICON_OFF if off else ICON_ON, title],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def set_mode(self, mode):
        return self.send({"cmd": "set_mode", "mode": mode})

    def send(self, request):
        ask_service(request)
        self.refresh()
        return False


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
