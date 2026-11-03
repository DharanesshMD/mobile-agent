import json
import unittest

from agent import a2a_exec
from agent.master import ensure_phone_bot
from test_chat import Spark, act, say, setup, until


def paired(c, app, name="Pixel 8"):
    code = c.post("/api/pair/code").json()["code"]
    did = c.post("/api/pair", json={"code": code, "name": name}).json()["device_id"]
    return did, ensure_phone_bot(app.state.db, None, did, {"name": name})


def card(c, bot):
    return until(lambda: next((m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["kind"] == "action_item" and m["meta"]["status"] == "open"), None))


def devices(c):
    return [d["id"] for d in c.get("/api/devices").json()]


class Unpair(unittest.TestCase):
    def start(self, *actions):
        spark = Spark(*actions)
        c, _, bot, app = setup(spark)
        did, pbot = paired(c, app)
        return c, spark, bot, app, did, pbot

    def test_the_owner_picks_on_a_card_and_nothing_happens_before(self):
        c, spark, bot, app, did, pbot = self.start(act(type="unpair_phone", phone="pixel"))
        say(c, bot, "unpair the phone")
        m = card(c, bot)
        self.assertIn("Unpair Pixel 8?", m["content"])
        self.assertEqual(m["meta"]["options"], ["Unpair and keep its chat", "Unpair and delete its chat", "Cancel"])
        self.assertEqual(devices(c), [did])  # still paired while the card is open
        self.assertEqual(len(spark.calls), 1)  # no model turn decides it

    def test_cancel_keeps_the_phone_paired(self):
        c, spark, bot, app, did, pbot = self.start(act(type="unpair_phone", phone="Pixel 8"))
        say(c, bot, "unpair it")
        m = card(c, bot)
        self.assertEqual(c.post(f"/api/items/{m['meta']['item_id']}/answer", json={"choice": "Cancel"}).status_code, 200)
        until(lambda: any("stays paired" in x["content"] for x in c.get(f"/api/bots/{bot}/state").json()["messages"]))
        self.assertEqual(devices(c), [did])
        self.assertIsNotNone(app.state.db.get_bot(pbot["id"]))

    def test_unpair_and_keep_the_chat(self):
        c, spark, bot, app, did, pbot = self.start(act(type="unpair_phone", phone="Pixel 8"))
        say(c, bot, "unpair it")
        m = card(c, bot)
        c.post(f"/api/items/{m['meta']['item_id']}/answer", json={"choice": "Unpair and keep its chat"})
        until(lambda: devices(c) == [])
        out = until(lambda: next((x["content"] for x in c.get(f"/api/bots/{bot}/state").json()["messages"] if "is unpaired" in x["content"]), None))
        self.assertIn("Its chat is still in the list", out)
        self.assertIn("pair it again", out)
        self.assertIsNotNone(app.state.db.get_bot(pbot["id"]))
        self.assertEqual(c.post(f"/api/items/{m['meta']['item_id']}/answer", json={"choice": "Cancel"}).status_code, 409)  # one answer only

    def test_unpair_and_delete_the_chat(self):
        c, spark, bot, app, did, pbot = self.start(act(type="unpair_phone", phone="Pixel 8"))
        say(c, bot, "forget this phone")
        m = card(c, bot)
        c.post(f"/api/items/{m['meta']['item_id']}/answer", json={"choice": "Unpair and delete its chat"})
        until(lambda: devices(c) == [])
        until(lambda: app.state.db.get_bot(pbot["id"]) is None)
        until(lambda: any("its chat is deleted" in x["content"] for x in c.get(f"/api/bots/{bot}/state").json()["messages"]))

    def test_the_model_hears_why_when_it_cannot_be_done(self):
        c, spark, bot, app, did, pbot = self.start(act(type="unpair_phone", phone="Galaxy"), act(type="unpair_phone", phone="phone 2"), act(type="reply", text="ok"))
        say(c, bot, "unpair the galaxy and phone 2")
        until(lambda: len(spark.calls) == 3)
        fed = json.dumps(spark.calls[1][1]) + json.dumps(spark.calls[2][1])
        self.assertIn("no phone paired through the app is called 'Galaxy'", fed)
        self.assertIn("paired phones: Pixel 8", fed)
        self.assertIn("is not a phone that was paired through the app", fed)
        self.assertEqual(devices(c), [did])
        self.assertFalse([m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["kind"] == "action_item"])  # no card for either

    def test_an_outside_agent_can_never_unpair(self):
        self.assertIn("unpair_phone", a2a_exec.NEVER_FOR_OUTSIDE)


if __name__ == "__main__":
    unittest.main()
