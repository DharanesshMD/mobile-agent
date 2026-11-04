import shlex
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.apps import INTENT_KINDS, build_intent
from agent.device import Device
from agent.loop import execute


def parsed(kind, **p):
    """What the phone's shell would see: the argument list after quote removal, plus the model-facing note."""
    args, note = build_intent(kind, p, "Asia/Kolkata")
    return shlex.split(" ".join(args)), note


class Builder(unittest.TestCase):
    def test_alarm_and_timer_skip_the_ui_and_carry_label(self):
        a, note = parsed("set_alarm", hour=6, minute=45, label="Gym day")
        self.assertEqual(a[:2], ["-a", "android.intent.action.SET_ALARM"])
        self.assertIn("android.intent.extra.alarm.SKIP_UI", a)
        self.assertEqual(a[a.index("android.intent.extra.alarm.MESSAGE") + 1], "Gym day")
        self.assertEqual(note, "alarm set for 06:45 (Gym day)")
        t, note = parsed("set_timer", seconds=300)
        self.assertEqual(t[t.index("android.intent.extra.alarm.LENGTH") + 1], "300")

    def test_awkward_text_survives_the_shell(self):
        body = "it's 5pm & \"done\" $(rm -rf /) ; `x` ñ தமிழ்"
        a, note = parsed("compose_sms", number="(555) 123-4567", body=body)
        self.assertEqual(a[a.index("sms_body") + 1], body)
        self.assertEqual(a[a.index("-d") + 1], "sms:5551234567")
        self.assertIn("not sent", note)

    def test_contact_email_event_maps_share(self):
        c, _ = parsed("insert_contact", name="Asha Raman", phone="+91 98765 43210", email="a@x.com")
        self.assertEqual(c[c.index("name") + 1], "Asha Raman")
        self.assertEqual(c[c.index("phone") + 1], "+919876543210")
        e, _ = parsed("compose_email", to="a@x.com", subject="Hi there", body="Line one")
        self.assertEqual(e[e.index("-d") + 1], "mailto:a@x.com")
        m, _ = parsed("maps_search", query="Chennai Airport & cafes")
        self.assertEqual(m[m.index("-d") + 1], "geo:0,0?q=Chennai%20Airport%20%26%20cafes")
        n, _ = parsed("navigate", destination="MG Road")
        self.assertEqual(n[n.index("-d") + 1], "google.navigation:q=MG%20Road")
        s, _ = parsed("share_text", text="hello")
        self.assertEqual(s[s.index("-t") + 1], "text/plain")
        self.assertEqual(parsed("dial", number="5551234567")[0][-1], "tel:5551234567")
        self.assertEqual(parsed("camera")[0], ["-a", "android.media.action.STILL_IMAGE_CAMERA"])

    def test_event_times_use_the_phone_time_zone(self):
        e, _ = parsed("insert_event", title="Review", start="2026-10-29 15:00", minutes=30)
        begin = int(e[e.index("beginTime") + 1])
        want = int(datetime(2026, 10, 29, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata")).timestamp() * 1000)
        self.assertEqual(begin, want)
        self.assertEqual(int(e[e.index("endTime") + 1]) - begin, 30 * 60000)
        args, _ = build_intent("insert_event", dict(title="x", start="2026-10-29 15:00"), "UTC")
        self.assertNotEqual(int(args[args.index("beginTime") + 1]), want)  # different zone, different instant

    def test_event_description_and_keyboard_hint(self):
        e, note = parsed("insert_event", title="Market", start="2026-11-21 19:00", location="Park", description="Food, music. Free entry")
        self.assertEqual(e[e.index("description") + 1], "Food, music. Free entry")
        self.assertIn("press back once", note)

    def test_validation_gives_helpful_errors(self):
        bad = [("set_alarm", dict(hour=24, minute=0), "between 0 and 23"), ("set_alarm", dict(hour="x", minute=0), "whole number"),
               ("set_timer", dict(seconds=0), "between 1"), ("dial", dict(number="abc"), "not a phone number"),
               ("compose_sms", dict(number="5551234567"), "missing 'body'"), ("compose_email", dict(to="nope", subject="s", body="b"), "not an email"),
               ("insert_event", dict(title="t", start="tomorrow"), "must look like"), ("maps_search", dict(), "missing 'query'"),
               ("share_text", dict(text="x" * 5000), "longer than")]
        for kind, p, msg in bad:
            with self.assertRaises(ValueError, msg=kind) as cm:
                build_intent(kind, p)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            build_intent("launch_missiles", {})
        self.assertIn("set_alarm", str(cm.exception))
        self.assertEqual(len(INTENT_KINDS), 11)


class FakePhone:
    def __init__(self, alarm="2026-10-29 06:45:00", timer=True):
        self.started, self._alarm, self._timer = [], alarm, timer

    def timezone(self):
        return "Asia/Kolkata"

    def start_intent(self, args):
        self.started.append(args)

    def next_alarm(self):
        return self._alarm

    def has_timer(self):
        return self._timer


class Executor(unittest.TestCase):
    def test_alarm_result_is_confirmed_from_the_phone(self):
        ph = FakePhone()
        note = execute(ph, {"type": "intent", "kind": "set_alarm", "hour": 6, "minute": 45}, [], 2.0, (720, 1600))
        self.assertIn("next alarm on the phone: 2026-10-29 06:45:00", note)
        self.assertEqual(len(ph.started), 1)
        self.assertIn("could not confirm", execute(FakePhone(timer=False), {"type": "intent", "kind": "set_timer", "seconds": 60}, [], 2.0, (1, 1)))

    def test_prefill_intents_do_not_claim_to_be_done(self):
        note = execute(FakePhone(), {"type": "intent", "kind": "insert_contact", "name": "A B", "phone": "5551234567"}, [], 2.0, (1, 1))
        self.assertIn("tap Save", note)

    def test_invalid_intent_raises_before_touching_the_phone(self):
        ph = FakePhone()
        with self.assertRaises(ValueError):
            execute(ph, {"type": "intent", "kind": "set_alarm", "hour": 99, "minute": 0}, [], 2.0, (1, 1))
        self.assertEqual(ph.started, [])


class DeviceHelpers(unittest.TestCase):
    def device(self, out):
        d = Device(use_u2=False)
        d._run = lambda *a: out if a[:3] == ("shell", "am", "start") or a[:3] == ("shell", "dumpsys", "alarm") else b""
        return d

    def test_start_intent_raises_on_am_errors_even_with_exit_code_zero(self):
        d = self.device(b"Starting: Intent { act=x }\nError: Activity not started, unable to resolve Intent { act=x }\n")
        with self.assertRaises(ValueError) as cm:
            d.start_intent(["-a", "x"])
        self.assertIn("unable to resolve", str(cm.exception))
        self.device(b"Starting: Intent { act=x }\n").start_intent(["-a", "x"])

    def test_next_alarm_and_timer_are_read_from_dumpsys(self):
        dump = (b"  Next alarm clock information: \n    user:0 pendingSend:false time:1793236500000 = 2026-10-29 06:45:00.000 = +18h\n"
                b"  46 pending alarms:\n    tag=*walarm*:com.android.deskclock.action.TIMER_EXPIRED\n")
        d = self.device(dump)
        self.assertEqual(d.next_alarm(), "2026-10-29 06:45:00")
        self.assertTrue(d.has_timer())
        empty = self.device(b"  Next alarm clock information: \n  \n  0 pending alarms:\n")
        self.assertIsNone(empty.next_alarm())
        self.assertFalse(empty.has_timer())


if __name__ == "__main__":
    unittest.main()
