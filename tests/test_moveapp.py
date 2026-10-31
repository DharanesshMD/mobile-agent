"""Moving an app between the adb phones without a store, the Master pipeline around it, and each phone's Google account in the directory."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import phones
from agent.device import Device
from agent.moveapp import move_app
from test_chat import Spark, Worker, act, say, texts, until
from test_hybrid import Games, jobs_of, make
from test_queue import RealisticPhone
from test_remote import connect

PKG = "com.supercell.clashofclans"
REMOTE = ["/data/app/~~x==/com.supercell.clashofclans-y==/base.apk", "/data/app/~~x==/com.supercell.clashofclans-y==/split_config.arm64_v8a.apk"]


class ApkPhone(RealisticPhone):
    """A fake adb phone that has APK files to pull and can install them."""
    has_play = True

    def __init__(self, serial, packages=(), accounts=()):
        super().__init__(packages=packages, accounts=accounts)
        self.serial, self.pulled, self.installed, self.pull_dirs, self.fail_install = serial, [], [], [], False

    def apk_paths(self, package):
        return list(REMOTE) if package in self._packages else []

    def pull(self, remote, local):
        self.pulled.append(remote)
        self.pull_dirs.append(str(Path(local).parent))
        Path(local).write_bytes(b"APK" * 1000)

    def install_apks(self, paths):
        if self.fail_install:
            raise ValueError("install failed: INSTALL_FAILED_NO_MATCHING_ABIS")
        self.installed.append([Path(p).name for p in paths])
        self._packages.append(PKG)


class GamesApk(ApkPhone):
    has_play = False

    def screenshot(self):
        return super().screenshot()


class DeviceHelpers(unittest.TestCase):
    def test_apk_paths_pull_and_install_use_the_phones_own_serial(self):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            out = b"package:/data/app/a/base.apk\npackage:/data/app/a/split_x.apk\n" if "path" in cmd else b"Performing Streamed Install\nSuccess\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")
        d = Device("localhost:5565", use_u2=False)
        with mock.patch("agent.device.subprocess.run", fake_run):
            self.assertEqual(d.apk_paths(PKG), ["/data/app/a/base.apk", "/data/app/a/split_x.apk"])
            d.pull("/data/app/a/base.apk", "/tmp/x.apk")
            d.install_apks(["/tmp/a.apk", "/tmp/b.apk"])
        self.assertTrue(all(c[:3] == ["adb", "-s", "localhost:5565"] for c in calls))  # every call names its phone
        self.assertIn(["adb", "-s", "localhost:5565", "install-multiple", "-r", "/tmp/a.apk", "/tmp/b.apk"], calls)

    def test_a_failed_install_is_an_error_not_a_silent_success(self):
        d = Device("x", use_u2=False)
        with mock.patch("agent.device.subprocess.run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"Failure [INSTALL_FAILED_NO_MATCHING_ABIS]", stderr=b"")):
            with self.assertRaises(ValueError):
                d.install_apks(["/tmp/a.apk"])


class MoveApp(unittest.TestCase):
    def test_the_apks_are_pulled_installed_verified_and_the_copies_are_deleted(self):
        src, dst = ApkPhone("emulator-5554", [PKG]), GamesApk("localhost:5565")
        detail = move_app(src, dst, PKG)
        self.assertEqual(src.pulled, REMOTE)  # the base APK and the split, nothing from the app's data
        self.assertTrue(all(p.endswith(".apk") and "/data/data" not in p and "/sdcard" not in p for p in src.pulled))
        self.assertEqual(len(dst.installed[0]), 2)
        self.assertIn(PKG, dst.packages())
        self.assertIn("2 APK file(s)", detail)
        self.assertFalse(os.path.exists(src.pull_dirs[0]))  # the temporary copies are gone

    def test_an_app_that_is_not_there_or_will_not_install_is_reported_and_cleaned_up(self):
        with self.assertRaises(ValueError) as cm:
            move_app(ApkPhone("a"), GamesApk("b"), PKG)
        self.assertIn("not installed on the source phone", str(cm.exception))
        src, dst = ApkPhone("a", [PKG]), GamesApk("b")
        dst.fail_install = True
        with self.assertRaises(ValueError) as cm:
            move_app(src, dst, PKG)
        self.assertIn("NO_MATCHING_ABIS", str(cm.exception))
        self.assertFalse(os.path.exists(src.pull_dirs[0]))


class MasterMove(unittest.TestCase):
    def build(self, spark, worker=None, emulator_has=True):
        emulator, games = ApkPhone("emulator-5554", [PKG] if emulator_has else []), GamesApk("localhost:5565")
        c, app, em, gm, glc, backend, bots = make(spark, worker or Worker(), emulator=emulator, games=games)
        return c, app, emulator, games, bots

    def test_master_moves_an_app_with_a_progress_message_a_result_and_a_follow_up_turn(self):
        spark = Spark(act(type="move_app", app="clash of clans", **{"from": "emulator", "to": "games"}), act(type="reply", text="It is on the games phone now."))
        c, app, emulator, games, bots = self.build(spark)
        say(c, bots["Master"], "put clash of clans on the games phone")
        until(lambda: "It is on the games phone now." in texts(c, bots["Master"], "assistant"), timeout=30)
        said = texts(c, bots["Master"], "assistant")
        self.assertTrue(any(t.startswith("Moving clash of clans from the shared emulator to the games phone") for t in said))
        self.assertTrue(any("Moved clash of clans from the shared emulator to the games phone" in t and "not its data or its sign-in" in t for t in said))
        self.assertIn("move_app finished", spark.calls[1][1][-1]["content"])
        self.assertIn(PKG, games.packages())
        self.assertEqual(jobs_of(c, bots["Master"]), [])  # no phone task was needed

    def test_a_failed_move_is_reported_honestly(self):
        spark = Spark(act(type="move_app", app="clash of clans"), act(type="reply", text="It did not move."))
        c, app, emulator, games, bots = self.build(spark, emulator_has=False)
        say(c, bots["Master"], "move it")
        until(lambda: "It did not move." in texts(c, bots["Master"], "assistant"), timeout=30)
        self.assertTrue(any("I could not move clash of clans" in t and "no installed app matches" in t for t in texts(c, bots["Master"], "assistant")))
        self.assertIn("move_app failed", spark.calls[1][1][-1]["content"])

    def test_a_real_phone_is_refused_clearly(self):
        spark = Spark(act(type="move_app", app="clash of clans", to="Pixel 8"), act(type="move_app", app="clash of clans", **{"from": "Pixel 8", "to": "games"}))
        c, app, emulator, games, bots = self.build(spark)
        app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")
        refusals = lambda: len([t for t in texts(c, bots["Master"], "assistant") if "own Play Store" in t])
        for n, ask in enumerate(("move it to my phone", "move it from my phone"), start=1):
            say(c, bots["Master"], ask)
            until(lambda: refusals() >= n, timeout=20)
        self.assertEqual(games.installed, [])
        self.assertEqual(games.pulled, [])

    def test_only_master_can_move_apps_and_the_same_phone_twice_is_refused(self):
        spark = Spark(act(type="move_app", app="clash of clans", **{"from": "games", "to": "games"}))
        c, app, emulator, games, bots = self.build(spark)
        say(c, bots["Master"], "move it")
        until(lambda: any("same phone twice" in t for t in texts(c, bots["Master"], "assistant")))
        self.assertEqual(games.installed, [])

    def test_the_pipeline_runs_in_order_and_each_step_is_checked_before_the_next(self):
        spark = Spark(act(type="mobile_task", goal="install clash of clans from the play store", verify={"kind": "app_installed", "query": "clash of clans"}, say="Installing."),
                      act(type="move_app", app="clash of clans", **{"from": "emulator", "to": "games", "say": "Moving it."}),
                      act(type="phone_check", kind="app_installed", query="clash of clans", on="games"),
                      act(type="delegate_task", bot="Games phone", goal="open clash of clans and report what you see", say="Opening it there."),
                      act(type="reply", text="It opens on the games phone."), act(type="reply", text="It opens on the games phone."))  # the games bot and Master's final answer
        worker = Worker(act(type="done", answer="installed"), act(type="done", answer="clash of clans is on the screen"))
        c, app, emulator, games, bots = self.build(spark, worker)
        say(c, bots["Master"], "get clash of clans running on the games phone")
        until(lambda: jobs_of(c, bots["Games phone"]) and jobs_of(c, bots["Games phone"])[0]["status"] == "done", timeout=60)
        self.assertIn("finished with status 'done'", spark.calls[1][1][-1]["content"])  # 1: the install finished and was verified, only then
        self.assertIn("move_app finished", spark.calls[2][1][-1]["content"])  # 2: the move, only then
        self.assertIn("[tool result for phone_check] yes (on the games phone)", spark.calls[3][1][-1]["content"])  # 3: checked on the destination, only then
        self.assertEqual(jobs_of(c, bots["Games phone"])[0]["target"], "virtual:games")  # 4: opened by the games phone's own bot
        self.assertFalse(jobs_of(c, bots["Master"])[0]["target"])  # the install ran on the emulator lane
        until(lambda: "It opens on the games phone." in texts(c, bots["Master"], "assistant"), timeout=20)  # a request that moved an app ends with Master's own answer
        self.assertIn("The task you handed to Games phone finished", json.dumps(spark.calls[5][1]) + json.dumps(spark.calls[4][1]))


class Accounts(unittest.TestCase):
    def test_each_phone_reports_its_google_account_without_naming_it(self):
        emulator = ApkPhone("emulator-5554", [], accounts=[("someone@gmail.com", "com.google")])
        c, app, em, gm, glc, backend, bots = make(Spark(act(type="reply", text="ok")), Worker(), emulator=emulator, games=GamesApk("g"))
        rows = {e["key"]: e for e in phones.describe(app.state.db, app.state.jobs)}
        self.assertEqual(rows["emulator"]["account"], "1 Google account(s) signed in")
        self.assertIn("none (it has no Google services", rows["games"]["account"])
        text = phones.render(list(rows.values()), None, master=True)
        self.assertIn("Google account: 1 Google account(s) signed in", text)
        self.assertNotIn("someone@gmail.com", text)
        self.assertIn("The games phone has no Google services and can never sign in", text)
        self.assertIn("never copied between phones", text)

    def test_a_sleeping_phone_is_not_asked_and_an_answer_is_reused_for_a_while(self):
        emulator = ApkPhone("emulator-5554", [], accounts=[("a@b.c", "com.google")])
        asked = []
        real = emulator.accounts
        emulator.accounts = lambda: (asked.append(1), real())[1]
        c, app, em, gm, glc, backend, bots = make(Spark(act(type="reply", text="ok")), Worker(), emulator=emulator)
        phones.describe(app.state.db, app.state.jobs)
        phones.describe(app.state.db, app.state.jobs)
        self.assertEqual(len(asked), 1)  # reused
        app.state.jobs.__dict__.pop("_account_cache")
        app.state.jobs.lifecycle = mock.Mock(info=lambda: {"power": "sleeping"})
        self.assertEqual({e["key"]: e for e in phones.describe(app.state.db, app.state.jobs)}["emulator"]["account"], "unknown (the phone is asleep)")
        self.assertEqual(len(asked), 1)  # a sleeping phone is never asked: its adb call would hang

    def test_phone_check_account_works_on_each_phone(self):
        spark = Spark(act(type="phone_check", kind="account", on="emulator"), act(type="phone_check", kind="account", on="games"),
                      act(type="phone_check", kind="account", on="Pixel 8"), act(type="reply", text="done"))
        emulator = ApkPhone("emulator-5554", [], accounts=[("someone@gmail.com", "com.google")])
        c, app, em, gm, glc, backend, bots = make(spark, Worker(), emulator=emulator, games=GamesApk("g"))
        app.state.db.create_bot("Pixel 8", "", kind="phone", phone_id="local")
        ws, _ = connect(c, app)
        try:
            say(c, bots["Assistant"], "who is signed in where?")
            until(lambda: "done" in texts(c, bots["Assistant"], "assistant"), timeout=30)
        finally:
            ws.__exit__(None, None, None)
        self.assertIn("yes (on the shared emulator): 1 Google account(s) signed in", spark.calls[1][1][-1]["content"])
        self.assertNotIn("someone@gmail.com", spark.calls[1][1][-1]["content"])
        self.assertIn("no (on the games phone): it has no Google services", spark.calls[2][1][-1]["content"])
        self.assertIn("no (on Pixel 8): this phone does not report its accounts: ask the owner", spark.calls[3][1][-1]["content"])


if __name__ == "__main__":
    unittest.main()
