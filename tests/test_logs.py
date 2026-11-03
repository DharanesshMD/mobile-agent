import json
import logging
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from agent import logs
from test_chat import Spark, act, setup, until


def read(d: Path, name: str) -> str:
    return (d / f"{name}.log").read_text()


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()) / "logs"
        logs.setup_logging(self.d, level="INFO", console=False)
        self.addCleanup(logs.teardown_logging)
        self.addCleanup(logging.getLogger("agent").setLevel, logging.NOTSET)


class Redaction(unittest.TestCase):
    def test_secrets_never_survive(self):
        cases = [
            ("Authorization: Bearer abcDEF123456789xyz", "abcDEF123456789xyz"),
            ("GET /x?token=hunter2hunter2&after=3", "hunter2hunter2"),
            ("calling with api_key=sk-live-123456789012345678", "sk-live-1234"),
            ("token a2a_" + "Ab1" * 14, "Ab1Ab1Ab1Ab1"),
            ("a long secret " + "Qz9_" * 14, "Qz9_Qz9_Qz9_"),
            ("my card is 4111 1111 1111 1111", "4111 1111"),
            ("password is hunter2hunter2", "hunter2hunter2"),
            ("signed in as owner.name@gmail.com", "owner.name@gmail.com"),
        ]
        for text, secret in cases:
            self.assertNotIn(secret, logs.redact(text), text)
        self.assertIn("o***@gmail.com", logs.redact("signed in as owner.name@gmail.com"))
        self.assertEqual(logs.redact(""), "")
        self.assertEqual(logs.redact("plain words only"), "plain words only")

    def test_query_strings_keep_keys_not_secret_values(self):
        q = logs.clean_query("token=abc&after=7&code=1234&x=" + "y" * 100)
        self.assertEqual(q, "token=***&after=7&code=***&x=" + "y" * 30)


class Files(Base):
    def test_layout_permissions_and_routing(self):
        self.assertEqual(stat.S_IMODE(self.d.stat().st_mode), 0o700)
        for n in logs.FILES:
            self.assertEqual(stat.S_IMODE((self.d / f"{n}.log").stat().st_mode), 0o600, n)
        with logs.bind(bot="b1", job="j9"):
            logging.getLogger("agent.jobs").info("started goal=open settings")
            logging.getLogger("agent.orch").info("decided mobile_task")
        logging.getLogger("agent.access").info("GET /api/x 200 3ms")
        logging.getLogger("agent.audit").info("bot created name=Shopper")
        logging.getLogger("agent.model").warning("HTTP 503 retrying")
        logging.getLogger("agent.phone").info("woke up")
        logging.getLogger("somelib").error("a library broke")
        logs.event("agent.a2a", "SendMessage ok", client="c1", task="t1", ok=True)
        j, a, e = read(self.d, "jobs"), read(self.d, "app"), read(self.d, "errors")
        self.assertIn("[bot=b1 job=j9] started goal=open settings", j)
        self.assertIn("decided mobile_task", j)
        self.assertNotIn("GET /api/x", j + a)  # requests only in access.log
        self.assertIn("GET /api/x 200", read(self.d, "access"))
        self.assertIn("bot created name=Shopper", read(self.d, "audit"))
        self.assertIn("woke up", a)
        self.assertIn("HTTP 503 retrying", e)
        self.assertIn("a library broke", e)
        self.assertNotIn("woke up", e)  # errors.log is WARNING and up
        row = json.loads(read(self.d, "a2a").strip().splitlines()[-1])
        self.assertEqual((row["client"], row["task"], row["ok"], row["msg"]), ("c1", "t1", True, "SendMessage ok"))

    def test_setup_twice_does_not_duplicate_lines(self):
        logs.setup_logging(self.d, console=False)
        logging.getLogger("agent.jobs").info("only once")
        self.assertEqual(read(self.d, "jobs").count("only once"), 1)

    def test_every_line_is_redacted_even_if_the_call_site_forgot(self):
        logging.getLogger("agent.jobs").info("sending Authorization: Bearer secretsecretsecret to me@gmail.com")
        logging.getLogger("agent.a2a").info("x", extra={"fields": {"detail": "token=abcdef123456"}})
        text = read(self.d, "jobs") + read(self.d, "app") + read(self.d, "a2a")
        for leak in ("secretsecretsecret", "me@gmail.com", "abcdef123456"):
            self.assertNotIn(leak, text)

    def test_crashes_are_captured(self):
        def boom():
            raise ValueError("thread went wrong")
        t = threading.Thread(target=boom, name="worker-x")
        t.start()
        t.join()
        try:
            raise KeyError("handled but logged")
        except KeyError:
            logging.getLogger("agent").exception("turn failed")
        e = read(self.d, "errors")
        self.assertIn("thread worker-x crashed", e)
        self.assertIn("ValueError: thread went wrong", e)
        self.assertIn("Traceback", e)
        self.assertIn("KeyError", e)

    def test_files_rotate(self):
        old = logs.MAX_BYTES
        logs.MAX_BYTES = 2000
        self.addCleanup(setattr, logs, "MAX_BYTES", old)
        logs.teardown_logging()
        d = self.d.parent / "small"
        logs.setup_logging(d, console=False)
        for i in range(80):
            logging.getLogger("agent.jobs").info("line %d %s", i, "x" * 60)
        self.assertTrue((d / "jobs.log.1").exists())
        self.assertLessEqual((d / "jobs.log").stat().st_size, 2200)


class Tail(Base):
    def test_filters_and_multiline_records(self):
        log = logging.getLogger("agent.jobs")
        log.info("first info")
        log.warning("a warning about phone")
        try:
            1 / 0
        except ZeroDivisionError:
            log.error("job exploded", exc_info=True)
        log.info("last info")
        self.assertEqual(len(logs.tail("jobs", 50)), 4)
        errs = logs.tail("jobs", 50, level="warning")
        self.assertEqual(len(errs), 2)
        self.assertIn("ZeroDivisionError", errs[-1])  # the traceback stays with its record
        self.assertEqual(len(logs.tail("jobs", 50, contains="PHONE")), 1)
        self.assertEqual(len(logs.tail("jobs", 2)), 2)
        self.assertEqual(logs.tail("audit", 10), [])
        with self.assertRaises(KeyError):
            logs.tail("passwd")

    def test_tail_redacts_what_is_already_in_the_file(self):
        with open(self.d / "jobs.log", "a") as f:
            f.write("2026-11-03 10:00:00.000 INFO  agent.jobs old line Authorization: Bearer leakedleakedleaked\n")
        self.assertNotIn("leakedleakedleaked", " ".join(logs.tail("jobs", 5)))


class Requests(Base):
    def make(self):
        app = FastAPI()
        app.add_middleware(logs.AccessLog, listener="test")

        @app.get("/ok")
        def ok():
            return {"ok": True}

        @app.get("/denied")
        def denied():
            from fastapi import HTTPException
            raise HTTPException(401, "no")

        @app.get("/crash")
        def crash():
            raise RuntimeError("handler bug")

        @app.get("/stream")
        def stream():
            def gen():
                for i in range(3):
                    yield f"data: {i}\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream")

        @app.get("/who")
        def who():
            logging.getLogger("agent").info("inside the handler")
            return {}

        return TestClient(app, raise_server_exceptions=False)

    def test_one_line_per_request_with_an_id_and_no_secret(self):
        c = self.make()
        r = c.get("/ok?token=s3cr3tvalue&after=4")
        rid = r.headers["x-request-id"]
        self.assertEqual(len(rid), 8)
        self.assertEqual(c.get("/denied").status_code, 401)
        self.assertEqual(c.get("/crash").status_code, 500)
        self.assertEqual(c.get("/stream").text, "data: 0\n\ndata: 1\n\ndata: 2\n\n")  # streaming passes through untouched
        a = read(self.d, "access")
        self.assertIn("GET /ok?token=***&after=4 200", a)
        self.assertNotIn("s3cr3tvalue", read(self.d, "app") + a)
        self.assertIn("via=test", a)
        self.assertRegex(a, r"GET /denied 401")
        self.assertIn("WARNING", [ln.split()[2] for ln in a.splitlines() if "/denied" in ln][0])
        self.assertIn("GET /crash -> crashed", a)
        self.assertIn("RuntimeError: handler bug", read(self.d, "errors"))
        c.get("/who")
        self.assertRegex(read(self.d, "app"), r"agent \[req=[0-9a-f]{8}\] inside the handler")  # the request id follows the work done for it

    def test_routine_polling_is_debug_only(self):
        c = self.make()
        app = c.app

        @app.get("/api/mobile/state")
        def st():
            return {}
        c.get("/api/mobile/state")
        self.assertNotIn("/api/mobile/state", read(self.d, "access"))  # INFO level hides the UI's polling


class InTheApp(Base):
    def test_no_message_text_or_token_reaches_any_file(self):
        spark = Spark(act(type="reply", text="the answer is 42 secret-reply-text"))
        c, _, bot, app = setup(spark)
        c.post(f"/api/bots/{bot}/messages", json={"text": "my private plan is zebra-quartz-77"})
        until(lambda: "the answer" in json.dumps(c.get(f"/api/bots/{bot}/state").json()))
        r = c.post("/api/a2a/clients", json={"name": "Zed", "scopes": ["ask"], "rate_per_min": 60})
        token = r.json()["token"]
        c.post("/api/connectors", json={"custom": {"name": "Bad", "url": "http://example.com/mcp"}})
        time.sleep(0.5)
        everything = "".join(read(self.d, n) for n in logs.FILES)
        for leak in ("zebra-quartz-77", "secret-reply-text", token):
            self.assertNotIn(leak, everything)
        self.assertIn("turn done", read(self.d, "jobs"))
        self.assertIn("decided reply", read(self.d, "jobs"))
        self.assertIn("A2A caller created", read(self.d, "audit"))
        self.assertIn("name=Zed", read(self.d, "audit"))

    def test_the_owner_can_read_logs_through_the_api(self):
        c, _, bot, app = setup(Spark())
        c.post("/api/bots", json={"name": "Reader"})
        d = c.get("/api/logs").json()
        self.assertEqual({f["name"] for f in d["files"]}, set(logs.FILES))
        self.assertTrue(d["dir"].endswith("logs"))
        r = c.get("/api/logs/audit", params={"lines": 5, "q": "reader"}).json()
        self.assertTrue(any("bot created" in ln for ln in r["lines"]))
        self.assertEqual(c.get("/api/logs/secrets").status_code, 404)
        self.assertEqual(c.get("/api/logs/app", params={"lines": 5000}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
