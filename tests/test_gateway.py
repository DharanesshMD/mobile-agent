"""The phone gateway: one-time links, the route allow-list, the two clocks, pairing through it, history and revoke."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent import gateway as gwmod
from agent.gateway import LinkTokens, Limiter, PhoneGateway, apk_info, hide_secrets, lan_address
from agent.remote import RemoteDevice
from test_chat import Spark, act, setup, until


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeListener:
    started = stopped = 0

    def __init__(self, app, host, port):
        self.host, self.port = host, port

    def start(self):
        FakeListener.started += 1

    def stop(self):
        FakeListener.stopped += 1


class FakePhones:
    def __init__(self, via=()):
        self.via = list(via)

    def listing(self):
        return [{"id": v, "via": "gateway"} for v in self.via]


def make_gateway(clock=None, apk: bytes | None = b"APK-BYTES", phones=None, pair_ok=True):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "mobile-agent.apk"
    if apk is not None:
        path.write_bytes(apk)
    calls = {"codes": 0, "revoked": []}

    def issue_code():
        calls["codes"] += 1
        return "ABCD-EFGH"

    def do_pair(code, name):
        if not pair_ok or code != "ABCD-EFGH":
            from fastapi import HTTPException
            raise HTTPException(401, "invalid or expired pairing code")
        return "dev1", "dev_secrettoken"

    class Store:
        def find_device(self, token):
            return {"id": "dev1", "name": "Pixel"} if token == "dev_secrettoken" else None

    gw = PhoneGateway(Store(), phones or FakePhones(), do_pair, issue_code, lambda dev: [{"id": "j1", "goal": "g"}],
                      lambda dev: calls["revoked"].append(dev["id"]), path, 8765, clock=clock or time.time,
                      listener_factory=FakeListener, lan=lambda: "192.168.1.20")
    return gw, calls, TestClient(gw.app)


class Tokens(unittest.TestCase):
    def test_a_link_expires_and_downloads_are_capped(self):
        clock = Clock()
        t = LinkTokens(clock, ttl=600)
        tok = t.issue()
        self.assertTrue(t.view(tok))
        self.assertTrue(all(t.download(tok) for _ in range(3)))
        self.assertFalse(t.download(tok))  # the fourth download is refused
        tok2 = t.issue()
        clock.t += 601
        self.assertFalse(t.view(tok2))  # expired
        self.assertFalse(t.view("not-a-token"))

    def test_tokens_are_random_32_bytes_and_stored_only_as_hashes(self):
        t = LinkTokens()
        a, b = t.issue(), t.issue()
        self.assertNotEqual(a, b)
        self.assertGreaterEqual(len(a), 43)
        self.assertNotIn(a, t._items)

    def test_bad_tokens_lock_an_address(self):
        clock = Clock()
        lim = Limiter(clock)
        for _ in range(gwmod.FAIL_LIMIT):
            lim.fail("1.2.3.4")
        self.assertFalse(lim.allow("1.2.3.4"))
        clock.t += gwmod.FAIL_LOCK_S + 1
        self.assertTrue(lim.allow("1.2.3.4"))


class Door(unittest.TestCase):
    def test_only_the_allow_listed_routes_exist(self):
        gw, _, c = make_gateway()
        for path in ("/", "/docs", "/openapi.json", "/api/bots", "/api/device/screenshot", "/api/mobile/state", "/api/runs", "/api/gateway/open", "/api/devices", "/api/phones"):
            self.assertEqual(c.get(path).status_code, 404, path)
        self.assertEqual(c.post("/api/bots/x/messages", json={"text": "hi"}).status_code, 404)

    def test_the_landing_page_and_the_apk_need_an_open_window_and_the_right_token(self):
        gw, _, c = make_gateway()
        self.assertEqual(c.get("/get/whatever").status_code, 404)  # the door is closed
        state = gw.open()
        token = state["url"].rsplit("/", 1)[1]
        self.assertEqual(c.get("/get/wrong").status_code, 404)
        r = c.get(f"/get/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("mobileagent://pair?server=http://192.168.1.20:8765&amp;code=ABCD-EFGH", r.text)
        self.assertIn("SHA-256", r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(r.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertNotIn("http://", r.text.replace("http://192.168.1.20:8765", ""))  # no other address, nothing external
        for _ in range(3):
            d = c.get(f"/apk/{token}")
            self.assertEqual((d.status_code, d.content), (200, b"APK-BYTES"))
        self.assertIn("mobile-agent.apk", d.headers["content-disposition"])
        self.assertEqual(c.get(f"/apk/{token}").status_code, 404)  # the cap

    def test_a_missing_apk_is_said_plainly_on_the_page_and_404_on_download(self):
        gw, _, c = make_gateway(apk=None)
        token = gw.open()["url"].rsplit("/", 1)[1]
        self.assertIn("build_apk.sh", c.get(f"/get/{token}").text)
        self.assertEqual(c.get(f"/apk/{token}").status_code, 404)
        self.assertFalse(gw.state()["apk"]["present"])

    def test_pairing_closes_the_window_soon_and_kills_the_links(self):
        clock = Clock()
        gw, calls, c = make_gateway(clock)
        token = gw.open()["url"].rsplit("/", 1)[1]
        self.assertEqual(c.post("/api/pair", json={"code": "WRONG-CODE", "name": "x"}).status_code, 401)
        r = c.post("/api/pair", json={"code": "ABCD-EFGH", "name": "Pixel 8"})
        self.assertEqual(r.json(), {"device_id": "dev1", "token": "dev_secrettoken"})
        self.assertEqual(c.get(f"/get/{token}").status_code, 404)  # the link died with the pairing
        self.assertLessEqual(gw.window_until - clock.t, gwmod.AFTER_PAIR_S)
        self.assertEqual(gw.state()["paired"][0]["name"], "Pixel 8")

    def test_history_and_revoke_need_a_device_token(self):
        gw, calls, c = make_gateway()
        self.assertEqual(c.get("/api/me/history").status_code, 401)
        self.assertEqual(c.get("/api/me/history", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(c.get("/api/me/history", headers={"Authorization": "Bearer dev_secrettoken"}).json()[0]["id"], "j1")
        self.assertEqual(c.post("/api/me/revoke").status_code, 401)
        self.assertEqual(c.post("/api/me/revoke", headers={"Authorization": "Bearer dev_secrettoken"}).json(), {"ok": True})
        self.assertEqual(calls["revoked"], ["dev1"])

    def test_the_phone_socket_refuses_a_bad_token(self):
        gw, _, c = make_gateway()
        with self.assertRaises(Exception):
            with c.websocket_connect("/api/phone/ws?token=nope") as ws:
                ws.receive_json()


class Clocks(unittest.TestCase):
    def setUp(self):
        FakeListener.started = FakeListener.stopped = 0

    def test_the_window_closes_after_15_minutes_and_the_listener_2_minutes_later_when_no_phone_is_connected(self):
        clock = Clock()
        gw, _, c = make_gateway(clock)
        gw.open()
        self.assertTrue(gw.window_open() and gw.listening)
        clock.t += gwmod.WINDOW_S + 1
        gw.tick()
        self.assertFalse(gw.window_open())
        self.assertTrue(gw.listening)  # idle clock just started
        clock.t += gwmod.IDLE_S + 1
        gw.tick()
        self.assertFalse(gw.listening)
        self.assertEqual(FakeListener.stopped, 1)
        self.assertIsNone(gw.state()["url"])

    def test_a_connected_phone_keeps_the_listener_on_after_the_window_closes(self):
        clock = Clock()
        phones = FakePhones(via=["dev1"])
        gw, _, _ = make_gateway(clock, phones=phones)
        gw.open()
        clock.t += gwmod.WINDOW_S + 1
        gw.tick()
        clock.t += gwmod.IDLE_S * 5
        gw.tick()
        self.assertTrue(gw.listening)  # the paired phone is still connected through it
        phones.via.clear()
        gw.tick()
        clock.t += gwmod.IDLE_S + 1
        gw.tick()
        self.assertFalse(gw.listening)

    def test_no_lan_address_is_said_honestly_and_nothing_listens(self):
        gw, _, _ = make_gateway()
        gw.lan = lambda: None
        s = gw.open()
        self.assertFalse(s["listening"])
        self.assertIn("No local network address", s["error"])
        self.assertEqual(FakeListener.started, 0)

    def test_closing_stops_the_listener_and_forgets_the_links(self):
        gw, _, c = make_gateway()
        token = gw.open()["url"].rsplit("/", 1)[1]
        gw.close()
        self.assertFalse(gw.listening)
        self.assertEqual(c.get(f"/get/{token}").status_code, 404)


class Helpers(unittest.TestCase):
    def test_lan_detection_accepts_only_a_private_address(self):
        for ip, want in (("192.168.1.20", "192.168.1.20"), ("10.0.0.5", "10.0.0.5"), ("8.8.8.8", None), ("127.0.0.1", None), ("169.254.3.4", None)):
            with mock.patch("agent.gateway.socket.socket") as sock:
                sock.return_value.getsockname.return_value = (ip, 5)
                self.assertEqual(lan_address(), want, ip)
        with mock.patch("agent.gateway.socket.socket") as sock:
            sock.return_value.connect.side_effect = OSError("no network")
            self.assertIsNone(lan_address())

    def test_apk_info_has_size_and_sha256(self):
        tmp = Path(tempfile.mkdtemp()) / "a.apk"
        self.assertFalse(apk_info(tmp)["present"])
        tmp.write_bytes(b"abc")
        info = apk_info(tmp)
        self.assertEqual((info["size"], info["sha256"]), (3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"))

    def test_secrets_are_hidden(self):
        self.assertEqual(hide_secrets("token dev_abcdefghijklmnopqrstuvwx ok"), "token [hidden] ok")


class Integrated(unittest.TestCase):
    def test_pairing_through_the_gateway_creates_the_phone_bot_and_the_hub_can_drive_it(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        gw = app.state.gateway
        gw.listener_factory, gw.lan = FakeListener, (lambda: "192.168.1.20")
        state = c.post("/api/gateway/open").json()
        self.assertTrue(state["window"] and state["url"].startswith("http://192.168.1.20:8765/get/"))
        g = TestClient(gw.app)
        code = state["code"]
        r = g.post("/api/pair", json={"code": code, "name": "Pixel 8"})
        self.assertEqual(r.status_code, 200)
        token, did = r.json()["token"], r.json()["device_id"]
        with g.websocket_connect(f"/api/phone/ws?token={token}") as ws:
            ws.send_json({"type": "hello", "name": "Pixel 8", "model": "Pixel 8", "sdk": 34, "size": [1080, 2400]})
            until(lambda: app.state.phones.online(did))
            self.assertEqual(app.state.db.phone_bot(did)["name"], "Pixel 8")
            self.assertEqual(app.state.phones.listing()[0]["via"], "gateway")
            self.assertTrue(c.get("/api/gateway/state").json()["connected"])
            out = {}
            t = threading.Thread(target=lambda: out.update(size=RemoteDevice(app.state.phones, did).size()))  # a call crosses from the main app to the gateway's loop
            t.start()
            req = ws.receive_json()
            self.assertEqual(req["op"], "size")
            ws.send_json({"id": req["id"], "ok": True, "result": [1080, 2400]})
            t.join(10)
            self.assertEqual(out["size"], (1080, 2400))
            self.assertEqual(c.get("/api/devices").json()[0]["id"], did)

    def test_main_app_history_and_revoke_are_device_only(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        self.assertEqual(c.get("/api/me/history").status_code, 401)  # even with no APP_TOKEN set
        self.assertEqual(c.post("/api/me/revoke").status_code, 401)
        code = app.state.issue_pair_code()
        r = c.post("/api/pair", json={"code": code, "name": "Pixel"}).json()
        h = {"Authorization": "Bearer " + r["token"]}
        self.assertEqual(c.get("/api/me/history", headers=h).json(), [])  # no phone bot yet
        pb = app.state.db.create_bot("Pixel", "", kind="phone", phone_id=r["device_id"])
        job = app.state.db.create_job(pb["id"], "open Settings, token dev_abcdefghijklmnopqrstuvwxyz1234")
        app.state.db.update_job(job["id"], status="done", result="done by someone@example.com " + "x" * 300)
        rows = c.get("/api/me/history", headers=h).json()
        self.assertEqual(rows[0]["status"], "done")
        self.assertNotIn("dev_abcdefghijklmnopqrstuvwxyz1234", rows[0]["goal"])
        self.assertLessEqual(len(rows[0]["result"]), 240)
        self.assertEqual(c.post("/api/me/revoke", headers=h).json(), {"ok": True})
        self.assertEqual(c.get("/api/me/history", headers=h).status_code, 401)  # the token stopped working
        self.assertEqual(c.get("/api/devices").json(), [])


if __name__ == "__main__":
    unittest.main()
