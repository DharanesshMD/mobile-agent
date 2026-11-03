"""Hybrid routing: a directory of the phones, phone_check on another phone, Master moving a failed task to a phone that can do it (with limits),
and a phone without Google Play never being offered or sent to the Play Store."""
import json
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient

from agent import phones
from agent.db import Database
from agent.graph import CRASH_PREFIX, run as run_graph
from agent.lifecycle import Lifecycle
from agent.loop import NO_PLAY_MESSAGE, execute, is_play_url, system_prompt
from agent.service import create_app
from test_chat import Spark, Worker, act, say, texts, until
from test_lanes import GamesDevice
from test_lifecycle import Clock, FakeBackend
from test_queue import RealisticPhone
from test_remote import connect


class Games(GamesDevice):
    """Phone 2: no Google Play, a couple of installed apps."""
    has_play = False

    def __init__(self, packages=()):
        super().__init__()
        self._packages = list(packages)
        self.opened = []

    def packages(self):
        return self._packages

    def accounts(self):
        return []

    def foreground(self):
        return ""

    def open_url(self, url):
        self.opened.append(url)


class AlwaysFails:
    """A worker that gives up at once with a reason, for every job."""
    tokens = {"prompt": 0, "completion": 0}

    def complete(self, system, prompt, image=None, max_tokens=0):
        return json.dumps({"thought": "t", "action": {"type": "fail", "reason": "Clash of Clans is not installed and cannot be installed here"}})


class Brain:
    """A scripted orchestrator that answers by who is asking (Master or not) and what it was just told, so threads cannot swap the answers."""
    def __init__(self, master_rules):
        self.rules, self.calls, self.tokens, self.reroutes = master_rules, [], {"prompt": 0, "completion": 0}, 0
        self.lock = threading.Lock()

    def chat(self, system, messages, max_tokens=0):
        with self.lock:
            self.calls.append((system, messages))
            last = messages[-1]["content"]
            action = {"type": "reply", "text": "ok"}
            if "You are the MASTER bot" in system:
                if "did not complete" in last and "[tool result" not in last:
                    self.reroutes += 1
                action = self.rules(self.reroutes, last)
            return json.dumps({"thought": "t", "action": action})


def make(model, worker, games_packages=(), emulator_packages=(), emulator=None, games=None):
    emulator, games = emulator or RealisticPhone(packages=emulator_packages), games or Games(games_packages)
    clock, backend = Clock(), FakeBackend("running")
    glc = Lifecycle(backend, clock=clock, idle_s=300, stop_s=3600)
    app = create_app(emulator, lambda name: model if name in ("muse-spark-1.3", "muse-voice-transcribe-1.0") else worker, settle=0, db=Database(":memory:"),
                     data_dir=tempfile.mkdtemp(), master=True, virtuals={"games": (games, glc)})
    c = TestClient(app)
    bots = {b["name"]: b["id"] for b in c.get("/api/bots").json()}
    return c, app, emulator, games, glc, backend, bots


def jobs_of(c, bot):
    return c.get(f"/api/bots/{bot}/jobs").json()


class Directory(unittest.TestCase):
    def test_every_phone_with_its_state_what_it_cannot_do_and_apps_known_not_to_run(self):
        c, app, emulator, games, glc, backend, bots = make(Spark(act(type="reply", text="ok")), AlwaysFails())
        orch, db = app.state.orch, app.state.db
        entries = {e["key"]: e for e in phones.describe(db, app.state.jobs)}
        self.assertEqual(set(entries), {"emulator", "games"})
        self.assertEqual(entries["emulator"]["status"], "ready")
        self.assertIn("NO Google Play", entries["games"]["can"])
        self.assertIn("Google Play and Google sign-in work here", entries["emulator"]["can"])
        games.third_party_packages = lambda: ["com.shatteredpixel.shatteredpixeldungeon"]
        emulator.third_party_packages = lambda: ["com.instagram.android"]
        entries = {e["key"]: e for e in phones.describe(db, app.state.jobs)}
        self.assertEqual(entries["games"]["apps"], "com.shatteredpixel.shatteredpixeldungeon")
        self.assertEqual(entries["emulator"]["apps"], "com.instagram.android")
        glc.sleep()
        games.third_party_packages = lambda: (_ for _ in ()).throw(AssertionError("an asleep phone must not be asked"))
        entries = {e["key"]: e for e in phones.describe(db, app.state.jobs)}
        self.assertIn("as last seen", entries["games"]["apps"])  # the remembered list, not a wake-up
        self.assertIn("shatteredpixeldungeon", entries["games"]["apps"])
        self.assertTrue(phones.describe(db, app.state.jobs)[1]["status"].startswith("asleep"))
        # a real phone, offline then connected
        pid = db.create_bot("Pixel 8", "", kind="phone", phone_id="local")["id"]
        real = lambda: {e["key"]: e for e in phones.describe(db, app.state.jobs)}["phone:local"]
        self.assertIn("OFFLINE", real()["status"])
        ws, _ = connect(c, app)
        try:
            self.assertEqual(real()["status"], "connected")
        finally:
            ws.__exit__(None, None, None)
        # an app that crashed on its own on the emulator is listed as known not to run there, only there
        job = db.create_job(bots["Master"], "open clash of clans", target="")
        db.update_job(job["id"], status="failed", result=CRASH_PREFIX + "com.supercell.clashofclans crashed 3 times while opening")
        entries = {e["key"]: e for e in phones.describe(db, app.state.jobs)}
        self.assertEqual(entries["emulator"]["known_bad"], ["com.supercell.clashofclans"])
        self.assertEqual(entries["games"]["known_bad"], [])
        text = phones.render(list(entries.values()), "emulator", master=True)
        self.assertIn("Known NOT to run here (crashed during a task): com.supercell.clashofclans", text)
        self.assertIn("<- this is YOUR phone", text)
        self.assertIn("How to route a phone task", text)
        self.assertNotIn("How to route a phone task", phones.render(list(entries.values()), "games"))

    def test_every_bot_sees_the_directory_and_master_also_the_routing_rules(self):
        spark = Spark(act(type="reply", text="ok"), act(type="reply", text="ok"))
        c, app, *_, bots = make(spark, AlwaysFails())
        say(c, bots["Assistant"], "hi")
        until(lambda: "ok" in texts(c, bots["Assistant"], "assistant"))
        self.assertIn("Phones you can know about", spark.calls[0][0])
        self.assertIn("ask Master", spark.calls[0][0])
        self.assertNotIn("How to route a phone task", spark.calls[0][0])
        say(c, bots["Master"], "hi")
        until(lambda: "ok" in texts(c, bots["Master"], "assistant"))
        self.assertIn("How to route a phone task", spark.calls[1][0])

    def test_the_games_phone_context_forbids_offering_a_play_store_install(self):
        spark = Spark(act(type="reply", text="ok"))
        c, app, *_, bots = make(spark, AlwaysFails())
        say(c, bots["Phone 2"], "open clash of clans")
        until(lambda: "ok" in texts(c, bots["Phone 2"], "assistant"))
        system = spark.calls[0][0]
        self.assertIn("NEVER offer, ask about or promise a Play Store install", system)
        self.assertIn("<- this is YOUR phone", system)
        self.assertIn("NO Google Play", system)


class PhoneCheckOn(unittest.TestCase):
    def test_a_bot_can_see_where_an_app_is_installed_on_another_phone(self):
        spark = Spark(act(type="phone_check", kind="app_installed", query="shattered pixel", on="games"),
                      act(type="phone_check", kind="app_installed", query="shattered pixel", on="emulator"),
                      act(type="reply", text="it is on Phone 2"))
        c, app, emulator, games, glc, backend, bots = make(spark, AlwaysFails(), games_packages=["com.shatteredpixel.shatteredpixeldungeon"])
        say(c, bots["Assistant"], "where is shattered pixel dungeon?")
        until(lambda: "it is on Phone 2" in texts(c, bots["Assistant"], "assistant"))
        self.assertIn("[tool result for phone_check] yes (on Phone 2): installed: com.shatteredpixel.shatteredpixeldungeon", json.dumps(spark.calls[1][1]))
        self.assertIn("[tool result for phone_check] no (on Phone)", json.dumps(spark.calls[2][1]))

    def test_checking_a_sleeping_phone_wakes_it(self):
        spark = Spark(act(type="phone_check", kind="app_installed", query="shattered", on="games"), act(type="reply", text="done"))
        c, app, emulator, games, glc, backend, bots = make(spark, AlwaysFails(), games_packages=["com.shatteredpixel.shatteredpixeldungeon"])
        glc.sleep()
        say(c, bots["Assistant"], "check")
        until(lambda: "done" in texts(c, bots["Assistant"], "assistant"))
        self.assertIn("resume", backend.calls)
        self.assertIn("yes (on Phone 2)", json.dumps(spark.calls[1][1]))

    def test_an_offline_or_unknown_phone_answers_honestly(self):
        spark = Spark(act(type="phone_check", kind="app_installed", query="x", on="Pixel 8"), act(type="phone_check", kind="app_installed", query="x", on="toaster"),
                      act(type="reply", text="done"))
        c, app, emulator, games, glc, backend, bots = make(spark, AlwaysFails())
        app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")
        say(c, bots["Assistant"], "check")
        until(lambda: "done" in texts(c, bots["Assistant"], "assistant"))
        self.assertIn("error: Pixel 8 is offline (the Mobile Agent app is not connected)", json.dumps(spark.calls[1][1]))
        self.assertIn("error: unknown phone 'toaster'; use phone, phone 2, Pixel 8", json.dumps(spark.calls[2][1]))


class Reroute(unittest.TestCase):
    GOAL = "open clash of clans and report what you see"

    @staticmethod
    def rules(reroutes, last):
        if "[tool result for delegate_task] error" in last:
            return {"type": "reply", "text": "Nothing can run it here."}
        if reroutes == 0:
            return {"type": "delegate_task", "bot": "Phone 2", "goal": Reroute.GOAL, "say": "On it."}
        if reroutes == 1:
            return {"type": "delegate_task", "bot": "Assistant", "goal": Reroute.GOAL, "say": "Trying the emulator."}
        if reroutes == 2:
            return {"type": "mobile_task", "goal": Reroute.GOAL + " (myself)", "say": "Trying myself."}
        return {"type": "delegate_task", "bot": "Phone 2", "goal": Reroute.GOAL + " again", "say": "Once more."}

    def test_a_failed_handover_is_rerouted_to_another_phone_and_the_limit_stops_it(self):
        brain = Brain(self.rules)
        c, app, emulator, games, glc, backend, bots = make(brain, AlwaysFails())
        say(c, bots["Master"], "open clash of clans and report what you see")
        until(lambda: "Nothing can run it here." in texts(c, bots["Master"], "assistant"), timeout=60)
        j = {name: jobs_of(c, bots[name]) for name in ("Phone 2", "Assistant", "Master")}
        self.assertEqual([len(v) for v in j.values()], [1, 1, 1])  # the original handover and exactly two reroutes
        self.assertEqual(j["Phone 2"][0]["target"], "virtual:games")
        self.assertFalse(j["Assistant"][0]["target"])  # the emulator lane
        self.assertIn(bots["Master"], app.state.orch._explain_only)
        self.assertEqual(brain.reroutes, 3)  # the third failure was explain-only: its delegate was refused
        blob = json.dumps([m for _, ms in brain.calls for m in ms])
        self.assertIn("you already rerouted this request twice", blob)

    def test_master_never_sends_the_same_goal_to_a_phone_that_already_tried_it(self):
        def rules(reroutes, last):
            if "[tool result for delegate_task] error" in last:
                return {"type": "reply", "text": "Phone 2 already tried."}
            return {"type": "delegate_task", "bot": "Phone 2", "goal": Reroute.GOAL, "say": "On it."}
        brain = Brain(rules)
        c, app, emulator, games, glc, backend, bots = make(brain, AlwaysFails())
        say(c, bots["Master"], "open clash of clans")
        until(lambda: "Phone 2 already tried." in texts(c, bots["Master"], "assistant"), timeout=40)
        self.assertEqual(len(jobs_of(c, bots["Phone 2"])), 1)
        self.assertIn("Phone 2 already tried this", json.dumps([m for _, ms in brain.calls for m in ms]))

    def test_the_reroute_budget_is_new_for_each_owner_request(self):
        c, app, emulator, games, glc, backend, bots = make(Spark(act(type="reply", text="ok")), AlwaysFails())
        orch = app.state.orch
        orch._reroutes[bots["Master"]] = 2
        orch._explain_only.add(bots["Master"])
        say(c, bots["Master"], "something else")
        until(lambda: "ok" in texts(c, bots["Master"], "assistant"))
        self.assertEqual(orch._reroutes[bots["Master"]], 0)
        self.assertNotIn(bots["Master"], orch._explain_only)

    def test_a_non_master_bot_is_told_to_name_the_better_phone_and_never_to_promise(self):
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."), act(type="reply", text="It needs another phone; ask Master."))
        c, app, emulator, games, glc, backend, bots = make(spark, AlwaysFails())
        say(c, bots["Phone 2"], "open clash of clans")
        until(lambda: "It needs another phone; ask Master." in texts(c, bots["Phone 2"], "assistant"), timeout=40)
        note = spark.calls[-1][1][-1]["content"]
        self.assertIn("tell the owner to ask Master", note)
        self.assertIn("Never offer or promise an install", note)


class NoPlay(unittest.TestCase):
    def test_play_store_links_are_recognised(self):
        for url in ("market://details?id=com.supercell.clashofclans", "https://play.google.com/store/apps/details?id=x",
                    "intent://details?id=x#Intent;scheme=market://details;end", "play.google.com/store"):
            self.assertTrue(is_play_url(url), url)
        for url in ("https://example.com/play", "https://f-droid.org/packages/x"):
            self.assertFalse(is_play_url(url), url)

    def test_a_phone_without_play_refuses_a_play_link_with_a_message_the_worker_can_act_on(self):
        games = Games()
        for act_ in ({"type": "open_url", "url": "market://details?id=a.b"}, {"type": "open_url", "url": "https://play.google.com/store/apps/details?id=a.b"},
                     {"type": "open_app", "package": "com.android.vending"}):
            with self.assertRaises(ValueError) as cm:
                execute(games, act_, [], 1.0, (1080, 2400))
            self.assertEqual(str(cm.exception), NO_PLAY_MESSAGE)
        self.assertEqual(games.opened, [])
        execute(games, {"type": "open_url", "url": "https://f-droid.org"}, [], 1.0, (1080, 2400))  # other links still open
        self.assertEqual(games.opened, ["https://f-droid.org"])
        emulator = RealisticPhone()
        emulator.open_url = lambda url: emulator.taps.append(url)
        execute(emulator, {"type": "open_url", "url": "market://details?id=a.b"}, [], 1.0, (1080, 2400))  # a phone with Play opens it as before
        self.assertEqual(emulator.taps, ["market://details?id=a.b"])

    def test_the_worker_prompt_never_offers_a_play_install_on_a_phone_without_play(self):
        with_play, without = system_prompt(True), system_prompt(False)
        self.assertIn("offer installing it from the Play Store", with_play)
        self.assertNotIn("offer installing it from the Play Store", without)
        self.assertIn("this phone has NO Google Play", without)

    def test_a_play_link_in_a_task_fails_fast_and_the_model_sees_why(self):
        games = Games()
        worker = Worker(act(type="open_url", url="https://play.google.com/store/apps/details?id=com.supercell.clashofclans"),
                        act(type="fail", reason="Clash of Clans is not installed and cannot be installed here (no Play Store, it needs an APK)"))
        res = run_graph("open clash of clans", games, worker, settle=0)
        self.assertEqual(res.status, "failed")
        self.assertEqual(games.opened, [])  # no WebView error page was ever loaded
        self.assertIn("this phone has no Google Play", worker.prompts[1])


if __name__ == "__main__":
    unittest.main()
