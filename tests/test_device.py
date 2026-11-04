import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from agent.device import Device


class DeviceBasics(unittest.TestCase):
    def device_with(self, outputs: dict):
        d = Device()
        d.use_u2 = False  # these tests exercise the adb path, never the real emulator
        calls = []

        def fake_run(*args):
            calls.append(args)
            return outputs.get(args[:3], b"")

        d._run = fake_run
        return d, calls

    def test_size_uses_the_override_when_present(self):
        d, _ = self.device_with({("shell", "wm", "size"): b"Physical size: 1080x2400\nOverride size: 720x1600\n"})
        self.assertEqual(d.size(), (720, 1600))
        d, _ = self.device_with({("shell", "wm", "size"): b"Physical size: 1080x2400\n"})
        self.assertEqual(d.size(), (1080, 2400))

    def test_size_follows_the_display_rotation_on_the_adb_path(self):
        wm = {("shell", "wm", "size"): b"Physical size: 720x1600\n"}
        d, _ = self.device_with({**wm, ("shell", "dumpsys", "input"): b"  SurfaceOrientation: 1\n"})
        self.assertEqual((d.size(), d.orientation()), ((1600, 720), "landscape"))
        d, _ = self.device_with({**wm, ("shell", "dumpsys", "input"): b"  SurfaceOrientation: 0\n"})
        self.assertEqual((d.size(), d.orientation()), ((720, 1600), "portrait"))

    def test_rotate_locks_the_screen_and_rejects_unknown_values(self):
        d, calls = self.device_with({})
        d.rotate("landscape")
        self.assertIn(("shell", "settings", "put", "system", "user_rotation", "1"), calls)
        self.assertIn(("shell", "settings", "put", "system", "accelerometer_rotation", "0"), calls)
        d.rotate("portrait")
        self.assertIn(("shell", "settings", "put", "system", "user_rotation", "0"), calls)
        d.rotate("auto")
        self.assertIn(("shell", "settings", "put", "system", "accelerometer_rotation", "1"), calls)
        with self.assertRaises(ValueError):
            d.rotate("upside-down")

    def test_prepare_and_restore_set_resolution_and_animations(self):
        d, calls = self.device_with({})
        d.prepare()
        self.assertIn(("shell", "wm", "size", "720x1600"), calls)
        self.assertEqual(sum(1 for c in calls if c[:4] == ("shell", "settings", "put", "global") and c[-1] == "0"), 3)
        calls.clear()
        d.restore()
        self.assertIn(("shell", "wm", "size", "reset"), calls)
        self.assertEqual(sum(1 for c in calls if c[:4] == ("shell", "settings", "put", "global") and c[-1] == "1"), 3)

    def test_is_emulator_reads_the_qemu_property(self):
        d, _ = self.device_with({("shell", "getprop", "ro.kernel.qemu"): b"1\n"})
        self.assertTrue(d.is_emulator())
        d, _ = self.device_with({})
        self.assertFalse(d.is_emulator())


if __name__ == "__main__":
    unittest.main()


class FakeU2:
    XML = ('<hierarchy><node text="Battery" bounds="[0,100][720,300]" clickable="true"/>'
           '<node text="" content-desc="Search" bounds="[10,10][110,60]" clickable="false"/></hierarchy>')

    def __init__(self, fail=False):
        self.fail, self.stopped, self.shots = fail, False, 0

    def dump_hierarchy(self):
        if self.fail:
            raise RuntimeError("service died")
        return self.XML

    def screenshot(self):
        from PIL import Image
        self.shots += 1
        return Image.new("RGB", (8, 16), (1, 2, 3))

    def stop_uiautomator(self):
        self.stopped = True

    def send_keys(self, text):
        if getattr(self, "keys_fail", False):
            raise RuntimeError("no ime")
        self.typed = getattr(self, "typed", []) + [text]


class ResidentReader(unittest.TestCase):
    def device(self, u2, adb_dump=b"", use_u2=True):
        d = Device(use_u2=use_u2)
        d._u2 = u2
        d._run = lambda *a: adb_dump if a[:2] == ("exec-out", "cat") else b"\x89PNG-adb" if a[:2] == ("exec-out", "screencap") else b""
        return d

    def test_uses_the_resident_service_for_the_tree_and_screenshot(self):
        u2 = FakeU2()
        d = self.device(u2)
        els = d.elements()
        self.assertEqual([(e.label, e.center, e.clickable) for e in els], [("Battery", (360, 200), True), ("Search", (60, 35), False)])
        self.assertTrue(d.screenshot().startswith(b"\x89PNG"))
        self.assertEqual(u2.shots, 1)

    def test_a_failing_service_is_stopped_and_the_adb_dump_takes_over(self):
        u2 = FakeU2(fail=True)
        d = self.device(u2, adb_dump=FakeU2.XML.encode())
        els = d.elements()
        self.assertTrue(u2.stopped)  # stopped so the old uiautomator dump can connect
        self.assertEqual(len(els), 2)
        d.elements()
        self.assertTrue(d._u2_failed)  # not retried

    def test_adb_mode_never_touches_the_service(self):
        u2 = FakeU2()
        d = self.device(u2, adb_dump=FakeU2.XML.encode(), use_u2=False)
        self.assertEqual(len(d.elements()), 2)
        self.assertEqual(d.screenshot(), b"\x89PNG-adb")
        self.assertEqual(u2.shots, 0)


class FilteredTree(unittest.TestCase):
    XML = ('<hierarchy>'
           '<node package="com.android.systemui" text="11:48" bounds="[0,0][720,60]"/>'
           '<node package="com.android.settings" text="Phone" bounds="[0,300][720,400]" clickable="true"/>'
           '<node package="com.google.android.inputmethod.latin" text="q" bounds="[0,1000][70,1100]" clickable="true"/>'
           '<node package="com.google.android.inputmethod.latin" text="w" bounds="[70,1000][140,1100]" clickable="true"/>'
           '<node package="com.android.systemui" text="Notifications" bounds="[0,700][720,800]" clickable="true"/>'
           '<node package="com.android.settings" text="" bounds="[0,1500][720,1600]"/>'
           '</hierarchy>')

    def device(self, xml):
        d = Device(use_u2=False)
        d._dump = lambda *a, **k: ET.fromstring(xml)
        return d

    def test_keyboard_keys_and_status_bar_are_left_out_and_the_keyboard_is_flagged(self):
        d = self.device(self.XML)
        labels = [e.label for e in d.elements()]
        self.assertEqual(labels, ["Phone", "Notifications"])  # real content stays, even from systemui below the status bar
        self.assertTrue(d.keyboard_open)

    def test_no_keyboard_flag_without_keyboard_nodes(self):
        d = self.device('<hierarchy><node package="com.android.settings" text="Phone" bounds="[0,300][720,400]" clickable="true"/></hierarchy>')
        d.elements()
        self.assertFalse(d.keyboard_open)

    def test_prompt_mentions_the_keyboard(self):
        from agent.loop import build_prompt
        self.assertIn("On-screen keyboard: open", build_prompt("g", [], [], 1.0, 0, [], keyboard=True))
        self.assertNotIn("On-screen keyboard", build_prompt("g", [], [], 1.0, 0, []))


class Typing(unittest.TestCase):
    def test_types_through_the_resident_service_including_unicode(self):
        u2 = FakeU2()
        d = Device(use_u2=True)
        d._u2 = u2
        calls = []
        d._run = lambda *a: calls.append(a) or b""
        d.type_text("வணக்கம் é")
        self.assertEqual(u2.typed, ["வணக்கம் é"])
        self.assertEqual(calls, [])  # adb was not needed

    def test_plain_text_goes_through_adb_input_text_and_never_the_clipboard(self):
        u2 = FakeU2()
        d = Device(use_u2=True)
        d._u2 = u2
        calls = []
        d._run = lambda *a: calls.append(a) or b""
        d.type_text("hello world")
        self.assertEqual(calls, [("shell", "input", "text", "hello%sworld")])
        self.assertFalse(hasattr(u2, "typed"))  # uiautomator2 pastes through the clipboard: not used for ASCII

    def test_a_failing_adb_input_falls_back_to_the_resident_service(self):
        import subprocess
        u2 = FakeU2()
        d = Device(use_u2=True)
        d._u2 = u2
        def boom(*a):
            raise subprocess.CalledProcessError(1, "adb")
        d._run = boom
        d.type_text("91")
        self.assertEqual(u2.typed, ["91"])

    def test_a_private_secret_is_typed_by_adb_and_never_reaches_the_clipboard_for_ascii(self):
        u2 = FakeU2()
        d = Device(use_u2=True)
        d._u2 = u2
        calls = []
        d._run = lambda *a: calls.append(a) or b""
        d.type_private("9943 08")
        self.assertEqual(calls, [("shell", "input", "text", "9943%s08")])
        self.assertFalse(hasattr(u2, "typed"))
