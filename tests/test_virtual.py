"""Phone 2 backend (Redroid in a Lima VM): the contract Lifecycle needs, without Lima or Docker."""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.lifecycle import Lifecycle
from agent.virtual import GAMES, SERIAL, LimaRedroidBackend


class FakeRun:
    """Stands in for subprocess.run: answers redroid.sh and adb commands from a small state machine."""
    def __init__(self, state="stopped", booted=True):
        self.state, self.booted, self.calls = state, booted, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = ""
        if cmd[0] == "/bin/bash":  # the redroid.sh script
            verb = cmd[2]
            if verb == "state":
                out = self.state
            elif verb == "pause":
                self.state = "paused"
            elif verb == "resume":
                self.state = "running"
            elif verb == "shutdown":
                self.state = "stopped"
        elif cmd[0] == "adb":
            if "getprop" in cmd:
                out = "1" if self.booted and self.state == "running" else ""
            elif "wm" in cmd and "size" in cmd:
                out = "Physical size: 720x1280" if self.state == "running" else ""
        elif cmd[0] == "limactl":
            out = "rd\n"
        return SimpleNamespace(stdout=out, returncode=0)


def backend(run, **kw):
    return LimaRedroidBackend(script=Path("/x/redroid.sh"), log=Path(tempfile.mkdtemp()) / "v.log", run=run, **kw)


class Backend(unittest.TestCase):
    def test_every_adb_call_names_the_games_phone(self):
        run = FakeRun("running")
        b = backend(run)
        b.booted(), b.healthy(), b.pause()
        adb = [c for c in run.calls if c[0] == "adb"]
        self.assertTrue(adb)
        self.assertTrue(all(c[1:3] == ["-s", SERIAL] for c in adb), adb)

    def test_status_follows_the_container_and_the_vm(self):
        for state in ("running", "paused", "stopped"):
            self.assertEqual(backend(FakeRun(state)).status(), state)
        self.assertEqual(backend(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("x", 1))).status(), "stopped")  # a hung script is "not usable"

    def test_pause_freezes_and_resume_waits_until_adb_answers(self):
        run = FakeRun("running")
        b = backend(run)
        b.pause()
        self.assertEqual(run.state, "paused")
        self.assertTrue(any(c[:2] == ["adb", "-s"] and "KEYCODE_SLEEP" in c for c in run.calls))  # screen off before the freeze
        self.assertTrue(b.resume())
        self.assertEqual(run.state, "running")
        self.assertTrue(any("KEYCODE_WAKEUP" in c for c in run.calls))

    def test_resume_says_false_when_the_container_is_gone(self):
        b = backend(lambda cmd, **kw: SimpleNamespace(stdout="stopped" if cmd[0] == "/bin/bash" and cmd[2] == "state" else "", returncode=0))
        self.assertFalse(b.resume())

    def test_stop_shuts_the_vm_down_too(self):
        run = FakeRun("running")
        backend(run).stop()
        self.assertEqual(run.state, "stopped")
        self.assertIn(["/bin/bash", "/x/redroid.sh", "shutdown"], run.calls)

    def test_start_runs_in_the_background_and_does_not_block(self):
        started = []
        b = LimaRedroidBackend(script=Path("/x/redroid.sh"), log=Path(tempfile.mkdtemp()) / "v.log", run=FakeRun(), popen=lambda cmd, **kw: started.append(cmd))
        b.start()
        self.assertEqual(started, [["/bin/bash", "/x/redroid.sh", "start"]])

    def test_booted_reconnects_adb_after_a_vm_restart(self):
        run = FakeRun("running", booted=False)
        b = backend(run)
        self.assertFalse(b.booted())
        self.assertIn(["adb", "-s", SERIAL, "connect", SERIAL], run.calls)

    def test_available_needs_lima_and_the_vm(self):
        self.assertTrue(LimaRedroidBackend.available(run=FakeRun()))
        self.assertFalse(LimaRedroidBackend.available(vm="other", run=FakeRun()))

    def test_lifecycle_sleeps_and_wakes_it_like_any_phone(self):
        run = FakeRun("running")
        t = [0.0]
        lc = Lifecycle(backend(run), clock=lambda: t[0], idle_s=60, stop_s=600)
        self.assertEqual(lc.power, "on")
        t[0] = 61
        lc.tick()
        self.assertEqual(lc.power, "sleeping")
        self.assertEqual(run.state, "paused")
        self.assertTrue(lc.ensure_healthy(5))
        self.assertEqual((lc.power, run.state), ("on", "running"))
        t[0] = 61 + 60
        lc.tick()
        t[0] = 61 + 60 + 601
        lc.tick()
        self.assertEqual((lc.power, run.state), ("off", "stopped"))  # a long sleep stops the container and the VM

    def test_the_shell_script_has_the_verbs_the_backend_uses(self):
        text = (Path(__file__).resolve().parent.parent / "scripts" / "virtual-android" / "redroid.sh").read_text()
        for verb in ("start)", "shutdown)", "state)", "pause)", "resume)"):
            self.assertIn(verb, text)
        self.assertEqual(GAMES, "games")


class OpenApp(unittest.TestCase):
    RESOLVE = ("priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
               "com.shatteredpixel.shatteredpixeldungeon/.android.AndroidLauncher\n")

    def device(self, monkey_fails: bool, resolve: str):
        from agent.device import Device
        d = Device("localhost:5565")
        d.calls = []

        def run(*args):
            d.calls.append(args)
            if "monkey" in args:
                if monkey_fails:
                    raise subprocess.CalledProcessError(251, "monkey")
                return b"** No activities found to run, monkey aborted.\n" if monkey_fails is None else b"Events injected: 1\n"
            if "resolve-activity" in args:
                return resolve.encode()
            return b"Starting: Intent { cmp=... }\n"
        d._run = run
        return d

    def test_the_launcher_activity_is_read_from_the_resolve_output(self):
        from agent.device import parse_launcher_activity
        self.assertEqual(parse_launcher_activity(self.RESOLVE), "com.shatteredpixel.shatteredpixeldungeon/.android.AndroidLauncher")
        self.assertEqual(parse_launcher_activity("No activity found"), "")

    def test_monkey_that_fails_falls_back_to_starting_the_launcher_activity(self):
        d = self.device(True, self.RESOLVE)
        d.open_app("com.shatteredpixel.shatteredpixeldungeon")
        self.assertIn(("shell", "am", "start", "-n", "com.shatteredpixel.shatteredpixeldungeon/.android.AndroidLauncher"), d.calls)

    def test_monkey_that_finds_nothing_also_falls_back(self):
        d = self.device(None, self.RESOLVE)
        d.open_app("com.shatteredpixel.shatteredpixeldungeon")
        self.assertTrue(any(c[:3] == ("shell", "am", "start") for c in d.calls))

    def test_monkey_that_works_is_all_that_runs(self):
        d = self.device(False, self.RESOLVE)
        d.open_app("com.shatteredpixel.shatteredpixeldungeon")
        self.assertFalse(any("resolve-activity" in c for c in d.calls))

    def test_an_app_with_no_launcher_activity_says_so(self):
        d = self.device(True, "No activity found")
        with self.assertRaises(ValueError) as cm:
            d.open_app("com.example.nothing")
        self.assertIn("no launcher activity", str(cm.exception))


class Animated(unittest.TestCase):
    def flicker(self):
        import io
        from PIL import Image
        n = [0]

        def shot():
            n[0] += 1
            buf = io.BytesIO()
            Image.new("L", (100, 200), 255 if n[0] % 2 else 0).save(buf, "PNG")  # never the same twice: a game that always animates
            return buf.getvalue()
        return SimpleNamespace(screenshot=shot)

    def test_a_screen_that_never_settles_stops_costing_the_full_wait_after_two_tries(self):
        from unittest import mock
        import agent.loop as loop
        dev = self.flicker()
        with mock.patch.object(loop, "ANIMATED_TIMEOUT", 0.1):
            took = []
            for _ in range(4):
                t0 = time.time()
                loop.settle(dev, 0, timeout=0.6)
                took.append(time.time() - t0)
        self.assertGreater(min(took[:2]), 0.5)  # the first two wait the whole time
        self.assertLess(max(took[2:]), 0.3)  # then the wait is cut

    def test_a_screen_that_settles_resets_the_shortcut(self):
        import agent.loop as loop
        dev = self.flicker()
        dev.unsettled = 5
        dev.screenshot = lambda: self.still
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L", (100, 200), 128).save(buf, "PNG")
        self.still = buf.getvalue()
        loop.settle(dev, 0, timeout=0.6)
        self.assertEqual(dev.unsettled, 0)


class GameScreens(unittest.TestCase):
    """An always-animated game never produces the same bytes twice, so "the screen did not change" has to be judged on the picture."""
    def run_taps(self, animated: bool, levels):
        import io
        from PIL import Image
        from unittest import mock
        import agent.loop as loop
        from agent.graph import run
        from test_chat import Worker
        from test_service import Phone

        class Game(Phone):
            def screenshot(self):
                buf = io.BytesIO()
                Image.new("RGB", (1080, 2400), (levels[self.i % len(levels)],) * 3).save(buf, "PNG")
                return buf.getvalue()
        dev = Game()
        dev.unsettled = 2 if animated else 0
        worker = Worker(*([{"type": "tap", "element": 0}] * 3 + [{"type": "done", "answer": "x"}]))
        with mock.patch.object(loop, "ANIMATED_TIMEOUT", 0.05):
            run("play", dev, worker, settle=0, max_steps=10)
        return worker.prompts

    def test_a_tap_that_changes_nothing_but_the_sprites_is_called_out_on_an_animated_screen(self):
        prompts = self.run_taps(True, [100, 103])  # frames differ by 3 grey levels: not settled, but the same screen
        self.assertIn("has not changed", prompts[2])

    def test_an_ordinary_app_keeps_exact_comparison(self):
        prompts = self.run_taps(False, [100, 101])  # a 1-level flicker settles at once, so the exact comparison stays in force: the bytes differ, the screen "changed"
        self.assertNotIn("has not changed", prompts[2])

    def test_a_real_change_on_an_animated_screen_is_not_called_unchanged(self):
        prompts = self.run_taps(True, [20, 220])
        self.assertNotIn("has not changed", prompts[2])


if __name__ == "__main__":
    unittest.main()
