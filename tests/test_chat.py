import json
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from agent.db import Database, fts_query
from agent.hub import Hub
from agent.service import create_app
from test_service import Phone, Script


class Spark:
    """Scripted orchestrator model."""
    def __init__(self, *actions):
        self.replies = [a if isinstance(a, str) else json.dumps({"thought": "t", "action": a}) for a in actions]
        self.calls, self.tokens = [], {"prompt": 0, "completion": 0}

    def chat(self, system, messages, max_tokens=0):
        self.calls.append((system, messages))
        if not self.replies:
            raise RuntimeError("no more scripted replies")
        return self.replies.pop(0)


class Worker(Script):
    def __init__(self, *actions):
        super().__init__(*actions)
        self.prompts = []

    def complete(self, system, prompt, image=None, max_tokens=0):
        self.prompts.append(prompt)
        return super().complete(system, prompt, image, max_tokens)


def act(**a):
    return a


def setup(spark, *workers, db=None):
    queue = list(workers)

    def factory(name):
        if name in ("muse-spark-1.3", "muse-voice-transcribe-1.0"):
            return spark
        assert name == "muse-wonder-1.0", name
        return queue.pop(0)

    tmp = tempfile.mkdtemp()
    phone = Phone()
    app = create_app(phone, factory, settle=0, db=db or Database(":memory:"), data_dir=tmp)
    c = TestClient(app)
    bot = c.get("/api/bots").json()[0]["id"]
    return c, phone, bot, app


def until(fn, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def msgs(c, bot):
    return c.get(f"/api/bots/{bot}/state").json()["messages"]


def texts(c, bot, role=None):
    return [m["content"] for m in msgs(c, bot) if m["kind"] in ("text", "result") and (role is None or m["role"] == role)]


def say(c, bot, text):
    return c.post(f"/api/bots/{bot}/messages", json={"text": text}).json()


def open_card(c, bot):
    return next((m for m in msgs(c, bot) if m["kind"] == "action_item" and m["meta"]["status"] == "open"), None)


class Storage(unittest.TestCase):
    def test_memory_search_is_ranked_scoped_and_safe(self):
        db = Database(":memory:")
        a, b = db.create_bot("A")["id"], db.create_bot("B")["id"]
        db.memory_add(a, "Owner lives in Bangalore and prefers 30 days notice")
        db.memory_add(b, "Owner lives in Mumbai")
        self.assertEqual(db.memory_search(a, "which city Bangalore?"), ["Owner lives in Bangalore and prefers 30 days notice"])
        self.assertEqual(db.memory_search(a, 'x" OR 1=1 -- ;'), [])  # quotes and operators cannot break the query
        self.assertEqual(fts_query("a b"), "")

    def test_hub_replays_from_a_cursor(self):
        h = Hub(keep=3)
        for i in range(5):
            h.publish("b", {"type": "x", "i": i})
        self.assertEqual([e["i"] for e in h.since("b", -1)], [2, 3, 4])  # oldest dropped
        self.assertEqual([e["i"] for e in h.since("b", 3)], [4])
        self.assertEqual(h.last_seq("b"), 4)


class OrchestratorTurns(unittest.TestCase):
    def test_plain_reply_and_bot_list_preview(self):
        c, _, bot, _ = setup(Spark(act(type="reply", text="Hi there")))
        say(c, bot, "hello")
        until(lambda: "Hi there" in texts(c, bot, "assistant"))
        self.assertEqual(c.get("/api/bots").json()[0]["last"], "Hi there")

    def test_bad_json_then_model_failure_are_handled(self):
        c, _, bot, _ = setup(Spark("garbage", "still garbage"))
        say(c, bot, "hello")
        until(lambda: any("could not work out" in t for t in texts(c, bot)))
        c2, _, bot2, _ = setup(Spark())  # raises on first call
        say(c2, bot2, "hello")
        until(lambda: any("hit a problem" in t for t in texts(c2, bot2)))

    def test_memory_tools_loop_then_reply(self):
        spark = Spark(act(type="memory_save", text="Owner prefers short answers"),
                      act(type="memory_search", query="prefers answers"), act(type="reply", text="Noted"))
        c, _, bot, _ = setup(spark)
        say(c, bot, "I prefer short answers")
        until(lambda: "Noted" in texts(c, bot, "assistant"))
        last_msgs = spark.calls[-1][1]
        self.assertIn("Owner prefers short answers", last_msgs[-1]["content"])

    def test_orchestrator_question_card_then_answer_triggers_next_turn(self):
        spark = Spark(act(type="ask_user", kind="choice", question="Which app?", options=["Settings", "Chrome"]),
                      act(type="reply", text="Chrome it is"))
        c, _, bot, _ = setup(spark)
        say(c, bot, "open something")
        card = until(lambda: open_card(c, bot))
        self.assertEqual(card["meta"]["options"], ["Settings", "Chrome"])
        self.assertEqual(c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"choice": "Firefox"}).status_code, 422)
        self.assertEqual(c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"choice": "Chrome"}).status_code, 200)
        until(lambda: "Chrome it is" in texts(c, bot, "assistant"))
        self.assertIn("Chrome", texts(c, bot, "user"))
        self.assertEqual(c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"choice": "Chrome"}).status_code, 409)


class Jobs(unittest.TestCase):
    def test_phone_task_runs_in_background_and_reports(self):
        spark = Spark(act(type="mobile_task", goal="Read the Android version", say="On it"),
                      act(type="reply", text="The phone shows Android 14."))
        c, phone, bot, app = setup(spark, Worker(act(type="tap", element=0), act(type="done", answer="Android 14")))
        say(c, bot, "what android version is the phone?")
        until(lambda: "The phone shows Android 14." in texts(c, bot, "assistant"))
        self.assertIn("On it", texts(c, bot, "assistant"))
        self.assertEqual(phone.taps, [(540, 1800)])
        job = c.get(f"/api/bots/{bot}/jobs").json()[0]
        self.assertEqual((job["status"], job["result"]), ("done", "Android 14"))
        self.assertFalse([m for m in msgs(c, bot) if m["kind"] == "status"])  # live status line is removed
        lib = c.get(f"/api/bots/{bot}/library").json()
        self.assertEqual(len(lib), 1)
        self.assertEqual(c.get(f"/api/library/{lib[0]['id']}/file").headers["content-type"], "image/png")
        self.assertIn("Android 14", app.state.db.memory_search(bot, "Android version")[0])
        self.assertIn("finished with status 'done'", spark.calls[-1][1][-1]["content"])

    def test_one_job_per_bot(self):
        spark = Spark(act(type="mobile_task", goal="g1", say="ok"), act(type="mobile_task", goal="g2", say="ok"))
        c, _, bot, _ = setup(spark, Worker(act(type="handoff", reason="your turn")))
        say(c, bot, "first")
        until(lambda: open_card(c, bot))
        say(c, bot, "second")
        until(lambda: any("already working" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(len(c.get(f"/api/bots/{bot}/jobs").json()), 1)

    def test_worker_confirm_card_and_takeover_rules(self):
        spark = Spark(act(type="mobile_task", goal="send it", say="ok"), act(type="reply", text="sent"))
        c, phone, bot, _ = setup(spark, Worker(act(type="ask", question="Send the message?"), act(type="done", answer="sent")))
        say(c, bot, "send it")
        card = until(lambda: open_card(c, bot))
        self.assertEqual((card["meta"]["kind"], card["content"]), ("confirm", "Send the message?"))
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 200)  # paused at a gate: allowed
        self.assertEqual(c.get("/api/mobile/state").json()["state"], "waiting")
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"approved": True})
        until(lambda: "sent" in texts(c, bot, "assistant"))
        done_card = next(m for m in msgs(c, bot) if m["kind"] == "action_item")
        self.assertEqual((done_card["meta"]["status"], done_card["meta"]["answer"]), ("answered", "Approved"))

    def test_need_info_answered_from_memory_without_asking(self):
        db = Database(":memory:")
        spark = Spark(act(type="mobile_task", goal="fill form", say="ok"),
                      act(type="answer_worker", value="Bangalore"), act(type="reply", text="filled"))
        worker = Worker(act(type="need_info", field="city", why="the form asks for a city"), act(type="done", answer="submitted"))
        c, _, bot, app = setup(spark, worker, db=db)
        db.memory_add(bot, "Owner said city: Bangalore")
        say(c, bot, "fill the form")
        until(lambda: "filled" in texts(c, bot, "assistant"))
        self.assertFalse([m for m in msgs(c, bot) if m["kind"] == "action_item"])
        self.assertIn("owner provided city: Bangalore", worker.prompts[-1])
        self.assertIn("Bangalore", spark.calls[1][1][0]["content"])  # memory hit was shown to the model

    def test_need_info_asks_owner_then_remembers_the_answer(self):
        spark = Spark(act(type="mobile_task", goal="fill form", say="ok"),
                      act(type="ask_user", kind="text", question="Which city should I use?"), act(type="reply", text="filled"))
        worker = Worker(act(type="need_info", field="city", why="form asks"), act(type="done", answer="ok"))
        c, _, bot, app = setup(spark, worker)
        say(c, bot, "fill the form")
        card = until(lambda: open_card(c, bot))
        self.assertEqual(card["meta"]["field"], "city")
        self.assertEqual(c.post(f"/api/items/{card['meta']['item_id']}/answer", json={}).status_code, 422)
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"value": "Mumbai"})
        until(lambda: "filled" in texts(c, bot, "assistant"))
        self.assertIn("owner provided city: Mumbai", worker.prompts[-1])
        self.assertEqual(app.state.db.memory_search(bot, "city"), ["Owner said city: Mumbai"])
        self.assertNotIn("Mumbai", texts(c, bot, "user"))  # a worker answer is not replayed as a chat turn

    def test_otp_card_types_code_and_never_stores_it(self):
        spark = Spark(act(type="mobile_task", goal="sign in", say="ok"), act(type="reply", text="in"))
        c, phone, bot, app = setup(spark, Worker(act(type="request_otp", app="Gmail"), act(type="done", answer="ok")))
        say(c, bot, "sign in")
        card = until(lambda: open_card(c, bot))
        self.assertEqual(card["meta"]["kind"], "otp")
        self.assertEqual(c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"code": "12ab"}).status_code, 422)
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"code": "482913"})
        until(lambda: "in" in texts(c, bot, "assistant"))
        self.assertEqual(phone.typed, ["482913"])
        dump = json.dumps(msgs(c, bot)) + json.dumps(app.state.db._q("select * from items"))
        self.assertNotIn("482913", dump)

    def test_cancel_at_a_gate_closes_the_card_and_stops(self):
        spark = Spark(act(type="mobile_task", goal="g", say="ok"))
        c, _, bot, _ = setup(spark, Worker(act(type="handoff", reason="sign in")))
        say(c, bot, "go")
        until(lambda: open_card(c, bot))
        job = c.get(f"/api/bots/{bot}/jobs").json()[0]
        self.assertEqual(c.post(f"/api/jobs/{job['id']}/cancel").status_code, 200)
        until(lambda: "Stopped the task." in texts(c, bot, "assistant"))
        self.assertIsNone(open_card(c, bot))
        self.assertEqual(c.get(f"/api/bots/{bot}/jobs").json()[0]["status"], "cancelled")

    def test_owner_control_pauses_the_worker_until_released(self):
        spark = Spark(act(type="mobile_task", goal="tap", say="ok"), act(type="reply", text="finished"))
        c, phone, bot, _ = setup(spark, Worker(act(type="tap", element=0), act(type="done", answer="ok")))
        self.assertEqual(c.post("/api/mobile/control", json={"action": "take"}).json()["state"], "control")
        say(c, bot, "tap it")
        until(lambda: c.get("/api/mobile/state").json()["job"])
        time.sleep(0.6)
        self.assertEqual(phone.taps, [])  # the worker waits while the owner is in control
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.1, "fy": 0.1}).status_code, 200)
        c.post("/api/mobile/control", json={"action": "release"})
        until(lambda: "finished" in texts(c, bot, "assistant"))
        self.assertIn((540, 1800), phone.taps)

    def test_restart_fails_leftover_jobs_and_closes_cards(self):
        db = Database(":memory:")
        bot = db.create_bot("A")["id"]
        job = db.create_job(bot, "old")
        db.update_job(job["id"], status="waiting")
        item = db.create_item(bot, job["id"], "confirm", {"kind": "confirm", "question": "?"})
        c, _, _, app = setup(Spark(), db=db)
        self.assertEqual(db.get_job(job["id"])["status"], "failed")
        self.assertEqual(db.get_item(item["id"])["status"], "cancelled")


class Bots(unittest.TestCase):
    def test_create_bot_with_persona_reaches_the_model(self):
        spark = Spark(act(type="reply", text="ok"))
        c, _, _, _ = setup(spark)
        bot = c.post("/api/bots", json={"name": "Shopper", "persona": "You help with groceries."}).json()
        self.assertEqual(c.post("/api/bots", json={"name": ""}).status_code, 422)
        say(c, bot["id"], "hi")
        until(lambda: spark.calls)
        self.assertIn("You help with groceries.", spark.calls[0][0])
        self.assertEqual(c.get("/api/bots/nope/state").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class BotManagement(unittest.TestCase):
    def test_rename_and_delete_remove_everything_the_bot_owns(self):
        import os
        c, _, first, app = setup(Spark())
        bot = c.post("/api/bots", json={"name": "Temp"}).json()["id"]
        r = c.patch(f"/api/bots/{bot}", json={"name": "  Renamed ", "persona": "be brief"})
        self.assertEqual((r.json()["name"], r.json()["persona"]), ("Renamed", "be brief"))
        self.assertEqual(c.patch(f"/api/bots/{bot}", json={"name": "Only name"}).json()["persona"], "be brief")
        self.assertEqual(c.patch("/api/bots/nope", json={"name": "x"}).status_code, 404)
        db = app.state.db
        db.add_message(bot, "user", "text", "hi")
        db.memory_add(bot, "likes tea")
        app.state.scheduler.add(bot, "r", "p", {"every_minutes": 5})
        job = db.create_job(bot, "g")
        db.update_job(job["id"], status="done")
        lib = os.path.join(app.state.jobs.data_dir, "x.png")
        __import__("pathlib").Path(lib).write_bytes(b"x")
        db.add_library(bot, job["id"], "t", lib)
        self.assertEqual(c.delete(f"/api/bots/{bot}").status_code, 200)
        for table in ("messages", "jobs", "items", "library", "routines"):
            self.assertEqual(db._q(f"select * from {table} where bot_id=?", (bot,)), [], table)
        self.assertEqual(db.memory_search(bot, "tea"), [])
        self.assertFalse(os.path.exists(lib))
        self.assertEqual([b["id"] for b in c.get("/api/bots").json()], [first])
        self.assertEqual(c.delete(f"/api/bots/{bot}").status_code, 404)

    def test_deleting_keeps_shared_secrets_and_removes_task_traces(self):
        import os
        import pathlib
        c, _, first, app = setup(Spark())
        bot = c.post("/api/bots", json={"name": "Temp"}).json()["id"]
        app.state.vault.put("netflix", "pw")
        job = app.state.db.create_job(bot, "g")
        app.state.db.update_job(job["id"], status="done")
        trace = pathlib.Path(app.state.jobs.data_dir) / "runs" / job["id"]
        trace.mkdir(parents=True)
        (trace / "step01.png").write_bytes(b"x")
        app.state.db.add_message(bot, "user", "text", "hello")
        pre = c.get(f"/api/bots/{bot}/delete-preview").json()
        self.assertEqual((pre["messages"], pre["tasks"], pre["secrets"], pre["last"], pre["running"]), (1, 1, 1, False, False))
        self.assertEqual(c.delete(f"/api/bots/{bot}?erase_secrets=true").status_code, 409)  # shared with the other bot
        r = c.delete(f"/api/bots/{bot}")
        self.assertEqual((r.status_code, r.json()["bot"]), (200, None))
        self.assertFalse(os.path.exists(trace))
        self.assertEqual([s["label"] for s in c.get("/api/vault").json()], ["netflix"])  # secrets stay

    def test_deleting_the_last_bot_leaves_a_fresh_assistant_and_can_erase_secrets(self):
        c, _, bot, app = setup(Spark())
        app.state.vault.put("netflix", "pw")
        app.state.vault.reveal("netflix", "j", "app")
        app.state.scheduler.add(bot, "r", "p", {"every_minutes": 5})
        self.assertTrue(c.get(f"/api/bots/{bot}/delete-preview").json()["last"])
        r = c.delete(f"/api/bots/{bot}?erase_secrets=true").json()
        self.assertEqual((r["bot"]["name"], r["erased_secrets"]), ("Assistant", 1))
        bots = c.get("/api/bots").json()
        self.assertEqual([b["id"] for b in bots], [r["bot"]["id"]])
        self.assertNotEqual(bots[0]["id"], bot)
        self.assertEqual((c.get("/api/vault").json(), c.get("/api/vault/audit").json()), ([], []))
        self.assertEqual(app.state.db.routines(bot), [])

    def test_last_bot_delete_without_the_erase_option_keeps_secrets(self):
        c, _, bot, app = setup(Spark())
        app.state.vault.put("netflix", "pw")
        c.delete(f"/api/bots/{bot}")
        self.assertEqual(len(c.get("/api/vault").json()), 1)

    def test_a_late_message_for_a_deleted_bot_is_dropped(self):
        c, _, _, app = setup(Spark())
        bot = c.post("/api/bots", json={"name": "Temp"}).json()["id"]
        c.delete(f"/api/bots/{bot}")
        app.state.orch.post(bot, "too late")
        app.state.orch.turn(bot, "note")
        self.assertEqual(app.state.db._q("select * from messages where bot_id=?", (bot,)), [])


class PointerControl(unittest.TestCase):
    def test_drag_long_press_and_pc_keys_reach_the_phone(self):
        c, phone, _, _ = setup(Spark())
        swipes, keys = [], []
        phone.swipe = lambda *a: swipes.append(a)
        phone.key = lambda name: keys.append(name)
        post = lambda path, body: c.post(path, json=body).status_code
        self.assertEqual(post("/api/device/swipe", {"fx1": 0.5, "fy1": 0.8, "fx2": 0.5, "fy2": 0.2, "ms": 180}), 200)
        self.assertEqual(swipes[-1], (540, 1920, 540, 480, 180))  # the fake phone is 1080x2400
        self.assertEqual(post("/api/device/swipe", {"fx1": 0.5, "fy1": 0.5, "fx2": 0.5, "fy2": 0.5, "ms": 900}), 200)  # long press
        self.assertEqual(swipes[-1], (540, 1200, 540, 1200, 900))
        self.assertEqual(post("/api/device/swipe", {"direction": "up"}), 200)
        self.assertEqual(post("/api/device/swipe", {"fx1": 0.5}), 422)
        self.assertEqual(post("/api/device/swipe", {"fx1": 1.5, "fy1": 0, "fx2": 0, "fy2": 0}), 422)
        self.assertEqual(post("/api/device/swipe", {"fx1": 0, "fy1": 0, "fx2": 0, "fy2": 0, "ms": 5}), 422)
        for k, code in (("delete", "KEYCODE_DEL"), ("tab", "KEYCODE_TAB"), ("left", "KEYCODE_DPAD_LEFT"), ("page_down", "KEYCODE_PAGE_DOWN")):
            self.assertEqual(post("/api/device/key", {"key": k}), 200)
            self.assertEqual(keys[-1], code)
        self.assertEqual(post("/api/device/key", {"key": "power"}), 422)


class LiveTouch(unittest.TestCase):
    def test_touch_stream_reaches_the_phone_in_order_and_lifts_on_disconnect(self):
        c, phone, _, _ = setup(Spark())
        events = []
        phone.touch = lambda kind, x, y: events.append((kind, x, y))
        with c.websocket_connect("/api/device/touch") as ws:
            ws.send_json({"t": "move", "fx": 0.5, "fy": 0.5})  # no finger down yet: ignored
            ws.send_json({"t": "down", "fx": 0.5, "fy": 0.5})
            ws.send_json({"t": "move", "fx": 0.5, "fy": 0.25})
            ws.send_json({"t": "bogus", "fx": 0.5, "fy": 0.5})
            ws.send_json({"t": "move", "fx": 2, "fy": 0.5})  # out of range: ignored
            ws.send_json({"t": "up", "fx": 0.5, "fy": 0.2})
            until(lambda: len(events) >= 3)
        self.assertEqual(events[0], ("down", 540, 1200))
        self.assertEqual(events[-1], ("up", 540, 480))
        self.assertTrue(all(e[0] in ("down", "move", "up") for e in events))
        events.clear()
        with c.websocket_connect("/api/device/touch") as ws:
            ws.send_json({"t": "down", "fx": 0.1, "fy": 0.2})
            until(lambda: events)
        until(lambda: events[-1] == ("up", 108, 480))  # browser closed mid-drag: the finger is lifted where it was

    def test_touch_stream_needs_the_token_when_one_is_set(self):
        from starlette.websockets import WebSocketDisconnect
        tmp = __import__("tempfile").mkdtemp()
        app = create_app(Phone(), lambda m: None, token="s3cret", settle=0, db=Database(":memory:"), data_dir=tmp)
        c = TestClient(app)
        with self.assertRaises(WebSocketDisconnect):
            with c.websocket_connect("/api/device/touch") as ws:
                ws.receive_json()
        with c.websocket_connect("/api/device/touch?token=s3cret") as ws:
            ws.send_json({"t": "down", "fx": 0.5, "fy": 0.5})


class BotToBot(unittest.TestCase):
    def two_bots(self, spark):
        c, _, a, app = setup(spark)
        b = c.post("/api/bots", json={"name": "Research", "persona": "knows the owner's travel plans"}).json()["id"]
        return c, a, b, app

    def notes(self, c, bot):
        return [m["content"] for m in msgs(c, bot) if m["kind"] == "bot_note"]

    def test_a_bot_asks_another_and_both_chats_show_it(self):
        # model calls happen in this order: Assistant asks, Research answers, Assistant reports back to the owner
        spark = Spark(act(type="ask_bot", bot="research", question="Where am I flying?"), act(type="reply", text="Mumbai, then Pune."),
                      act(type="reply", text="Research says Mumbai, then Pune."))
        c, a, b, _ = self.two_bots(spark)
        say(c, a, "check with Research about my trip")
        until(lambda: "Research says Mumbai, then Pune." in texts(c, a, "assistant"))
        self.assertIn("You asked Research: Where am I flying?\nResearch: Mumbai, then Pune.", self.notes(c, a))
        self.assertIn("Assistant asked: Where am I flying?\nYou answered: Mumbai, then Pune.", self.notes(c, b))
        self.assertIn("Research: knows the owner's travel plans", spark.calls[0][0])  # the asking bot sees who it can ask
        self.assertIn("not from the owner", json.dumps(spark.calls[1][1]))  # the answering bot knows who is asking

    def test_the_answering_bot_cannot_start_phone_tasks_or_loop_back(self):
        spark = Spark(act(type="ask_bot", bot="Research", question="q"),
                      act(type="mobile_task", goal="do it", say="ok"),          # Research tries the phone
                      act(type="ask_bot", bot="Assistant", question="loop?"),   # ...then to ask back
                      act(type="reply", text="I only know X."),
                      act(type="reply", text="Done."))
        c, a, b, app = self.two_bots(spark)
        say(c, a, "ask research")
        until(lambda: "Done." in texts(c, a, "assistant"))
        blob = json.dumps(spark.calls[3][1])
        self.assertIn("not available while answering another bot", blob)
        self.assertIn("no unique bot named 'assistant'", blob)
        self.assertEqual(app.state.db.jobs(b, 5), [])

    def test_errors_for_unknown_self_and_empty_questions(self):
        spark = Spark(act(type="ask_bot", bot="Nobody", question="hi"), act(type="ask_bot", bot="Assistant", question="me?"),
                      act(type="ask_bot", bot="Research", question=""), act(type="reply", text="ok"))
        c, a, _, _ = self.two_bots(spark)
        say(c, a, "go")
        until(lambda: "ok" in texts(c, a, "assistant"))
        blob = json.dumps(spark.calls[-1][1])
        self.assertIn("no unique bot named 'nobody'", blob)
        self.assertIn("no unique bot named 'assistant'", blob)  # a bot cannot ask itself
        self.assertIn("a question is required", blob)

    def test_chain_is_limited_to_three_bots(self):
        spark = Spark(act(type="ask_bot", bot="B", question="1"), act(type="ask_bot", bot="C", question="2"), act(type="ask_bot", bot="D", question="3"),
                      act(type="reply", text="c"), act(type="reply", text="b"), act(type="reply", text="a"))
        c, _, _, app = setup(spark)
        ids = {n: c.post("/api/bots", json={"name": n}).json()["id"] for n in "BCD"}
        a = c.get("/api/bots").json()
        a = next(x["id"] for x in a if x["name"] == "Assistant")
        say(c, a, "chain")
        until(lambda: "a" in texts(c, a, "assistant"))
        self.assertIn("too many bots in a row", json.dumps(spark.calls))


class NewBotGreeting(unittest.TestCase):
    def test_a_new_bot_opens_the_conversation_from_its_focus(self):
        spark = Spark(act(type="reply", text="I can track your gym plan. What should we start with: a weekly plan, a reminder routine, or logging today's workout?"))
        c, _, _, _ = setup(spark)
        bot = c.post("/api/bots", json={"name": "Coach", "persona": "personal fitness coach", "greet": True}).json()["id"]
        until(lambda: any("What should we start with" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(texts(c, bot, "user"), [])  # the bot spoke first
        system, messages = spark.calls[0]
        self.assertIn("personal fitness coach", system)
        self.assertIn("Coach", messages[-1]["content"])
        self.assertIn("ONE concrete question", messages[-1]["content"])

    def test_bots_created_without_the_flag_stay_quiet(self):
        spark = Spark()
        c, _, _, _ = setup(spark)
        c.post("/api/bots", json={"name": "Quiet", "persona": "x"})
        time.sleep(0.3)
        self.assertEqual(spark.calls, [])


class WebSearch(unittest.TestCase):
    class SearchSpark(Spark):
        def __init__(self, *a, fail=False):
            super().__init__(*a)
            self.queries, self.fail = [], fail

        def search(self, query):
            self.queries.append(query)
            if self.fail:
                raise RuntimeError("down")
            return {"text": "Python 3.15.2 was released on 25 Oct 2026.", "sources": [{"title": "Python.org", "url": "https://www.python.org/downloads/", "snippet": ""}]}

    def test_search_result_and_sources_reach_the_model_and_the_chat_shows_what_was_searched(self):
        spark = self.SearchSpark(act(type="web_search", query="  latest   python version "), act(type="reply", text="3.15.2 (https://www.python.org/downloads/)"))
        c, _, bot, _ = setup(spark)
        say(c, bot, "what is the newest python?")
        until(lambda: any("3.15.2" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(spark.queries, ["latest python version"])
        result = spark.calls[1][1][-1]["content"]
        self.assertIn("[tool result for web_search] Python 3.15.2 was released", result)
        self.assertIn("https://www.python.org/downloads/", result)
        notes = [m["content"] for m in msgs(c, bot) if m["kind"] == "bot_note"]
        self.assertEqual(notes, ["Searched the web: latest python version"])
        self.assertIn("Today is ", spark.calls[0][0])  # the model knows the date so it can judge what is current

    def test_failed_and_empty_searches_are_reported_not_raised(self):
        spark = self.SearchSpark(act(type="web_search", query="a"), act(type="web_search", query="first"), act(type="web_search", query="second"),
                                 act(type="reply", text="done"), fail=True)
        c, _, bot, _ = setup(spark)
        say(c, bot, "search a lot")
        until(lambda: "done" in texts(c, bot, "assistant"))
        blob = json.dumps(spark.calls[-1][1])
        self.assertIn("a search query is required", blob)
        self.assertIn("the search failed (RuntimeError)", blob)
        self.assertEqual(spark.queries, ["first", "second"])  # the too-short query never reached the API

    def test_a_model_without_search_support_is_handled(self):
        spark = Spark(act(type="web_search", query="weather today"), act(type="reply", text="I could not search."))
        c, _, bot, _ = setup(spark)
        say(c, bot, "weather?")
        until(lambda: "I could not search." in texts(c, bot, "assistant"))
        self.assertIn("web search is not available", json.dumps(spark.calls[1][1]))


class RotateApi(unittest.TestCase):
    def test_rotate_endpoint_and_orientation_in_mobile_state(self):
        c, phone, _, _ = setup(Spark())
        self.assertEqual(c.get("/api/mobile/state").json()["orientation"], "portrait")  # a phone without rotation support reports portrait
        phone.mode = "portrait"
        phone.rotate = lambda o: setattr(phone, "mode", o)
        phone.orientation = lambda: phone.mode
        r = c.post("/api/device/rotate", json={"orientation": "landscape"})
        self.assertEqual((r.status_code, r.json()["orientation"]), (200, "landscape"))
        self.assertEqual(c.get("/api/mobile/state").json()["orientation"], "landscape")
        self.assertEqual(c.post("/api/device/rotate", json={"orientation": "sideways"}).status_code, 422)


def wav_bytes(seconds=1.0, rate=16000):
    import struct
    n = int(seconds * rate)
    return (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n * 2) + b"\x00\x01" * n)


class Transcribe(unittest.TestCase):
    class Hearing(Spark):
        def __init__(self, text="open youtube", fail=False):
            super().__init__()
            self.text, self.fail, self.heard = text, fail, []

        def transcribe(self, wav):
            self.heard.append(len(wav))
            if self.fail:
                raise RuntimeError("api down")
            return self.text

    def test_wav_goes_to_the_transcribe_model_and_the_text_comes_back_unsaved(self):
        spark = self.Hearing("open youtube and search for jazz")
        c, _, bot, app = setup(spark)
        wav = wav_bytes()
        r = c.post("/api/transcribe", content=wav, headers={"Content-Type": "audio/wav"})
        self.assertEqual((r.status_code, r.json()), (200, {"text": "open youtube and search for jazz"}))
        self.assertEqual(spark.heard, [len(wav)])
        self.assertEqual(msgs(c, bot), [])  # speech never becomes a message until the owner sends it
        self.assertEqual(app.state.db._q("select * from messages"), [])

    def test_rejects_non_wav_tiny_and_oversized_audio_before_calling_the_model(self):
        spark = self.Hearing()
        c, _, _, _ = setup(spark)
        post = lambda body: c.post("/api/transcribe", content=body, headers={"Content-Type": "audio/wav"}).status_code
        self.assertEqual(post(b"not audio" * 200), 422)
        self.assertEqual(post(b"RIFF"), 422)
        self.assertEqual(post(b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (21 * 1024 * 1024)), 413)
        self.assertEqual(spark.heard, [])

    def test_a_model_failure_is_a_clean_error(self):
        c, _, _, _ = setup(self.Hearing(fail=True))
        r = c.post("/api/transcribe", content=wav_bytes(), headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("api down", r.text)

    def test_needs_the_token_when_one_is_set(self):
        tmp = __import__("tempfile").mkdtemp()
        app = create_app(Phone(), lambda m: self.Hearing(), token="s3cret", settle=0, db=Database(":memory:"), data_dir=tmp)
        c = TestClient(app)
        self.assertEqual(c.post("/api/transcribe", content=wav_bytes()).status_code, 401)
        self.assertEqual(c.post("/api/transcribe", content=wav_bytes(), headers={"Authorization": "Bearer s3cret"}).status_code, 200)
