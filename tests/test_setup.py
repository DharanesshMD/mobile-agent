"""The Setup endpoints: the key is checked before it is saved, the address must be a private LAN one, and saved values survive a restart."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import setup


class FakeGateway:
    def state(self):
        return {"apk": {"present": False}, "address": None}


class FakePhones:
    def listing(self):
        return []


class FakeInstaller:
    def __init__(self, available=False):
        self.a, self.started = available, []

    def available(self):
        return self.a

    def state(self):
        return {"state": "idle", "step": "", "pct": 0, "message": "", "error": ""}

    def start(self, accepted):
        if not accepted:
            raise ValueError("Tick the box")
        self.started.append(1)
        return {"state": "running"}


class Setup(unittest.TestCase):
    def make(self, installer=None):
        self.d = tempfile.mkdtemp()
        app = FastAPI()
        setup.register_setup(app, lambda: None, self.d, FakeGateway(), FakePhones(), installer or FakeInstaller())
        return TestClient(app)

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MOBILE_AGENT_DOCKER": "1"}, clear=False)
        self.env.start()
        os.environ.pop("META_API_KEY", None)
        os.environ.pop("PHONE_GATEWAY_ADVERTISE_IP", None)
        setup.SOURCE.clear()

    def tearDown(self):
        self.env.stop()

    def test_state_says_what_is_missing(self):
        s = self.make().get("/api/setup").json()
        self.assertFalse(s["key"]["set"])
        self.assertTrue(s["phone"]["needs_address"])
        self.assertFalse(s["emulator"]["available"])
        self.assertEqual(s["emulator"]["install"]["state"], "idle")

    def test_a_key_is_saved_only_after_the_api_accepts_it(self):
        c = self.make()
        with mock.patch.object(setup, "check_key", return_value="The model API did not accept this key."):
            r = c.post("/api/setup/key", json={"key": "bad"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(os.environ.get("META_API_KEY"))
        with mock.patch.object(setup, "check_key", return_value=None):
            self.assertEqual(c.post("/api/setup/key", json={"key": "good-key"}).status_code, 200)
        self.assertEqual(os.environ["META_API_KEY"], "good-key")
        f = Path(self.d) / "settings.env"
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("good-key", str(c.get("/api/setup").json()))
        os.environ.pop("META_API_KEY")
        setup.load_saved(f)  # a restart
        self.assertEqual(os.environ["META_API_KEY"], "good-key")

    def test_the_address_must_be_a_private_lan_address(self):
        c = self.make()
        for bad in ("nope", "127.0.0.1", "8.8.8.8", "169.254.1.1", ""):
            self.assertEqual(c.post("/api/setup/address", json={"ip": bad}).status_code, 400, bad)
        self.assertEqual(c.post("/api/setup/address", json={"ip": "192.168.1.20"}).status_code, 200)
        self.assertFalse(c.get("/api/setup").json()["phone"]["needs_address"])

    def test_a_key_with_spaces_is_refused_without_calling_the_api(self):
        with mock.patch.object(setup, "check_key") as ck:
            self.assertEqual(self.make().post("/api/setup/key", json={"key": "a b"}).status_code, 400)
        ck.assert_not_called()

    def test_the_emulator_install_needs_the_licence_tick(self):
        inst = FakeInstaller()
        c = self.make(inst)
        self.assertEqual(c.post("/api/setup/emulator", json={"accept": False}).status_code, 400)
        self.assertEqual(c.post("/api/setup/emulator", json={"accept": "yes"}).status_code, 400)
        self.assertEqual(inst.started, [])
        self.assertEqual(c.post("/api/setup/emulator", json={"accept": True}).status_code, 200)
        self.assertEqual(inst.started, [1])


if __name__ == "__main__":
    unittest.main()
