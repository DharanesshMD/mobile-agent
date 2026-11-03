import unittest

from agent import nudge
from test_chat import Spark, setup


class Reminders(unittest.TestCase):
    def make(self, kind="confirm", question="To install WhatsApp the Play Store wants to remove Clash of Clans. Allow that?"):
        c, _, bot, app = setup(Spark())
        db, orch = app.state.db, app.state.orch
        item = orch.make_item(bot, None, {"kind": kind, "question": question, "options": ["a", "b"] if kind == "choice" else []})
        now = {"t": item["created"]}
        n = nudge.Nudger(db, app.state.hub, clock=lambda: now["t"])
        return c, db, bot, item, n, now

    def texts(self, db, bot):
        return [m["content"] for m in db.messages(bot, 50) if (m["meta"] or {}).get("nudge")]

    def test_a_waiting_card_is_followed_up_at_45_seconds_3_minutes_and_10_minutes_then_never(self):
        c, db, bot, item, n, now = self.make()
        self.assertEqual(n.check(), 0)
        now["t"] = item["created"] + 44
        self.assertEqual(n.check(), 0)  # not yet
        now["t"] = item["created"] + 46
        self.assertEqual(n.check(), 1)
        self.assertEqual(n.check(), 0)  # once per step
        now["t"] = item["created"] + 181
        self.assertEqual(n.check(), 1)
        now["t"] = item["created"] + 601
        self.assertEqual(n.check(), 1)
        now["t"] = item["created"] + 5000
        self.assertEqual(n.check(), 0)  # three reminders at most
        t = self.texts(db, bot)
        self.assertEqual(len(t), 3)
        self.assertTrue(t[0].startswith("Waiting for you: To install WhatsApp"))
        self.assertTrue(t[1].startswith("Still waiting for you"))
        self.assertTrue(t[2].startswith("This is still open"))
        self.assertIn("Approve or decline on the card above.", t[0])

    def test_the_reminder_is_a_normal_assistant_message_the_page_receives_at_once(self):
        c, db, bot, item, n, now = self.make()
        now["t"] = item["created"] + 50
        n.check()
        m = [x for x in c.get(f"/api/bots/{bot}/state").json()["messages"] if (x["meta"] or {}).get("nudge")][0]
        self.assertEqual((m["role"], m["kind"], m["meta"]["nudge"]), ("assistant", "text", item["id"]))

    def test_an_answered_card_gets_no_more_reminders_and_is_forgotten(self):
        c, db, bot, item, n, now = self.make()
        now["t"] = item["created"] + 50
        n.check()
        self.assertEqual(n.sent, {item["id"]: 1})
        self.assertEqual(c.post(f"/api/items/{item['id']}/answer", json={"approved": True}).status_code, 200)
        now["t"] = item["created"] + 400
        self.assertEqual(n.check(), 0)
        self.assertEqual(n.sent, {})
        self.assertEqual(len(self.texts(db, bot)), 1)

    def test_every_kind_of_card_says_how_to_answer_and_long_questions_are_cut(self):
        for kind in ("confirm", "choice", "text", "handoff", "otp", "secret"):
            c, db, bot, item, n, now = self.make(kind, "q " * 200)
            now["t"] = item["created"] + 50
            n.check()
            t = self.texts(db, bot)[0]
            self.assertIn(nudge.HOW[kind], t, kind)
            self.assertLess(len(t), 260)
            self.assertIn("…", t)

    def test_a_card_of_a_deleted_bot_is_ignored(self):
        c, db, bot, item, n, now = self.make()
        other = db.create_bot("Other", "p")
        db.delete_bot(bot)
        now["t"] = item["created"] + 50
        self.assertEqual(n.check(), 0)
        self.assertEqual(db.messages(other["id"], 5), [])


if __name__ == "__main__":
    unittest.main()
