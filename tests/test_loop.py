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
        run("g", dev, model, settle=0)
        self.assertEqual(dev.taps, [(200, 400)])  # 1080 / 540 = 2x

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
        model = FakeModel([reply(type="wait")] * 3)
        dev = FakeDevice([png(5)])
        res = run("g", dev, model, max_steps=3, settle=0)
        self.assertEqual(res.status, "max_steps")
        self.assertIn("has not changed", model.calls[2])


if __name__ == "__main__":
    unittest.main()
