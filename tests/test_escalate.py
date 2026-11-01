"""Automatic escalation: an app that cannot run on one phone is tried on the others, in order, with no question; extra adb phones (ADB_PHONES); launch verification."""
import subprocess
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from agent import phones
from agent.adbphones import AdbPhones, load_adb_phones, parse_adb_phones
from agent.db import Database
from agent.escalate import app_name, package_in_goal, verify_launch
from agent.graph import CRASH_PREFIX
from agent.lifecycle import Lifecycle
from agent.service import create_app
from test_chat import Spark, Worker, act, say, texts, until
from test_lifecycle import Clock, FakeBackend
from test_moveapp import ApkPhone, GamesApk, PKG
from test_remote import connect

CRASH = CRASH_PREFIX + PKG + " crashed 3 times while opening, so I stopped."


class Launch:
    """What opening an app does on a fake phone: it comes to the front, or it crashes."""
    crash_on_open = False

    def open_app(self, package):
        self.opened = getattr(self, "opened", []) + [package]
        if self.crash_on_open:
            self.crash_log = getattr(self, "crash_log", []) + [(package, str(900 + len(getattr(self, "crash_log", []))))]
        else:
            self._front = package

    def crashes(self):
        return list(getattr(self, "crash_log", []))


class Emu(Launch, ApkPhone):
    pass


class GamesP(Launch, GamesApk):
    pass


class Extra(Launch, ApkPhone):
    pass


class FakeAdb:
    """`adb -s <serial> get-state` and `pm list packages com.android.vending` for AdbPhones, with phones that can be unplugged."""
    def __init__(self, serials, play=()):
        self.up, self.play, self.calls = {s: True for s in serials}, set(play), []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        serial, rest = cmd[2], cmd[3:]
        out = ""
        if rest == ["get-state"]:
            out = "device\n" if self.up.get(serial) else ""
        elif "pm" in rest:
            out = "package:com.android.vending\n" if serial in self.play else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def build(spark, *workers, emulator, games=None, extras=None, adb=None, up=None):
    """The whole app with an emulator, optionally the games phone and extra adb phones. extras: {name: (serial, device)}."""
    queue = list(workers)
    extras = extras or {}

    def factory(name):
        if name in ("muse-spark-1.3", "muse-voice-transcribe-1.0"):
            return spark
        return queue.pop(0)

    clock, backend = Clock(), FakeBackend("running")
    virtuals = {"games": (games, Lifecycle(backend, clock=clock, idle_s=300, stop_s=3600))} if games is not None else {}
    adb_phones = None
    if extras:
        fake = adb or FakeAdb([s for s, _ in extras.values()])
        adb_phones = AdbPhones({n: s for n, (s, _) in extras.items()}, run=fake)
        adb_phones.device_factory = lambda serial: next(d for s, d in extras.values() if s == serial)
    app = create_app(emulator, factory, settle=0, db=Database(":memory:"), data_dir=tempfile.mkdtemp(), master=True, virtuals=virtuals, adb_phones=adb_phones)
    esc = app.state.orch.escalator
    esc.describe, esc.sleep, esc.launch_wait, esc.lane_wait = (lambda dev: "an age prompt"), (lambda s: None), 2, 0  # no real waiting in tests
    c = TestClient(app)
    bots = {b["name"]: b["id"] for b in c.get("/api/bots").json()}
    return c, app, bots


def assistant(bots):
    return bots["Assistant"]


def said(c, bot, needle):
    return any(needle in t for t in texts(c, bot, "assistant"))


class Pure(unittest.TestCase):
    def test_the_app_an_open_goal_names(self):
        self.assertEqual(package_in_goal([PKG, "com.other.thing"], "Open Clash of Clans and report what you see"), PKG)
        self.assertIsNone(package_in_goal([PKG], "install clash of clans"))  # an install is not an open
        self.assertIsNone(package_in_goal([PKG], "uninstall clash of clans"))
        self.assertIsNone(package_in_goal([PKG], "check the battery"))

    def test_the_name_the_owner_used_is_kept_and_neatly_capitalised(self):
        self.assertEqual(app_name("open clash of clans", PKG), "Clash of Clans")
        self.assertEqual(app_name("Launch Clash of Clans app, complete loading", PKG), "Clash of Clans")
        self.assertEqual(app_name("do something odd", PKG), "Clashofclans")


class FakeFront:
    def __init__(self, front_after, crashes_after=None):
        self.front_after, self.crash_after, self.opened, self._crashes, self.polls = front_after, crashes_after, False, [("old.app", "1")], 0

    def open_app(self, package):
        self.opened = True

    def foreground(self):
        return self.front_after if self.opened else ""

    def crashes(self):
        self.polls += 1
        if self.opened and self.crash_after is not None and self.polls > self.crash_after:
            return self._crashes + [(PKG, "77")]
        return list(self._crashes)


class VerifyLaunch(unittest.TestCase):
    def test_an_app_that_stays_in_front_without_a_crash_is_up(self):
        self.assertEqual(verify_launch(FakeFront(PKG), PKG, wait=20, poll=2, sleep=lambda s: None), (True, ""))

    def test_a_crash_of_the_app_during_the_wait_fails_it(self):
        ok, why = verify_launch(FakeFront(PKG, crashes_after=3), PKG, wait=20, poll=2, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("crashed within", why)

    def test_an_older_crash_is_not_counted_and_a_wrong_app_in_front_fails_it(self):
        ok, why = verify_launch(FakeFront("com.android.launcher"), PKG, wait=6, poll=2, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("did not stay in front", why)

    def test_it_waits_the_whole_time_before_saying_so(self):
        slept = []
        verify_launch(FakeFront(PKG), PKG, wait=20, poll=2, sleep=slept.append)
        self.assertEqual(sum(slept), 20)


class AdbPhoneConfig(unittest.TestCase):
    def test_names_and_serials_are_parsed_and_bad_ones_are_reported(self):
        ok, problems = parse_adb_phones("Pixel=R5CN1234, bs=127.0.0.1:5565 , games=abc123, x=emulator-5554, y=emulator-5556, two=R5CN1234, ab=x, z=bad serial!")
        self.assertEqual(ok, {"pixel": "R5CN1234"})
        text = " ".join(problems)
        self.assertIn("clashes with the shared emulator or the games phone", text)  # 5565 and emulator-5554 and any emulator-*
        self.assertIn("reserved", text)
        self.assertIn("already used", text)
        self.assertIn("not an adb serial", text)

    def test_environment_and_file_are_merged_and_the_environment_wins(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/phones.json"
            with open(path, "w") as f:
                f.write('{"cloud": "10.0.0.5:5555", "pixel": "FILE123"}')
            found, problems = load_adb_phones({"ADB_PHONES": "pixel=ENV456"}, path)
        self.assertEqual(found, {"cloud": "10.0.0.5:5555", "pixel": "ENV456"})
        self.assertEqual(problems, [])
        self.assertEqual(load_adb_phones({}, "/nonexistent/phones.json"), ({}, []))

    def test_online_uses_get_state_with_the_serial_and_caches_briefly(self):
        fake, now = FakeAdb(["R5"], play=["R5"]), [100.0]
        adb = AdbPhones({"pixel": "R5"}, run=fake, clock=lambda: now[0])
        self.assertTrue(adb.online("pixel"))
        n = len(fake.calls)
        self.assertTrue(adb.online("pixel"))
        self.assertEqual(len(fake.calls), n)  # cached
        fake.up["R5"], now[0] = False, 200.0
        self.assertFalse(adb.online("pixel"))
        self.assertTrue(all(c[:3] == ["adb", "-s", "R5"] for c in fake.calls))  # every call names its phone
        fake.up["R5"] = True
        adb._state.clear()
        self.assertTrue(adb.has_play("pixel"))  # pm list packages com.android.vending found it


class ExtraPhone(unittest.TestCase):
    def test_a_reachable_phone_gets_a_bot_a_lane_a_live_view_and_a_directory_entry(self):
        extra, fake = Extra("R5CN1234", []), FakeAdb(["R5CN1234"], play=["R5CN1234"])
        c, app, bots = build(Spark(act(type="reply", text="ok")), emulator=Emu("emulator-5554", []), extras={"pixel": ("R5CN1234", extra)}, adb=fake)
        self.assertIn("Pixel phone", bots)
        bot = next(b for b in c.get("/api/bots").json() if b["name"] == "Pixel phone")
        self.assertEqual((bot["kind"], bot["phone_id"]), ("virtual", "pixel"))
        jobs = app.state.jobs
        self.assertIn("pixel", jobs.virtuals)
        self.assertEqual(jobs.lane_of(bot["id"]), "virtual:pixel")
        self.assertEqual(c.get("/api/mobile/state?phone=pixel").json()["phone"]["power"], "on")
        self.assertEqual(c.get("/api/device/screenshot?phone=pixel").status_code, 200)
        entry = next(e for e in phones.describe(app.state.db, jobs) if e["key"] == "pixel")
        self.assertEqual(entry["status"], "ready")
        self.assertIn("R5CN1234", entry["can"])
        self.assertIn("Google Play is installed", entry["can"])  # detected with pm list packages

    def test_a_phone_adb_cannot_reach_at_startup_gets_no_bot_until_it_appears(self):
        fake = FakeAdb(["R5CN1234"])
        fake.up["R5CN1234"] = False
        c, app, bots = build(Spark(act(type="reply", text="ok")), emulator=Emu("emulator-5554", []), extras={"pixel": ("R5CN1234", Extra("R5CN1234", []))}, adb=fake)
        self.assertNotIn("Pixel phone", bots)
        self.assertNotIn("pixel", app.state.jobs.virtuals)
        fake.up["R5CN1234"] = True
        app.state.jobs.adb_phones._state.clear()  # the cache is short; waiting is not the point of this test
        c.get("/api/bots")  # polling the list looks again, in the background
        until(lambda: any(b["name"] == "Pixel phone" for b in c.get("/api/bots").json()))

    def test_a_phone_that_goes_offline_fails_its_jobs_honestly_and_shows_offline(self):
        fake = FakeAdb(["R5CN1234"])
        extra = Extra("R5CN1234", [])
        c, app, bots = build(Spark(act(type="reply", text="ok")), emulator=Emu("emulator-5554", []), extras={"pixel": ("R5CN1234", extra)}, adb=fake)
        fake.up["R5CN1234"] = False
        app.state.jobs.adb_phones._state.clear()
        entry = next(e for e in phones.describe(app.state.db, app.state.jobs) if e["key"] == "pixel")
        self.assertIn("OFFLINE", entry["status"])
        self.assertEqual(c.get("/api/mobile/state?phone=pixel").json()["phone"]["power"], "off")
        self.assertEqual(c.get("/api/device/screenshot?phone=pixel").status_code, 409)
        job = app.state.jobs.submit(bots["Pixel phone"], "open settings")
        until(lambda: app.state.db.get_job(job["id"])["status"] == "failed", timeout=20)
        self.assertIn("offline", app.state.db.get_job(job["id"])["result"])
        self.assertEqual(extra.taps, [])  # and nothing fell back to another phone


class Chain(unittest.TestCase):
    def crash_on_emulator(self, c, bots):
        say(c, assistant(bots), "open clash of clans")

    def test_a_crash_moves_the_app_to_the_games_phone_with_no_question_and_one_honest_message(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games)
        self.crash_on_emulator(c, bots)
        until(lambda: said(c, assistant(bots), "so I ran it on the games phone"), timeout=40)
        msgs = texts(c, assistant(bots), "assistant")
        final = next(t for t in msgs if "so I ran it on the games phone" in t)
        self.assertIn("Clash of Clans doesn't run on the shared emulator", final)
        self.assertIn("I copied the app over, only the app", final)
        self.assertIn("an age prompt", final)  # what the screen shows
        self.assertIn("nothing was tapped", final)
        self.assertIn("publisher bans accounts that use bots", final)
        self.assertIn("Take control", final)
        self.assertIn(PKG, games.packages())
        self.assertEqual(games.opened, [PKG])
        self.assertEqual(emulator.pulled, ["/data/app/~~x==/com.supercell.clashofclans-y==/base.apk", "/data/app/~~x==/com.supercell.clashofclans-y==/split_config.arm64_v8a.apk"])
        self.assertEqual(app.state.db.open_items(assistant(bots)), [])  # nothing was asked of the owner
        self.assertFalse(any("want me to" in t.lower() for t in msgs))
        self.assertIn(("games", PKG), [(f["phone"], f["package"]) for f in app.state.db.launch_facts() if f["ok"]])

    def test_the_next_ask_goes_straight_to_the_phone_that_worked(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."), act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games)
        self.crash_on_emulator(c, bots)
        until(lambda: said(c, assistant(bots), "so I ran it on the games phone"), timeout=40)
        games.opened.clear()
        say(c, assistant(bots), "open clash of clans again")
        until(lambda: sum("so I ran it on the games phone" in t for t in texts(c, assistant(bots), "assistant")) == 2, timeout=40)
        self.assertEqual(len(c.get(f"/api/bots/{assistant(bots)}/jobs").json()), 1)  # no second job on the emulator: it was known not to run there
        self.assertEqual(games.opened, [PKG])
        self.assertEqual(len(games.installed), 1)  # and nothing was copied again

    def test_a_phone_where_it_crashes_again_is_recorded_and_the_chain_goes_on_in_order(self):
        emulator, games, pixel = Emu("emulator-5554", [PKG]), GamesP("localhost:5565"), Extra("R5", [])
        games.crash_on_open = True
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games, extras={"pixel": ("R5", pixel)})
        self.crash_on_emulator(c, bots)
        until(lambda: said(c, assistant(bots), "so I ran it on the pixel phone"), timeout=40)
        final = next(t for t in texts(c, assistant(bots), "assistant") if "so I ran it on the pixel phone" in t)
        self.assertIn("I tried the games phone: it crashed within", final)
        facts = {(f["phone"], f["package"]): f["ok"] for f in app.state.db.launch_facts()}
        self.assertEqual(facts[("games", PKG)], 0)  # known not to run on the games phone from now on
        self.assertEqual(facts[("pixel", PKG)], 1)
        self.assertTrue(phones.known_bad(app.state.db, "games", PKG))
        self.assertEqual(games.opened, [PKG])
        self.assertEqual(pixel.opened, [PKG])

    def test_a_phone_known_not_to_run_it_is_skipped_without_trying(self):
        emulator, games, pixel = Emu("emulator-5554", [PKG]), GamesP("localhost:5565"), Extra("R5", [])
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games, extras={"pixel": ("R5", pixel)})
        app.state.db.set_launch_fact("games", PKG, False, "crashed on an earlier try")
        self.crash_on_emulator(c, bots)
        until(lambda: said(c, assistant(bots), "so I ran it on the pixel phone"), timeout=40)
        self.assertEqual(getattr(games, "opened", []), [])  # skipped: nothing was copied there or opened
        self.assertEqual(games.installed, [])

    def test_when_no_phone_can_run_it_it_stops_honestly_and_names_what_would_unblock_it(self):
        emulator, games, pixel = Emu("emulator-5554", [PKG]), GamesP("localhost:5565"), Extra("R5", [])
        games.crash_on_open = pixel.crash_on_open = True
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games, extras={"pixel": ("R5", pixel)})
        self.crash_on_emulator(c, bots)
        until(lambda: said(c, assistant(bots), "I couldn't get Clash of Clans to run on any of your phones"), timeout=40)
        final = next(t for t in texts(c, assistant(bots), "assistant") if "couldn't get" in t)
        self.assertIn("the games phone: it crashed within", final)
        self.assertIn("the pixel phone: it crashed within", final)
        self.assertIn("connect your own phone with the Mobile Agent app", final)  # the one thing that would unblock it
        self.assertEqual(len(c.get(f"/api/bots/{assistant(bots)}/jobs").json()), 1)  # and it did not loop

    def test_the_chain_is_never_longer_than_CHAIN_MAX_phones(self):
        emulator = Emu("emulator-5554", [PKG])
        extras = {f"p{i}": (f"S{i}00", Extra(f"S{i}00", [])) for i in range(5)}
        for _, d in extras.values():
            d.crash_on_open = True
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        with mock.patch("agent.escalate.CHAIN_MAX", 3):
            c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, extras=extras)
            self.crash_on_emulator(c, bots)
            until(lambda: said(c, assistant(bots), "couldn't get"), timeout=40)
        self.assertEqual(sum(bool(d.opened) if hasattr(d, "opened") else False for _, d in extras.values()), 3)

    def test_the_answer_lands_in_the_chat_where_masters_delegated_task_was_asked(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        games._packages.append(PKG)
        spark = Spark(act(type="delegate_task", bot="Games phone", goal="open clash of clans", say="Asking the games phone."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games)
        say(c, bots["Master"], "open clash of clans")
        until(lambda: said(c, bots["Master"], "so I ran it on the shared emulator"), timeout=40)
        final = next(t for t in texts(c, bots["Master"], "assistant") if "so I ran it on the shared emulator" in t)
        self.assertIn("doesn't run on the games phone", final)
        self.assertEqual(emulator.opened, [PKG])

    def test_an_app_known_not_to_run_on_the_asked_phone_is_not_sent_there(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        games._packages.append(PKG)
        spark = Spark(act(type="delegate_task", bot="Games phone", goal="open clash of clans", say="Asking."))
        c, app, bots = build(spark, Worker(act(type="done", answer="never used")), emulator=emulator, games=games)
        app.state.db.set_launch_fact("games", PKG, False, "crashed")
        app.state.db.set_launch_fact("emulator", PKG, True, "")
        say(c, bots["Master"], "open clash of clans on the games phone")
        until(lambda: said(c, bots["Master"], "so I ran it on the shared emulator"), timeout=40)
        self.assertEqual(c.get(f"/api/bots/{bots['Games phone']}/jobs").json(), [])  # no job was started on the games phone
        self.assertEqual(getattr(games, "opened", []), [])


class RealPhoneLast(unittest.TestCase):
    def test_the_owners_phone_is_the_last_resort_and_only_when_connected(self):
        emulator = Emu("emulator-5554", [PKG])
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator)
        ws, phone = connect(c, app)
        try:
            bots = {b["name"]: b["id"] for b in c.get("/api/bots").json()}
            say(c, bots["Assistant"], "open clash of clans")
            until(lambda: said(c, bots["Assistant"], "so I ran it on"), timeout=40)
            final = next(t for t in texts(c, bots["Assistant"], "assistant") if "so I ran it on" in t)
            self.assertIn("It's open now and shows: an age prompt", final)
            self.assertIn(("open_app", {"package": PKG}), phone.log)
        finally:
            ws.__exit__(None, None, None)

    def test_a_phone_that_is_not_connected_is_skipped_and_named_as_the_thing_to_fix(self):
        emulator = Emu("emulator-5554", [PKG])
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator)
        app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")
        say(c, bots["Assistant"], "open clash of clans")
        until(lambda: said(c, bots["Assistant"], "couldn't get Clash of Clans to run"), timeout=40)
        final = next(t for t in texts(c, bots["Assistant"], "assistant") if "couldn't get" in t)
        self.assertIn("Pixel 8: your phone is not connected", final)
        self.assertIn("open the Mobile Agent app on your phone", final)

    def test_an_app_that_is_not_on_the_owners_phone_is_installed_from_its_own_play_store_by_its_bot(self):
        emulator = Emu("emulator-5554", [PKG])
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."))
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), Worker(act(type="done", answer="The Play Store page of the game, installing")), emulator=emulator)
        ws, phone = connect(c, app)
        try:
            phone.installed.clear()  # not on the owner's phone yet
            phone_bot = next(b["id"] for b in c.get("/api/bots").json() if b["kind"] == "phone")
            say(c, bots["Assistant"], "open clash of clans")
            until(lambda: len(c.get(f"/api/bots/{phone_bot}/jobs").json()) == 1, timeout=40)
            job = c.get(f"/api/bots/{phone_bot}/jobs").json()[0]
            self.assertIn("install it if it is not installed", job["goal"])
            self.assertIn("Do not sign in, buy anything or play", job["goal"])
            self.assertEqual(job["target"], "phone:local")
            until(lambda: said(c, bots["Assistant"], "so I ran it on"), timeout=40)
            final = next(t for t in texts(c, bots["Assistant"], "assistant") if "so I ran it on" in t)
            self.assertIn("The Play Store page of the game", final)
        finally:
            ws.__exit__(None, None, None)


class OpenShortcut(unittest.TestCase):
    def test_a_plain_open_message_for_a_known_bad_app_goes_to_the_chain_with_no_model_turn(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        games._packages.append(PKG)
        spark = Spark()  # no scripted reply: a model turn would raise
        c, app, bots = build(spark, Worker(), emulator=emulator, games=games)
        app.state.db.set_launch_fact("emulator", PKG, False, "crashed")
        app.state.db.set_launch_fact("games", PKG, True, "")
        say(c, assistant(bots), "open clash of clans")
        until(lambda: said(c, assistant(bots), "so I ran it on the games phone"), timeout=40)
        self.assertEqual(spark.calls, [])  # the model was not asked anything
        self.assertEqual(c.get(f"/api/bots/{assistant(bots)}/jobs").json(), [])  # and no job went to the emulator
        self.assertEqual(games.opened, [PKG])

    def test_a_longer_request_is_left_to_the_model_and_other_apps_are_not_touched(self):
        emulator, games = Emu("emulator-5554", [PKG]), GamesP("localhost:5565")
        spark = Spark(act(type="reply", text="Sure."), act(type="reply", text="Done."))
        c, app, bots = build(spark, Worker(), emulator=emulator, games=games)
        app.state.db.set_launch_fact("emulator", PKG, False, "crashed")
        say(c, assistant(bots), "open clash of clans and gather all my resources then upgrade the walls please")
        until(lambda: said(c, assistant(bots), "Sure."))
        say(c, assistant(bots), "open settings")
        until(lambda: said(c, assistant(bots), "Done."))
        self.assertEqual(len(spark.calls), 2)


if __name__ == "__main__":
    unittest.main()
