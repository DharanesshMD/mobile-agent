"""The replay exporter (scripts/export_replay.py) and the static player (web/replay.html)."""
import contextlib
import hashlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import export_replay as ex  # noqa: E402
from agent.db import Database  # noqa: E402

SAFE_IMG = re.compile(r"^img/[A-Za-z0-9._-]+\.jpg$")
PRIVATE_DIR = str(Path(tempfile.gettempdir()) / "alex-private")  # a path the masker must hide, built at run time so no real home directory is written here


def png(w=1080, h=2400, color=(30, 60, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def tree_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class Masking(unittest.TestCase):
    def test_emails_ips_tokens_codes_paths_numbers_and_url_queries_are_hidden(self):
        raw = ("Signed in as someone.private@gmail.com from 192.168.10.171 and fe80::1ff:fe23:4567:890a, token dev_Zk3P9xQw1LmN8rTy5VbC2aSdFgHj "
               "Bearer abcdef1234567890 key 0123456789abcdef0123456789abcdef01234567 code MYKT-HFQ5, file " + PRIVATE_DIR + "/x.png, "
               "call +91 98765 43210, https://example.com/page?token=abc123&u=me")
        out = ex.mask_text(raw)
        for secret in ("someone.private", "gmail.com", "192.168", "fe80", "dev_Zk3P", "abcdef1234567890", "0123456789abcdef0123", "MYKT-HFQ5",
                       PRIVATE_DIR, "98765 43210", "abc123", "u=me"):
            self.assertNotIn(secret, out)
        for tag in ("[email]", "[ip]", "[token]", "[code]", "[path]", "[number]"):
            self.assertIn(tag, out)
        self.assertIn("https://example.com/page?…", out)

    def test_ordinary_text_is_left_alone(self):
        for ok in ("It is 12:30:45 on Android 14 (version 1.3.0)", "open com.supercell.clashofclans and tap 'Play' at (540, 1200)", "step 3 of 12, 4.3 s"):
            self.assertEqual(ex.mask_text(ok), ok)

    def test_extra_text_is_hidden_case_insensitively(self):
        self.assertEqual(ex.mask_text("Hello Dharun, hi DHARUN", ["dharun"]), "Hello [hidden], hi [hidden]")
        self.assertEqual(ex.mask_text("", ["x"]), "")


class Pictures(unittest.TestCase):
    def test_a_screenshot_becomes_a_small_jpeg_with_the_same_proportions(self):
        data, w, h = ex.shrink_jpeg(png(1080, 2400))
        img = Image.open(io.BytesIO(data))
        self.assertEqual((img.format, img.size), ("JPEG", (360, 800)))
        self.assertEqual((w, h), (360, 800))
        self.assertLess(len(data), 20_000)

    def test_a_small_picture_is_not_enlarged(self):
        self.assertEqual(ex.shrink_jpeg(png(200, 300))[1:], (200, 300))

    def test_a_redact_box_is_blacked_out(self):
        data, w, h = ex.shrink_jpeg(png(720, 1280, (240, 240, 240)), redact=[(0, 0, 1, 0.1)])
        img = Image.open(io.BytesIO(data)).convert("L")
        self.assertLess(img.getpixel((w // 2, 5)), 20)       # inside the box
        self.assertGreater(img.getpixel((w // 2, h // 2)), 200)  # outside it

    def test_the_placeholder_is_a_small_jpeg(self):
        img = Image.open(io.BytesIO(ex.placeholder(1000)))
        self.assertEqual((img.format, img.width), ("JPEG", 360))


class Captions(unittest.TestCase):
    def test_an_element_id_becomes_the_label_the_model_saw(self):
        prompt = "UI elements:\n0: 'Home' at (1,2)\n3: 'Wi-Fi' at (4,5) [clickable]\n"
        self.assertEqual(ex.describe_action({"type": "tap", "element": 3}, prompt), "tap “Wi-Fi”")
        self.assertEqual(ex.describe_action({"type": "tap", "x": 5, "y": 6}), "tap (5, 6)")
        self.assertEqual(ex.describe_action({"type": "swipe", "direction": "down"}), "swipe down")

    def test_typed_text_is_masked_and_secrets_are_never_shown(self):
        self.assertNotIn("me@x.com", ex.describe_action({"type": "type", "text": "me@x.com"}))
        self.assertIn("never shown", ex.describe_action({"type": "type_secret", "label": "Netflix"}))


class Timing(unittest.TestCase):
    def test_idle_gaps_are_capped_and_order_is_kept(self):
        self.assertEqual(ex._compress([100.0, 100.0, 102.5, 400.0, 401.0], 8.0), [0, 0, 2500, 10500, 11500])
        self.assertEqual(ex._compress([], 8.0), [])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_manifest_matches_what_the_player_expects(self):
        m = ex.export_demo(self.dir / "sessions" / "demo")
        on_disk = json.loads((self.dir / "sessions" / "demo" / "session.json").read_text())
        self.assertEqual(m, on_disk)
        self.assertEqual((on_disk["version"], on_disk["name"], on_disk["synthetic"]), (1, "demo", True))
        for key in ("title", "bot", "phones", "duration_ms", "steps", "events"):
            self.assertIn(key, on_disk)
        times = [e["t"] for e in on_disk["events"]]
        self.assertEqual(times, sorted(times))                       # events are in time order
        self.assertEqual(on_disk["duration_ms"], times[-1])
        self.assertEqual(on_disk["steps"], sum(e["type"] == "step" for e in on_disk["events"]))
        self.assertEqual(set(on_disk["phones"]), {"emulator", "games"})
        for e in on_disk["events"]:
            self.assertIn(e["type"], ("message", "card", "job", "step"))
            if e["type"] == "step":
                self.assertRegex(e["shot"], SAFE_IMG)
                self.assertTrue((self.dir / "sessions" / "demo" / e["shot"]).exists())
                self.assertTrue(e["caption"] and e["thought"])
            if e["type"] == "message" and e.get("card"):
                self.assertIn(e["card"]["kind"], ("choice", "confirm", "handoff", "text", "secret", "otp"))
        # every card that is answered later exists first
        opened = {e["card"]["id"] for e in on_disk["events"] if e["type"] == "message" and e.get("card")}
        self.assertTrue({e["id"] for e in on_disk["events"] if e["type"] == "card"} <= opened)

    def test_the_index_lists_every_session_sorted_by_name(self):
        ex.export_demo(self.dir / "s" / "zeta")
        ex.export_demo(self.dir / "s" / "alpha")
        idx = json.loads((self.dir / "s" / "index.json").read_text())
        self.assertEqual([s["name"] for s in idx["sessions"]], ["alpha", "zeta"])
        self.assertTrue(all(s["synthetic"] and s["steps"] == 6 for s in idx["sessions"]))

    def test_exporting_twice_gives_identical_files(self):
        ex.export_demo(self.dir / "a" / "demo")
        ex.export_demo(self.dir / "b" / "demo")
        self.assertEqual(tree_hash(self.dir / "a" / "demo"), tree_hash(self.dir / "b" / "demo"))

    def test_a_re_export_leaves_no_stale_pictures(self):
        out = self.dir / "s" / "demo"
        ex.export_demo(out)
        (out / "img" / "old.jpg").write_bytes(b"x")
        ex.export_demo(out)
        self.assertFalse((out / "img" / "old.jpg").exists())

    def test_the_cli_demo_mode_runs(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ex.main(["--demo", "--out", str(self.dir / "s" / "demo")]), 0)
        self.assertTrue((self.dir / "s" / "demo" / "session.json").exists())


class Check(unittest.TestCase):
    def test_the_check_passes_a_clean_session_and_flags_leaks_and_bad_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "replay"
            ex.export_demo(root / "demo")
            self.assertEqual(ex.check_folder(root), [])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ex.main(["--check", str(root)]), 0)
            sj = root / "demo" / "session.json"
            data = json.loads(sj.read_text())
            data["events"][1]["text"] = "mail me at someone@example.com from 10.0.0.7"
            sj.write_text(json.dumps(data))
            (root / "demo" / "img" / "big.png").write_bytes(png(10, 10))
            problems = ex.check_folder(root)
            self.assertTrue(any("unmasked" in p for p in problems))
            self.assertTrue(any("big.png" in p for p in problems))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ex.main(["--check", str(root)]), 1)


class RealDatabase(unittest.TestCase):
    """Export from a database with the app's real schema, with things that must never be published in it."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.dbfile = self.dir / "app.db"
        db = Database(str(self.dbfile))
        bot = db.create_bot("Assistant", "")["id"]
        self.bot = bot
        photo = self.dir / "uploads" / "p1.jpg"
        photo.parent.mkdir()
        buf = io.BytesIO()
        Image.new("RGB", (1200, 800), (200, 30, 30)).save(buf, "JPEG")
        photo.write_bytes(buf.getvalue())
        db.conn.execute("insert into attachments(id,bot_id,message_id,mime,path,w,h,bytes,created) values(?,?,?,?,?,?,?,?,?)", ("p1", bot, None, "image/jpeg", str(photo), 1200, 800, len(buf.getvalue()), 0))
        db.conn.commit()
        db.add_message(bot, "user", "text", "email my report to boss.person@company.com please", {"attachments": [{"id": "p1", "w": 1200, "h": 800}]})
        job = db.create_job(bot, f"Open the report from {PRIVATE_DIR}/secret.pdf and send it to boss.person@company.com")
        trace = self.dir / "runs" / job["id"]
        trace.mkdir(parents=True)
        (trace / "step01.png").write_bytes(png(720, 1280))
        (trace / "step01.json").write_text(json.dumps({"prompt": "UI elements:\n2: 'Send' at (1,2)\n", "reply": {"thought": "Sending to boss.person@company.com", "action": {"type": "tap", "element": 2}}}))
        (trace / "timing.jsonl").write_text(json.dumps({"step": 1, "observe": 200, "model": 2000, "act": 300, "settle": 500}) + "\n")
        db.update_job(job["id"], status="done", ended=job["started"] + 4, result="Sent to boss.person@company.com", trace_dir=str(trace))
        item = db.create_item(bot, job["id"], "confirm", {"kind": "confirm", "question": "Send it to boss.person@company.com?", "options": []})
        db.add_message(bot, "assistant", "action_item", "Send it to boss.person@company.com?", {"item_id": item["id"], "status": "open", "kind": "confirm", "question": "Send it?"})
        db.close_item(item["id"], "answered", "Approved")
        db.add_message(bot, "assistant", "text", "Done. I used token dev_Zk3P9xQw1LmN8rTy5VbC2aSdFgHj.")
        db.conn.close()
        self.before = hashlib.sha256(self.dbfile.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def run_export(self, *extra) -> dict:
        out = self.dir / "web" / "replay" / "mine"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ex.main([self.bot, "--db", str(self.dbfile), "--out", str(out), "--max-gap", "5", *extra]), 0)
        return json.loads((out / "session.json").read_text())

    def test_nothing_private_survives_in_any_exported_file(self):
        self.run_export("--mask", "Dharun")
        text = "".join(p.read_text() for p in (self.dir / "web" / "replay" / "mine").rglob("*.json"))
        for secret in ("boss.person", "company.com", PRIVATE_DIR, "dev_Zk3P", "Dharun"):
            self.assertNotIn(secret, text)
        self.assertIn("[email]", text)

    def test_the_live_database_file_is_not_modified(self):
        self.run_export()
        self.assertEqual(hashlib.sha256(self.dbfile.read_bytes()).hexdigest(), self.before)

    def test_owner_pictures_are_a_placeholder_unless_public(self):
        m = self.run_export()
        first = next(e for e in m["events"] if e["type"] == "message" and e["role"] == "user")
        self.assertEqual(first["images"], [{"src": "img/att-01.jpg", "hidden": True}])
        img = Image.open(self.dir / "web" / "replay" / "mine" / "img" / "att-01.jpg")
        self.assertEqual(img.getpixel((50, 150))[0] < 100, True)  # grey, not the red photo
        m2 = self.run_export("--public-images")
        first2 = next(e for e in m2["events"] if e["type"] == "message" and e["role"] == "user")
        self.assertEqual(first2["images"][0]["hidden"], False)
        pub = Image.open(self.dir / "web" / "replay" / "mine" / "img" / "att-01.jpg")
        self.assertLessEqual(pub.width, 360)
        self.assertGreater(pub.getpixel((10, 10))[0], 150)  # the red photo

    def test_the_card_the_answer_the_job_and_the_step_are_exported_in_order(self):
        m = self.run_export()
        kinds = [(e["type"], e.get("status") or e.get("kind")) for e in m["events"]]
        self.assertIn(("job", "running"), kinds)
        self.assertIn(("step", None), kinds)
        self.assertIn(("job", "done"), kinds)
        self.assertEqual([e for e in m["events"] if e["type"] == "card"][0]["answer"], "Approved")
        step = next(e for e in m["events"] if e["type"] == "step")
        self.assertEqual(step["caption"], "tap “Send”")
        self.assertEqual(step["phone"], "emulator")
        self.assertTrue(all(b - a <= 5000 for a, b in zip([e["t"] for e in m["events"]], [e["t"] for e in m["events"]][1:])))  # --max-gap 5

    def test_a_redact_region_is_applied_to_every_screenshot(self):
        self.run_export("--redact", "0,0,1,1")
        img = Image.open(next((self.dir / "web" / "replay" / "mine" / "img").glob("mine-*.jpg"))).convert("L")
        self.assertLess(img.getextrema()[1], 20)

    def test_a_missing_bot_or_range_stops_with_a_clear_message(self):
        with self.assertRaises(SystemExit):
            ex.main(["nope", "--db", str(self.dbfile), "--out", str(self.dir / "x" / "y")])
        with self.assertRaises(SystemExit):
            ex.main([self.bot, "--db", str(self.dbfile), "--out", str(self.dir / "x" / "y"), "--from-msg", "9999"])


class Player(unittest.TestCase):
    """web/replay.html is one static file: check it stays offline, safe and wired to the exporter's format."""
    HTML = (ROOT / "web" / "replay.html").read_text()

    def test_it_cannot_reach_the_network_beyond_its_own_files(self):
        self.assertIn("default-src 'none'", self.HTML)
        self.assertIn("connect-src 'self'", self.HTML)
        self.assertNotRegex(self.HTML, r"(src|href)=[\"']https?://")
        self.assertNotRegex(self.HTML, r"\burl\(\s*[\"']?https?://")
        self.assertNotIn("@import", self.HTML)

    def test_it_never_builds_html_from_session_text(self):
        for risky in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(risky, self.HTML)

    def test_it_only_loads_safe_names_and_pictures(self):
        self.assertIn("SAFE_IMG", self.HTML)
        self.assertIn("(?!\\.)", self.HTML)  # no ".." as a session name

    def test_it_reads_the_files_the_exporter_writes(self):
        for needle in ("replay/index.json", "/session.json", "sess.version !== 1", "duration_ms", "e.type === \"step\"", "e.type === \"card\"", "e.type === \"job\""):
            self.assertIn(needle, self.HTML)

    def test_it_has_the_controls_and_the_banner_the_plan_asks_for(self):
        for needle in ("Replay of a recorded session", "no live phone, no model calls", '"Restart"', '"Speed " + x', 'scrub.type = "range"', 'setAttribute("role", "log")', "All replays"):
            self.assertIn(needle, self.HTML)


if __name__ == "__main__":
    unittest.main()
