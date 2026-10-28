import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import agent.service as service
from agent.device import parse_accounts
from agent.service import create_app
from agent.store import Store
from test_service import Phone, Script

ADMIN = {"Authorization": "Bearer admin-secret"}

DUMPSYS = """Accounts: 3
  Account {name=me@gmail.com, type=com.google}
  Account {name=me@gmail.com, type=com.google}
  Account {name=work, type=com.example.sync}
  Account {name=other@gmail.com, type=com.google}
Active Sessions: 0
"""


class AccountPhone(Phone):
    def __init__(self):
        super().__init__()
        self.acc: list[tuple[str, str]] = []

    def accounts(self):
        return list(self.acc)


def make(*scripts, store=None):
    phone, queue = AccountPhone(), list(scripts)
    app = create_app(phone, lambda m: queue.pop(0), token="admin-secret", settle=0, store=store or Store(None))
    return TestClient(app), phone, app


def act(**a):
    return a


def wait(c, rid, pred, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/runs/{rid}", headers=ADMIN).json()
        if pred(r):
            return r
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state: {r.get('status')}")


def pair(c, app, name="my phone"):
    code = app.state.issue_pair_code()
    r = c.post("/api/pair", json={"code": code.lower(), "name": name})  # lower case and dash are tolerated
    assert r.status_code == 200, r.text
    return r.json()


class ParseAndStore(unittest.TestCase):
    def test_parse_accounts_dedupes_and_keeps_types(self):
        self.assertEqual(parse_accounts(DUMPSYS), [("me@gmail.com", "com.google"), ("other@gmail.com", "com.google"),
                                                   ("work", "com.example.sync")])
        self.assertEqual(parse_accounts("Accounts: 0\n"), [])

    def test_store_persists_and_never_writes_the_token(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            s = Store(path)
            did, token = s.add_device("phone")
            s.set_cloud_account("me@gmail.com")
            self.assertNotIn(token, path.read_text())
            self.assertEqual(oct(path.stat().st_mode)[-3:], "600")
            again = Store(path)
            self.assertEqual(again.find_device(token)["id"], did)
            self.assertIsNone(again.find_device(token + "x"))
            self.assertEqual(again.cloud_account()["email"], "me@gmail.com")


class Pairing(unittest.TestCase):
    def test_pair_then_use_device_token_and_see_me(self):
        c, _, app = make()
        self.assertEqual(c.get("/api/me").status_code, 401)
        paired = pair(c, app)
        me = c.get("/api/me", headers={"Authorization": "Bearer " + paired["token"]}).json()
        self.assertEqual(me["principal"]["kind"], "device")
        self.assertEqual(me["principal"]["name"], "my phone")
        self.assertIsNone(me["google"])
        self.assertEqual(c.get("/api/me", headers=ADMIN).json()["principal"]["kind"], "admin")

    def test_code_is_single_use_and_wrong_codes_are_rejected(self):
        c, _, app = make()
        code = app.state.issue_pair_code()
        self.assertEqual(c.post("/api/pair", json={"code": "ZZZZ-ZZZZ"}).status_code, 401)
        self.assertEqual(c.post("/api/pair", json={"code": code}).status_code, 200)
        self.assertEqual(c.post("/api/pair", json={"code": code}).status_code, 401)

    def test_expired_code_fails(self):
        c, _, app = make()
        with mock.patch.object(service, "PAIR_TTL", -1):
            code = app.state.issue_pair_code()
        self.assertEqual(c.post("/api/pair", json={"code": code}).status_code, 401)

    def test_five_wrong_codes_lock_pairing_even_for_the_right_code(self):
        c, _, app = make()
        code = app.state.issue_pair_code()
        for _ in range(5):
            self.assertEqual(c.post("/api/pair", json={"code": "AAAA-BBBB"}).status_code, 401)
        self.assertEqual(c.post("/api/pair", json={"code": code}).status_code, 429)

    def test_authenticated_owner_can_issue_codes_and_revoke_devices(self):
        c, _, app = make()
        self.assertEqual(c.post("/api/pair/code").status_code, 401)
        code = c.post("/api/pair/code", headers=ADMIN).json()["code"]
        paired = c.post("/api/pair", json={"code": code, "name": "tablet"}).json()
        devs = c.get("/api/devices", headers=ADMIN).json()
        self.assertEqual([d["name"] for d in devs], ["tablet"])
        self.assertNotIn("hash", json.dumps(devs))
        self.assertEqual(c.delete(f"/api/devices/{paired['device_id']}", headers=ADMIN).status_code, 200)
        self.assertEqual(c.get("/api/me", headers={"Authorization": "Bearer " + paired["token"]}).status_code, 401)


class CloudAccount(unittest.TestCase):
    def test_get_reflects_the_device_as_source_of_truth(self):
        c, phone, _ = make()
        self.assertIsNone(c.get("/api/cloud-account", headers=ADMIN).json()["linked"])
        phone.acc = [("me@gmail.com", "com.google")]  # e.g. the owner signed in through the live view
        r = c.get("/api/cloud-account", headers=ADMIN).json()
        self.assertEqual(r["linked"], "me@gmail.com")
        self.assertEqual(c.get("/api/me", headers=ADMIN).json()["google"], "me@gmail.com")
        phone.acc = []
        self.assertIsNone(c.get("/api/cloud-account", headers=ADMIN).json()["linked"])

    def test_start_requires_consent_and_a_valid_email(self):
        c, _, _ = make()
        self.assertEqual(c.post("/api/cloud-account/start", headers=ADMIN, json={"consent": False}).status_code, 422)
        self.assertEqual(c.post("/api/cloud-account/start", headers=ADMIN, json={"consent": True, "email": "nope"}).status_code, 422)

    def test_provisioning_flow_handoff_then_verified_link(self):
        c, phone, _ = make(Script(act(type="type", text="me@gmail.com"), act(type="handoff", reason="Enter your password"),
                                  act(type="done", answer="signed in")))
        run = c.post("/api/cloud-account/start", headers=ADMIN, json={"consent": True, "email": "me@gmail.com"}).json()
        self.assertEqual(run["kind"], "provision")
        self.assertIn("me@gmail.com", run["goal"])
        self.assertIn("never type a password", run["goal"])
        wait_for_ok = wait(c, run["id"], lambda r: r["status"] == "waiting")
        self.assertEqual(wait_for_ok["pending"]["kind"], "handoff")
        phone.acc = [("me@gmail.com", "com.google")]  # the owner signs in through the phone view
        c.post(f"/api/runs/{run['id']}/resume", json={"done": True}, headers=ADMIN)
        r = wait(c, run["id"], lambda r: r["status"] == "done")
        events = [e for e in r["log"] if e["type"] == "account"]
        self.assertEqual(events[0]["email"], "me@gmail.com")
        info = c.get("/api/cloud-account", headers=ADMIN).json()
        self.assertEqual(info["linked"], "me@gmail.com")
        self.assertEqual(info["consent"]["email_hint"], "me@gmail.com")
        again = c.post("/api/cloud-account/start", headers=ADMIN, json={"consent": True})
        self.assertEqual(again.status_code, 409)

    def test_model_claiming_success_does_not_link_without_a_real_account(self):
        c, _, _ = make(Script(act(type="done", answer="all signed in")))
        run = c.post("/api/cloud-account/start", headers=ADMIN, json={"consent": True}).json()
        r = wait(c, run["id"], lambda r: r["status"] == "done")
        self.assertIsNone([e for e in r["log"] if e["type"] == "account"][0]["email"])
        self.assertIsNone(c.get("/api/cloud-account", headers=ADMIN).json()["linked"])

    def test_remove_needs_confirm_and_an_account_then_verifies_removal(self):
        c, phone, _ = make(Script(act(type="ask", question="Remove me@gmail.com?"), act(type="done", answer="removed")))
        self.assertEqual(c.delete("/api/cloud-account?confirm=true", headers=ADMIN).status_code, 404)
        phone.acc = [("me@gmail.com", "com.google")]
        self.assertEqual(c.delete("/api/cloud-account", headers=ADMIN).status_code, 422)
        run = c.delete("/api/cloud-account?confirm=true", headers=ADMIN).json()
        self.assertEqual(run["kind"], "deprovision")
        self.assertIn("device-activity", run["reminder"])
        wait(c, run["id"], lambda r: r["status"] == "waiting")
        phone.acc = []  # the removal happens on the device
        c.post(f"/api/runs/{run['id']}/resume", json={"approved": True}, headers=ADMIN)
        wait(c, run["id"], lambda r: r["status"] == "done")
        self.assertIsNone(c.get("/api/cloud-account", headers=ADMIN).json()["linked"])

    def test_state_survives_restart_with_a_file_store(self):
        with tempfile.TemporaryDirectory() as d:
            c, phone, app = make(store=Store(Path(d) / "s.json"))
            paired = pair(c, app)
            phone.acc = [("me@gmail.com", "com.google")]
            c.get("/api/cloud-account", headers=ADMIN)
            c2 = TestClient(create_app(AccountPhone(), lambda m: None, token="admin-secret", settle=0, store=Store(Path(d) / "s.json")))
            self.assertEqual(c2.get("/api/me", headers={"Authorization": "Bearer " + paired["token"]}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
