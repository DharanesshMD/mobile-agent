import io
import json
import unittest

from PIL import Image

from agent.device import Element
from agent.graph import run
from agent.loop import parse_action


def png(color: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), (color, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


class FakeDevice:
    def __init__(self, shots):
        self.shots, self.i, self.taps, self.keys = shots, 0, [], []

    def size(self):
        return 1080, 2400

    def screenshot(self):
        return self.shots[min(self.i, len(self.shots) - 1)]

    def elements(self):
        return [Element(0, "Battery", (540, 1800), True)]

    def tap(self, x, y):
        self.taps.append((x, y))
        self.i += 1

    def key(self, name):
        self.keys.append(name)
        self.i += 1

    def type_text(self, text):
        self.typed = getattr(self, "typed", []) + [text]
        self.i += 1


class FakeModel:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.tokens = {"prompt": 0, "completion": 0}

    def complete(self, system, prompt, image=None, max_tokens=0):
        self.calls.append(prompt)
        return self.replies.pop(0)


def reply(**action):
    return json.dumps({"thought": "t", "action": action})


class LoopTests(unittest.TestCase):
    def test_parse_action_extracts_json_from_chatter(self):
        self.assertEqual(parse_action('Sure! ```json\n{"action":{"type":"wait"}}\n```')["action"]["type"], "wait")
        with self.assertRaises(ValueError):
            parse_action("no json here")
        with self.assertRaises(ValueError):
            parse_action('{"action":{}}')

    def test_tap_element_then_done(self):
        dev = FakeDevice([png(1), png(2), png(2)])
        model = FakeModel([reply(type="tap", element=0), reply(type="done", answer="100%")])
        res = run("battery", dev, model, settle=0)
        self.assertEqual((res.status, res.answer, res.steps), ("done", "100%", 2))
        self.assertEqual(dev.taps, [(540, 1800)])

    def test_xy_tap_is_scaled_to_device_pixels(self):
        dev = FakeDevice([png(1), png(2), png(2)])
        model = FakeModel([reply(type="tap", x=100, y=200), reply(type="done", answer="ok")])
        run("g", dev, model, settle=0, image_width=540)
        self.assertEqual(dev.taps, [(200, 400)])  # 1080 / 540 = 2x
        dev2 = FakeDevice([png(1), png(2), png(2)])
        run("g", dev2, FakeModel([reply(type="tap", x=100, y=200), reply(type="done", answer="ok")]), settle=0)
        self.assertEqual(dev2.taps, [(300, 600)])  # default width 360: 1080 / 360 = 3x

    def test_invalid_json_is_retried_once_then_fails(self):
        res = run("g", FakeDevice([png(1)]), FakeModel(["nope", "still nope"]), settle=0)
        self.assertEqual(res.status, "failed")
        ok = run("g", FakeDevice([png(1)]), FakeModel(["nope", reply(type="done", answer="x")]), settle=0)
        self.assertEqual(ok.status, "done")

    def test_oscillating_swipes_trigger_warning(self):
        swipes = [reply(type="swipe", direction=d) for d in ("up", "down", "up", "down")]
        model = FakeModel(swipes + [reply(type="done", answer="x")])
        run("g", FakeDevice([png(i) for i in range(1, 8)]), model, settle=0)
        self.assertNotIn("keep scrolling", model.calls[3])
        self.assertIn("keep scrolling", model.calls[4])

    def test_ask_resumes_when_approved_and_ends_when_denied(self):
        ask = reply(type="ask", question="Send it?")
        ok = run("g", FakeDevice([png(1), png(2)]), FakeModel([ask, reply(type="done", answer="sent")]), settle=0,
                 handler=lambda p: {"approved": True})
        self.assertEqual((ok.status, ok.answer), ("done", "sent"))
        no = run("g", FakeDevice([png(1)]), FakeModel([ask]), settle=0, handler=lambda p: {"approved": False})
        self.assertEqual(no.status, "denied")

    def test_handoff_waits_for_owner_then_continues(self):
        seen = []
        model = FakeModel([reply(type="handoff", reason="Sign in to Google"), reply(type="done", answer="x")])
        res = run("g", FakeDevice([png(1), png(2)]), model, settle=0, handler=lambda p: seen.append(p) or {"done": True})
        self.assertEqual(res.status, "done")
        self.assertEqual(seen, [{"kind": "handoff", "text": "Sign in to Google"}])
        self.assertIn("owner handled", model.calls[1])

    def test_otp_is_typed_on_device_but_never_reaches_model_or_history(self):
        dev = FakeDevice([png(1), png(2), png(3)])
        model = FakeModel([reply(type="request_otp", app="Gmail"), reply(type="done", answer="in")])
        events = []
        res = run("g", dev, model, settle=0, emit=events.append, handler=lambda p: {"code": "482913"})
        self.assertEqual(res.status, "done")
        self.assertEqual(dev.typed, ["482913"])
        blob = json.dumps([model.calls, res.history, events])
        self.assertNotIn("482913", blob)
        self.assertIn("one-time code entered", model.calls[1])

    def test_cancel_stops_before_next_step(self):
        import threading
        cancel = threading.Event()
        cancel.set()
        res = run("g", FakeDevice([png(1)]), FakeModel([]), settle=0, cancel=cancel)
        self.assertEqual(res.status, "cancelled")

    def test_ask_stops_for_approval(self):
        res = run("g", FakeDevice([png(1)]), FakeModel([reply(type="ask", question="Send it?")]), settle=0)
        self.assertEqual((res.status, res.answer), ("needs_approval", "Send it?"))

    def test_bad_element_is_reported_and_loop_continues(self):
        model = FakeModel([reply(type="tap", element=99), reply(type="done", answer="x")])
        res = run("g", FakeDevice([png(1)]), model, settle=0)
        self.assertEqual(res.status, "done")
        self.assertIn("error", model.calls[1])

    def test_unchanged_screen_triggers_warning_and_step_budget(self):
        model = FakeModel([reply(type="key", key="back")] * 3)
        dev = FakeDevice([png(5)])
        res = run("g", dev, model, max_steps=3, settle=0)
        self.assertEqual(res.status, "max_steps")
        self.assertIn("has not changed", model.calls[2])

    def test_waiting_does_not_use_the_step_budget_and_is_not_called_stuck(self):
        # a download: several waits on a screen that barely changes, then done. Four waits would have ended a 3-step budget before.
        model = FakeModel([reply(type="wait", seconds=1)] * 4 + [reply(type="done", answer="installed")])
        res = run("g", FakeDevice([png(5)]), model, max_steps=3, settle=0)
        self.assertEqual((res.status, res.answer), ("done", "installed"))
        self.assertNotIn("has not changed", model.calls[4])  # an unchanged screen while waiting is expected
        self.assertIn("waited 1 s", res.history[0])

    def test_a_wait_can_be_stopped_at_once(self):
        import threading, time
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t = time.time()
        res = run("g", FakeDevice([png(5)]), FakeModel([reply(type="wait", seconds=60)]), settle=0, cancel=cancel)
        self.assertEqual(res.status, "cancelled")
        self.assertLess(time.time() - t, 5)

    def test_the_time_budget_ends_a_task_that_never_finishes(self):
        res = run("g", FakeDevice([png(5)]), FakeModel([reply(type="done", answer="x")]), max_minutes=0, settle=0)
        self.assertEqual(res.status, "max_steps")
        self.assertIn("time budget exhausted", res.answer)

    def test_the_time_the_owner_takes_does_not_count_against_the_budget(self):
        import time
        model = FakeModel([reply(type="handoff", reason="sign in"), reply(type="done", answer="ok")])

        def slow_owner(payload):
            time.sleep(1.5)  # longer than the whole budget below
            return {"done": True}

        res = run("g", FakeDevice([png(1), png(2)]), model, max_minutes=0.025, settle=0, handler=slow_owner)
        self.assertEqual(res.status, "done")


if __name__ == "__main__":
    unittest.main()


class ImageUse(unittest.TestCase):
    class Spy(FakeModel):
        def __init__(self, replies):
            super().__init__(replies)
            self.images = []

        def complete(self, system, prompt, image=None, max_tokens=0):
            self.images.append(image is not None)
            return super().complete(system, prompt, image, max_tokens)

    class Rich(FakeDevice):
        def elements(self):
            return [Element(i, f"row {i}", (540, 100 + i * 50), True) for i in range(6)]

    def run_three(self, mode, replies):
        model = self.Spy(replies)
        res = run("g", self.Rich([png(i) for i in range(1, 10)]), model, settle=0, image_mode=mode)
        return res, model

    def test_adaptive_sends_an_image_first_then_text_only_then_verifies_before_done(self):
        replies = [reply(type="tap", element=0), reply(type="tap", element=1), reply(type="done", answer="x"), reply(type="done", answer="x")]
        res, model = self.run_three("adaptive", replies)
        self.assertEqual(res.status, "done")
        # step 1 sees the image, step 2 is text only, step 3 decides "done" from text and is then re-checked with the image
        self.assertEqual(model.images, [True, False, False, True])

    def test_verification_can_overrule_a_text_only_done(self):
        replies = [reply(type="tap", element=0), reply(type="tap", element=1), reply(type="done", answer="x"),
                   reply(type="tap", element=2), reply(type="done", answer="really done")]
        res, model = self.run_three("adaptive", replies)
        self.assertEqual((res.status, res.answer), ("done", "really done"))

    def test_always_never_differ(self):
        _, always = self.run_three("always", [reply(type="tap", element=0), reply(type="done", answer="x")])
        self.assertEqual(always.images, [True, True])
        _, never = self.run_three("never", [reply(type="tap", element=0), reply(type="done", answer="x")])
        self.assertEqual(never.images[:2], [False, False])


class Landscape(unittest.TestCase):
    def test_landscape_screens_get_a_larger_picture_a_note_and_correct_swipes(self):
        class Land(FakeDevice):
            def __init__(self):
                super().__init__([])
                self.swipes, self.keyboard_open = [], False

            def size(self):
                return 2400, 1080

            def screenshot(self):
                buf = io.BytesIO()
                Image.new("RGB", (2400, 1080), (9, 0, 0)).save(buf, "PNG")
                return buf.getvalue()

            def swipe(self, *a):
                self.swipes.append(a)

            def rotate(self, orientation):
                self.rotated = orientation

        dev = Land()
        model = FakeModel([reply(type="scroll", direction="down"), reply(type="rotate", orientation="portrait"), reply(type="done", answer="ok")])
        seen = {}
        orig = model.complete
        model.complete = lambda system, prompt, image=None, max_tokens=0: (seen.setdefault("w", Image.open(io.BytesIO(image)).size), orig(system, prompt, image, max_tokens))[1]
        res = run("g", dev, model, settle=0)
        self.assertEqual(res.status, "done")
        self.assertEqual(seen["w"], (800, 360))  # short side 360 like a portrait screenshot, not 360x162
        self.assertIn("rotated to landscape", model.calls[0])
        x1, y1, x2, y2 = dev.swipes[0]
        self.assertEqual((x1, x2), (1200, 1200))  # centred on the landscape width, not on the old portrait one
        self.assertLessEqual(max(y1, y2), 1080)
        self.assertEqual(dev.rotated, "portrait")
        self.assertIn("screen is now portrait", model.calls[2])
