"""The sidebar starts with the master bot alone. A phone's bot shows up when its phone is working or connected (or once it has been used); the owner makes every other bot."""
import tempfile
import unittest

from fastapi.testclient import TestClient

from agent.db import Database
from agent.lifecycle import Lifecycle
from agent.service import create_app
from test_chat import Spark, act, say, until
from test_lanes import GamesDevice
from test_lifecycle import Clock, FakeBackend
from test_service import Phone, Script


def make(power="stopped"):
    db = Database(":memory:")
    lc = Lifecycle(FakeBackend(power), clock=Clock(), idle_s=300, stop_s=3600)
    app = create_app(Phone(), lambda name: Spark(act(type="reply", text="ok")) if "spark" in name else Script(), settle=0, db=db, data_dir=tempfile.mkdtemp(),
                     master=True, default_assistant=False, virtuals={"games": (GamesDevice(), lc)})
    return TestClient(app), app, db, lc


def names(c, all_=False):
    return [b["name"] for b in c.get("/api/bots", params={"all": "true"} if all_ else {}).json()]


class StartList(unittest.TestCase):
    def test_only_the_master_bot_at_first_and_no_default_assistant(self):
        c, app, db, lc = make()
        self.assertEqual(names(c), ["Master"])
        self.assertEqual(sorted(b["kind"] for b in db.list_bots()), ["master", "virtual"])  # Phone 2's bot exists, it is just not listed
        self.assertEqual(sorted(names(c, True)), ["Master", "Phone 2"])

    def test_phone_2_shows_up_when_it_is_working_and_stays_once_it_was_used(self):
        c, app, db, lc = make()
        lc._set("waking")
        self.assertEqual(names(c), ["Master", "Phone 2"])  # starting counts
        lc._set("on")
        self.assertIn("Phone 2", names(c))
        lc._set("sleeping")
        self.assertEqual(names(c), ["Master"])  # asleep and never used: gone again
        games = db.phone_bot("games", "virtual")
        db.create_job(games["id"], "open a game")  # a task was given to it
        self.assertEqual(names(c), ["Master", "Phone 2"])  # now it stays, asleep or not

    def test_a_greeting_alone_does_not_make_a_bot_listed_but_a_message_from_the_owner_does(self):
        c, app, db, lc = make()
        games = db.phone_bot("games", "virtual")
        self.assertTrue(db.messages(games["id"], 5))  # the intro is there
        self.assertFalse(db.has_activity(games["id"]))
        db.add_message(games["id"], "user", "text", "hi")
        self.assertTrue(db.has_activity(games["id"]))
        self.assertEqual(names(c), ["Master", "Phone 2"])

    def test_a_connected_phone_is_listed_and_an_unconnected_unused_one_is_not(self):
        c, app, db, lc = make()
        from agent.master import ensure_phone_bot
        pb = ensure_phone_bot(db, None, "d1", {"name": "Pixel 8"})
        self.assertEqual(names(c), ["Master"])  # paired once, not connected now, never used
        self.assertIn("Pixel 8", names(c, True))
        app.state.jobs.phones.links["d1"] = object()  # connected now
        self.assertEqual(names(c), ["Master", "Pixel 8"])
        del app.state.jobs.phones.links["d1"]
        self.assertEqual(names(c), ["Master"])
        db.add_message(pb["id"], "user", "text", "what is my battery")
        self.assertEqual(names(c), ["Master", "Pixel 8"])  # used: it stays, with its offline mark in the page

    def test_the_owner_makes_other_bots_and_deleting_the_last_one_does_not_bring_back_an_assistant(self):
        c, app, db, lc = make()
        bot = c.post("/api/bots", json={"name": "Shopping"}).json()
        self.assertEqual(names(c), ["Master", "Shopping"])
        r = c.delete(f"/api/bots/{bot['id']}").json()
        self.assertIsNone(r["bot"])
        self.assertEqual(names(c), ["Master"])


if __name__ == "__main__":
    unittest.main()
