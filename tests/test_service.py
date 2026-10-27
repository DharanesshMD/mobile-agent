import io
import json
import time
import unittest

from fastapi.testclient import TestClient
from PIL import Image

from agent.device import Element
from agent.service import create_app


def png(color: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1080, 2400), (color, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


class Phone:
    def __init__(self):
        self.i, self.taps, self.typed, self.keys = 0, [], [], []

    def size(self):
        return 1080, 2400

    def screenshot(self):
        return png(self.i % 200)

    def elements(self):
        return [Element(0, "Battery", (540, 1800), True)]

    def tap(self, x, y):
        self.taps.append((x, y)); self.i += 1

    def type_text(self, text):
        self.typed.append(text); self.i += 1

    def key(self, name):
        self.keys.append(name); self.i += 1

    def swipe(self, *a):
        self.i += 1


class Script:
    def __init__(self, *actions):
        self.replies = [json.dumps({"thought": "t", "action": a}) for a in actions]
        self.tokens = {"prompt": 0, "completion": 0}

    def complete(self, system, prompt, image=None, max_tokens=0):
        return self.replies.pop(0)


def client(*actions, token=None):
    phone = Phone()
    app = create_app(phone, lambda m: Script(*actions), token=token, settle=0)
    return TestClient(app), phone


def wait_for(c, rid, pred, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        r = c.get(f"/api/runs/{rid}").json()
        if pred(r):
            return r
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state: {r['status']}")


class ServiceTests(unittest.TestCase):
    def test_run_completes_and_logs_steps(self):
        c, phone = client({"type": "tap", "element": 0}, {"type": "done", "answer": "100%"})
        rid = c.post("/api/runs", json={"goal": "battery"}).json()["id"]
        r = wait_for(c, rid, lambda r: r["status"] == "done")
        self.assertEqual(r["answer"], "100%")
        self.assertEqual([e["type"] for e in r["log"] if e["type"] in ("step", "final")], ["step", "step", "final"])
        self.assertEqual(phone.taps, [(540, 1800)])

    def test_approval_flow(self):
        c, _ = client({"type": "ask", "question": "Send it?"}, {"type": "done", "answer": "sent"})
        rid = c.post("/api/runs", json={"goal": "g"}).json()["id"]
        r = wait_for(c, rid, lambda r: r["status"] == "waiting")
        self.assertEqual(r["pending"], {"kind": "ask", "text": "Send it?"})
        self.assertEqual(c.post(f"/api/runs/{rid}/resume", json={}).status_code, 422)
        self.assertEqual(c.post(f"/api/runs/{rid}/resume", json={"approved": True}).status_code, 200)
        self.assertEqual(wait_for(c, rid, lambda r: r["status"] == "done")["answer"], "sent")

    def test_denied_ask_ends_run(self):
        c, _ = client({"type": "ask", "question": "Delete?"})
        rid = c.post("/api/runs", json={"goal": "g"}).json()["id"]
        wait_for(c, rid, lambda r: r["status"] == "waiting")
        c.post(f"/api/runs/{rid}/resume", json={"approved": False})
        wait_for(c, rid, lambda r: r["status"] == "denied")

    def test_otp_goes_to_phone_and_never_into_the_log(self):
        c, phone = client({"type": "request_otp", "app": "Gmail"}, {"type": "done", "answer": "in"})
        rid = c.post("/api/runs", json={"goal": "g"}).json()["id"]
        wait_for(c, rid, lambda r: r["status"] == "waiting")
        self.assertEqual(c.post(f"/api/runs/{rid}/resume", json={"code": "12ab"}).status_code, 422)
        self.assertEqual(c.post(f"/api/runs/{rid}/resume", json={"code": "482913"}).status_code, 200)
        r = wait_for(c, rid, lambda r: r["status"] == "done")
        self.assertEqual(phone.typed, ["482913"])
        self.assertNotIn("482913", json.dumps(r) + json.dumps(c.get("/api/runs").json()))

    def test_takeover_only_when_agent_is_paused(self):
        c, phone = client({"type": "handoff", "reason": "Sign in"}, {"type": "done", "answer": "ok"})
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.5}).status_code, 200)  # idle: allowed
        rid = c.post("/api/runs", json={"goal": "g"}).json()["id"]
        wait_for(c, rid, lambda r: r["status"] == "waiting")
        self.assertEqual(c.post("/api/device/tap", json={"fx": 0.5, "fy": 0.25}).status_code, 200)  # handoff: allowed
        self.assertEqual(phone.taps[-1], (540, 600))
        self.assertEqual(c.post("/api/device/type", json={"text": "me@example.com"}).status_code, 200)
        c.post(f"/api/runs/{rid}/resume", json={"done": True})
        wait_for(c, rid, lambda r: r["status"] == "done")

    def test_second_run_is_rejected_while_one_is_active_and_cancel_works(self):
        c, _ = client({"type": "ask", "question": "ok?"})
        rid = c.post("/api/runs", json={"goal": "g"}).json()["id"]
        wait_for(c, rid, lambda r: r["status"] == "waiting")
        self.assertEqual(c.post("/api/runs", json={"goal": "again"}).status_code, 409)
        c.post(f"/api/runs/{rid}/cancel")
        wait_for(c, rid, lambda r: r["status"] == "cancelled")

    def test_token_is_required_when_configured(self):
        c, _ = client(token="s3cret")
        self.assertEqual(c.get("/api/device/screenshot").status_code, 401)
        self.assertEqual(c.get("/api/device/screenshot", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(c.get("/api/device/screenshot", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
        self.assertEqual(c.get("/api/device/screenshot?token=s3cret").status_code, 200)
        self.assertEqual(c.get("/api/health").status_code, 200)

    def test_screenshot_is_a_small_jpeg_and_unknown_model_rejected(self):
        c, _ = client()
        r = c.get("/api/device/screenshot")
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        self.assertLess(Image.open(io.BytesIO(r.content)).width, 541)
        self.assertEqual(c.post("/api/runs", json={"goal": "g", "model": "gpt-9"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
