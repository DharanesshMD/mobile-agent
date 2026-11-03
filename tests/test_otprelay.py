"""The OTP relay: the owner's real phone finds a code, the owner approves THAT code with one click, the server types it on the cloud phone. The code never leaves memory."""
import json
import logging
import threading
import time
import unittest
from types import SimpleNamespace

from agent.otprelay import OtpRelay, clean_label, mask
from agent.remote import PhoneOffline
from test_chat import Spark, Worker, act, msgs, open_card, say, setup, until

CODE = "482913"
OTHER = "775021"


class FakeHub:
    """The owner's phone link, scripted: otp_watch / otp_poll / otp_ack / otp_stop."""

    def __init__(self, online=("px",)):
        self.online_ids, self.calls, self.codes, self.watch = set(online), [], [], {"enabled": True, "watching": True, "reason": ""}
        self.poll_extra, self.fail_poll = {}, None
        self.links = {}

    def online(self, pid=None):
        return pid in self.online_ids if pid else bool(self.online_ids)

    def listing(self):
        return []

    def call(self, pid, op, args=None, timeout=0, grace=0):
        self.calls.append((pid, op, dict(args or {})))
        if pid not in self.online_ids:
            raise PhoneOffline("offline")
        if op == "otp_watch":
            if isinstance(self.watch, Exception):
                raise self.watch
            return self.watch
        if op == "otp_poll":
            if self.fail_poll:
                raise self.fail_poll
            return {"watching": True, "remaining_s": 60, "codes": list(self.codes), **self.poll_extra}
        return {"ok": True}

    def ops(self, op):
        return [c for c in self.calls if c[1] == op]


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + str(getattr(record, "fields", "")))


def code(i="a1b2c3d4", c=CODE, at=None, source="Messages", title="HDFCBK"):
    return {"id": i, "code": c, "source": source, "title": title, "at_ms": at or int(time.time() * 1000)}


class World:
    """An app with the owner's phone 'Pixel' connected through the fake hub, and a fast relay."""

    def __init__(self, spark=None, *workers, hub=None, **relay):
        self.spark = spark or Spark()
        self.c, self.phone, self.bot, self.app = setup(self.spark, *workers)
        self.db, self.jobs, self.hubmsg = self.app.state.db, self.app.state.jobs, self.app.state.hub
        self.pixel = self.db.create_bot("Pixel", "", kind="phone", phone_id="px")
        self.hub = hub or FakeHub()
        self.jobs.phones = self.hub
        opts = dict(window_s=1.5, extend_s=2.5, poll_s=0.02, ttl_s=3.0)
        opts.update(relay)
        self.relay = self.jobs.otp = OtpRelay(self.db, self.hubmsg, self.jobs, **opts)
        self.cap = Capture()
        for n in ("agent.otp", "agent.audit"):
            lg = logging.getLogger(n)
            lg.addHandler(self.cap)
            lg.setLevel(logging.DEBUG)
        self.seen = []  # every API answer text

    def close(self):
        for n in ("agent.otp", "agent.audit"):
            logging.getLogger(n).removeHandler(self.cap)

    def card(self):
        """An open code card with no task behind it (answerable through the API), plus a stand-in for the job that asked."""
        it = self.app.state.orch.make_item(self.bot, None, {"kind": "otp", "question": "Gmail sent a one-time code. Enter it here."})
        jr = SimpleNamespace(target="", cancel=threading.Event(), timed_out=False)
        return it, jr

    def relay_meta(self, item):
        m = self.db.get_message(item["message_id"])
        return (m["meta"] or {}).get("relay")

    def answer(self, item, **body):
        r = self.c.post(f"/api/items/{item['id']}/answer", json=body)
        self.seen.append(r.text)
        return r

    def everything(self):
        db = self.db
        parts = [json.dumps(db._q("select * from messages")), json.dumps(db._q("select * from items")), json.dumps(db._q("select * from jobs")),
                 "\n".join(self.cap.lines), "\n".join(self.seen)]
        return "\n".join(parts)

    def no_code_anywhere(self, test, *codes):
        blob = self.everything()
        for c in codes or (CODE, OTHER):
            test.assertNotIn(c, blob)
            test.assertNotIn(c[:4], blob.replace("1793", ""))  # a prefix of it neither (epoch times start with 17)


class Basics(unittest.TestCase):
    def test_masking_and_labels(self):
        self.assertEqual(mask("482913"), "••••13")
        self.assertEqual(mask("1234"), "••34")
        self.assertNotRegex(clean_label("Your code is 482913, do not share"), r"\d{4}")
        self.assertEqual(len(clean_label("ab " * 30)), 40)
        self.assertEqual(clean_label("x" * 100), "***")  # a long unbroken string is redacted like any secret-looking text


class Flow(unittest.TestCase):
    def test_a_found_code_is_approved_once_and_typed_on_the_cloud_phone(self):
        spark = Spark(act(type="mobile_task", goal="sign in", say="ok"), act(type="reply", text="in"))
        w = World(spark, Worker(act(type="request_otp", app="Gmail"), act(type="done", answer="ok")))
        self.addCleanup(w.close)
        say(w.c, w.bot, "sign in to gmail")
        card = until(lambda: open_card(w.c, w.bot))
        self.assertEqual(card["meta"]["kind"], "otp")
        until(lambda: (open_card(w.c, w.bot)["meta"].get("relay") or {}).get("state") == "watching")
        r = open_card(w.c, w.bot)["meta"]["relay"]
        self.assertEqual((r["phone"], r["hint"]), ("Pixel", ""))
        self.assertEqual(w.hub.ops("otp_watch")[0][2], {"seconds": 1})  # int(window_s)
        w.hub.codes = [code()]
        until(lambda: (open_card(w.c, w.bot)["meta"].get("relay") or {}).get("state") == "found")
        r = open_card(w.c, w.bot)["meta"]["relay"]
        self.assertEqual((r["hint"], r["source"], r["title"], r["phone"]), ("••••13", "Messages", "HDFCBK", "Pixel"))
        self.assertEqual(w.phone.typed, [])  # nothing typed before the owner's click
        self.assertEqual(w.hub.ops("otp_ack")[0][2], {"id": "a1b2c3d4"})
        item_id = card["meta"]["item_id"]
        res = w.c.post(f"/api/items/{item_id}/answer", json={"approved": True, "use_relay": True})
        w.seen.append(res.text)
        self.assertEqual(res.status_code, 200)
        until(lambda: "in" in [m["content"] for m in msgs(w.c, w.bot) if m["role"] == "assistant"])
        self.assertEqual(w.phone.typed, [CODE])
        self.assertFalse(w.relay.has_candidate(item_id))
        until(lambda: w.hub.ops("otp_stop"))
        done = next(m for m in msgs(w.c, w.bot) if m["kind"] == "action_item")
        self.assertEqual(done["meta"]["answer"], "Code from Pixel sent to the phone")
        self.assertNotIn("relay", done["meta"])  # the masked hint is cleared once it is used
        self.assertEqual(w.c.post(f"/api/items/{item_id}/answer", json={"approved": True, "use_relay": True}).status_code, 409)  # one approval, one use
        text = w.everything() + json.dumps(msgs(w.c, w.bot))
        self.assertNotIn(CODE, text)
        self.assertTrue(any("otp code approved" in ln for ln in w.cap.lines))
        self.assertTrue(any("otp relay started" in ln for ln in w.cap.lines))

    def test_the_newest_code_replaces_an_older_one(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.hub.codes = [code("aaaaaaaa", CODE, at=1000)]
        until(lambda: (w.relay_meta(item) or {}).get("hint") == mask(CODE))
        w.hub.codes = [code("bbbbbbbb", OTHER, at=2000, source="Bank", title="ICICI")]
        until(lambda: (w.relay_meta(item) or {}).get("hint") == mask(OTHER))
        self.assertEqual(w.relay_meta(item)["source"], "Bank")
        got = w.relay.take(item["id"])
        self.assertEqual(got, (OTHER, "Pixel"))
        self.assertIsNone(w.relay.take(item["id"]))  # taken once
        w.no_code_anywhere(self)

    def test_use_relay_through_the_api_resumes_with_the_code_but_never_returns_it(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        self.assertEqual(w.answer(item, approved=True, use_relay=True).status_code, 409)  # nothing found yet
        w.hub.codes = [code()]
        until(lambda: w.relay.has_candidate(item["id"]))
        r = w.answer(item, approved=True, use_relay=True)
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        m = w.db.get_message(item["message_id"])
        self.assertEqual((m["meta"]["status"], m["meta"]["answer"]), ("answered", "Code from Pixel sent to the phone"))
        self.assertEqual(w.answer(item, approved=True, use_relay=True).status_code, 409)  # replay
        w.no_code_anywhere(self)

    def test_use_relay_for_the_wrong_card_or_an_expired_code_is_refused(self):
        w = World(ttl_s=0.2, window_s=0.3, extend_s=0.3)
        self.addCleanup(w.close)
        a, jra = w.card()
        b, _ = w.card()
        w.relay.begin(a, jra)
        w.hub.codes = [code()]
        until(lambda: w.relay.has_candidate(a["id"]))
        self.assertEqual(w.answer(b, approved=True, use_relay=True).status_code, 409)  # B has no code of its own
        self.assertTrue(w.relay.has_candidate(a["id"]))
        time.sleep(0.5)  # the code outlived its lifetime
        self.assertEqual(w.answer(a, approved=True, use_relay=True).status_code, 409)
        w.no_code_anywhere(self)

    def test_declining_and_typing_still_work_and_stop_the_watch(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.hub.codes = [code()]
        until(lambda: w.relay.has_candidate(item["id"]))
        self.assertEqual(w.answer(item, approved=False).status_code, 200)
        self.assertFalse(w.relay.has_candidate(item["id"]))
        until(lambda: w.hub.ops("otp_stop"))
        # a typed code is accepted as before, even while a code from the phone is waiting
        item2, jr2 = w.card()
        w.relay.begin(item2, jr2)
        until(lambda: w.relay.has_candidate(item2["id"]))
        r = w.answer(item2, code="123456")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(w.relay.has_candidate(item2["id"]))
        self.assertEqual(w.db.get_message(item2["message_id"])["meta"]["answer"], "Code sent to the phone")
        self.assertEqual(w.answer(item, code="12ab").status_code, 409)  # already answered

    def test_codes_the_phone_did_not_return_as_codes_are_ignored(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.hub.codes = [code("e1e1e1e1", "12"), code("e2e2e2e2", "abcdef"), code("e3e3e3e3", "123456789"), {"id": "e4", "code": 482913}, "junk"]
        time.sleep(0.4)
        self.assertFalse(w.relay.has_candidate(item["id"]))
        self.assertEqual((w.relay_meta(item) or {}).get("state"), "watching")

    def test_a_title_that_quotes_the_code_is_scrubbed(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.hub.codes = [code(title="Your code is 482913", source="Msgs 775021")]
        until(lambda: (w.relay_meta(item) or {}).get("state") == "found")
        meta = json.dumps(w.relay_meta(item))
        self.assertNotIn("482913", meta)
        self.assertNotIn("775021", meta)


class States(unittest.TestCase):
    def test_no_phone_connected_only_adds_a_hint(self):
        w = World(hub=FakeHub(online=()))
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        r = w.relay_meta(item)
        self.assertEqual((r["state"], r["reason"]), ("none", "Connect your phone to fetch codes automatically"))
        self.assertEqual(w.hub.calls, [])
        self.assertEqual(w.answer(item, code="654321").status_code, 200)  # typing works as before

    def test_a_task_on_the_owners_own_phone_gets_no_relay(self):
        w = World(hub=FakeHub(online=("px",)))
        self.addCleanup(w.close)
        item, jr = w.card()
        jr.target = "phone:px"  # the job runs on the very phone that receives the SMS
        w.relay.begin(item, jr)
        self.assertIsNone(w.relay_meta(item))
        self.assertEqual(w.hub.calls, [])

    def test_a_second_real_phone_can_serve_a_task_on_the_first(self):
        w = World()
        self.addCleanup(w.close)
        w.db.create_bot("Spare", "", kind="phone", phone_id="py")
        w.hub.online_ids.add("py")
        item, jr = w.card()
        jr.target = "phone:py"
        w.relay.begin(item, jr)
        until(lambda: w.hub.ops("otp_watch"))
        self.assertEqual(w.hub.ops("otp_watch")[0][0], "px")  # not the phone the task is on
        w.relay.finish(item["id"])

    def test_off_and_blocked_reasons_are_said_plainly(self):
        for reason, expect in (("off", "switched off in the app on Pixel"), ("no_accessibility", "Turn on Mobile Agent in Accessibility on Pixel"),
                               ("notifications_blocked", "hid the message text"), ("", "switched off")):
            w = World()
            self.addCleanup(w.close)
            w.hub.watch = {"enabled": False, "watching": False, "reason": reason}
            item, jr = w.card()
            w.relay.begin(item, jr)
            until(lambda: (w.relay_meta(item) or {}).get("state") not in (None, "watching"))
            self.assertIn(expect, w.relay_meta(item)["reason"], reason)
            self.assertFalse(w.hub.ops("otp_poll"))
            self.assertEqual(w.db.get_item(item["id"])["status"], "open")  # the card still works by typing

    def test_an_old_app_that_does_not_know_the_op_is_told_to_update(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.watch = RuntimeError("the phone refused: unknown op otp_watch")
        item, jr = w.card()
        w.relay.begin(item, jr)
        until(lambda: (w.relay_meta(item) or {}).get("state") == "unavailable")
        self.assertIn("Update the app on Pixel", w.relay_meta(item)["reason"])

    def test_redacted_text_is_reported_then_a_code_can_still_arrive(self):
        w = World()
        self.addCleanup(w.close)
        w.hub.poll_extra = {"redacted": True}
        item, jr = w.card()
        w.relay.begin(item, jr)
        until(lambda: (w.relay_meta(item) or {}).get("state") == "redacted")
        self.assertIn("hid the message text", w.relay_meta(item)["reason"])
        w.hub.codes = [code()]
        until(lambda: (w.relay_meta(item) or {}).get("state") == "found")

    def test_the_phone_going_offline_mid_watch_is_reported(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        until(lambda: (w.relay_meta(item) or {}).get("state") == "watching")
        w.hub.online_ids.clear()
        until(lambda: (w.relay_meta(item) or {}).get("state") == "offline")
        self.assertEqual(w.relay_meta(item)["reason"], "Your phone is not connected")
        self.assertEqual(w.db.get_item(item["id"])["status"], "open")

    def test_no_code_in_the_window_says_so_and_stops(self):
        w = World(window_s=0.3, extend_s=0.3)
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        until(lambda: (w.relay_meta(item) or {}).get("state") == "expired")
        self.assertIn("No code arrived in 2 minutes", w.relay_meta(item)["reason"])
        until(lambda: w.hub.ops("otp_stop"))
        self.assertFalse(w.relay.has_candidate(item["id"]))

    def test_the_window_is_extended_once_while_the_owner_waits(self):
        w = World(window_s=0.3, extend_s=0.9)
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        until(lambda: len(w.hub.ops("otp_watch")) == 2)
        until(lambda: (w.relay_meta(item) or {}).get("state") == "expired")
        self.assertEqual(len(w.hub.ops("otp_watch")), 2)  # once, not forever

    def test_three_watches_an_hour_per_phone(self):
        w = World(window_s=0.2, extend_s=0.2)
        self.addCleanup(w.close)
        items = []
        for _ in range(4):
            item, jr = w.card()
            w.relay.begin(item, jr)
            items.append(item)
        r = w.relay_meta(items[3])
        self.assertEqual(r["state"], "unavailable")
        self.assertIn("Too many code requests", r["reason"])
        self.assertEqual(len(w.hub.ops("otp_watch")), 3)

    def test_one_watch_per_card(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.relay.begin(item, jr)
        until(lambda: w.hub.ops("otp_watch"))
        time.sleep(0.2)
        self.assertEqual(len(w.hub.ops("otp_watch")), 1)
        w.relay.finish(item["id"])

    def test_a_cancelled_or_timed_out_task_ends_the_watch_and_forgets_the_code(self):
        for how in ("cancel", "timeout"):
            w = World()
            self.addCleanup(w.close)
            item, jr = w.card()
            w.relay.begin(item, jr)
            w.hub.codes = [code()]
            until(lambda: w.relay.has_candidate(item["id"]))
            jr.cancel.set() if how == "cancel" else setattr(jr, "timed_out", True)
            until(lambda: not w.relay.has_candidate(item["id"]))
            until(lambda: w.hub.ops("otp_stop"))
            self.assertIsNone(w.relay.take(item["id"]), how)
            w.no_code_anywhere(self)

    def test_an_expired_card_ends_the_watch(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.relay.begin(item, jr)
        w.hub.codes = [code()]
        until(lambda: w.relay.has_candidate(item["id"]))
        w.db.close_item(item["id"], "expired")
        until(lambda: not w.relay.has_candidate(item["id"]))
        until(lambda: w.hub.ops("otp_stop"))

    def test_a_relay_failure_never_breaks_the_card(self):
        w = World()
        self.addCleanup(w.close)
        item, jr = w.card()
        w.jobs.phones = None  # no hub at all
        w.relay.begin(item, jr)  # must not raise
        self.assertEqual(w.answer(item, code="112233").status_code, 200)


if __name__ == "__main__":
    unittest.main()
