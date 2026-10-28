import unittest
from unittest import mock

from agent.device import Device


class DeviceBasics(unittest.TestCase):
    def device_with(self, outputs: dict):
        d = Device()
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
