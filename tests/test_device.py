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


class LiveTouchInjection(unittest.TestCase):
    """A gesture uses one injector from down to up; mixing them leaves the finger pressed and the whole phone ignores touches."""

    class FakeTouch:
        def __init__(self, fail_on=()):
            self.log, self.fail_on = [], set(fail_on)

        def __getattr__(self, kind):
            def go(x, y):
                if kind in self.fail_on:
                    raise RuntimeError("u2 lost")
                self.log.append(kind)
            return go

    def device(self, fail_on=()):
        d = Device(use_u2=True)
        d._console = False  # these tests are about uiautomator2 and adb; the console route has its own tests below
        d._u2 = mock.Mock()
        d._u2.touch = self.FakeTouch(fail_on)
        calls = []
        d._run = lambda *a: calls.append(a[-3:]) or b""
        return d, calls

    def test_a_gesture_stays_on_uiautomator2_when_it_works(self):
        d, calls = self.device()
        for k in ("down", "move", "up"):
            d.touch(k, 5, 6)
        self.assertEqual(d._u2.touch.log, ["down", "move", "up"])
        self.assertEqual(calls, [])

    def test_a_gesture_whose_down_failed_goes_entirely_through_adb(self):
        d, calls = self.device(fail_on=("down",))
        for k in ("down", "move", "up"):
            d.touch(k, 5, 6)
        self.assertEqual(d._u2.touch.log, [])  # no u2 up for an adb down
        self.assertEqual([c[0] for c in calls], ["DOWN", "MOVE", "UP"])

    def test_losing_uiautomator2_midway_lifts_the_finger_both_ways(self):
        d, calls = self.device(fail_on=("move",))
        d.touch("down", 5, 6)
        d.touch("move", 5, 7)
        self.assertEqual(d._u2.touch.log, ["down", "up"])  # release_touch: u2 up ...
        self.assertEqual([c[0] for c in calls], ["UP"])    # ... and the adb up


class ConsoleTouch(unittest.TestCase):
    """On an emulator a touch goes through the console as a real touchscreen event: WhatsApp's sign-up screens ignore adb's and uiautomator2's touches."""

    def emulator(self, size=(720, 1600), reply=b"OK\n"):
        import time as _t
        d = Device("emulator-5554", use_u2=False)
        d._size_at = (_t.time() + 1000, size)
        calls = []
        d._run = lambda *a, **k: calls.append(a) or reply
        return d, calls

    def test_a_tap_is_a_down_and_an_up_with_scaled_coordinates(self):
        d, calls = self.emulator()
        d.tap(360, 800)
        self.assertEqual([c[:3] for c in calls], [("emu", "event", "send")] * 2)
        down, up = calls
        self.assertIn("EV_ABS:ABS_MT_POSITION_X:16384", down)  # 360 / 720 of the 0..32767 range
        self.assertIn("EV_ABS:ABS_MT_POSITION_Y:16384", down)
        self.assertIn("EV_KEY:BTN_TOUCH:1", down)
        self.assertIn("EV_ABS:ABS_MT_TRACKING_ID:-1", up)
        self.assertEqual(down[-1], "EV_SYN:0:0")

    def test_a_swipe_moves_in_steps_between_down_and_up(self):
        d, calls = self.emulator()
        d.swipe(360, 1200, 360, 400, ms=120)
        self.assertTrue(len(calls) >= 5)
        self.assertIn("EV_KEY:BTN_TOUCH:1", calls[0])
        self.assertIn("EV_KEY:BTN_TOUCH:0", calls[-1])

    def test_a_rotated_screen_or_a_refusing_console_falls_back_to_adb_input(self):
        d, calls = self.emulator(size=(1600, 720))
        d.tap(100, 200)
        self.assertEqual(calls, [("shell", "input", "tap", "100", "200")])
        d2, calls2 = self.emulator(reply=b"KO: nope\n")
        d2.tap(100, 200)
        self.assertEqual(calls2[-1], ("shell", "input", "tap", "100", "200"))

    def test_a_real_phone_never_uses_the_console(self):
        d = Device("R5CT123", use_u2=False)
        calls = []
        d._run = lambda *a, **k: calls.append(a) or b""
        d.tap(1, 2)
        self.assertEqual(calls, [("shell", "input", "tap", "1", "2")])


class KeyboardTyping(unittest.TestCase):
    """WhatsApp's sign-up fields ignore `input text`, key events and uiautomator2 typing: on an emulator digits are typed by touching the keyboard's own keys."""

    def emulator(self, keys):
        import time as _t
        d = Device("emulator-5554", use_u2=False)
        d.ime_wait_s = 0
        d._size_at = (_t.time() + 1000, (720, 1600))
        taps, other = [], []
        d.tap = lambda x, y: taps.append((x, y))
        d._ime_keys = lambda: keys
        d._run = lambda *a, **k: other.append(a) or b""
        return d, taps, other

    KEYS = {str(i): (100 + i * 10, 1200 + i) for i in range(10)}

    def test_digits_are_typed_by_touching_the_keys_and_nothing_goes_through_input_text(self):
        d, taps, other = self.emulator(self.KEYS)
        d.type_private("99 43-0")
        self.assertEqual(taps, [self.KEYS[c] for c in "99430"])
        self.assertEqual(other, [])

    def test_text_with_letters_or_missing_keys_uses_the_old_way(self):
        d, taps, other = self.emulator(self.KEYS)
        d.type_private("pass1")
        self.assertEqual(taps, [])
        self.assertEqual(other[-1][:3], ("shell", "input", "text"))
        d2, taps2, other2 = self.emulator({"1": (1, 1)})  # no keyboard with a 9 on screen
        d2.type_private("19")
        self.assertEqual(taps2, [])
        self.assertEqual(other2[-1][:3], ("shell", "input", "text"))

    def test_a_real_phone_types_the_old_way(self):
        d = Device("R5CT123", use_u2=False)
        calls = []
        d._run = lambda *a, **k: calls.append(a) or b""
        d._ime_keys = lambda: {str(i): (1, 1) for i in range(10)}
        d.type_private("1234")
        self.assertEqual(calls, [("shell", "input", "text", "1234")])

    def test_it_waits_for_the_keyboard_to_come_up_before_typing(self):
        d, taps, other = self.emulator({})
        d.ime_wait_s = 3
        answers = iter([{}, {}, self.KEYS])
        d._ime_keys = lambda: next(answers)
        d.type_private("55")
        self.assertEqual(taps, [self.KEYS["5"], self.KEYS["5"]])
        self.assertEqual(other, [])
