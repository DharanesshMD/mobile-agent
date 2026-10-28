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
