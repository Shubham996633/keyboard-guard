#!/usr/bin/env python3
"""Runs the service logic against a fake /sys, so it needs no root and touches no hardware.
Usage: python3 test_kbguard.py
"""
import json
import os
import tempfile
import unittest

import kbguardd

KEYBOARD_KEYS = "402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe"
MOUSE_KEYS = "3f00733fff 0 0 483ffff17aff32d bfd4444600000000 ffff0001 130ff38b17d000 677bfad9415fed 19ed68000004400 10000002"
HOTKEY_KEYS = "400000000000080 4000000000000 0 0 10000000000 0 1000800001000 f80080000000000 0 0"


def add_device(sysfs, event, name, bus, keys, parent="devices"):
    real = os.path.join(sysfs, "..", parent, event)
    os.makedirs(os.path.join(real, "id"))
    os.makedirs(os.path.join(real, "capabilities"))
    files = {"name": name, "id/bustype": bus, "capabilities/key": keys, "inhibited": "0"}
    for relative, value in files.items():
        with open(os.path.join(real, relative), "w") as handle:
            handle.write(value + "\n")
    event_dir = os.path.join(sysfs, event)
    os.makedirs(event_dir)
    os.symlink(os.path.realpath(real), os.path.join(event_dir, "device"))
    return os.path.join(real, "inhibited")


def read(path):
    with open(path) as handle:
        return handle.read().strip()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.sysfs = os.path.join(self.root.name, "class")
        os.makedirs(self.sysfs)
        self.state = os.path.join(self.root.name, "state.json")
        self.laptop = add_device(self.sysfs, "event3", "AT keyboard", "0011", KEYBOARD_KEYS)
        add_device(self.sysfs, "event6", "Hotkeys", "0000", HOTKEY_KEYS)
        add_device(self.sysfs, "event14", "Logitech Mouse", "0003", MOUSE_KEYS)

    def tearDown(self):
        self.root.cleanup()

    def add_external(self):
        return add_device(self.sysfs, "event13", "Logitech Keyboard", "0003", KEYBOARD_KEYS)

    def guard(self):
        return kbguardd.Guard(self.sysfs, self.state)

    def test_mouse_and_hotkeys_are_not_keyboards(self):
        names = [k["name"] for k in kbguardd.scan_keyboards(self.sysfs)]
        self.assertEqual(names, ["AT keyboard"])

    def test_auto_stays_on_without_external(self):
        self.guard().apply()
        self.assertEqual(read(self.laptop), "0")

    def test_auto_follows_plug_and_unplug(self):
        guard = self.guard()
        external = self.add_external()
        guard.apply()
        self.assertEqual(read(self.laptop), "1")
        self.assertEqual(read(external), "0", "external keyboard must stay on")
        os.unlink(os.path.join(self.sysfs, "event13", "device"))
        os.rmdir(os.path.join(self.sysfs, "event13"))
        guard.apply()
        self.assertEqual(read(self.laptop), "0")

    def test_always_off_and_always_on(self):
        guard = self.guard()
        guard.set_mode("off")
        self.assertEqual(read(self.laptop), "1")
        self.add_external()
        guard.set_mode("on")
        self.assertEqual(read(self.laptop), "0")

    def test_mode_survives_restart(self):
        self.guard().set_mode("off")
        self.assertEqual(self.guard().mode, "off")

    def test_corrupt_state_falls_back_to_auto(self):
        with open(self.state, "w") as handle:
            handle.write("not json")
        self.assertEqual(self.guard().mode, "auto")

    def test_release_turns_keyboard_back_on(self):
        guard = self.guard()
        guard.set_mode("off")
        guard.enable_all_internal()
        self.assertEqual(read(self.laptop), "0")

    def test_requests_are_validated(self):
        guard = self.guard()
        for bad in (b"junk", b'{"cmd":"rm"}', b'{"cmd":"set_mode","mode":"x"}', b"[]"):
            self.assertIn("error", kbguardd.handle_request(guard, bad))
            self.assertEqual(guard.mode, "auto")

    def test_status_is_json_safe_and_hides_paths(self):
        status = kbguardd.handle_request(self.guard(), b'{"cmd":"status"}')
        self.assertNotIn("_inhibit_path", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
