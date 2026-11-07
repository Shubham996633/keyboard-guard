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
        self.now = 1000
        return kbguardd.Guard(self.sysfs, self.state, clock=lambda: self.now)

    def request(self, guard, **fields):
        return kbguardd.handle_request(guard, json.dumps(fields).encode())

    def test_turn_on_for_a_while_then_back_off(self):
        guard = self.guard()
        self.add_external()
        guard.apply()
        self.assertEqual(read(self.laptop), "1")
        status = self.request(guard, cmd="turn_on_for", minutes=10)
        self.assertEqual(read(self.laptop), "0")
        self.assertEqual(status["temporary_minutes_left"], 10)
        self.now += 9 * 60 + 1
        self.assertEqual(guard.status()["temporary_minutes_left"], 1)
        self.assertEqual(read(self.laptop), "0")
        self.now += 60
        guard.apply()
        self.assertEqual(read(self.laptop), "1")
        self.assertEqual(guard.status()["temporary_minutes_left"], 0)

    def test_cancel_turns_it_back_off_now(self):
        guard = self.guard()
        self.add_external()
        self.request(guard, cmd="turn_on_for", minutes=10)
        self.request(guard, cmd="cancel_temporary")
        self.assertEqual(read(self.laptop), "1")

    def test_changing_mode_cancels_the_timer(self):
        guard = self.guard()
        self.add_external()
        self.request(guard, cmd="turn_on_for", minutes=10)
        status = self.request(guard, cmd="set_mode", mode="off")
        self.assertEqual(status["temporary_minutes_left"], 0)
        self.assertEqual(read(self.laptop), "1")

    def test_timer_does_not_survive_a_restart(self):
        guard = self.guard()
        self.add_external()
        self.request(guard, cmd="turn_on_for", minutes=10)
        self.guard().apply()
        self.assertEqual(read(self.laptop), "1")

    def test_ignored_device_does_not_count_and_is_remembered(self):
        guard = self.guard()
        self.add_external()
        status = self.request(guard, cmd="set_ignored", name="Logitech Keyboard", ignored=True)
        self.assertEqual(read(self.laptop), "0")
        self.assertTrue([k for k in status["keyboards"] if k["kind"] == "external"][0]["ignored"])
        restarted = self.guard()
        restarted.apply()
        self.assertEqual(read(self.laptop), "0", "ignore list must survive a restart")
        self.request(restarted, cmd="set_ignored", name="Logitech Keyboard", ignored=False)
        self.assertEqual(read(self.laptop), "1")

    def test_only_a_connected_external_keyboard_can_be_ignored(self):
        guard = self.guard()
        self.add_external()
        for name in ("Nothing plugged in", "AT keyboard"):
            self.assertIn("error", self.request(guard, cmd="set_ignored", name=name, ignored=True))
        self.assertEqual(guard.ignored, [])

    def test_new_requests_are_validated(self):
        guard = self.guard()
        bad_minutes = (0, -1, 121, "10", 2.5, True, None)
        for minutes in bad_minutes:
            self.assertIn("error", self.request(guard, cmd="turn_on_for", minutes=minutes))
        for fields in ({"name": 5, "ignored": True}, {"name": "x"}, {"name": "", "ignored": True},
                       {"name": "x" * 300, "ignored": True}, {"name": "x", "ignored": "yes"}):
            self.assertIn("error", self.request(guard, cmd="set_ignored", **fields))

    def test_state_file_from_the_previous_version_still_loads(self):
        with open(self.state, "w") as handle:
            handle.write('{"mode": "off"}')
        guard = self.guard()
        self.assertEqual((guard.mode, guard.ignored), ("off", []))

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
