"""Demo readiness: account addresses are hidden, friendly phone names, stale alerts clear, no duplicate lines, local time."""
import time
import unittest
from unittest import mock

from agent import device as device_mod
from agent.db import Database
from agent.device import Device, local_timezone
from agent.hub import Hub
from agent.master import DEFAULT_ASSISTANT_PERSONA, Watcher, ensure_phone_bot, find_alerts, phone_name, resolved
from agent.privacy import AccountMasker, mask_email
from test_chat import Spark, act, setup, texts


class Masking(unittest.TestCase):
    def test_the_address_keeps_its_first_letter_and_domain(self):
        self.assertEqual(mask_email("dharun@gmail.com"), "d***@gmail.com")

    def test_only_the_phones_own_addresses_are_hidden(self):
        m = AccountMasker()
        m.add("Owner@Gmail.com")
        self.assertEqual(m.apply("Signed in as owner@gmail.com; Mom is mom@example.com"), "Signed in as o***@gmail.com; Mom is mom@example.com")
        self.assertEqual(m.apply("no address here"), "no address here")

    def test_a_phone_that_fails_to_answer_never_breaks_saving(self):
        def boom():
            raise RuntimeError("adb hung")
        m = AccountMasker(boom)
        self.assertEqual(m.apply("x@y.com"), "x@y.com")
        time.sleep(0.2)  # the background refresh failed quietly

    def test_the_provider_runs_in_the_background_and_is_remembered(self):
        m = AccountMasker(lambda: ["me@gmail.com"])
        m.apply("hello me@gmail.com")  # starts the refresh
        for _ in range(50):
            if "me@gmail.com" in m.known:
                break
            time.sleep(0.05)
        self.assertEqual(m.apply("hello me@gmail.com"), "hello m***@gmail.com")

    def test_what_the_app_stores_is_masked_but_the_owners_own_words_are_not(self):
        db = Database(":memory:")
        m = AccountMasker()
        m.add("me@gmail.com")
        db.mask = m.apply
        bot = db.create_bot("A", "p")
        self.assertEqual(db.add_message(bot["id"], "assistant", "text", "It is me@gmail.com")["content"], "It is m***@gmail.com")
        self.assertEqual(db.add_message(bot["id"], "user", "text", "my address is me@gmail.com")["content"], "my address is me@gmail.com")
        status = db.add_message(bot["id"], "assistant", "status", "Starting")
        self.assertEqual(db.update_message(status["id"], content="Reading me@gmail.com")["content"], "Reading m***@gmail.com")
        job = db.create_job(bot["id"], "read the account")
        self.assertEqual(db.update_job(job["id"], result="account: me@gmail.com")["result"], "account: m***@gmail.com")
        self.assertEqual(db.add_library(bot["id"], job["id"], "Contacts for me@gmail.com", "x.png")["title"], "Contacts for m***@gmail.com")

    def test_the_app_knows_the_linked_account_from_the_first_message(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        app.state.masker.add("who@gmail.com")
        app.state.orch.post(bot, "Account who@gmail.com is signed in")
        self.assertIn("w***@gmail.com", texts(c, bot, "assistant")[-1])


class Names(unittest.TestCase):
    def test_raw_model_strings_become_my_phone(self):
        self.assertEqual(phone_name({"name": "sdk_gphone64_arm64"}), "My phone")
        self.assertEqual(phone_name({"model": "redroid13_arm64_only"}), "My phone")
        self.assertEqual(phone_name({"name": ""}), "My phone")
        self.assertEqual(phone_name({"name": "Pixel 8"}), "Pixel 8")
        self.assertEqual(phone_name({"name": "Dharun's Galaxy"}), "Dharun's Galaxy")

    def test_a_second_phone_is_my_phone_2_and_the_greeting_does_not_claim_a_connection(self):
        db = Database(":memory:")
        a = ensure_phone_bot(db, None, "p1", {"name": "sdk_gphone64_arm64"})
        b = ensure_phone_bot(db, None, "p2", {"name": "sdk_gphone64_arm64"})
        self.assertEqual((a["name"], b["name"]), ("My phone", "My phone 2"))
        first = db.messages(a["id"], 5)[0]["content"]
        self.assertIn("is paired", first)
        self.assertNotIn("connected", first)


class Alerts(unittest.TestCase):
    def setUp(self):
        self.db, self.hub = Database(":memory:"), Hub()
        self.master = self.db.create_bot("Master", "", kind="master")

    def alert(self, key, bot, created=None):
        m = self.db.add_message(self.master["id"], "assistant", "alert", "x", {"bot_id": bot["id"], "bot": bot["name"], "key": key})
        if created:
            self.db._x("update messages set created=? where id=?", (created, m["id"]))
        return self.db.get_message(m["id"])

    def test_a_focus_alert_clears_once_the_bot_has_a_focus(self):
        bot = self.db.create_bot("Research", "")
        a = self.alert(f"focus:{bot['id']}", bot)
        self.assertFalse(resolved(self.db, a, time.time()))
        self.db._x("update bots set persona=? where id=?", ("Reads papers", bot["id"]))
        self.assertTrue(resolved(self.db, a, time.time()))

    def test_a_waiting_alert_clears_when_the_card_is_answered(self):
        bot = self.db.create_bot("R", "p")
        item = self.db.create_item(bot["id"], None, "confirm", {"question": "ok?"})
        a = self.alert(f"item:{item['id']}", bot)
        self.assertFalse(resolved(self.db, a, time.time()))
        self.db.close_item(item["id"], "answered")
        self.assertTrue(resolved(self.db, a, time.time()))

    def test_a_failed_task_alert_clears_after_a_later_success_or_after_24_hours(self):
        bot = self.db.create_bot("R", "p")
        bad = self.db.create_job(bot["id"], "open it")
        self.db.update_job(bad["id"], status="failed", ended=time.time() - 100)
        a = self.alert(f"job:{bad['id']}", bot)
        self.assertFalse(resolved(self.db, a, time.time()))
        good = self.db.create_job(bot["id"], "open it again")
        self.db.update_job(good["id"], status="done", ended=time.time())
        self.assertTrue(resolved(self.db, a, time.time()))
        old = self.alert("job:zzz", bot, created=time.time() - 25 * 3600)
        self.assertTrue(resolved(self.db, old, time.time()))

    def test_the_watcher_removes_resolved_alerts_and_tells_the_page(self):
        bot = self.db.create_bot("Research", "")
        w = Watcher(self.db, self.hub)
        w.check()  # posts the focus alert
        self.assertEqual(len([m for m in self.db.messages(self.master["id"], 20) if m["kind"] == "alert"]), 1)
        self.db._x("update bots set persona=? where id=?", ("Reads papers", bot["id"]))
        w.check()
        self.assertEqual([m for m in self.db.messages(self.master["id"], 20) if m["kind"] == "alert"], [])

    def test_a_fresh_install_has_no_focus_alert(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        names = {b["name"]: b for b in app.state.db.list_bots()}
        self.assertEqual(names["Assistant"]["persona"], DEFAULT_ASSISTANT_PERSONA)
        self.assertEqual([a for a in find_alerts(app.state.db) if a["key"].startswith("focus:")], [])


class Duplicates(unittest.TestCase):
    def test_two_near_identical_lines_a_moment_apart_are_one(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        orch = app.state.orch
        a = orch.post(bot, "Stopped the task.")
        b = orch.post(bot, "Stopped the task!")
        self.assertEqual(a["id"], b["id"])
        orch.post(bot, "Something else entirely.")
        self.assertEqual(len([t for t in texts(c, bot, "assistant") if "Stopped" in t]), 1)

    def test_the_owner_speaking_in_between_or_waiting_longer_allows_a_repeat(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        orch = app.state.orch
        a = orch.post(bot, "Done.")
        app.state.db.add_message(bot, "user", "text", "again?")
        b = orch.post(bot, "Done.")
        self.assertNotEqual(a["id"], b["id"])
        with mock.patch("agent.orchestrator.DUPLICATE_WINDOW_S", 0):
            self.assertNotEqual(orch.post(bot, "Done.")["id"], b["id"])


class LocalTime(unittest.TestCase):
    def test_the_model_is_told_the_owners_local_time_and_zone(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        ctx = app.state.orch._context(bot)
        self.assertIn("local time", ctx)
        self.assertIn("never UTC", ctx)

    def test_the_local_zone_comes_from_etc_localtime(self):
        with mock.patch("os.path.realpath", return_value="/var/db/timezone/zoneinfo/Asia/Kolkata"):
            self.assertEqual(local_timezone(), "Asia/Kolkata")
        with mock.patch("os.path.realpath", return_value="/etc/UTC"):
            self.assertEqual(local_timezone(), "")

    def test_the_phone_is_put_in_the_local_zone(self):
        d = Device.__new__(Device)
        calls = []
        d._run = lambda *a: calls.append(a) or b""
        self.assertEqual(d.sync_timezone("Asia/Kolkata"), "Asia/Kolkata")
        self.assertEqual(calls, [("shell", "service", "call", "alarm", "3", "s16", "Asia/Kolkata")])
        calls.clear()
        with mock.patch.object(device_mod, "local_timezone", return_value=""):  # a computer whose zone cannot be told: nothing is sent
            d.sync_timezone("")
            d.sync_timezone()
        self.assertEqual(calls, [])


class AppName(unittest.TestCase):
    def test_a_package_id_in_the_goal_does_not_cut_the_app_name(self):
        from agent.escalate import app_name
        pkg = "com.supercell.clashofclans"
        self.assertEqual(app_name("Open Clash of Clans (com.supercell.clashofclans) and tell me what the screen shows", pkg), "Clash of Clans")
        self.assertEqual(app_name("open clash of clans", pkg), "Clash of Clans")
        self.assertEqual(app_name("Open Clash of Clans. Report what you see.", pkg), "Clash of Clans")
        self.assertEqual(app_name("launch something unrelated", pkg), "Clashofclans")


if __name__ == "__main__":
    unittest.main()
