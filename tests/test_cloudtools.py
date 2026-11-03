"""Cloud-phone use cases: named captures and reports, the verdict, set_device and its undo, inspect_app, the observe-only link check, and the report in the Library."""
import io
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from agent import cloudtools, report
from agent.artifacts import Artifacts
from agent.device import Element
from test_chat import Spark, Worker, act, setup, until
from test_service import Phone, png

DUMP = """Packages:
  Package [com.example.notes] (abc123):
    appId=10150
    versionCode=7 minSdk=24 targetSdk=34
    versionName=2.4.1
    installerPackageName=com.android.vending
    signatures=PackageSignatures{d5c1fd2 version:3, signatures:[e3ca78d8], past signatures:[]}
    declared permissions:
      com.example.notes.permission.DEBUG: prot=signature
    requested permissions:
      android.permission.INTERNET
      android.permission.CAMERA
      android.permission.READ_SMS
      android.permission.ACCESS_NETWORK_STATE
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=1 installed=true
  Activity Resolver Table:
    Non-Data Actions:
        android.intent.action.MAIN:
          1a2b com.example.notes/.MainActivity filter 3c4d
          5e6f com.example.notes/.SettingsActivity filter 7a8b
"""


class CloudPhone(Phone):
    """A phone that has the cloud-only methods (the emulator and Cloud phone 2 have them; a real phone does not)."""
    def __init__(self):
        super().__init__()
        self.night, self.scale, self.display, self.cleared, self.calls = False, 1.0, "start", [], []
        self.supports_locale = True
        self.timeout = 2147483647

    def night_mode(self): return self.night
    def set_night(self, on): self.night = on; self.calls.append(("night", on))
    def font_scale(self): return self.scale
    def set_font_scale(self, scale): self.scale = scale; self.calls.append(("font", scale))
    def set_display(self, w, h, dpi): self.display = f"{w}x{h}@{dpi}"; self.calls.append(("display", self.display))
    def reset_display(self): self.display = "start"; self.calls.append(("display", "reset"))
    def screen_timeout(self): return self.timeout
    def set_screen_timeout(self, ms): self.timeout = ms; self.calls.append(("timeout", ms))
    def set_app_language(self, package, tag): self.calls.append(("lang", package, tag)); return self.supports_locale or not tag
    def package_dump(self, package): return DUMP if package == "com.example.notes" else ""
    def clear_app(self, package): self.cleared.append(package)


def ctx(device, art=None):
    return {"device": device, "artifacts": art or Artifacts()}


class Captures(unittest.TestCase):
    def test_a_capture_is_kept_small_and_captioned(self):
        art = Artifacts()
        n = art.add_capture(png(5), "  Open   Settings ", "Tap the gear", 3)
        self.assertEqual(n, 1)
        cap = art.captures[0]
        self.assertEqual((cap["title"], cap["note"], cap["step"]), ("Open Settings", "Tap the gear", 3))
        img = Image.open(io.BytesIO(cap["jpeg"]))
        self.assertEqual((img.format, img.width), ("JPEG", 540))

    def test_the_number_of_pictures_is_limited(self):
        art = Artifacts()
        for i in range(30):
            art.add_capture(png(i), f"s{i}")
        with self.assertRaises(ValueError):
            art.add_capture(png(1), "one too many")

    def test_a_verdict_uses_the_three_levels_and_never_says_safe(self):
        art = Artifacts()
        art.set_verdict("Safe", ["looks fine"], ["address"], ["downloads"])
        self.assertEqual(art.verdict["level"], "nothing_found")  # a "safe" claim becomes "nothing found in these checks"
        art.set_verdict("phishing", ["imitates a bank"], [], [])
        self.assertEqual(art.verdict["level"], "harmful")
        with self.assertRaises(ValueError):
            art.set_verdict("great", [], [], [])

    def test_the_capture_and_verdict_actions_run_through_the_tool_runner(self):
        c = ctx(CloudPhone())
        self.assertEqual(cloudtools.run(c, {"step": 2}, {"type": "capture", "title": "Home", "note": "n"}), "captured picture 1")
        out = cloudtools.run(c, {"step": 3}, {"type": "verdict", "level": "suspicious", "reasons": ["urgent wording"], "checked": ["address bar"], "not_checked": ["downloads"]})
        self.assertIn("suspicious", out)
        self.assertEqual(c["artifacts"].verdict["reasons"], ["urgent wording"])


class Reports(unittest.TestCase):
    def build(self, kind="link", **kw):
        art = Artifacts()
        art.add_capture(png(9), "<script>alert(1)</script>", "tap <b>here</b>")
        art.set_verdict("harmful", ["Chrome showed a warning page"], ["address bar", "warning page"], ["downloads"])
        args = dict(kind=kind, title="Link check: https://x.test", goal="is https://x.test safe?", status="done", answer="Done.", captures=art.captures,
                    verdict=art.verdict, phone="the shared emulator", facts=["a fact"], mask=lambda s: s.replace("me@gmail.com", "m***@gmail.com"))
        args.update(kw)
        return report.build(**args)

    def test_the_page_is_self_contained_escaped_and_states_what_was_not_checked(self):
        md, page = self.build()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))  # no outside requests (links in text are escaped text, not src/href)
        self.assertNotIn('src="http', page)
        self.assertIn("data:image/jpeg;base64,", page)
        self.assertIn("Looks harmful", page)
        self.assertIn("What was NOT checked", page)
        self.assertIn("downloads", md)
        self.assertIn("nothing was typed", page.lower())

    def test_a_clean_result_is_worded_as_nothing_found(self):
        art = Artifacts()
        art.set_verdict("nothing_found", [], [], [])
        md, page = report.build("link", "t", "g", "done", "", [], art.verdict)
        self.assertIn("Nothing found in these checks", md)
        self.assertNotIn("safe", md.lower().replace("not been confirmed safe", ""))

    def test_account_addresses_are_masked_everywhere(self):
        md, page = self.build(answer="signed in as me@gmail.com", facts=["me@gmail.com"])
        self.assertNotIn("me@gmail.com", md + page)
        self.assertIn("m***@gmail.com", md)

    def test_a_guide_is_a_numbered_list_and_a_matrix_a_grid(self):
        _, guide = self.build("guide", verdict=None)
        self.assertIn('<ol class="steps">', guide)
        _, grid = self.build("matrix", verdict=None)
        self.assertIn('class="grid"', grid)


class SetDevice(unittest.TestCase):
    def test_changes_are_undone_when_the_task_ends(self):
        phone, art = CloudPhone(), Artifacts()
        out = cloudtools.run({"device": phone, "artifacts": art}, {}, {"type": "set_device", "dark_mode": "on", "font_scale": "large", "size": "small"})
        self.assertIn("restored when the task ends", out)
        self.assertEqual((phone.night, phone.scale, phone.display), (True, 1.3, "540x1200@240"))
        self.assertEqual(art.finish(), [])
        self.assertEqual((phone.night, phone.scale, phone.display), (False, 1.0, "start"))

    def test_it_refuses_on_the_owners_own_phone(self):
        with self.assertRaises(ValueError) as cm:
            cloudtools.run(ctx(Phone()), {}, {"type": "set_device", "dark_mode": "on"})  # a device without the cloud methods, like the real-phone app's
        self.assertIn("only works on Phone and Phone 2", str(cm.exception))

    def test_bad_values_and_unsupported_languages_say_so(self):
        c = ctx(CloudPhone())
        with self.assertRaises(ValueError):
            cloudtools.run(c, {}, {"type": "set_device", "font_scale": 9})
        with self.assertRaises(ValueError):
            cloudtools.run(c, {}, {"type": "set_device", "size": "gigantic"})
        with self.assertRaises(ValueError):
            cloudtools.run(c, {}, {"type": "set_device"})
        c["device"].supports_locale = False
        with self.assertRaises(ValueError) as cm:
            cloudtools.run(c, {}, {"type": "set_device", "language": "hi", "package": "com.example.notes"})
        self.assertIn("does not support a per-app language", str(cm.exception))

    def test_one_failing_undo_does_not_stop_the_others(self):
        art, calls = Artifacts(), []
        art.on_finish(lambda: calls.append("a"))
        art.on_finish(lambda: (_ for _ in ()).throw(RuntimeError("phone gone")))
        art.on_finish(lambda: calls.append("c"))
        errors = art.finish()
        self.assertEqual(calls, ["c", "a"])
        self.assertEqual(len(errors), 1)


class Snapshot(unittest.TestCase):
    def test_what_a_guide_changes_in_settings_is_put_back(self):
        phone, art = CloudPhone(), Artifacts()
        cloudtools.snapshot(phone, art)
        phone.set_night(True); phone.set_screen_timeout(120000); phone.set_font_scale(1.8)  # what the worker does while it demonstrates the steps
        self.assertEqual(art.finish(), [])
        self.assertEqual((phone.night, phone.timeout, phone.scale), (False, 2147483647, 1.0))

    def test_nothing_happens_on_a_phone_without_the_cloud_methods(self):
        art = Artifacts()
        cloudtools.snapshot(Phone(), art)
        self.assertEqual(art.finish(), [])


class InspectApp(unittest.TestCase):
    def test_permissions_are_read_and_the_dangerous_ones_explained(self):
        info = cloudtools.parse_package_dump(DUMP, "com.example.notes")
        self.assertEqual((info["version"], info["target_sdk"], info["installer"], info["key_id"]), ("2.4.1", "34", "com.android.vending", "e3ca78d8"))
        self.assertEqual(info["components"], 2)
        danger = dict(cloudtools.dangerous_permissions(info["permissions"]))
        self.assertEqual(sorted(danger), ["CAMERA", "READ_SMS"])
        self.assertIn("text messages", danger["READ_SMS"])

    def test_the_tool_reports_facts_and_adds_them_to_the_report(self):
        c = ctx(CloudPhone())
        out = cloudtools.run(c, {}, {"type": "inspect_app", "package": "com.example.notes"})
        self.assertIn("READ_SMS: reads your text messages", out)
        self.assertTrue(any("signing key id e3ca78d8" in f for f in c["artifacts"].facts))

    def test_an_app_that_is_not_installed_is_reported(self):
        with self.assertRaises(ValueError) as cm:
            cloudtools.run(ctx(CloudPhone()), {}, {"type": "inspect_app", "package": "com.nope.app"})
        self.assertIn("not installed", str(cm.exception))
        with self.assertRaises(ValueError):
            cloudtools.run(ctx(CloudPhone()), {}, {"type": "inspect_app", "package": "../etc"})


class ObserveOnly(unittest.TestCase):
    ELS = [Element(0, "Got it", (10, 10), True), Element(1, "Log in to your bank", (20, 20), True), Element(2, "Back to safety", (30, 30), True)]

    def test_typing_is_never_allowed_and_only_the_browsers_own_screens_may_be_tapped(self):
        self.assertIsNotNone(cloudtools.observe_block({"type": "type", "text": "x"}, self.ELS))
        self.assertIsNotNone(cloudtools.observe_block({"type": "type_secret", "label": "x"}, self.ELS))
        self.assertIsNone(cloudtools.observe_block({"type": "tap", "element": 0}, self.ELS))
        self.assertIsNone(cloudtools.observe_block({"type": "tap", "element": 2}, self.ELS))
        self.assertIsNotNone(cloudtools.observe_block({"type": "tap", "element": 1}, self.ELS))  # a page link or button
        self.assertIsNotNone(cloudtools.observe_block({"type": "tap", "x": 5, "y": 5}, self.ELS))  # coordinates could hit anything
        self.assertIsNone(cloudtools.observe_block({"type": "key", "key": "back"}, self.ELS))
        self.assertIsNotNone(cloudtools.observe_block({"type": "key", "key": "enter"}, self.ELS))
        self.assertIsNone(cloudtools.observe_block({"type": "open_url", "url": "https://x.test"}, self.ELS))


class InJobs(unittest.TestCase):
    def setup(self, *worker_actions):
        c, phone, bot, app = setup(Spark(act(type="reply", text="ok")), Worker(*worker_actions))
        cloud = CloudPhone()
        app.state.jobs.device = cloud
        return c, cloud, bot, app

    def job(self, c, bot):
        return c.get(f"/api/bots/{bot}/jobs").json()[0]

    def test_a_link_check_leaves_a_verdict_a_report_and_a_clean_browser(self):
        c, cloud, bot, app = self.setup(
            act(type="open_url", url="https://x.test/login"),
            act(type="capture", title="Landing page", note="login form"),
            act(type="verdict", level="suspicious", reasons=["asks for a password"], checked=["address"], not_checked=["downloads"]),
            act(type="done", answer="Suspicious: asks for a password."))
        app.state.jobs.submit(bot, "is https://x.test/login safe?", report="link")
        until(lambda: c.get(f"/api/bots/{bot}/jobs").json() and self.job(c, bot)["status"] == "done", timeout=30)
        job = self.job(c, bot)
        self.assertTrue(job["result"].startswith("Verdict: Looks suspicious. asks for a password"))
        self.assertIn("com.android.chrome", cloud.cleared)
        lib = c.get(f"/api/bots/{bot}/library").json()
        reports = [i for i in lib if i["kind"] == "report"]
        self.assertEqual(len(reports), 1)
        page = c.get(f"/api/library/{reports[0]['id']}/file")
        self.assertEqual(page.headers["content-type"], "text/html; charset=utf-8")
        self.assertIn("sandbox", page.headers["content-security-policy"])
        self.assertIn("What was NOT checked", page.text)
        self.assertIn("downloads", c.get(f"/api/library/{reports[0]['id']}/md").text)
        card = [m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["kind"] == "report"]
        self.assertEqual(card[0]["meta"]["verdict"], "suspicious")
        self.assertEqual(card[0]["meta"]["library_id"], reports[0]["id"])

    def test_typing_on_a_link_check_is_refused_and_nothing_is_typed(self):
        c, cloud, bot, app = self.setup(act(type="type", text="hunter2"), act(type="type", text="hunter2"), act(type="type", text="hunter2"))
        app.state.jobs.submit(bot, "is https://x.test safe?", report="link")
        until(lambda: c.get(f"/api/bots/{bot}/jobs").json() and self.job(c, bot)["status"] in ("denied", "failed", "done"), timeout=30)
        self.assertEqual(cloud.typed, [])
        self.assertIn("Observe-only", self.job(c, bot)["result"])

    def test_screen_setting_checks_never_run_on_a_phone_without_the_cloud_methods(self):
        c, phone, bot, app = setup(Spark(act(type="reply", text="ok")), Worker(act(type="done", answer="x")))
        app.state.jobs.submit(bot, "show settings in dark mode", report="matrix")  # the default test phone is like the real-phone app: no cloud methods
        until(lambda: c.get(f"/api/bots/{bot}/jobs").json() and c.get(f"/api/bots/{bot}/jobs").json()[0]["status"] == "failed", timeout=20)
        self.assertIn("never run on a phone paired through the app", c.get(f"/api/bots/{bot}/jobs").json()[0]["result"])

    def test_a_guide_keeps_every_capture_as_a_numbered_step_and_restores_the_phone(self):
        c, cloud, bot, app = self.setup(
            act(type="set_device", dark_mode="on"),
            act(type="capture", title="Open Settings", note="Tap the gear"),
            act(type="capture", title="Tap Display", note="Then Dark theme"),
            act(type="done", answer="Guide ready."))
        app.state.jobs.submit(bot, "make a guide to turn on dark mode", report="guide")
        until(lambda: c.get(f"/api/bots/{bot}/jobs").json() and self.job(c, bot)["status"] == "done", timeout=30)
        self.assertEqual(cloud.night, False)  # restored
        item = next(i for i in c.get(f"/api/bots/{bot}/library").json() if i["kind"] == "report")
        page = c.get(f"/api/library/{item['id']}/file").text
        self.assertIn("Open Settings", page)
        self.assertIn("Tap Display", page)
        self.assertIn('<ol class="steps">', page)

    def test_the_screenshots_of_ordinary_tasks_are_still_plain_library_pictures(self):
        c, cloud, bot, app = self.setup(act(type="tap", element=0), act(type="done", answer="ok"))
        app.state.jobs.submit(bot, "tap it")
        until(lambda: c.get(f"/api/bots/{bot}/jobs").json() and self.job(c, bot)["status"] == "done", timeout=30)
        lib = c.get(f"/api/bots/{bot}/library").json()
        self.assertTrue(lib and all(i["kind"] in (None, "screenshot") for i in lib))
        self.assertEqual(c.get(f"/api/library/{lib[0]['id']}/file").headers["content-type"], "image/png")


if __name__ == "__main__":
    unittest.main()


class AccountScreens(unittest.TestCase):
    def state(self, *labels):
        return {"step": 1, "obs": {"elements": [{"id": i, "label": l} for i, l in enumerate(labels)]}}

    def test_a_screen_with_an_account_address_is_never_captured(self):
        c = ctx(CloudPhone())
        for labels in (("Make Chrome your own", "owner@gmail.com"), ("Choose an account.", "Stay signed out"), ("Continue as Dharun",)):
            with self.assertRaises(ValueError) as cm:
                cloudtools.run(c, self.state(*labels), {"type": "capture", "title": "x"})
            self.assertIn("not kept in a report", str(cm.exception))
        self.assertEqual(c["artifacts"].captures, [])
        self.assertEqual(cloudtools.run(c, self.state("Example Domain", "More information..."), {"type": "capture", "title": "Page"}), "captured picture 1")

    def test_an_avatar_description_that_is_not_drawn_does_not_block_a_capture(self):
        c = ctx(CloudPhone())  # Google Keep's top bar: the address is only in the avatar button's content description
        self.assertEqual(cloudtools.run(c, self.state("Google Account: Dharun (owner@gmail.com)", "Search your notes"), {"type": "capture", "title": "Notes"}), "captured picture 1")
        self.assertEqual(cloudtools.run(c, self.state("Signed in as owner@gmail.com", "Search Keep"), {"type": "capture", "title": "Notes 2"}), "captured picture 2")

    def test_staying_signed_out_is_an_allowed_first_run_tap(self):
        els = [Element(0, "Stay signed out", (1, 1), True), Element(1, "Continue as Dharun", (2, 2), True)]
        self.assertIsNone(cloudtools.observe_block({"type": "tap", "element": 0}, els))
        self.assertIsNotNone(cloudtools.observe_block({"type": "tap", "element": 1}, els))  # signing in is never part of a link check


class Titles(unittest.TestCase):
    def test_a_link_report_is_named_after_the_address(self):
        self.assertEqual(report.title_for("link", "Check this link: https://example.com/a?b=1 - open it"), "Link check: https://example.com/a?b=1")
        self.assertEqual(report.title_for("guide", "make a guide to turn on dark mode\nextra"), "Step-by-step guide: Turn on dark mode")
        self.assertEqual(report.title_for("plain", ""), "Report")
        self.assertEqual(report.title_for("guide", "Create a step-by-step guide with screenshots for an elderly reader: how to open Google Keep and write a note"),
                         "Step-by-step guide: Open Google Keep and write a note")
        self.assertEqual(report.title_for("plain", "1) Set an alarm for 6:45 tomorrow morning (Nov 3). 2) Note current battery percentage. 3) Check if Keep is installed"),
                         "Report: Set an alarm for 6:45 tomorrow morning (Nov 3) + 2 more")
        long = report.title_for("matrix", "Open the Clock app and capture it in two states for the matrix report, with a very long description that goes on and on about nothing")
        self.assertTrue(long.endswith("…") and len(long) < 90)
