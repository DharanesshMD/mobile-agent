"""Finding the emulator on the Docker host through its adb server."""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import hostemulator as he


def run_with(out):
    return lambda cmd, **kw: SimpleNamespace(stdout=out)


class Host(unittest.TestCase):
    def test_probe_reads_a_ready_emulator_and_ignores_offline_ones(self):
        self.assertEqual(he.probe(run_with("List of devices attached\nemulator-5554\tdevice\n\n")), "emulator-5554")
        self.assertIsNone(he.probe(run_with("List of devices attached\nemulator-5554\toffline\nabc123\tdevice\n")))
        self.assertIsNone(he.probe(run_with("")))

    def test_probe_survives_no_adb_and_a_hung_server(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 6)
        self.assertIsNone(he.probe(boom))
        self.assertIsNone(he.probe(lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError())))

    def test_probe_names_the_remote_server_so_it_never_starts_or_kills_one(self):
        seen = []
        he.probe(lambda cmd, **kw: seen.append(cmd) or SimpleNamespace(stdout=""))
        self.assertEqual(seen[0][:5], ["adb", "-H", he.HOST, "-P", "5037"])

    def test_a_found_emulator_is_saved_once_and_the_app_restarts(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADB_SERVER_SOCKET", None)
            saved, restarts = {}, []
            h = he.HostEmulator(saved.__setitem__, lambda: restarts.append(1), probe_fn=lambda: "emulator-5554")
            h.check(); h.check()
            self.assertEqual(saved["ANDROID_SERIAL"], "emulator-5554")
            self.assertEqual(saved["ADB_SERVER_SOCKET"], "tcp:host.docker.internal:5037")
            self.assertEqual(restarts, [1])

    def test_nothing_is_done_when_no_emulator_or_already_configured(self):
        saved, restarts = {}, []
        with mock.patch.dict(os.environ, {"ADB_SERVER_SOCKET": ""}):
            os.environ.pop("ADB_SERVER_SOCKET")
            he.HostEmulator(saved.__setitem__, lambda: restarts.append(1), probe_fn=lambda: None).check()
        with mock.patch.dict(os.environ, {"ADB_SERVER_SOCKET": "tcp:host.docker.internal:5037"}):
            he.HostEmulator(saved.__setitem__, lambda: restarts.append(1), probe_fn=lambda: "emulator-5554").check()
        self.assertEqual((saved, restarts), ({}, []))


if __name__ == "__main__":
    unittest.main()
