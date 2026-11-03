"""Every phone is its own lane (emulator, Phone 2, real phones), and Phone 2 has its own bot, power and live view."""
import io
import json
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from agent.db import Database
from agent.lifecycle import Lifecycle
from agent.service import create_app, default_serial
from test_chat import Spark, act, say, texts, until
from test_lifecycle import Clock, FakeBackend
from test_service import Phone, png


class GamesDevice(Phone):
    """Phone 2: same fake screen as the emulator, with a screenshot that looks different and a record of what it was asked."""
    def screenshot(self):
        return png(150 + self.i % 40)

    def frame(self):
        return Image.open(io.BytesIO(self.screenshot()))

    def is_emulator(self):
        return False


class Routed:
    """A worker model that answers by the goal it was given, so jobs on different lanes can run at the same time in any order."""
    def __init__(self, scripts: dict[str, list[dict]], gates: dict[str, threading.Event] | None = None):
        self.scripts = {k: [json.dumps({"thought": "t", "action": a}) for a in v] for k, v in scripts.items()}
        self.gates, self.started, self.tokens = gates or {}, [], {"prompt": 0, "completion": 0}

    def complete(self, system, prompt, image=None, max_tokens=0):
        for key, replies in self.scripts.items():
            if key in prompt:
                if key not in self.started:
                    self.started.append(key)
                if key in self.gates:
                    self.gates[key].wait(30)  # the test decides when this lane's job may go on
                return replies.pop(0)
        raise AssertionError("no script for this goal: " + prompt[:80])


def make(spark, worker, with_games=True):
    emulator, games = Phone(), GamesDevice()
    clock, backend = Clock(), FakeBackend("running")
    lc = Lifecycle(backend, clock=clock, idle_s=300, stop_s=3600)
    app = create_app(emulator, lambda name: spark if name in ("muse-spark-1.3", "muse-voice-transcribe-1.0") else worker, settle=0, db=Database(":memory:"),
                     data_dir=tempfile.mkdtemp(), virtuals={"games": (games, lc)} if with_games else None)
    c = TestClient(app)
    bots = {b["kind"]: b["id"] for b in c.get("/api/bots").json()}
    return c, app, emulator, games, lc, backend, bots


def job_status(c, bot):
    js = c.get(f"/api/bots/{bot}/jobs").json()
    return js[0]["status"] if js else None


class GamesBot(unittest.TestCase):
    def test_the_games_phone_gets_its_own_honest_bot_once(self):
        c, app, *_ = make(Spark(act(type="reply", text="ok")), Routed({}))
        games = [b for b in c.get("/api/bots").json() if b["kind"] == "virtual"]
        self.assertEqual([(b["name"], b["phone_id"]) for b in games], [("Phone 2", "games")])
        intro = c.get(f"/api/bots/{games[0]['id']}/state").json()["messages"][0]["content"]
        for honest in ("no Google Play", "APK", "software-rendered", "refuse to run", "another phone"):
            self.assertIn(honest, intro)
        from agent.master import ensure_games_bot
        self.assertEqual(ensure_games_bot(app.state.db, None)["id"], games[0]["id"])  # created once, reused after

    def test_no_games_bot_when_the_games_phone_does_not_exist_here(self):
        c, *_ = make(Spark(act(type="reply", text="ok")), Routed({}), with_games=False)
        self.assertFalse([b for b in c.get("/api/bots").json() if b["kind"] == "virtual"])

    def test_its_tasks_run_on_the_games_phone_and_never_on_the_emulator(self):
        worker = Routed({"GAMEJOB": [{"type": "tap", "x": 10, "y": 10}, {"type": "done", "answer": "played"}]})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        app.state.jobs.submit(bots["virtual"], "GAMEJOB play a level")
        until(lambda: job_status(c, bots["virtual"]) == "done", timeout=30)
        self.assertTrue(games.taps)
        self.assertEqual(emulator.taps, [])
        self.assertEqual(c.get(f"/api/bots/{bots['virtual']}/jobs").json()[0]["target"], "virtual:games")

    def test_other_bots_never_use_the_games_phone(self):
        worker = Routed({"EMUJOB": [{"type": "tap", "x": 10, "y": 10}, {"type": "done", "answer": "ok"}]})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        app.state.jobs.submit(bots["bot"], "EMUJOB open settings")
        until(lambda: job_status(c, bots["bot"]) == "done", timeout=30)
        self.assertTrue(emulator.taps)
        self.assertEqual(games.taps, [])

    def test_the_games_bot_context_is_honest_and_other_bots_know_it_exists(self):
        spark = Spark(act(type="reply", text="ok"), act(type="reply", text="ok"))
        c, app, emulator, games, lc, backend, bots = make(spark, Routed({}))
        say(c, bots["virtual"], "hi")
        until(lambda: "ok" in texts(c, bots["virtual"], "assistant"))
        self.assertIn("PHONE 2", spark.calls[0][0])
        self.assertIn("no UI tree", spark.calls[0][0])
        self.assertIn("never on Phone", spark.calls[0][0])
        say(c, bots["bot"], "hi")
        until(lambda: "ok" in texts(c, bots["bot"], "assistant"))
        self.assertIn("Phone 2", spark.calls[1][0])
        self.assertIn("has its own bot", spark.calls[1][0])

    def test_a_sleeping_games_phone_wakes_for_its_task(self):
        worker = Routed({"GAMEJOB": [{"type": "done", "answer": "ok"}]})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        lc.sleep()
        self.assertEqual(lc.power, "sleeping")
        app.state.jobs.submit(bots["virtual"], "GAMEJOB")
        until(lambda: job_status(c, bots["virtual"]) == "done", timeout=30)
        self.assertIn("resume", backend.calls)
        self.assertEqual(lc.power, "on")


class Lanes(unittest.TestCase):
    def test_a_long_game_task_does_not_block_the_emulator(self):
        gate = threading.Event()
        worker = Routed({"GAMEJOB": [{"type": "done", "answer": "g"}], "EMUJOB": [{"type": "done", "answer": "e"}]}, gates={"GAMEJOB": gate})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        jobs.submit(bots["virtual"], "GAMEJOB long")
        until(lambda: "GAMEJOB" in worker.started)
        jobs.submit(bots["bot"], "EMUJOB quick")
        until(lambda: job_status(c, bots["bot"]) == "done", timeout=30)  # finished while the game job is still waiting at its gate
        self.assertEqual(job_status(c, bots["virtual"]), "running")
        self.assertTrue(jobs.busy("virtual:games"))
        self.assertFalse(jobs.busy(""))
        gate.set()
        until(lambda: job_status(c, bots["virtual"]) == "done", timeout=30)

    def test_one_job_at_a_time_inside_a_lane(self):
        gate = threading.Event()
        worker = Routed({"FIRST": [{"type": "done", "answer": "1"}], "SECOND": [{"type": "done", "answer": "2"}]}, gates={"FIRST": gate})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        jobs.submit(bots["virtual"], "FIRST")
        until(lambda: "FIRST" in worker.started)
        second = jobs.submit(bots["virtual"], "SECOND")
        time.sleep(1.5)
        self.assertNotIn("SECOND", worker.started)  # it waits for its own lane
        self.assertEqual(jobs.state("virtual:games")["queued"], 1)
        self.assertTrue(any("Queued (1)" in m["content"] for m in c.get(f"/api/bots/{bots['virtual']}/state").json()["messages"] if m["kind"] == "status"))
        gate.set()
        until(lambda: c.get(f"/api/bots/{bots['virtual']}/jobs").json()[0]["status"] == "done" and "SECOND" in worker.started, timeout=30)

    def test_a_job_can_wait_for_a_job_in_another_lane(self):
        gate = threading.Event()
        worker = Routed({"GAMEJOB": [{"type": "done", "answer": "g"}], "EMUJOB": [{"type": "done", "answer": "e"}]}, gates={"GAMEJOB": gate})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        first = jobs.submit(bots["virtual"], "GAMEJOB")
        until(lambda: "GAMEJOB" in worker.started)
        jobs.submit(bots["bot"], "EMUJOB", after=first["id"])
        time.sleep(1.5)
        self.assertNotIn("EMUJOB", worker.started)  # the dependency crosses lanes
        gate.set()
        until(lambda: job_status(c, bots["bot"]) == "done", timeout=30)

    def test_a_failed_job_skips_the_dependent_job_in_another_lane(self):
        worker = Routed({"GAMEJOB": [{"type": "fail", "reason": "crashed"}], "EMUJOB": [{"type": "done", "answer": "e"}]})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok"), act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        first = jobs.submit(bots["virtual"], "GAMEJOB")
        jobs.submit(bots["bot"], "EMUJOB", after=first["id"])
        until(lambda: job_status(c, bots["bot"]) == "skipped", timeout=30)

    def test_the_shared_phone_panel_only_shows_the_emulator_lane(self):
        gate = threading.Event()
        worker = Routed({"GAMEJOB": [{"type": "done", "answer": "g"}], "GAME2": [{"type": "done", "answer": "g"}]}, gates={"GAMEJOB": gate})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        jobs.submit(bots["virtual"], "GAMEJOB")
        until(lambda: "GAMEJOB" in worker.started)
        jobs.submit(bots["virtual"], "GAME2")
        shared = c.get("/api/mobile/state").json()
        self.assertEqual((shared["state"], shared["queued"], shared["job"]), ("ready", 0, None))
        self.assertEqual(shared["lanes"]["virtual:games"], {"power": "on", "busy": True})
        mine = c.get("/api/mobile/state", params={"phone": "games"}).json()
        self.assertEqual((mine["state"], mine["queued"]), ("working", 1))
        self.assertEqual(mine["job"]["target"], "virtual:games")
        gate.set()
        until(lambda: not jobs.busy("virtual:games"), timeout=30)

    def test_an_emulator_job_does_not_make_the_games_phone_look_busy(self):
        gate = threading.Event()
        worker = Routed({"EMUJOB": [{"type": "done", "answer": "e"}]}, gates={"EMUJOB": gate})
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), worker)
        jobs = app.state.jobs
        jobs.submit(bots["bot"], "EMUJOB")
        until(lambda: "EMUJOB" in worker.started)
        self.assertTrue(jobs.busy(""))
        self.assertFalse(jobs.busy("virtual:games"))
        self.assertTrue(jobs.busy())
        self.assertEqual(c.get("/api/mobile/state", params={"phone": "games"}).json()["state"], "ready")
        gate.set()
        until(lambda: job_status(c, bots["bot"]) == "done", timeout=30)


class LiveView(unittest.TestCase):
    def test_the_games_phone_has_its_own_live_view_and_power_controls(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        shot = c.get("/api/device/screenshot", params={"phone": "games"})
        self.assertEqual((shot.status_code, shot.headers["content-type"]), (200, "image/jpeg"))
        self.assertNotEqual(shot.content, c.get("/api/device/screenshot").content)  # a different phone, a different picture
        # sleep and wake Phone 2 only
        self.assertEqual(c.post("/api/mobile/sleep", params={"phone": "games"}).json()["phone"]["power"], "sleeping")
        self.assertEqual(c.get("/api/device/screenshot", params={"phone": "games"}).status_code, 409)  # watching never wakes it
        self.assertEqual(c.get("/api/device/screenshot").status_code, 200)  # the emulator is untouched
        c.post("/api/mobile/wake", params={"phone": "games"})
        until(lambda: lc.power == "on", timeout=10)
        self.assertEqual(c.get("/api/mobile/state", params={"phone": "games"}).json()["phone"]["power"], "on")

    def test_an_unknown_phone_is_a_404(self):
        c, *_ = make(Spark(act(type="reply", text="ok")), Routed({}))
        for call in (lambda: c.get("/api/device/screenshot", params={"phone": "nope"}), lambda: c.get("/api/mobile/state", params={"phone": "nope"}),
                     lambda: c.post("/api/mobile/wake", params={"phone": "nope"})):
            self.assertEqual(call().status_code, 404)

    def test_the_bot_state_carries_its_own_phone(self):
        c, app, emulator, games, lc, backend, bots = make(Spark(act(type="reply", text="ok")), Routed({}))
        lc.sleep()
        self.assertEqual(c.get(f"/api/bots/{bots['virtual']}/state").json()["mobile"]["phone"]["power"], "sleeping")
        self.assertEqual(c.get(f"/api/bots/{bots['bot']}/state").json()["mobile"]["phone"]["power"], "on")  # the emulator's own panel


class Serial(unittest.TestCase):
    def test_every_phone_is_named_so_a_second_adb_device_cannot_break_the_first(self):
        self.assertEqual(default_serial(None, True), "emulator-5554")
        self.assertEqual(default_serial("ABC123", True), "ABC123")
        self.assertIsNone(default_serial(None, False))  # a lone USB phone stays the only device

    def test_device_and_backend_commands_carry_the_serial(self):
        from agent.device import Device
        from agent.lifecycle import EmulatorBackend
        d = Device("emulator-5554")
        self.assertEqual(d.base, ["adb", "-s", "emulator-5554"])
        self.assertEqual(EmulatorBackend(d.base).adb[:2], ["adb", "-s"])


if __name__ == "__main__":
    unittest.main()
