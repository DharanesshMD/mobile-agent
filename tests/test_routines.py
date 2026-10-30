import json
import unittest
from datetime import datetime

from agent.db import Database
from agent.routines import Scheduler, describe, next_run, parse_schedule
from test_chat import Spark, act, msgs, setup, texts, until, say


class Schedules(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(parse_schedule({"every_minutes": "30"}), {"every_minutes": 30})
        self.assertEqual(parse_schedule({"daily_at": "7:05"}), {"daily_at": "07:05"})
        for bad in ({}, {"every_minutes": 1}, {"every_minutes": 99999}, {"every_minutes": "x"}, {"daily_at": "25:00"}, {"daily_at": "noon"}, "hourly"):
            with self.assertRaises(ValueError, msg=bad):
                parse_schedule(bad)

    def test_next_run(self):
        t = datetime(2026, 10, 28, 9, 0).astimezone().timestamp()
        self.assertEqual(next_run({"every_minutes": 15}, t), t + 900)
        today = datetime.fromtimestamp(next_run({"daily_at": "10:30"}, t))
        self.assertEqual((today.day, today.hour, today.minute), (28, 10, 30))
        tomorrow = datetime.fromtimestamp(next_run({"daily_at": "08:00"}, t))  # already passed today
        self.assertEqual((tomorrow.day, tomorrow.hour), (29, 8))
        self.assertGreater(next_run({"daily_at": "09:00"}, t), t)  # exactly now means tomorrow, never a double fire

    def test_describe(self):
        self.assertEqual([describe({"every_minutes": n}) for n in (30, 120, 1440)], ["every 30 min", "every 2 h", "every day"])
        self.assertEqual(describe({"daily_at": "07:30"}), "daily at 07:30")


class FakeOrch:
    def __init__(self):
        self.turns, self.posts = [], []

    def turn(self, bot_id, note=None, routine=None):
        self.turns.append((bot_id, note))

    def post(self, bot_id, text, kind="text", meta=None):
        self.posts.append((bot_id, text, kind))


class Firing(unittest.TestCase):
    def setUp(self):
        self.db, self.now, self.orch = Database(":memory:"), [1000.0], FakeOrch()
        self.bot = self.db.create_bot("b")["id"]
        self.s = Scheduler(self.db, self.orch, clock=lambda: self.now[0])

    def wait_turns(self, n):
        until(lambda: len(self.orch.turns) >= n)

    def test_due_routine_fires_once_and_is_rescheduled(self):
        r = self.s.add(self.bot, "check", "check the battery", {"every_minutes": 5})
        self.assertEqual(self.s.tick(), 0)
        self.now[0] += 301
        self.assertEqual(self.s.tick(), 1)
        self.assertEqual(self.s.tick(), 0)  # claimed: not fired again on the next tick
        self.wait_turns(1)
        bot, note = self.orch.turns[0]
        self.assertEqual(bot, self.bot)
        self.assertIn("'check'", note)
        self.assertIn("check the battery", note)
        self.assertIn("reaching out first", note)
        row = self.db.get_routine(r["id"])
        self.assertEqual(row["next_run"], self.now[0] + 300)
        self.assertEqual(row["last_run"], self.now[0])

    def test_long_downtime_fires_once_not_per_missed_slot(self):
        self.s.add(self.bot, "x", "do it", {"every_minutes": 5})
        self.now[0] += 5 * 60 * 100
        self.assertEqual(self.s.tick(), 1)
        self.assertEqual(self.s.tick(), 0)

    def test_paused_routine_does_not_fire(self):
        r = self.s.add(self.bot, "x", "do it", {"every_minutes": 5})
        self.db.routine_update(r["id"], enabled=0)
        self.now[0] += 10000
        self.assertEqual(self.s.tick(), 0)

    def test_add_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.s.add(self.bot, " ", "p", {"every_minutes": 5})
        with self.assertRaises(ValueError):
            self.s.add(self.bot, "n", "p", {"every_minutes": 2})


class Api(unittest.TestCase):
    def test_manage_but_not_create_through_the_api(self):
        c, _, bot, app = setup(Spark())
        self.assertEqual(c.post(f"/api/bots/{bot}/routines", json={"name": "x", "prompt": "y", "schedule": {"daily_at": "08:00"}}).status_code, 405)
        rid = app.state.scheduler.add(bot, "Morning", "say hi", {"daily_at": "08:00"})["id"]
        self.assertEqual([x["name"] for x in c.get(f"/api/bots/{bot}/routines").json()], ["Morning"])
        self.assertEqual(c.patch(f"/api/routines/{rid}", json={"enabled": False}).json()["enabled"], 0)
        self.assertEqual(c.delete(f"/api/routines/{rid}").status_code, 200)
        self.assertEqual(c.delete(f"/api/routines/{rid}").status_code, 404)

    def test_bot_reaches_out_first_when_a_routine_runs_now(self):
        spark = Spark(act(type="reply", text="Good morning! Nothing urgent."))
        c, _, bot, app = setup(spark)
        rid = app.state.scheduler.add(bot, "Morning", "greet me", {"daily_at": "08:00"})["id"]
        c.post(f"/api/routines/{rid}/run")
        until(lambda: "Good morning! Nothing urgent." in texts(c, bot, "assistant"))
        self.assertEqual(texts(c, bot, "user"), [])  # the owner never messaged
        self.assertIn("routine 'Morning'", spark.calls[0][1][-1]["content"])

    def test_owner_can_create_list_and_delete_a_routine_through_chat(self):
        spark = Spark(
            act(type="routine_create", name="Battery", prompt="check battery", schedule={"every_minutes": 60}),
            act(type="reply", text="Set: Battery every 60 min."),
            act(type="routine_list"), act(type="reply", text="You have Battery."),
            act(type="routine_create", name="Bad", prompt="p", schedule={"every_minutes": 1}),
            act(type="reply", text="That is too frequent."),
            act(type="routine_delete", name="battery"), act(type="reply", text="Deleted."))
        c, _, bot, _ = setup(spark)
        say(c, bot, "check my battery hourly")
        until(lambda: "Set: Battery every 60 min." in texts(c, bot, "assistant"))
        self.assertEqual([r["name"] for r in c.get(f"/api/bots/{bot}/routines").json()], ["Battery"])
        say(c, bot, "what routines do I have")
        until(lambda: "You have Battery." in texts(c, bot, "assistant"))
        say(c, bot, "every minute please")
        until(lambda: "That is too frequent." in texts(c, bot, "assistant"))
        self.assertIn("error: every_minutes must be between 5", json.dumps(spark.calls[-1][1]))
        say(c, bot, "delete the battery one")
        until(lambda: "Deleted." in texts(c, bot, "assistant"))
        self.assertEqual(c.get(f"/api/bots/{bot}/routines").json(), [])



class Refresh(unittest.TestCase):
    def test_creating_a_routine_in_chat_pushes_a_refresh_event(self):
        spark = Spark(act(type="routine_create", name="Hi", prompt="say hi", schedule={"daily_at": "08:00"}), act(type="reply", text="Set."))
        c, _, bot, app = setup(spark)
        say(c, bot, "say hi every day at 8")
        until(lambda: "Set." in texts(c, bot, "assistant"))
        self.assertTrue(any(e.get("type") == "refresh" and e.get("what") == "routines" for e in app.state.hub.since(bot, -1)))
        app.state.vault.put("x", "y")
        self.assertTrue(any(e.get("what") == "vault" for e in app.state.hub.since(bot, -1)))


class Reliability(unittest.TestCase):
    def test_a_daily_routine_that_is_hours_late_waits_for_tomorrow_and_says_so(self):
        db, now, orch = Database(":memory:"), [datetime(2026, 10, 30, 6, 0).astimezone().timestamp()], FakeOrch()
        bot = db.create_bot("b")["id"]
        s = Scheduler(db, orch, clock=lambda: now[0])
        r = s.add(bot, "Morning brief", "brief me", {"daily_at": "07:30"})
        now[0] = datetime(2026, 10, 30, 22, 0).astimezone().timestamp()  # the server was off all day
        self.assertEqual(s.tick(), 0)
        self.assertEqual(orch.turns, [])
        row = db.get_routine(r["id"])
        self.assertTrue(row["last_status"].startswith("missed"))
        self.assertGreater(row["next_run"], now[0])  # tomorrow 07:30
        self.assertIn("missed the routine 'Morning brief'", orch.posts[0][1])

    def test_a_daily_routine_a_little_late_still_runs(self):
        db, now, orch = Database(":memory:"), [datetime(2026, 10, 30, 6, 0).astimezone().timestamp()], FakeOrch()
        bot = db.create_bot("b")["id"]
        s = Scheduler(db, orch, clock=lambda: now[0])
        s.add(bot, "Morning brief", "brief me", {"daily_at": "07:30"})
        now[0] = datetime(2026, 10, 30, 8, 0).astimezone().timestamp()  # 30 minutes late
        self.assertEqual(s.tick(), 1)

    def test_routines_survive_a_restart_and_keep_their_schedule(self):
        db, now, orch = Database(":memory:"), [1000.0], FakeOrch()
        bot = db.create_bot("b")["id"]
        Scheduler(db, orch, clock=lambda: now[0]).add(bot, "x", "do it", {"every_minutes": 5})
        later = Scheduler(db, orch, clock=lambda: now[0])  # a new process over the same database
        now[0] += 301
        self.assertEqual(later.tick(), 1)

    def test_several_routines_due_together_all_fire(self):
        db, now, orch = Database(":memory:"), [1000.0], FakeOrch()
        a, b = db.create_bot("a")["id"], db.create_bot("b")["id"]
        s = Scheduler(db, orch, clock=lambda: now[0])
        s.add(a, "one", "p1", {"every_minutes": 5})
        s.add(b, "two", "p2", {"every_minutes": 5})
        now[0] += 301
        self.assertEqual(s.tick(), 2)
        until(lambda: len(orch.turns) == 2)


class EndToEnd(unittest.TestCase):
    """A routine through the real orchestrator, queue and worker."""

    def test_a_phone_routine_runs_the_task_and_records_how_it_went(self):
        from test_chat import Worker
        spark = Spark(act(type="mobile_task", goal="check the battery", say="On it."), act(type="reply", text="Battery is 80%."))
        c, _, bot, app = setup(spark, Worker(act(type="done", answer="80%")))
        r = app.state.scheduler.add(bot, "Battery", "check the battery", {"every_minutes": 60})
        c.post(f"/api/routines/{r['id']}/run")
        until(lambda: "Battery is 80%." in texts(c, bot, "assistant"), timeout=30)
        until(lambda: app.state.db.get_routine(r["id"])["last_status"] == "done")
        job = app.state.db.jobs(bot, 5)[0]
        self.assertEqual((job["status"], job["routine"]), ("done", r["id"]))  # the task is tagged with its routine
        self.assertEqual(texts(c, bot, "user"), [])  # the owner never messaged

    def test_a_chat_only_routine_is_marked_replied(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="Good morning!")))
        r = app.state.scheduler.add(bot, "Hello", "greet me", {"every_minutes": 60})
        c.post(f"/api/routines/{r['id']}/run")
        until(lambda: app.state.db.get_routine(r["id"])["last_status"] == "replied")

    def test_runs_never_pile_up_while_the_last_one_is_still_working(self):
        from test_chat import Worker, open_card
        spark = Spark(act(type="mobile_task", goal="slow job", say="ok"))
        c, _, bot, app = setup(spark, Worker(act(type="handoff", reason="your turn")))
        sched = app.state.scheduler
        r = sched.add(bot, "Slow", "do the slow job", {"every_minutes": 5})
        self.assertEqual(c.post(f"/api/routines/{r['id']}/run").status_code, 200)
        until(lambda: open_card(c, bot))  # the first run is alive, waiting for the owner
        self.assertEqual(c.post(f"/api/routines/{r['id']}/run").status_code, 409)  # "Run now" says so
        calls = len(spark.calls)
        due = db_row = app.state.db.get_routine(r["id"])
        app.state.db.routine_update(r["id"], next_run=0)  # the schedule says it is due again
        self.assertEqual(sched.tick(), 1)  # claimed
        until(lambda: "skipped" in (app.state.db.get_routine(r["id"])["last_status"] or ""))
        self.assertEqual(len(spark.calls), calls)  # no new model turn, no second task
        self.assertEqual(len(c.get(f"/api/bots/{bot}/jobs").json()), 1)

    def test_a_retry_after_a_failed_routine_run_stays_tagged(self):
        from test_chat import Worker
        spark = Spark(act(type="mobile_task", goal="try it", say="ok"), act(type="mobile_task", goal="try it again", say="retry"), act(type="reply", text="done"))
        c, _, bot, app = setup(spark, Worker(act(type="fail", reason="x")), Worker(act(type="done", answer="fine")))
        r = app.state.scheduler.add(bot, "R", "try it", {"every_minutes": 60})
        c.post(f"/api/routines/{r['id']}/run")
        until(lambda: sorted(j["status"] for j in app.state.db.jobs(bot, 5)) == ["done", "failed"], timeout=30)
        self.assertEqual({j["routine"] for j in app.state.db.jobs(bot, 5)}, {r["id"]})


if __name__ == "__main__":
    unittest.main()
