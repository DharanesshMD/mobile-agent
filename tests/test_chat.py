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
        if name == "muse-spark-1.3":
            return spark
        assert name == "muse-wonder-1.0", name
        return queue.pop(0)

    tmp = tempfile.mkdtemp()
    phone = Phone()
    app = create_app(phone, factory, settle=0, db=db or Database(":memory:"), data_dir=tmp)
    c = TestClient(app)
    bot = c.get("/api/bots").json()[0]["id"]
    return c, phone, bot, app


def until(fn, timeout=15):
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
        spark = Spark(act(type="computer_task", goal="Read the Android version", say="On it"),
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
        spark = Spark(act(type="computer_task", goal="g1", say="ok"), act(type="computer_task", goal="g2", say="ok"))
        c, _, bot, _ = setup(spark, Worker(act(type="handoff", reason="your turn")))
        say(c, bot, "first")
        until(lambda: open_card(c, bot))
        say(c, bot, "second")
        until(lambda: any("already working" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(len(c.get(f"/api/bots/{bot}/jobs").json()), 1)

    def test_worker_confirm_card_and_takeover_rules(self):
        spark = Spark(act(type="computer_task", goal="send it", say="ok"), act(type="reply", text="sent"))
        c, phone, bot, _ = setup(spark, Worker(act(type="ask", question="Send the message?"), act(type="done", answer="sent")))
        say(c, bot, "send it")
        card = until(lambda: open_card(c, bot))
        self.assertEqual((card["meta"]["kind"], card["content"]), ("confirm", "Send the message?"))
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 200)  # paused at a gate: allowed
        self.assertEqual(c.get("/api/computer/state").json()["state"], "waiting")
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"approved": True})
        until(lambda: "sent" in texts(c, bot, "assistant"))
        done_card = next(m for m in msgs(c, bot) if m["kind"] == "action_item")
        self.assertEqual((done_card["meta"]["status"], done_card["meta"]["answer"]), ("answered", "Approved"))

    def test_need_info_answered_from_memory_without_asking(self):
        db = Database(":memory:")
        spark = Spark(act(type="computer_task", goal="fill form", say="ok"),
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
        spark = Spark(act(type="computer_task", goal="fill form", say="ok"),
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
        spark = Spark(act(type="computer_task", goal="sign in", say="ok"), act(type="reply", text="in"))
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
        spark = Spark(act(type="computer_task", goal="g", say="ok"))
        c, _, bot, _ = setup(spark, Worker(act(type="handoff", reason="sign in")))
        say(c, bot, "go")
        until(lambda: open_card(c, bot))
        job = c.get(f"/api/bots/{bot}/jobs").json()[0]
        self.assertEqual(c.post(f"/api/jobs/{job['id']}/cancel").status_code, 200)
        until(lambda: "Stopped the task." in texts(c, bot, "assistant"))
        self.assertIsNone(open_card(c, bot))
        self.assertEqual(c.get(f"/api/bots/{bot}/jobs").json()[0]["status"], "cancelled")

    def test_owner_control_pauses_the_worker_until_released(self):
        spark = Spark(act(type="computer_task", goal="tap", say="ok"), act(type="reply", text="finished"))
        c, phone, bot, _ = setup(spark, Worker(act(type="tap", element=0), act(type="done", answer="ok")))
        self.assertEqual(c.post("/api/computer/control", json={"action": "take"}).json()["state"], "control")
        say(c, bot, "tap it")
        until(lambda: c.get("/api/computer/state").json()["job"])
        time.sleep(0.6)
        self.assertEqual(phone.taps, [])  # the worker waits while the owner is in control
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.1, "fy": 0.1}).status_code, 200)
        c.post("/api/computer/control", json={"action": "release"})
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
