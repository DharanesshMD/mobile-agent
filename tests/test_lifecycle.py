import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from agent.db import Database
from agent.lifecycle import EmulatorBackend, Lifecycle
from agent.service import create_app
from test_chat import Spark, Worker, act, msgs, say, texts, until
from test_service import Phone


class FakeBackend:
    def __init__(self, status="running", boot_polls=2):
        self.state, self.calls, self.boot_polls, self.fail_start, self.resume_ok = status, [], boot_polls, False, True

    def status(self):
        return self.state

    def booted(self):
        self.boot_polls -= 1
        return self.boot_polls <= 0

    def pause(self):
        self.calls.append("pause")
        self.state = "paused"

    def resume(self):
        self.calls.append("resume")
        if self.resume_ok:
            self.state = "running"
        return self.resume_ok

    def kick_network(self):
        self.calls.append("kick_network")

    def stop(self):
        self.calls.append("stop")
        self.state = "stopped"

    def start(self):
        if self.fail_start:
            raise OSError("no emulator")
        self.calls.append("start")
        self.state = "running"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def life(status="running", **kw):
    clock, backend = Clock(), FakeBackend(status)
    return Lifecycle(backend, clock=clock, idle_s=300, stop_s=3600, **kw), backend, clock


def settled(l, power):
    until(lambda: l.power == power, timeout=10)


class IdlePolicy(unittest.TestCase):
    def test_initial_state_follows_the_backend(self):
        self.assertEqual([life(s)[0].power for s in ("running", "paused", "stopped")], ["on", "sleeping", "off"])

    def test_sleeps_after_the_idle_time_and_stops_after_the_long_sleep(self):
        l, b, clock = life()
        clock.t += 299
        l.tick()
        self.assertEqual(l.power, "on")
        self.assertEqual(l.info()["sleeps_in"], 1)
        clock.t += 2
        l.tick()
        self.assertEqual((l.power, b.calls), ("sleeping", ["pause"]))
        clock.t += 3599
        l.tick()
        self.assertEqual(l.power, "sleeping")
        clock.t += 2
        l.tick()
        self.assertEqual((l.power, b.calls), ("off", ["pause", "stop"]))

    def test_it_notices_when_the_emulator_is_stopped_or_started_from_outside(self):
        l, b, _ = life()
        b.state = "stopped"
        l.tick()
        self.assertEqual((l.power, l.info()["detail"]), ("off", "The phone stopped unexpectedly"))
        b.state = "running"
        l.tick()
        self.assertEqual(l.power, "on")

    def test_activity_and_busy_work_keep_it_awake(self):
        busy = [True]
        l, b, clock = life(busy=lambda: busy[0])
        clock.t += 10_000
        l.tick()
        self.assertEqual(l.power, "on")  # a task is running
        self.assertNotIn("sleeps_in", l.info())
        busy[0] = False
        l.touch()
        clock.t += 200
        l.tick()
        self.assertEqual(l.power, "on")
        clock.t += 101
        l.tick()
        self.assertEqual(l.power, "sleeping")

    def test_sleep_now_refuses_while_busy(self):
        l, _, _ = life(busy=lambda: True)
        self.assertFalse(l.sleep())
        self.assertEqual(l.power, "on")


class Waking(unittest.TestCase):
    def test_a_paused_phone_that_is_still_online_is_not_reconnected(self):
        l, b, clock = life("paused")
        b.network_ok = lambda: True
        clock.t += 400
        l.wake()
        settled(l, "on")
        self.assertEqual(b.calls, ["resume"])  # nothing to repair: no Wi-Fi toggle, no "Reconnecting" wait

    def test_a_stale_connection_is_still_repaired(self):
        l, b, clock = life("paused")
        b.network_ok = lambda: False
        clock.t += 400
        l.wake()
        settled(l, "on")
        self.assertEqual(b.calls, ["resume", "kick_network"])

    def test_the_progress_estimate_for_a_resume_is_seconds_not_the_boot_minute(self):
        l, b, clock = life("paused")
        l.power, l.boot_started, l.waking_cold = "waking", clock(), False  # a resume in progress
        self.assertEqual(l.info()["expected"], round(l.last_resume_s))
        self.assertLess(l.info()["expected"], 15)
        l.waking_cold = True  # a boot keeps the long estimate
        self.assertEqual(l.info()["expected"], round(l.last_boot_s))

    def test_resume_from_sleep_is_quick_and_reconnects_after_a_long_nap(self):
        ready = []
        l, b, clock = life("paused", on_ready=lambda cold: ready.append(cold))
        clock.t += 120
        self.assertEqual(l.wake(), "waking")
        settled(l, "on")
        self.assertEqual((b.calls, ready), (["resume", "kick_network"], [False]))

    def test_a_phone_that_does_not_come_back_from_sleep_is_restarted(self):
        ready = []
        l, b, clock = life("paused", on_ready=lambda cold: ready.append(cold))
        b.resume_ok = False
        clock.t += 200
        l.wake()
        settled(l, "on")
        self.assertEqual((b.calls, ready), (["resume", "stop", "start"], [True]))

    def test_a_sleeping_phone_whose_process_vanished_boots_instead_of_resuming(self):
        l, b, _ = life("paused")
        b.state = "stopped"
        l.wake()
        settled(l, "on")
        self.assertEqual(b.calls, ["start"])

    def test_a_short_nap_skips_the_network_kick(self):
        l, b, clock = life("paused")
        clock.t += 30
        l.wake()
        settled(l, "on")
        self.assertEqual(b.calls, ["resume"])

    def test_cold_boot_waits_for_android_then_signals_cold(self):
        ready = []
        l, b, _ = life("stopped", on_ready=lambda cold: ready.append(cold))
        l.wake()
        until(lambda: "start" in b.calls)
        self.assertIn(l.power, ("waking", "on"))
        settled(l, "on")
        self.assertEqual(ready, [True])
        self.assertTrue(l.ensure_awake(1))

    def test_wake_is_idempotent_and_a_failed_start_leaves_a_retryable_state(self):
        l, b, _ = life("stopped")
        b.fail_start = True
        l.wake()
        l.wake()
        settled(l, "off")
        self.assertIn("Could not wake the phone", l.info()["detail"])
        b.fail_start = False
        l.wake()
        settled(l, "on")

    def test_ensure_awake_blocks_until_ready_or_times_out(self):
        l, b, _ = life("stopped")
        b.boot_polls = 10**9  # never finishes booting
        self.assertFalse(l.ensure_awake(0.3))
        self.assertEqual(l.power, "waking")
        self.assertIn("elapsed", l.info())


class Emulator(unittest.TestCase):
    def test_status_reads_the_process_state(self):
        e = EmulatorBackend(["adb"])
        with mock.patch.object(e, "pids", return_value=[]):
            self.assertEqual(e.status(), "stopped")
        with mock.patch.object(e, "pids", return_value=[42]), mock.patch("subprocess.run") as run:
            run.return_value.stdout = "TN\n"
            self.assertEqual(e.status(), "paused")
            run.return_value.stdout = "RN\n"
            self.assertEqual(e.status(), "running")

    def test_pids_match_the_emulator_process_but_never_a_shell_that_only_mentions_it(self):
        import subprocess
        e = EmulatorBackend(["adb"], avd="tstavd9")
        real = subprocess.Popen(["bash", "-c", 'exec -a "qemu-system-fake -avd tstavd9 -no-window" sleep 8'])
        decoy = subprocess.Popen(["bash", "-c", "sleep 8 # qemu-system-fake -avd tstavd9"])
        try:
            time.sleep(0.5)
            self.assertEqual(e.pids(), [real.pid])
        finally:
            real.kill()
            decoy.kill()
            real.wait()
            decoy.wait()

    def test_pause_sends_sleep_and_sigstop_and_resume_sends_sigcont(self):
        import signal
        e = EmulatorBackend(["adb"])
        sent = []
        with mock.patch.object(e, "pids", return_value=[7]), mock.patch.object(e, "netsim_pids", return_value=[9]), mock.patch.object(e, "_adb", side_effect=lambda *a, **k: "Physical size: 720x1600" if "wm" in a else "1"), \
                mock.patch("os.kill", lambda p, s: sent.append((p, s))), mock.patch("time.sleep"):
            e.pause()
            self.assertTrue(e.resume())
        # the emulator and its network helper freeze together, and the helper wakes first
        self.assertEqual(sent, [(7, signal.SIGSTOP), (9, signal.SIGSTOP), (9, signal.SIGCONT), (7, signal.SIGCONT)])

    def test_resume_reports_failure_when_the_emulator_is_gone_or_adb_never_answers(self):
        e = EmulatorBackend(["adb"])
        with mock.patch.object(e, "pids", return_value=[]), mock.patch.object(e, "netsim_pids", return_value=[]), mock.patch("time.sleep"):
            self.assertFalse(e.resume())
        with mock.patch.object(e, "pids", return_value=[7]), mock.patch.object(e, "netsim_pids", return_value=[]), mock.patch.object(e, "_adb", return_value=""), \
                mock.patch("os.kill"), mock.patch("time.sleep"):
            self.assertFalse(e.resume())


def phone_app(l, spark=None, workers=()):
    queue = list(workers)
    app = create_app(Phone(), lambda m: queue.pop(0) if m == "muse-wonder-1.0" else (spark or Spark()), settle=0, db=Database(":memory:"),
                     data_dir=tempfile.mkdtemp(), lifecycle=l)
    c = TestClient(app)
    return c, app, c.get("/api/bots").json()[0]["id"]


class Api(unittest.TestCase):
    def test_state_reports_power_and_a_sleeping_phone_refuses_frames_and_input(self):
        l, b, _ = life("paused")
        c, _, _ = phone_app(l)
        s = c.get("/api/mobile/state").json()
        self.assertEqual((s["phone"]["power"], s["orientation"]), ("sleeping", "portrait"))
        self.assertEqual(c.get("/api/device/screenshot").status_code, 409)
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 409)
        self.assertIn("asleep", c.post("/api/device/key", json={"key": "home"}).json()["detail"])

    def test_wake_then_use_and_watching_does_not_count_as_activity(self):
        l, b, clock = life("paused")
        c, _, _ = phone_app(l)
        self.assertIn(c.post("/api/mobile/wake").json()["phone"]["power"], ("waking", "on"))  # resuming is fast: it may already be done
        settled(l, "on")
        clock.t += 200
        self.assertEqual(c.get("/api/device/screenshot").status_code, 200)
        self.assertEqual(l.last_activity, 1000.0)  # a frame request is not owner activity
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 200)
        self.assertEqual(l.last_activity, clock.t)  # a tap is

    def test_sleep_now_works_when_idle_and_is_refused_when_a_task_runs(self):
        l, b, _ = life()
        c, app, bot = phone_app(l)
        self.assertEqual(c.post("/api/mobile/sleep").json()["phone"]["power"], "sleeping")
        self.assertEqual(c.post("/api/mobile/sleep").status_code, 409)
        l2, _, _ = life()
        c2, app2, _ = phone_app(l2)
        app2.state.jobs.held = True  # the owner has control
        self.assertEqual(c2.post("/api/mobile/sleep").status_code, 409)

    def test_taking_control_wakes_the_phone(self):
        l, b, _ = life("paused")
        c, _, _ = phone_app(l)
        c.post("/api/mobile/control", json={"action": "take"})
        settled(l, "on")
        self.assertIn("resume", b.calls)

    def test_a_phone_task_wakes_a_stopped_phone_first_and_then_runs(self):
        l, b, _ = life("stopped")
        spark = Spark(act(type="mobile_task", goal="check battery", say="On it."), act(type="reply", text="Battery is fine."))
        c, app, bot = phone_app(l, spark, [Worker(act(type="done", answer="100%"))])
        say(c, bot, "check the battery")
        until(lambda: "Battery is fine." in texts(c, bot, "assistant"), timeout=30)
        self.assertEqual(b.calls, ["start"])
        job = app.state.db.jobs(bot, 5)[0]
        self.assertEqual((job["status"], job["result"]), ("done", "100%"))
        self.assertEqual(l.power, "on")

    def test_a_task_fails_cleanly_when_the_phone_never_wakes(self):
        l, b, _ = life("stopped")
        b.fail_start = True
        spark = Spark(act(type="mobile_task", goal="g", say="ok"), act(type="reply", text="Sorry, the phone would not start."))
        c, app, bot = phone_app(l, spark)
        with mock.patch.object(l, "ensure_awake", lambda timeout=300: False):
            say(c, bot, "go")
            until(lambda: "Sorry, the phone would not start." in texts(c, bot, "assistant"), timeout=30)
        job = app.state.db.jobs(bot, 5)[0]
        self.assertEqual((job["status"], job["result"]), ("failed", "The phone did not wake up in time"))


if __name__ == "__main__":
    unittest.main()
