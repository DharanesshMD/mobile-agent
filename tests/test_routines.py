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
        self.turns = []

    def turn(self, bot_id, note=None):
        self.turns.append((bot_id, note))


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
    def test_crud_and_validation(self):
        c, _, bot, _ = setup(Spark())
        r = c.post(f"/api/bots/{bot}/routines", json={"name": "Morning", "prompt": "say hi", "schedule": {"daily_at": "08:00"}})
        self.assertEqual(r.status_code, 200)
        rid = r.json()["id"]
        self.assertEqual(c.post(f"/api/bots/{bot}/routines", json={"name": "x", "prompt": "y", "schedule": {"every_minutes": 1}}).status_code, 422)
        self.assertEqual([x["name"] for x in c.get(f"/api/bots/{bot}/routines").json()], ["Morning"])
        self.assertEqual(c.patch(f"/api/routines/{rid}", json={"enabled": False}).json()["enabled"], 0)
        self.assertEqual(c.delete(f"/api/routines/{rid}").status_code, 200)
        self.assertEqual(c.delete(f"/api/routines/{rid}").status_code, 404)

    def test_bot_reaches_out_first_when_a_routine_runs_now(self):
        spark = Spark(act(type="reply", text="Good morning! Nothing urgent."))
        c, _, bot, _ = setup(spark)
        rid = c.post(f"/api/bots/{bot}/routines", json={"name": "Morning", "prompt": "greet me", "schedule": {"daily_at": "08:00"}}).json()["id"]
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


if __name__ == "__main__":
    unittest.main()
