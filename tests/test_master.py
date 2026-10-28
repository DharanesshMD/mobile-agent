import json
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from agent.db import Database
from agent.hub import Hub
from agent.master import INTRO, Watcher, digest, ensure_master, find_alerts
from agent.service import create_app
from test_chat import Spark, act, msgs, say, texts, until
from test_service import Phone

NOW = 1_800_000_000.0


def world():
    db = Database(":memory:")
    hub = Hub()
    master = ensure_master(db, hub)
    research = db.create_bot("Research", "tracks travel plans")
    blank = db.create_bot("Blank", "")
    return db, hub, master, research, blank


class Basics(unittest.TestCase):
    def test_master_is_created_once_with_an_intro_and_stays_on_top(self):
        db, hub, master, research, _ = world()
        self.assertEqual(ensure_master(db, hub)["id"], master["id"])
        self.assertEqual(len([b for b in db.list_bots() if b["kind"] == "master"]), 1)
        self.assertEqual(db.messages(master["id"])[0]["content"], INTRO)
        db.add_message(research["id"], "user", "text", "hi")  # activity elsewhere does not push the master down
        self.assertEqual(db.list_bots()[0]["id"], master["id"])

    def test_old_databases_get_the_kind_column(self):
        import os
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.execute("create table bots(id text primary key, name text not null, persona text not null default '', color text, created real)")
        c.execute("insert into bots values('a','Old','', '#fff', 1)")
        c.commit()
        c.close()
        self.assertEqual(Database(path).get_bot("a")["kind"], "bot")


class Digest(unittest.TestCase):
    def test_digest_shows_every_other_bot_and_what_is_wrong(self):
        db, _, master, research, blank = world()
        db.add_message(research["id"], "user", "text", "plan my Mumbai trip")
        db.memory_add(research["id"], "flight Nov 3 seat 14C")
        job = db.create_job(research["id"], "book a cab")
        db.update_job(job["id"], status="failed", ended=time.time(), result="Uber not installed")
        db.create_item(research["id"], None, "text", {"question": "Which hotel?"})
        db._x("insert into routines(id,bot_id,name,prompt,schedule,enabled,next_run,created) values('r',?,?,?,?,1,1,1)",
              (research["id"], "Morning", "p", json.dumps({"daily_at": "08:00"})))
        d = digest(db)
        for needle in ("Research (focus: tracks travel plans)", "plan my Mumbai trip", "FAILED task", "Uber not installed", "WAITING for the owner", "Which hotel?",
                       "Morning (daily at 08:00)", "flight Nov 3 seat 14C", "Blank (focus: NOT SET)"):
            self.assertIn(needle, d)
        self.assertNotIn("Master", d)  # the master does not list itself

    def test_digest_is_capped(self):
        db, _, _, _, _ = world()
        for i in range(60):
            db.create_bot(f"Bot{i}", "x" * 150)
        self.assertLessEqual(len(digest(db)), 4600)


class Alerts(unittest.TestCase):
    def test_finds_missing_focus_stale_questions_and_failed_tasks(self):
        db, _, _, research, blank = world()
        item = db.create_item(research["id"], None, "text", {"question": "Which hotel?"})
        job = db.create_job(research["id"], "g")
        db.update_job(job["id"], status="failed", ended=NOW - 60, result="boom")
        keys = {a["key"] for a in find_alerts(db, item["created"] + 11 * 60)}
        self.assertEqual(keys, {f"focus:{blank['id']}", f"item:{item['id']}", f"job:{job['id']}"})
        self.assertIn(f"item:{item['id']}", keys)
        self.assertIn(f"focus:{blank['id']}", keys)
        self.assertNotIn(f"item:{item['id']}", {a["key"] for a in find_alerts(db, item["created"] + 5 * 60)})  # not waiting long enough yet

    def test_each_problem_is_posted_once_to_the_master_chat(self):
        db, hub, master, research, blank = world()
        clock = [time.time()]
        w = Watcher(db, hub, clock=lambda: clock[0])
        self.assertEqual(w.check(), 1)  # only the blank bot's missing focus
        self.assertEqual(w.check(), 0)
        db.create_item(research["id"], None, "text", {"question": "Which hotel?"})
        clock[0] += 11 * 60
        self.assertEqual(w.check(), 1)
        self.assertEqual(w.check(), 0)
        alerts = [m for m in db.messages(master["id"]) if m["kind"] == "alert"]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[1]["meta"]["bot"], "Research")
        self.assertIn("Which hotel?", alerts[1]["content"])
        self.assertTrue(any(e["message"]["kind"] == "alert" for e in hub.since(master["id"], -1) if e["type"] == "message"))

    def test_no_master_means_no_alerts(self):
        db = Database(":memory:")
        db.create_bot("x", "")
        self.assertEqual(Watcher(db, Hub()).check(), 0)


def master_app(spark):
    tmp = tempfile.mkdtemp()
    app = create_app(Phone(), lambda m: spark, settle=0, db=Database(":memory:"), data_dir=tmp, master=True)
    c = TestClient(app)
    bots = c.get("/api/bots").json()
    return c, app, next(b for b in bots if b["kind"] == "master"), next(b for b in bots if b["kind"] == "bot")


class Api(unittest.TestCase):
    def test_master_exists_first_and_cannot_be_deleted_but_can_be_renamed(self):
        c, _, master, assistant = master_app(Spark())
        self.assertEqual(c.get("/api/bots").json()[0]["id"], master["id"])
        self.assertEqual(c.delete(f"/api/bots/{master['id']}").status_code, 409)
        self.assertEqual(c.patch(f"/api/bots/{master['id']}", json={"name": "Chief"}).json()["kind"], "master")
        # the only regular bot is still "the last bot": deleting it leaves a fresh Assistant, and the master is untouched
        self.assertTrue(c.get(f"/api/bots/{assistant['id']}/delete-preview").json()["last"])

    def test_master_sees_the_digest_and_never_runs_phone_tasks(self):
        spark = Spark(act(type="mobile_task", goal="open youtube", say="on it"), act(type="reply", text="unused"))
        c, app, master, assistant = master_app(spark)
        app.state.db.add_message(assistant["id"], "user", "text", "remember I like jazz")
        say(c, master["id"], "open youtube please")
        until(lambda: any("don't run phone tasks" in t for t in texts(c, master["id"], "assistant")))
        system = spark.calls[0][0]
        self.assertIn("MASTER bot", system)
        self.assertIn("Workspace digest", system)
        self.assertIn("remember I like jazz", system)
        self.assertEqual(app.state.db.jobs(master["id"], 5), [])

    def test_master_answers_from_the_digest(self):
        spark = Spark(act(type="reply", text="Assistant has nothing waiting."))
        c, _, master, _ = master_app(spark)
        say(c, master["id"], "what is missing?")
        until(lambda: "Assistant has nothing waiting." in texts(c, master["id"], "assistant"))

    def test_alert_messages_are_listed_as_the_latest_activity(self):
        c, app, master, _ = master_app(Spark())
        app.state.watcher.check()
        listing = next(b for b in c.get("/api/bots").json() if b["id"] == master["id"])
        self.assertGreater(listing["last_id"], 0)


if __name__ == "__main__":
    unittest.main()
