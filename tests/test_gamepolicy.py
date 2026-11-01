"""Game accounts are never played by the agent (Supercell, Niantic and the owner's own list), and the owner can play them with Take control."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent import gamepolicy
from agent.db import Database
from agent.graph import MAX_BLOCKS, run
from agent.jobs import JobManager
from agent.lifecycle import Lifecycle
from agent.phones import render
from agent.device import Element
from agent.service import create_app
from test_chat import Spark, Worker, act, say, setup, texts, until
from test_lanes import GamesDevice, Routed, job_status, make
from test_lifecycle import Clock, FakeBackend
from test_loop import FakeDevice, FakeModel, reply
from test_queue import RealisticPhone, statuses

COC = "com.supercell.clashofclans"


class Policy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "policy.json"
        self.old, gamepolicy.POLICY_FILE = gamepolicy.POLICY_FILE, self.tmp
        gamepolicy._cache.update(stamp=None, owner=[])

    def tearDown(self):
        gamepolicy.POLICY_FILE = self.old
        gamepolicy._cache.update(stamp=None, owner=[])

    def test_the_publishers_that_ban_bots_are_blocked_and_ordinary_apps_are_not(self):
        for pkg in (COC, "com.supercell.clashroyale", "com.supercell.brawlstars", "com.supercell.hayday", "com.supercell.boombeach", "com.supercell.squadbusters",
                    "com.nianticlabs.pokemongo", "com.nianticproject.ingress"):
            self.assertIn("blocked to protect your account", gamepolicy.blocked(pkg), pkg)
        for pkg in ("com.android.settings", "com.shatteredpixel.shatteredpixeldungeon", "com.instagram.android", "", None, "com.supercellfake.app"):
            self.assertIsNone(gamepolicy.blocked(pkg), pkg)
        self.assertIn("Clash of Clans", gamepolicy.blocked(COC))
        self.assertIn("Take control", gamepolicy.blocked(COC))

    def test_every_builtin_entry_cites_its_public_rule(self):
        for prefix, (_, publisher, source) in gamepolicy.BUILTIN.items():
            self.assertTrue(source.startswith("https://"), prefix)
            self.assertTrue(publisher)

    def test_the_owner_can_add_apps_but_never_remove_built_in_ones(self):
        self.assertIsNone(gamepolicy.blocked("com.example.farmgame"))
        self.tmp.write_text(json.dumps({"blocked": ["com.example.farmgame", "not a package", 5]}))
        self.assertIn("blocked", gamepolicy.blocked("com.example.farmgame"))
        self.assertIn("blocked", gamepolicy.blocked("com.example.farmgame.beta"))
        self.assertIsNone(gamepolicy.blocked("com.example.other"))
        self.tmp.write_text(json.dumps({"blocked": []}))  # an empty list removes nothing built in
        self.assertIsNone(gamepolicy.blocked("com.example.farmgame"))
        self.assertIsNotNone(gamepolicy.blocked(COC))
        self.tmp.write_text("{ not json")  # a broken file adds nothing and breaks nothing
        self.assertIsNotNone(gamepolicy.blocked(COC))

    def test_a_request_that_names_a_blocked_game_is_recognised(self):
        self.assertEqual(gamepolicy.mentions_blocked("open clash of clans and gather my resources"), ["Clash of Clans"])
        self.assertEqual(gamepolicy.mentions_blocked("Play Brawl Stars, then check Clash Royale"), ["Brawl Stars", "Clash Royale"])
        self.assertEqual(gamepolicy.mentions_blocked("open shattered pixel dungeon"), [])

    def test_only_input_actions_count_as_playing(self):
        for a in ({"type": "tap", "x": 1, "y": 2}, {"type": "type", "text": "x"}, {"type": "scroll", "direction": "down"}, {"type": "swipe", "direction": "up"},
                  {"type": "key", "key": "enter"}, {"type": "type_secret", "label": "a"}):
            self.assertTrue(gamepolicy.is_input(a), a)
        for a in ({"type": "open_app", "package": COC}, {"type": "wait", "seconds": 5}, {"type": "key", "key": "back"}, {"type": "key", "key": "home"},
                  {"type": "find_app", "query": "x"}, {"type": "open_url", "url": "https://x"}, {"type": "done", "answer": "x"}, {"type": "rotate", "orientation": "portrait"}):
            self.assertFalse(gamepolicy.is_input(a), a)


class BlockedPhone(FakeDevice):
    """A phone with a blocked game in front: it records every input it is asked to send."""
    def __init__(self, front=COC):
        super().__init__([b""])
        self.front, self.opened, self.keys_sent = front, [], []

    def screenshot(self):
        from test_loop import png
        return png(1 + len(self.taps) + len(self.opened))

    def foreground(self):
        return self.front

    els = None

    def elements(self):
        return self.els if self.els is not None else super().elements()

    def open_app(self, package):
        self.opened.append(package)

    def swipe(self, *a):
        self.taps.append(("swipe", a))


class Guard(unittest.TestCase):
    def test_a_tap_in_a_blocked_game_is_refused_and_the_model_finishes_with_what_it_sees(self):
        dev = BlockedPhone()
        model = FakeModel([reply(type="tap", x=100, y=100), reply(type="done", answer="The age screen is showing")])
        res = run("play it", dev, model, settle=0)
        self.assertEqual(dev.taps, [])
        self.assertEqual((res.status, res.answer), ("done", "The age screen is showing"))
        self.assertIn("blocked to protect your account", "\n".join(res.history))
        self.assertIn("POLICY: Clash of Clans is open here", model.calls[0])  # the model is told before it tries

    def test_scroll_swipe_type_and_enter_are_refused_too(self):
        dev = BlockedPhone()
        model = FakeModel([reply(type="scroll", direction="down"), reply(type="type", text="hi"), reply(type="key", key="enter"), reply(type="done", answer="x")])
        res = run("play it", dev, model, settle=0)
        self.assertEqual((dev.taps, getattr(dev, "typed", []), dev.keys), ([], [], []))
        self.assertEqual(res.status, "denied")  # three refusals in a row end the task

    def test_after_repeated_attempts_the_task_ends_with_the_policy_reason(self):
        dev = BlockedPhone()
        model = FakeModel([reply(type="tap", x=1, y=1)] * (MAX_BLOCKS + 2))
        res = run("gather my resources", dev, model, settle=0)
        self.assertEqual(res.status, "denied")
        self.assertTrue(res.answer.startswith(gamepolicy.POLICY_PREFIX))
        self.assertIn("Take control", res.answer)
        self.assertEqual(dev.taps, [])
        self.assertEqual(len(model.calls), MAX_BLOCKS)

    def test_opening_waiting_back_and_home_are_allowed(self):
        dev = BlockedPhone()
        model = FakeModel([reply(type="open_app", package=COC), reply(type="wait", seconds=1), reply(type="key", key="back"), reply(type="key", key="home"),
                           reply(type="done", answer="It shows the age screen")])
        res = run("open clash of clans and tell me what you see", dev, model, settle=0)
        self.assertEqual(res.status, "done")
        self.assertEqual(dev.opened, [COC])
        self.assertEqual(dev.keys, ["KEYCODE_BACK", "KEYCODE_HOME"])

    def test_ordinary_apps_are_not_affected(self):
        dev = BlockedPhone(front="com.shatteredpixel.shatteredpixeldungeon")
        res = run("play a move", dev, FakeModel([reply(type="tap", x=5, y=5), reply(type="done", answer="moved")]), settle=0)
        self.assertEqual(res.status, "done")
        self.assertEqual(len(dev.taps), 1)

    def test_the_guard_holds_when_the_owner_steers_the_task(self):
        dev = BlockedPhone()
        sent = []
        model = FakeModel([reply(type="tap", x=1, y=1), reply(type="done", answer="x")])
        res = run("open it", dev, model, settle=0, steer=lambda: ["now tap the play button and collect everything"] if not sent and not sent.append(1) else [])
        self.assertEqual(dev.taps, [])
        self.assertIn("Owner update", model.calls[0])

    def test_a_saved_secret_is_not_typed_into_a_blocked_game(self):
        dev = BlockedPhone()

        class Vault:
            def labels(self):
                return ["supercell id"]

            def reveal(self, *a):
                raise AssertionError("the secret must never be typed into a blocked game")
        model = FakeModel([reply(type="type_secret", label="supercell id", app="Clash of Clans"), reply(type="done", answer="login screen")])
        res = run("log in", dev, model, settle=0, secrets=Vault(), handler=lambda p: {"approved": True})
        self.assertEqual(getattr(dev, "typed", []), [])
        self.assertEqual(res.status, "done")

    def test_the_guard_costs_no_extra_call_when_no_game_is_in_front(self):
        calls = []

        class Counting(BlockedPhone):
            def foreground(self):
                calls.append(1)
                return "com.android.settings"
        dev = Counting()
        run("tap", dev, FakeModel([reply(type="tap", x=1, y=1), reply(type="done", answer="x")]), settle=0)
        self.assertEqual(len(calls), 2)  # once per screen read, in parallel with the screenshot, never again at the action


CHROME = "com.android.chrome"


def page(url, *texts, clickable=("Play now",)):
    """What Chrome shows: the address bar at the top, then the page's own text."""
    els = [Element(0, url, (540, 150), True)] if url else []
    els += [Element(len(els) + i, t, (540, 600 + 120 * i), t in clickable) for i, t in enumerate(texts)]
    return els


class BrowserGuard(unittest.TestCase):
    def run_on(self, els, *actions, front=CHROME):
        dev = BlockedPhone(front=front)
        dev.els = els
        res = run("look at it", dev, FakeModel([reply(**a) for a in actions] + [reply(type="done", answer="described")]), settle=0)
        return dev, res

    def test_a_cloud_gaming_page_for_a_blocked_game_is_blocked(self):
        for els in (page("cloudmoon.com/play/clash-of-clans", "Loading"),
                    page("m.easyfun.gg/games/clashofclans", "Welcome"),
                    page("yaksgames.com/", "Clash of Clans", "Play now", "Start playing now"),
                    page("https://www.cloudmoon.com/", "Clash of Clans", "Streaming")):
            dev, res = self.run_on(els, {"type": "tap", "x": 5, "y": 5})
            self.assertEqual(dev.taps, [], [e.label for e in els])
            self.assertIn("blocked to protect your account", "\n".join(res.history))
            self.assertIn("cloud-gaming", "\n".join(res.history))

    def test_swipe_type_and_enter_are_blocked_on_it_too(self):
        dev, res = self.run_on(page("cloudmoon.com/play/clash-of-clans", "x"), {"type": "scroll", "direction": "down"}, {"type": "type", "text": "a"}, {"type": "key", "key": "enter"})
        self.assertEqual((dev.taps, getattr(dev, "typed", []), dev.keys), ([], [], []))

    def test_reading_leaving_and_opening_another_page_are_still_allowed(self):
        els = page("cloudmoon.com/play/clash-of-clans", "Loading", "Close tab", clickable=("Close tab",))
        dev, res = self.run_on(els, {"type": "key", "key": "back"}, {"type": "open_url", "url": "https://example.com"}, {"type": "key", "key": "home"},
                               {"type": "tap", "element": 2}, {"type": "wait", "seconds": 1})
        self.assertEqual(dev.keys, ["KEYCODE_BACK", "KEYCODE_HOME"])
        self.assertEqual(len(dev.taps), 1)  # the tab's close button
        self.assertEqual(res.status, "done")

    def test_a_normal_page_a_search_page_and_an_article_are_allowed(self):
        for els in (page("example.com", "Example Domain", "More information"),
                    page("google.com/search?q=clash+of+clans+cloudmoon", "CloudMoon - Play Clash of Clans online", "cloudmoon.com > clash-of-clans", "Clash of Clans on EasyFun.gg"),
                    page("en.wikipedia.org/wiki/Clash_of_Clans", "Clash of Clans is a freemium mobile game", "Play now on Android"),
                    page("supercell.com/en/games/clashofclans", "Clash of Clans", "Play now", "Start playing"),
                    page("cloudmoon.com", "Games", "Racing", "Puzzle", "Cooking", "Sports", "Action", "Cards", "Casual", "Arcade", "Strategy")):
            dev, res = self.run_on(els, {"type": "tap", "x": 5, "y": 5})
            self.assertEqual(len(dev.taps), 1, [e.label for e in els])

    def test_a_full_screen_stream_with_no_address_bar_stays_blocked_until_the_browser_shows_another_address(self):
        dev = BlockedPhone(front=CHROME)
        seen = iter([page("cloudmoon.com/play/clash-of-clans", "Loading"), page(None, "Streaming"), page(None, "Streaming"), page("example.com", "Example Domain")])
        dev.elements = lambda: next(seen)
        model = FakeModel([reply(type="wait", seconds=1), reply(type="tap", x=1, y=1), reply(type="tap", x=2, y=2), reply(type="done", answer="x")])
        res = run("watch", dev, model, settle=0)
        self.assertEqual(dev.taps, [])  # the stream still counted while its address bar was hidden
        dev2 = BlockedPhone(front=CHROME)
        seen2 = iter([page("cloudmoon.com/play/clash-of-clans", "Loading"), page("example.com", "Example Domain"), page("example.com", "Example Domain")])
        dev2.elements = lambda: next(seen2)
        res = run("go", dev2, FakeModel([reply(type="wait", seconds=1), reply(type="tap", x=1, y=1), reply(type="done", answer="x")]), settle=0)
        self.assertEqual(len(dev2.taps), 1)  # another address: input is allowed again

    def test_other_browsers_count_and_non_browsers_are_never_page_checked(self):
        for pkg in ("org.mozilla.firefox", "com.brave.browser", "org.chromium.webview_shell"):
            dev, _ = self.run_on(page("cloudmoon.com/play/clash-of-clans", "Loading"), {"type": "tap", "x": 5, "y": 5}, front=pkg)
            self.assertEqual(dev.taps, [], pkg)
        dev, _ = self.run_on(page("cloudmoon.com/play/clash-of-clans", "Loading"), {"type": "tap", "x": 5, "y": 5}, front="com.example.notes")
        self.assertEqual(len(dev.taps), 1)

    def test_a_request_to_play_a_blocked_game_in_the_browser_is_refused_before_any_task_exists(self):
        spark = Spark(act(type="mobile_task", goal="open chrome and play clash of clans on cloudmoon, gather my resources", say="On it."))
        c, phone, bot, app = setup(spark)
        say(c, bot, "play clash of clans in chrome on cloudmoon")
        until(lambda: any("through a browser or a cloud-gaming site" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(c.get(f"/api/bots/{bot}/jobs").json(), [])
        self.assertEqual(phone.taps, [])
        self.assertEqual(gamepolicy.cloud_play_request("open cloudmoon in chrome and search for clash of clans guides"), None)
        self.assertEqual(gamepolicy.cloud_play_request("open clash of clans"), None)
        self.assertEqual(gamepolicy.cloud_play_request("play shattered pixel dungeon in the browser"), None)
        self.assertEqual(gamepolicy.cloud_play_request("farm resources in Clash of Clans on easyfun.gg"), "Clash of Clans")



class JobsAndRoutines(unittest.TestCase):
    def test_a_job_started_by_a_routine_is_guarded_too(self):
        spark = Spark(act(type="reply", text="ok"))
        c, phone, bot, app = setup(spark, Worker(act(type="tap", x=5, y=5), act(type="tap", x=5, y=5), act(type="tap", x=5, y=5)))
        app.state.jobs.device = RealisticPhone(packages=[COC], front=COC)
        phone_dev = app.state.jobs.device
        app.state.jobs.submit(bot, "collect resources in clash of clans", routine="r-nightly")
        until(lambda: statuses(c, bot).get("collect resources in clash of clans") == "denied", timeout=30)
        job = c.get(f"/api/bots/{bot}/jobs").json()[0]
        self.assertTrue(job["result"].startswith(gamepolicy.POLICY_PREFIX))
        self.assertEqual(phone_dev.taps, [])

    def test_the_orchestrator_does_not_retry_and_explains_what_it_can_do(self):
        spark = Spark(act(type="mobile_task", goal="gather my resources in clash of clans", say="ok"), act(type="reply", text="I won't play it."))
        c, phone, bot, app = setup(spark, Worker(act(type="tap", x=5, y=5), act(type="tap", x=5, y=5), act(type="tap", x=5, y=5)))
        app.state.jobs.device = RealisticPhone(packages=[COC], front=COC)
        say(c, bot, "gather my resources in clash of clans")
        until(lambda: any("won't play" in t for t in texts(c, bot, "assistant")), timeout=30)
        self.assertEqual(app.state.orch._retries[bot], 1)  # no automatic retry is left
        last = spark.calls[-1][1][-1]["content"]
        self.assertIn("game-account guard", last)
        self.assertIn("Take control", last)
        self.assertEqual(len(c.get(f"/api/bots/{bot}/jobs").json()), 1)  # no second task, on any phone
        self.assertTrue(app.state.db.memory_search(bot, "Not played"))


class Wording(unittest.TestCase):
    def test_the_prompts_and_the_directory_state_the_policy(self):
        from agent.loop import SYSTEM
        root = Path(__file__).resolve().parent.parent
        self.assertIn("ban accounts that use bots", SYSTEM)
        self.assertIn("Automating a game can get your account banned", SYSTEM)  # the warning card for any other game
        for name in ("orchestrator.md", "master.md"):
            self.assertIn("Game accounts", (root / "prompts" / name).read_text())
        text = render([{"key": "emulator", "label": "Shared emulator", "bot": "x", "status": "ready", "can": "c", "known_bad": [], "runs": []}])
        self.assertIn("Game accounts: no phone plays Supercell games", text)
        self.assertIn("Take control", text)


class OwnerControl(unittest.TestCase):
    def test_taking_control_of_the_games_phone_pauses_only_its_lane(self):
        worker = Routed({"EMUJOB": [{"type": "tap", "x": 1, "y": 1}, {"type": "done", "answer": "emu"}],
                         "GAMEJOB": [{"type": "tap", "x": 2, "y": 2}, {"type": "done", "answer": "game"}]})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        r = c.post("/api/mobile/control?phone=games", json={"action": "take"}).json()
        self.assertTrue(r["held"])
        self.assertFalse(c.get("/api/mobile/state").json()["held"])  # the shared emulator is not held
        self.assertTrue(c.get("/api/mobile/state?phone=games").json()["held"])
        app.state.jobs.submit(bots["bot"], "EMUJOB open settings")
        app.state.jobs.submit(bots["virtual"], "GAMEJOB look around")
        until(lambda: job_status(c, bots["bot"]) == "done", timeout=30)  # the emulator carries on
        time.sleep(1.5)
        self.assertEqual(games.taps, [])  # the games job waits for the owner
        self.assertIn("Paused", [m for m in c.get(f"/api/bots/{bots['virtual']}/state").json()["messages"] if m["kind"] == "status"][0]["content"])
        c.post("/api/mobile/control?phone=games", json={"action": "release"})
        until(lambda: job_status(c, bots["virtual"]) == "done", timeout=30)
        self.assertTrue(games.taps)

    def test_owner_actions_go_to_the_named_phone_and_nowhere_else(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        self.assertEqual(c.post("/api/device/tap?phone=games", json={"fx": 0.5, "fy": 0.25}).status_code, 200)
        self.assertEqual(len(games.taps), 1)
        self.assertEqual(emulator.taps, [])
        self.assertEqual(c.post("/api/device/key?phone=games", json={"key": "home"}).status_code, 200)
        self.assertEqual(games.keys, ["KEYCODE_HOME"])
        self.assertEqual(c.post("/api/device/type?phone=games", json={"text": "abc"}).status_code, 200)
        self.assertEqual(games.typed, ["abc"])
        self.assertEqual(c.post("/api/device/swipe?phone=games", json={"direction": "down"}).status_code, 200)
        self.assertEqual(emulator.keys + emulator.typed, [])
        self.assertEqual(c.post("/api/device/tap?phone=nope", json={"fx": 0.5, "fy": 0.5}).status_code, 404)
        self.assertEqual(c.post("/api/mobile/control?phone=nope", json={"action": "take"}).status_code, 404)

    def test_the_live_touch_stream_drives_the_named_phone(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        touches = []
        games.touch = lambda kind, x, y: touches.append((kind, x, y))
        with c.websocket_connect("/api/device/touch?phone=games") as ws:
            ws.send_json({"t": "down", "fx": 0.5, "fy": 0.5})
            ws.send_json({"t": "up", "fx": 0.5, "fy": 0.5})
            until(lambda: len(touches) == 2)
        self.assertEqual([t[0] for t in touches], ["down", "up"])

    def test_a_sleeping_games_phone_is_not_driven_blindly(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        lc.sleep()
        self.assertEqual(c.post("/api/device/tap?phone=games", json={"fx": 0.5, "fy": 0.5}).status_code, 409)
        self.assertEqual(games.taps, [])

    def test_an_owner_who_walks_away_gives_the_phone_back_by_itself(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        jobs = app.state.jobs
        jobs.hold_idle_s = 600
        jobs.take_control("virtual:games")
        jobs._expire_hold("virtual:games")
        self.assertTrue(jobs.is_held("virtual:games"))  # just taken: not forgotten yet
        lc.last_activity -= 700  # nothing happened on that phone for a long time
        jobs._expire_hold("virtual:games")
        self.assertFalse(jobs.is_held("virtual:games"))

    def test_an_extra_adb_phone_without_power_management_expires_by_its_own_clock(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        jobs = app.state.jobs
        jobs.virtuals["usb"] = (GamesDevice(), None)  # no lifecycle: always on
        jobs.hold_idle_s = 0.2
        jobs.take_control("virtual:usb")
        self.assertTrue(jobs.is_held("virtual:usb"))
        time.sleep(0.4)
        jobs._expire_hold("virtual:usb")
        self.assertFalse(jobs.is_held("virtual:usb"))

    def test_the_emulators_own_control_still_works_as_before(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        self.assertTrue(c.post("/api/mobile/control", json={"action": "take"}).json()["held"])
        self.assertTrue(app.state.jobs.held)
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 200)
        self.assertEqual(len(emulator.taps), 1)
        self.assertFalse(c.post("/api/mobile/control", json={"action": "release"}).json()["held"])


if __name__ == "__main__":
    unittest.main()
