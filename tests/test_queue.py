"""Task queue: order, dependencies, verification, owner-wait timeout, phone recovery and restart recovery."""
import subprocess
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from agent.checks import find_packages, parse_crashes, parse_focus, run_check
from agent.db import Database
from agent.graph import CRASH_PREFIX, run as run_graph
from agent.jobs import is_phone_error
from agent.lifecycle import Lifecycle
from agent.orchestrator import same_goal
from agent.service import create_app
from test_chat import Spark, Worker, act, msgs, open_card, say, setup, texts, until
from test_lifecycle import FakeBackend, Clock
from test_service import Phone


def statuses(c, bot):
    return {j["goal"]: j["status"] for j in c.get(f"/api/bots/{bot}/jobs").json()}


class RealisticPhone(Phone):
    """A phone that can answer the checks (installed apps, accounts, app in front)."""
    def __init__(self, packages=(), accounts=(), front=""):
        super().__init__()
        self._packages, self._accounts, self._front = list(packages), list(accounts), front

    def packages(self):
        return self._packages

    def accounts(self):
        return self._accounts

    def foreground(self):
        return self._front


class Facts(unittest.TestCase):
    def test_package_matching_and_focus_parsing(self):
        pk = ["com.supercell.clashofclans", "com.android.settings", "com.instagram.android"]
        self.assertEqual(find_packages(pk, "Clash of Clans"), ["com.supercell.clashofclans"])
        self.assertEqual(find_packages(pk, "instagram reels"), ["com.instagram.android"])
        self.assertEqual(find_packages(pk, "tiktok"), [])
        self.assertEqual(parse_focus("mCurrentFocus=Window{bf5653d u0 com.android.vending/com.google.finsky.Foo}"), "com.android.vending")

    def test_checks_never_leak_account_names(self):
        ok, detail = run_check(RealisticPhone(accounts=[("me@gmail.com", "com.google")]), {"kind": "account"})
        self.assertTrue(ok)
        self.assertNotIn("me@gmail.com", detail)
        self.assertEqual(run_check(RealisticPhone(), {"kind": "account"}), (False, "no Google account is signed in"))
        self.assertFalse(run_check(RealisticPhone(), {"kind": "nope"})[0])

    def test_same_goal_catches_rewordings_but_not_different_tasks(self):
        self.assertTrue(same_goal("Install Clash of Clans on the phone.", "install clash of clans on the phone"))
        self.assertFalse(same_goal("open the camera", "check the battery"))

    def test_which_errors_deserve_a_phone_restart(self):
        self.assertTrue(is_phone_error(subprocess.TimeoutExpired("adb", 60)))
        self.assertTrue(is_phone_error(RuntimeError("adb: device offline")))
        self.assertFalse(is_phone_error(ValueError("bad hour")))
        self.assertFalse(is_phone_error(RuntimeError("model returned nonsense")))


class Queue(unittest.TestCase):
    def test_tasks_run_one_at_a_time_in_order(self):
        spark = Spark(act(type="mobile_task", goal="first task", say="ok"), act(type="mobile_task", goal="second task", say="ok"),
                      act(type="reply", text="first done"), act(type="reply", text="second done"))
        w1, w2 = Worker(act(type="handoff", reason="x"), act(type="done", answer="one")), Worker(act(type="done", answer="two"))
        c, _, bot, _ = setup(spark, w1, w2)
        say(c, bot, "a")
        card = until(lambda: open_card(c, bot))
        say(c, bot, "b")
        until(lambda: statuses(c, bot).get("second task") == "queued")
        self.assertEqual(w2.prompts, [])  # the second worker has not touched the phone
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"approved": True})
        until(lambda: statuses(c, bot) == {"first task": "done", "second task": "done"})

    def test_a_plan_runs_in_order_and_the_next_step_gets_the_result(self):
        steps = [{"goal": "install the app"}, {"goal": "open the app and read the score"}]
        spark = Spark(act(type="mobile_plan", say="Two steps.", steps=steps), act(type="reply", text="s1"), act(type="reply", text="s2"))
        w1, w2 = Worker(act(type="done", answer="installed v2.1")), Worker(act(type="done", answer="score 42"))
        c, _, bot, _ = setup(spark, w1, w2)
        say(c, bot, "do it")
        until(lambda: set(statuses(c, bot).values()) == {"done"} and len(statuses(c, bot)) == 2)
        self.assertIn("Result of the previous step: installed v2.1", w2.prompts[0])
        self.assertNotIn("Result of the previous step", w1.prompts[0])

    def test_a_step_is_skipped_when_the_one_before_it_fails_and_the_owner_is_told(self):
        steps = [{"goal": "install the app"}, {"goal": "open the app"}]
        spark = Spark(act(type="mobile_plan", steps=steps), act(type="reply", text="the install failed"), act(type="reply", text="the second step did not run"))
        c, _, bot, _ = setup(spark, Worker(act(type="fail", reason="no space")))
        say(c, bot, "go")
        until(lambda: statuses(c, bot) == {"install the app": "failed", "open the app": "skipped"})
        until(lambda: "the second step did not run" in texts(c, bot, "assistant"))

    def test_a_task_that_depends_on_an_unfinished_one_waits_but_does_not_block_others(self):
        db = Database(":memory:")
        c, phone, bot, app = setup(Spark(), db=db)
        jobs = app.state.jobs
        a = db.create_job(bot, "A is stuck")
        db.update_job(a["id"], status="waiting")  # an active job that is not in the queue, so it never finishes
        dep = db.create_job(bot, "needs A", after=a["id"])
        self.assertEqual(jobs._verdict(type("J", (), {"after": a["id"]})()), "wait")
        db.update_job(a["id"], status="failed")
        self.assertEqual(jobs._verdict(type("J", (), {"after": a["id"]})()), "skip")
        db.update_job(a["id"], status="done")
        self.assertEqual(jobs._verdict(type("J", (), {"after": a["id"]})()), "run")
        self.assertEqual(jobs._verdict(type("J", (), {"after": None})()), "run")

    def test_the_verification_gate_fails_a_job_the_phone_does_not_confirm(self):
        spark = Spark(act(type="mobile_task", goal="install clash of clans", say="ok", verify={"kind": "app_installed", "query": "clash of clans"}),
                      act(type="reply", text="it did not install"))
        c, phone, bot, app = setup(spark, Worker(act(type="done", answer="installed it")))
        app.state.jobs.device = RealisticPhone(packages=["com.android.settings"])
        say(c, bot, "install it")
        until(lambda: statuses(c, bot) == {"install clash of clans": "failed"})
        job = c.get(f"/api/bots/{bot}/jobs").json()[0]
        self.assertIn("check on the phone failed", job["result"])
        self.assertIn("no installed app matches", job["result"])

    def test_the_verification_gate_passes_when_the_phone_confirms(self):
        spark = Spark(act(type="mobile_task", goal="install clash of clans", say="ok", verify={"kind": "app_installed", "query": "clash of clans"}),
                      act(type="reply", text="installed"))
        c, phone, bot, app = setup(spark, Worker(act(type="done", answer="installed it")))
        app.state.jobs.device = RealisticPhone(packages=["com.supercell.clashofclans"])
        say(c, bot, "install it")
        until(lambda: statuses(c, bot) == {"install clash of clans": "done"})

    def test_the_orchestrator_can_check_the_phone_itself(self):
        spark = Spark(act(type="phone_check", kind="app_installed", query="instagram"), act(type="reply", text="Instagram is not installed yet."))
        c, phone, bot, app = setup(spark)
        app.state.jobs.device = RealisticPhone(packages=["com.android.settings"])
        say(c, bot, "is instagram installed?")
        until(lambda: "Instagram is not installed yet." in texts(c, bot, "assistant"))
        self.assertIn("[tool result for phone_check] no: no installed app matches 'instagram'", spark.calls[1][1][-1]["content"])

    def test_the_model_sees_its_queue_and_recent_outcomes(self):
        db = Database(":memory:")
        spark = Spark(act(type="reply", text="ok"))
        c, _, bot, app = setup(spark, db=db)
        old = db.create_job(bot, "install the game")
        db.update_job(old["id"], status="failed", result="no space")
        a = db.create_job(bot, "sign in")
        db.update_job(a["id"], status="waiting")
        b = db.create_job(bot, "open the game", after=a["id"])
        say(c, bot, "status?")
        until(lambda: spark.calls)
        system = spark.calls[0][0]
        self.assertIn(f"{b['id']} [queued] open the game (waits for {a['id']})", system)
        self.assertIn(f"{a['id']} [waiting] sign in", system)
        self.assertIn(f"{old['id']} failed: install the game -> no space", system)


class Stuck(unittest.TestCase):
    def test_a_task_waiting_for_the_owner_gives_the_phone_up_and_the_next_task_runs(self):
        spark = Spark(act(type="mobile_task", goal="needs the owner", say="ok"), act(type="mobile_task", goal="easy task", say="ok"),
                      act(type="reply", text="timed out"), act(type="reply", text="easy done"))
        w1, w2 = Worker(act(type="handoff", reason="your turn")), Worker(act(type="done", answer="done"))
        c, _, bot, app = setup(spark, w1, w2)
        app.state.jobs.owner_wait_s = 1.0
        say(c, bot, "a")
        until(lambda: open_card(c, bot))
        say(c, bot, "b")
        until(lambda: statuses(c, bot) == {"needs the owner": "failed", "easy task": "done"}, timeout=30)
        job = next(j for j in c.get(f"/api/bots/{bot}/jobs").json() if j["goal"] == "needs the owner")
        self.assertIn("Timed out", job["result"])
        card = next(m for m in msgs(c, bot) if m["kind"] == "action_item")
        self.assertEqual(card["meta"]["status"], "expired")

    def test_forgotten_owner_control_ends_by_itself(self):
        c, _, bot, app = setup(Spark())
        jobs = app.state.jobs
        clock = Clock()
        jobs.lifecycle = Lifecycle(FakeBackend(), clock=clock)
        jobs.hold_idle_s = 600
        jobs.take_control()
        clock.t += 300
        jobs._expire_hold()
        self.assertTrue(jobs.held)
        clock.t += 400
        jobs._expire_hold()
        self.assertFalse(jobs.held)

    def test_a_restart_puts_waiting_jobs_back_in_the_queue_and_fails_the_running_ones(self):
        db = Database(":memory:")
        bot = db.create_bot("Assistant")["id"]
        running = db.create_job(bot, "was running")
        db.update_job(running["id"], status="running")
        db.create_job(bot, "was queued")
        spark = Spark(act(type="reply", text="ok"))
        c, _, bot, app = setup(spark, Worker(act(type="done", answer="finished")), db=db)
        until(lambda: statuses(c, bot).get("was queued") == "done")
        self.assertEqual(statuses(c, bot)["was running"], "failed")
        self.assertTrue(any("interrupted by a restart" in t for t in texts(c, bot, "assistant")))


class Recovery(unittest.TestCase):
    def test_a_hung_phone_is_restarted_before_a_task_runs(self):
        backend = FakeBackend("running", boot_polls=1)
        backend.healthy_answers = [False, True]
        backend.healthy = lambda: backend.healthy_answers.pop(0)
        l = Lifecycle(backend, clock=Clock(), idle_s=300, stop_s=3600)
        self.assertTrue(l.ensure_healthy(10))
        self.assertEqual([x for x in backend.calls if x in ("stop", "start")], ["stop", "start"])
        self.assertEqual(l.power, "on")

    def test_a_task_that_hits_a_dead_phone_restarts_it_and_runs_again(self):
        class Dying:
            tokens = {"prompt": 0, "completion": 0}

            def complete(self, *a, **k):
                raise subprocess.TimeoutExpired("adb shell wm size", 60)

        backend = FakeBackend("running", boot_polls=1)
        l = Lifecycle(backend, clock=Clock(), idle_s=300, stop_s=3600)
        queue = [Dying(), Worker(act(type="done", answer="finished"))]
        spark = Spark(act(type="mobile_task", goal="open settings", say="ok"), act(type="reply", text="done"))
        app = create_app(Phone(), lambda m: queue.pop(0) if m == "muse-wonder-1.0" else spark, settle=0, db=Database(":memory:"),
                         data_dir=tempfile.mkdtemp(), lifecycle=l)
        c = TestClient(app)
        bot = c.get("/api/bots").json()[0]["id"]
        say(c, bot, "go")
        until(lambda: statuses(c, bot) == {"open settings": "done"}, timeout=30)
        self.assertIn("stop", backend.calls)
        self.assertIn("start", backend.calls)

    def test_a_watchdog_restarts_an_idle_phone_that_stopped_answering(self):
        backend = FakeBackend("running", boot_polls=1)
        backend.healthy = lambda: False
        clock = Clock()
        l = Lifecycle(backend, clock=clock, idle_s=10_000, stop_s=3600, health_every_s=60)
        l.tick()  # first check: unhealthy once, no action yet
        clock.t += 61
        l.tick()  # second: restart
        until(lambda: "stop" in backend.calls and "start" in backend.calls, timeout=10)



class SecureScreens(unittest.TestCase):
    """Password and purchase screens are blank to screencap. The owner's live view falls back to the emulator's own picture; the agent never does."""

    @staticmethod
    def jpeg_color(res):
        import io
        from PIL import Image
        return Image.open(io.BytesIO(res.content)).convert("RGB").getpixel((10, 10))

    def app_with(self, phone):
        return TestClient(create_app(phone, lambda m: Spark(), settle=0, db=Database(":memory:"), data_dir=tempfile.mkdtemp()))

    def test_blank_detection(self):
        from PIL import Image
        from agent.device import is_blank
        self.assertTrue(is_blank(Image.new("RGB", (100, 200), (0, 0, 0))))
        self.assertFalse(is_blank(Image.new("RGB", (100, 200), (30, 30, 30))))
        secure = Image.new("RGB", (100, 200), (0, 0, 0))
        secure.paste((255, 255, 255), (0, 0, 100, 10))  # the status bar stays visible on a secure screen
        secure.paste((255, 255, 255), (30, 192, 70, 196))  # and so does the navigation pill
        self.assertTrue(is_blank(secure))
        secure.paste((90, 90, 90), (20, 80, 80, 120))  # but real content in the middle is not blank
        self.assertFalse(is_blank(secure))

    def test_the_live_view_shows_the_real_screen_when_android_blanks_it(self):
        from PIL import Image

        class Secure(Phone):
            def frame(self):
                return Image.new("RGB", (100, 200), (0, 0, 0))  # what screencap gives for a secure window

            def host_frame(self):
                return Image.new("RGB", (100, 200), (200, 30, 30))  # what the emulator draws

        c = self.app_with(Secure())
        r, g, b = self.jpeg_color(c.get("/api/device/screenshot"))
        self.assertGreater(r, 150)
        self.assertLess(g, 80)

    def test_a_normal_screen_does_not_use_the_slower_host_capture(self):
        from PIL import Image

        class Normal(Phone):
            host_calls = 0

            def frame(self):
                return Image.new("RGB", (100, 200), (30, 90, 200))

            def host_frame(self):
                Normal.host_calls += 1
                return Image.new("RGB", (100, 200), (200, 30, 30))

        c = self.app_with(Normal())
        self.assertGreater(self.jpeg_color(c.get("/api/device/screenshot"))[2], 150)
        self.assertEqual(Normal.host_calls, 0)

    def test_a_device_that_is_not_an_emulator_keeps_the_blank_frame(self):
        from PIL import Image

        class Real(Phone):
            def frame(self):
                return Image.new("RGB", (100, 200), (0, 0, 0))

            def host_frame(self):
                raise subprocess.CalledProcessError(1, "adb emu")

        res = self.app_with(Real()).get("/api/device/screenshot")
        self.assertEqual(res.status_code, 200)
        self.assertLess(sum(self.jpeg_color(res)), 20)  # still black (JPEG may not be exactly 0)

    def test_host_frame_reads_the_emulator_screenshot_file(self):
        from pathlib import Path
        from PIL import Image
        from agent.device import Device
        d = Device()
        d.use_u2 = False

        def fake_run(*args):
            assert args[:3] == ("emu", "screenrecord", "screenshot")
            Image.new("RGB", (10, 20), (1, 2, 3)).save(Path(args[3]) / "Screenshot_1.png")
            return b"OK"

        d._run = fake_run
        self.assertEqual(d.host_frame().getpixel((0, 0))[:3], (1, 2, 3))



class ChatResumesHandoff(unittest.TestCase):
    def test_saying_done_in_chat_resumes_a_handoff_and_the_task_finishes(self):
        spark = Spark(act(type="mobile_task", goal="sign in to the store", say="ok"),
                      act(type="resume_task", say="Continuing."), act(type="reply", text="signed in"))
        c, _, bot, app = setup(spark, Worker(act(type="handoff", reason="enter your password"), act(type="done", answer="signed in")))
        say(c, bot, "sign in")
        until(lambda: open_card(c, bot))
        say(c, bot, "my part is done, you continue")
        until(lambda: statuses(c, bot) == {"sign in to the store": "done"})
        card = next(m for m in msgs(c, bot) if m["kind"] == "action_item")
        self.assertEqual((card["meta"]["status"], card["meta"]["answer"]), ("answered", "Done"))
        self.assertIn("Cards waiting for the owner", spark.calls[1][0])  # the model was told a handoff was open
        self.assertIn("Continuing.", texts(c, bot, "assistant"))

    def test_chat_cannot_approve_a_confirmation(self):
        spark = Spark(act(type="mobile_task", goal="send the message", say="ok"), act(type="resume_task"))
        c, _, bot, _ = setup(spark, Worker(act(type="ask", question="Send the message?")))
        say(c, bot, "go")
        until(lambda: open_card(c, bot))
        say(c, bot, "yes do it")
        until(lambda: any("Nothing is waiting for you to finish" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(open_card(c, bot)["meta"]["status"], "open")  # still waiting for a real approval
        self.assertEqual(statuses(c, bot), {"send the message": "waiting"})

    def test_resume_with_nothing_waiting_says_so(self):
        c, _, bot, _ = setup(Spark(act(type="resume_task")))
        say(c, bot, "done")
        until(lambda: any("Nothing is waiting" in t for t in texts(c, bot, "assistant")))



class RetryLimit(unittest.TestCase):
    def test_a_failed_task_is_retried_once_and_then_the_bot_stops_and_asks(self):
        spark = Spark(act(type="mobile_task", goal="install the game", say="ok"),
                      act(type="mobile_task", goal="install the game, continue", say="Retrying."),   # the retry after the first failure
                      act(type="mobile_task", goal="install the game, again", say="Retrying again."),  # a second retry must be refused
                      act(type="reply", text="unused"))
        c, _, bot, _ = setup(spark, Worker(act(type="fail", reason="slow")), Worker(act(type="fail", reason="slow")))
        say(c, bot, "install it")
        until(lambda: any("stopping here instead of looping" in t for t in texts(c, bot, "assistant")), timeout=30)
        self.assertEqual(len(c.get(f"/api/bots/{bot}/jobs").json()), 2)  # the original and exactly one retry

    def test_the_owner_asking_again_gets_a_fresh_retry(self):
        db = Database(":memory:")
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")), db=db)
        app.state.orch._retries[bot] = 1
        say(c, bot, "try again please")
        until(lambda: "ok" in texts(c, bot, "assistant"))
        self.assertEqual(app.state.orch._retries[bot], 0)


class CrashyPhone(RealisticPhone):
    """Every tap 'opens the app' and the app crashes at once, as Clash of Clans does on an emulator."""
    def __init__(self, emulator=True):
        super().__init__()
        self.logged, self.emulator = [("com.android.vending", "11")], emulator  # one crash from before the task

    def tap(self, x, y):
        super().tap(x, y)
        self.logged.append(("com.supercell.clashofclans", str(1000 + len(self.logged))))

    def crashes(self):
        return list(self.logged)

    def is_emulator(self):
        return self.emulator


class AppCrashes(unittest.TestCase):
    JAVA = ("10-30 17:39:43.991  8593  8593 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "10-30 17:39:43.991  8593  8593 E AndroidRuntime: Process: com.supercell.clashofclans, PID: 8593\n")
    NATIVE = "pid: 4242, tid: 4300, name: UnityMain  >>> com.example.game <<<\n"

    def test_java_and_native_crashes_are_read_from_the_crash_log(self):
        self.assertEqual(parse_crashes(self.JAVA + self.NATIVE), [("com.supercell.clashofclans", "8593"), ("com.example.game", "4242")])
        self.assertEqual(parse_crashes("nothing here"), [])

    def test_the_worker_stops_after_repeated_crashes_with_an_honest_reason(self):
        phone = CrashyPhone()
        tap = {"type": "tap", "element": 0}
        model = Worker(*([tap] * 6))
        res = run_graph("open clash of clans", phone, model, settle=0)
        self.assertEqual(res.status, "failed")
        self.assertTrue(res.answer.startswith(CRASH_PREFIX))
        self.assertIn("com.supercell.clashofclans crashed 3 times", res.answer)
        self.assertIn("emulated phone", res.answer)
        self.assertEqual(len(phone.taps), 3)  # it did not keep trying
        self.assertNotIn("CRASHES", model.prompts[0])  # the crash from before the task is not counted
        self.assertIn("com.supercell.clashofclans crashed 1x", model.prompts[1])  # the model learns about it on the next step

    def test_a_real_phone_is_not_blamed_on_emulation(self):
        res = run_graph("open it", CrashyPhone(emulator=False), Worker(*([{"type": "tap", "element": 0}] * 6)), settle=0)
        self.assertNotIn("emulated", res.answer)

    def test_a_crashed_task_is_not_retried_and_the_owner_hears_why(self):
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="ok"),
                      act(type="reply", text="Clash of Clans crashes on this phone, so I can't open it. The website is an option."))
        c, _, bot, app = setup(spark, Worker(act(type="fail", reason=CRASH_PREFIX + "com.supercell.clashofclans crashed 3 times")))
        say(c, bot, "open clash of clans")
        until(lambda: any("crashes on this phone" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(app.state.orch._retries[bot], 1)  # no automatic retry is left
        self.assertIn("do NOT start a retry", spark.calls[-1][1][-1]["content"])
        self.assertTrue(any("crashed 3 times" in m for m in app.state.orch.db.memory_search(bot, "crashed")))  # remembered for later chats


if __name__ == "__main__":
    unittest.main()
