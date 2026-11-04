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

    def test_a_dead_tap_is_named_and_the_worker_is_told_to_give_up_on_the_detail(self):
        taps = [reply(type="tap", x=180, y=700 + i) for i in range(5)]  # the same spot, the screen never changes
        model = FakeModel(taps + [reply(type="done", answer="saved, reminder not set")])
        res = run("g", FakeDevice([png(1)]), model, settle=0, max_steps=15)
        self.assertEqual(res.status, "done")
        self.assertNotIn("NOT tappable", model.calls[1])
        self.assertIn("NOT tappable", model.calls[3])      # after two unanswered taps on the same spot
        self.assertIn("clickable", model.calls[3])
        self.assertNotIn("STOP: nothing has changed", model.calls[3])
        self.assertIn("STOP: nothing has changed", model.calls[5])  # after four

    def test_a_rejection_after_typing_tells_the_worker_to_ask_the_owner_instead_of_guessing(self):
        class Dev(FakeDevice):
            def elements(self):
                return [Element(0, "The phone number +1 (994) 308-2281 you entered is invalid for the country: United States", (540, 1000), False), Element(1, "OK", (540, 1400), True)]
        model = FakeModel([reply(type="type", text="9943082281"), reply(type="tap", element=1), reply(type="done", answer="x")])
        run("g", Dev([png(1), png(2), png(3), png(4)]), model, settle=0, max_steps=15)
        self.assertNotIn("rejects what you entered", model.calls[0])   # before anything was typed
        self.assertIn("rejects what you entered", model.calls[1])      # right after typing
        self.assertIn('"need_info"', model.calls[1])
        self.assertIn("invalid for the country", model.calls[1])

    def test_the_same_submit_is_not_pressed_again_after_a_refusal_even_when_the_dialog_was_dismissed(self):
        class Dev(FakeDevice):
            def elements(self):
                if self.i == 2:
                    return [Element(0, "The number you entered is invalid for the country", (540, 1000), False), Element(1, "OK", (540, 1400), True)]
                return [Element(0, "Next", (540, 2000), True), Element(1, "Cancel", (200, 2000), True)]
        d = Dev([png(i) for i in range(1, 12)])
        seen = []
        model = FakeModel([reply(type="type", text="9943082281"), reply(type="tap", element=0),   # enter it, press Next: refused
                           reply(type="tap", element=1),                                         # dismiss the dialog
                           reply(type="tap", element=0),                                         # press Next again with the same input
                           reply(type="done", answer="asked")])
        res = run("g", d, model, settle=0, max_steps=15, handler=lambda p: seen.append(p) or {"value": "India"})
        self.assertEqual(res.status, "done")
        self.assertEqual(seen[0]["kind"], "need_info")
        self.assertIn("invalid for the country", seen[0]["text"])
        self.assertEqual(len(d.taps), 2)   # Next once and OK once: the second Next was never pressed

    def test_not_found_on_a_page_nobody_typed_into_is_not_a_rejection(self):
        from agent.loop import rejection
        self.assertEqual(rejection([Element(0, "Page not found", (1, 1), False)], [{"type": "tap", "x": 1, "y": 2}]), "")
        self.assertIn("Invalid", rejection([Element(0, "Invalid code", (1, 1), False)], [{"type": "type", "text": "1"}]))

    def test_a_handover_about_a_verification_code_becomes_a_code_request(self):
        seen = []
        res = run("g", FakeDevice([png(1), png(2)]),
                  FakeModel([reply(type="handoff", reason="The screen says to open WhatsApp on your other phone to get the 6-digit code"), reply(type="done", answer="in")]),
                  settle=0, handler=lambda p: seen.append(p) or {"code": "482913"})
        self.assertEqual(seen[0]["kind"], "request_otp")
        self.assertEqual(res.status, "done")

    def test_a_handover_about_an_empty_number_field_or_a_country_code_is_not_a_code_request(self):
        for why in ("Phone number field is still empty after typing; the country code box shows +1", "Change the country code to 91 yourself"):
            seen = []
            run("g", FakeDevice([png(1), png(2)]), FakeModel([reply(type="handoff", reason=why), reply(type="done", answer="in")]),
                settle=0, handler=lambda p: seen.append(p) or {"done": True})
            self.assertEqual(seen[0]["kind"], "handoff", why)

    def test_a_code_request_waits_until_a_field_has_focus(self):
        class Dev(FakeDevice):
            keyboard_open = False
        seen = []
        d = Dev([png(i) for i in range(1, 6)])
        model = FakeModel([reply(type="request_otp", app="WhatsApp"), reply(type="request_otp", app="WhatsApp"), reply(type="done", answer="in")])
        res = run("g", d, model, settle=0, handler=lambda p: seen.append(p) or {"code": "482913"})
        self.assertEqual(res.status, "done")
        self.assertIn("no keyboard is open", model.calls[1])   # told once to tap the code field first
        self.assertEqual(len(seen), 1)                         # the second request went through (the worker may have no field to focus)

    def test_a_handover_for_a_password_stays_a_handover(self):
        seen = []
        run("g", FakeDevice([png(1), png(2)]), FakeModel([reply(type="handoff", reason="Enter your password and the verification code"), reply(type="done", answer="in")]),
            settle=0, handler=lambda p: seen.append(p) or {"done": True})
        self.assertEqual(seen[0]["kind"], "handoff")

    def test_type_with_clear_empties_the_field_first(self):
        class Dev(FakeDevice):
            def clear_field(self):
                self.cleared = True
        d = Dev([png(1), png(2)])
        run("g", d, FakeModel([reply(type="type", text="91", clear=True), reply(type="done", answer="ok")]), settle=0)
        self.assertTrue(d.cleared)
        self.assertEqual(d.typed, ["91"])

    def test_bouncing_between_two_controls_is_called_out(self):
        bounce = [reply(type="tap", element=0) if i % 2 == 0 else reply(type="tap", x=300, y=50) for i in range(6)]
        model = FakeModel(bounce + [reply(type="done", answer="ok")])
        run("g", FakeDevice([png(i) for i in range(1, 10)]), model, settle=0, max_steps=15)  # the screen changes every time
        self.assertNotIn("one after the other", model.calls[5])
        self.assertIn("one after the other", model.calls[6])

    def test_a_screen_that_never_responds_stops_the_job_early(self):
        model = FakeModel([reply(type="tap", x=10 + i * 90, y=100) for i in range(30)])
        res = run("g", FakeDevice([png(1)]), model, settle=0, max_steps=25)
        self.assertEqual(res.status, "failed")
        self.assertIn("did not change", res.answer)
        self.assertLess(len(model.calls), 12)  # not the whole budget

    def test_waiting_on_an_unchanged_screen_is_not_stuck(self):
        model = FakeModel([reply(type="wait", seconds=1) for _ in range(10)] + [reply(type="done", answer="ok")])
        res = run("g", FakeDevice([png(1)]), model, settle=0, max_steps=25)
        self.assertEqual(res.status, "done")

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
