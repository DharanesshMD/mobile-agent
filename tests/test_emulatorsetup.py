"""The one-click emulator installer, with the downloads and the SDK tools faked."""
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from agent import emulatorsetup as es


def fake_fetch(url, dest, progress):
    progress(50)
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("cmdline-tools/bin/sdkmanager", "#!/bin/sh\n")
        z.writestr("cmdline-tools/bin/avdmanager", "#!/bin/sh\n")


class Installer(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"ANDROID_HOME": d + "/sdk", "ANDROID_AVD_HOME": d + "/avd"})
        self.env.start()
        self.saved, self.calls, self.restarted = {}, [], []

    def tearDown(self):
        self.env.stop()

    def make(self, code=0, avail=True):
        def run(cmd, progress, stdin="", env=None):
            self.calls.append((Path(cmd[0]).name, cmd[1:], stdin[:4]))
            progress(40, "Downloading")
            return code
        return es.EmulatorInstaller(self.saved.__setitem__, lambda: self.restarted.append(1), fetch=fake_fetch, run=run, available=lambda: avail)

    def wait(self, inst):
        for _ in range(100):
            if inst.state()["state"] not in ("running",):
                return
            time.sleep(0.05)

    def test_it_refuses_without_the_licence_tick(self):
        with self.assertRaises(ValueError):
            self.make().start(False)

    def test_the_steps_run_in_order_and_the_app_restarts(self):
        i = self.make()
        i.start(True)
        self.wait(i)
        self.assertEqual(i.state()["state"], "restarting")
        names = [c[0] for c in self.calls]
        self.assertEqual(names, ["sdkmanager", "sdkmanager", "avdmanager"])
        self.assertIn("--licenses", self.calls[0][1])
        self.assertIn(es.image_package(), self.calls[1][1])
        self.assertIn("emulator", self.calls[1][1])
        self.assertEqual(self.calls[0][2], "y\ny\n")
        self.assertEqual(Path(self.saved["ANDROID_HOME"]).resolve(), Path(os.environ["ANDROID_HOME"]).resolve())
        self.assertEqual(self.restarted, [1])

    def test_a_failed_install_says_why_and_does_not_restart(self):
        i = self.make(code=1)
        i.start(True)
        self.wait(i)
        self.assertEqual(i.state()["state"], "failed")
        self.assertTrue(i.state()["error"])
        self.assertEqual(self.restarted, [])

    def test_installed_but_not_seen_is_a_failure_not_a_restart_loop(self):
        i = self.make(avail=False)
        i.start(True)
        self.wait(i)
        self.assertEqual(i.state()["state"], "failed")
        self.assertEqual(self.restarted, [])

    def test_linux_on_arm_is_refused_up_front(self):
        with mock.patch.object(es, "host_os", return_value="linux"), mock.patch("platform.machine", return_value="aarch64"):
            self.assertFalse(es.supported())
            with self.assertRaises(ValueError):
                self.make().start(True)
        with mock.patch.object(es, "host_os", return_value="mac"), mock.patch("platform.machine", return_value="arm64"):
            self.assertTrue(es.supported())

    def test_the_host_picks_the_right_download_and_image(self):
        with mock.patch("platform.machine", return_value="arm64"):
            self.assertIn("arm64-v8a", es.image_package())
        with mock.patch("platform.machine", return_value="x86_64"):
            self.assertIn("x86_64", es.image_package())
        self.assertIn(es.TOOLS_BUILD, es.tools_url())


if __name__ == "__main__":
    unittest.main()
