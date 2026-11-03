"""A real phone connected through the app: the socket protocol, RemoteDevice, and tasks that run on it."""
import base64
import io
import threading
import time
import unittest

from PIL import Image

from agent.remote import PhoneHub, PhoneOffline, RemoteDevice
from test_chat import Spark, Worker, act, msgs, say, setup, texts, until


def png_b64(color=(40, 80, 160)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeAndroid:
    """What the Mobile Agent app does on a phone: answers each request. Runs in a thread next to a TestClient websocket."""
    def __init__(self, ws, installed=("com.supercell.clashofclans",)):
        self.ws, self.log, self.installed = ws, [], list(installed)
        self.answers = {
            "size": lambda a: [1080, 2400],
            "screenshot": lambda a: png_b64(),
            "elements": lambda a: {"keyboard": False, "elements": [{"label": "Play", "center": [540, 1200], "clickable": True, "state": ""}]},
            "packages": lambda a: self.installed, "launcher_apps": lambda a: self.installed,
            "foreground": lambda a: "com.supercell.clashofclans", "crashes": lambda a: [],
        }
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            while True:
                msg = self.ws.receive_json()
                self.log.append((msg["op"], msg["args"]))
                fn = self.answers.get(msg["op"], lambda a: None)
                self.ws.send_json({"id": msg["id"], "ok": True, "result": fn(msg["args"])})
        except Exception:
            pass  # the test closed the socket


def connect(c, app, **hello):
    ws = c.websocket_connect("/api/phone/ws")
    sock = ws.__enter__()
    sock.send_json({"type": "hello", "name": "Pixel 8", "model": "Pixel 8", "sdk": 34, "size": [1080, 2400], **hello})
    until(lambda: app.state.phones.online())
    phone = FakeAndroid(sock)
    phone.thread.start()
    return ws, phone


class Protocol(unittest.TestCase):
    def test_a_connected_phone_answers_device_calls(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        ws, phone = connect(c, app)
        try:
            dev = RemoteDevice(app.state.phones, app.state.phones.pick())
            self.assertEqual(dev.size(), (1080, 2400))
            els = dev.elements()
            self.assertEqual([(e.id, e.label, e.center, e.clickable) for e in els], [(0, "Play", (540, 1200), True)])
            self.assertEqual(Image.open(io.BytesIO(dev.screenshot())).size, (1080, 2400))
            dev.tap(540, 1200)
            dev.key("KEYCODE_BACK")
            self.assertIn(("tap", {"x": 540, "y": 1200}), phone.log)
            self.assertIn(("key", {"name": "KEYCODE_BACK"}), phone.log)
            self.assertEqual(dev.packages(), ["com.supercell.clashofclans"])
            self.assertFalse(dev.is_emulator())
            self.assertEqual(c.get("/api/phones").json()[0]["name"], "Pixel 8")
        finally:
            ws.__exit__(None, None, None)

    def test_jpeg_screenshots_are_turned_into_png(self):
        hub = PhoneHub()
        buf = io.BytesIO()
        Image.new("RGB", (100, 200), (200, 0, 0)).save(buf, "JPEG")
        hub.call = lambda pid, op, args=None, **k: base64.b64encode(buf.getvalue()).decode()
        png = RemoteDevice(hub, "x").screenshot()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_missing_phone_fails_fast_with_a_clear_reason(self):
        with self.assertRaises(PhoneOffline) as cm:
            PhoneHub().call("nobody", "size", grace=0)
        self.assertIn("Mobile Agent app", str(cm.exception))

    def test_the_phone_going_away_fails_a_call_in_flight(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        ws = c.websocket_connect("/api/phone/ws")
        sock = ws.__enter__()
        sock.send_json({"type": "hello", "name": "P"})
        until(lambda: app.state.phones.online())
        err = {}

        def ask():
            try:
                RemoteDevice(app.state.phones, "local").tap(1, 1)
            except Exception as e:
                err["e"] = e
        t = threading.Thread(target=ask)
        t.start()
        sock.receive_json()  # the request arrived, and the phone never answers
        ws.__exit__(None, None, None)
        t.join(10)
        self.assertIsInstance(err.get("e"), PhoneOffline)
        self.assertFalse(app.state.phones.online())


def jobs_of(c, bot):
    return c.get(f"/api/bots/{bot}/jobs").json()


class PhoneBot(unittest.TestCase):
    def test_connecting_a_phone_creates_its_own_persistent_bot_once(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        ws, phone = connect(c, app)
        try:
            pb = app.state.db.phone_bot("local")
            self.assertEqual((pb["kind"], pb["name"], pb["phone_id"]), ("phone", "Pixel 8", "local"))  # named after the device
            self.assertTrue(any("is paired" in m["content"] for m in c.get(f"/api/bots/{pb['id']}/state").json()["messages"]))
        finally:
            ws.__exit__(None, None, None)
        until(lambda: not app.state.phones.online())
        self.assertEqual(app.state.db.phone_bot("local")["id"], pb["id"])  # the bot stays when the phone leaves
        ws2, _ = connect(c, app)
        try:
            self.assertEqual(len([b for b in app.state.db.list_bots() if b["kind"] == "phone"]), 1)  # and is reused when it comes back
        finally:
            ws2.__exit__(None, None, None)

    def test_two_phones_with_one_device_name_get_a_number_added(self):
        from agent.master import ensure_phone_bot
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        db = app.state.db
        a = ensure_phone_bot(db, None, "p1", {"name": "Pixel 8"})
        b = ensure_phone_bot(db, None, "p2", {"name": "Pixel 8"})
        self.assertEqual((a["name"], b["name"]), ("Pixel 8", "Pixel 8 2"))
        self.assertEqual(ensure_phone_bot(db, None, "p2", {"name": "Pixel 8"})["id"], b["id"])  # reconnecting reuses it

    def test_a_task_of_the_phone_bot_runs_on_the_phone_and_never_touches_the_emulator(self):
        c, emulator, bot, app = setup(Spark(act(type="reply", text="ok")), Worker(act(type="tap", element=0), act(type="done", answer="played")))
        ws, phone = connect(c, app)
        try:
            pb = app.state.db.phone_bot("local")["id"]
            phone.answers["foreground"] = lambda a: "com.android.settings"  # an ordinary app in front: the game guard is about Supercell and friends
            app.state.jobs.submit(pb, "open the game")
            until(lambda: jobs_of(c, pb)[0]["status"] == "done", timeout=30)
            self.assertIn(("tap", {"x": 540, "y": 1200}), phone.log)
            self.assertEqual(emulator.taps, [])
            self.assertEqual(jobs_of(c, pb)[0]["result"], "played")
            self.assertEqual(jobs_of(c, pb)[0]["target"], "phone:local")
        finally:
            ws.__exit__(None, None, None)

    def test_other_bots_never_use_the_phone_even_when_it_is_connected(self):
        c, emulator, bot, app = setup(Spark(act(type="reply", text="ok")), Worker(act(type="tap", element=0), act(type="done", answer="ok")))
        ws, phone = connect(c, app)
        try:
            app.state.jobs.submit(bot, "open settings")
            until(lambda: jobs_of(c, bot)[0]["status"] == "done", timeout=30)
            self.assertTrue(emulator.taps)
            self.assertNotIn("tap", [op for op, _ in phone.log])
            self.assertFalse(jobs_of(c, bot)[0]["target"])
        finally:
            ws.__exit__(None, None, None)

    def test_a_task_for_a_phone_that_is_not_connected_fails_and_says_how_to_fix_it(self):
        c, emulator, bot, app = setup(Spark(act(type="reply", text="your phone is offline")))
        pb = app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")["id"]
        app.state.jobs.submit(pb, "open the game")
        until(lambda: jobs_of(c, pb)[0]["status"] == "failed", timeout=20)
        self.assertIn("not connected", jobs_of(c, pb)[0]["result"])
        self.assertEqual(emulator.taps, [])  # and it did not fall back to the emulator

    def test_the_phone_bot_queues_nothing_while_the_phone_is_offline_and_tells_the_owner(self):
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, _, bot, app = setup(spark)
        pb = app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")["id"]
        say(c, pb, "open clash of clans")
        until(lambda: any("not connected" in t for t in texts(c, pb, "assistant")))
        self.assertEqual(jobs_of(c, pb), [])
        self.assertIn("NOT connected", spark.calls[0][0])

    def test_the_phone_bot_runs_the_owners_request_on_the_phone(self):
        spark = Spark(act(type="mobile_task", goal="open clash of clans and collect resources", say="On it."), act(type="reply", text="collected"))
        c, emulator, bot, app = setup(spark, Worker(act(type="done", answer="collected")))
        ws, phone = connect(c, app)
        try:
            pb = app.state.db.phone_bot("local")["id"]
            say(c, pb, "open clash of clans and collect my resources")
            until(lambda: jobs_of(c, pb) and jobs_of(c, pb)[0]["status"] == "done", timeout=30)
            self.assertIn("You are the bot of Pixel 8, a phone paired through the Mobile Agent app", spark.calls[0][0])
            self.assertIn("The phone is connected now", spark.calls[0][0])
            self.assertTrue(phone.log)
            self.assertEqual(emulator.taps, [])
        finally:
            ws.__exit__(None, None, None)

    def test_other_bots_are_told_the_phone_has_its_own_bot(self):
        spark = Spark(act(type="reply", text="ok"))
        c, _, bot, app = setup(spark)
        app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")
        say(c, bot, "hello")
        until(lambda: "ok" in texts(c, bot, "assistant"))
        self.assertIn("Phones paired through the Mobile Agent app have their own bots: Pixel 8", spark.calls[0][0])


if __name__ == "__main__":
    unittest.main()
