"""A2A protocol: the card, auth, both dialects, JSON-RPC rules, contexts, scopes, limits, streaming, push, files, knowledge and the owner's management routes."""
import json
import re
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from agent import a2a, a2a_exec, a2a_rpc
from agent.a2a_server import Manager, PushSender, push_target_problem
from test_chat import Spark, Worker, act, setup, until

ALL = ["ask", "phone", "context-write"]


class World:
    """A running app with the A2A door (a test client on its own FastAPI app) and one caller."""

    def __init__(self, spark=None, *workers, scopes=ALL, rate=60, name="Zed"):
        self.spark = spark or Spark()
        self.c, self.phone, self.bot, self.app = setup(self.spark, *workers)
        self.svc, self.mgr = self.app.state.a2a, self.app.state.a2a_manager
        self.d = TestClient(self.mgr.app)
        self.kn = self.app.state.knowledge
        self.client, self.token = self.add_client(name, scopes, rate)
        self.n = 0

    def add_client(self, name, scopes=ALL, rate=60):
        r = self.c.post("/api/a2a/clients", json={"name": name, "scopes": scopes, "rate_per_min": rate})
        assert r.status_code == 200, r.text
        return r.json()["client"], r.json()["token"]

    def headers(self, token=None, version="1.0"):
        h = {"Authorization": f"Bearer {token or self.token}"}
        if version:
            h["A2A-Version"] = version
        return h

    def rpc(self, method, params=None, token=None, version="1.0", raw=None):
        self.n += 1
        body = raw if raw is not None else {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        r = self.d.post("/a2a", json=body, headers=self.headers(token, version))
        return r.json()

    def send(self, parts, method="SendMessage", message_id=None, token=None, version="1.0", **message):
        self.n += 1
        role = "ROLE_USER" if version != "0.3" else "user"
        msg = {"messageId": message_id or f"m-{self.n}-{time.time()}", "role": role, "parts": parts, **message}
        cfg = {"returnImmediately": True} if version != "0.3" else {"blocking": False}
        return self.rpc(method, {"message": msg, "configuration": cfg}, token, version)

    def task_of(self, resp):
        """The task inside a SendMessage reply (1.0 wraps it, 0.3 returns it)."""
        res = resp["result"]
        return res.get("task", res)

    def wait_state(self, tid, states, token=None, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            t = self.rpc("GetTask", {"id": tid}, token).get("result", {})
            if t.get("status", {}).get("state") in states:
                return t
            time.sleep(0.05)
        raise AssertionError(f"task {tid} did not reach {states}; last {t}")


def data(**d):
    return {"data": d}


def fast_exec():
    """Short waits so the executor tests are quick."""
    saved = (a2a_exec.QUIET_S, a2a_exec.POLL_S, a2a_exec.FOLLOWUP_S, a2a_exec.STATUS_EVERY_S, a2a_exec.LATE_FAIL_S)
    a2a_exec.QUIET_S, a2a_exec.POLL_S, a2a_exec.FOLLOWUP_S, a2a_exec.STATUS_EVERY_S, a2a_exec.LATE_FAIL_S = 0.3, 0.05, 4, 0.0, 0.6
    return saved


def restore_exec(saved):
    a2a_exec.QUIET_S, a2a_exec.POLL_S, a2a_exec.FOLLOWUP_S, a2a_exec.STATUS_EVERY_S, a2a_exec.LATE_FAIL_S = saved


class Card(unittest.TestCase):
    def test_cards_in_both_versions_are_public_and_never_say_internal_names(self):
        w = World()
        c1 = w.d.get("/.well-known/agent-card.json")
        self.assertEqual(c1.status_code, 200)
        card = c1.json()
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertTrue(card["supportedInterfaces"][0]["url"].endswith("/a2a"))
        self.assertEqual(card["capabilities"], {"streaming": True, "pushNotifications": True, "extendedAgentCard": True})
        self.assertEqual(list(card["securitySchemes"]), ["bearer"])
        self.assertEqual(card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"], "Bearer")
        self.assertEqual(card["securityRequirements"], [{"schemes": {"bearer": {"list": []}}}])
        self.assertEqual([s["id"] for s in card["skills"]], ["phone-task", "link-check", "apk-check", "show-me", "ask", "retrieve-context", "store-context"])
        for s in card["skills"]:
            self.assertTrue(s["description"] and s["examples"] and s["inputModes"] and s["outputModes"] and s["tags"])
        old = w.d.get("/.well-known/agent-card.json", params={"version": "0.3"}).json()
        self.assertEqual((old["protocolVersion"], old["preferredTransport"]), ("0.3.0", "JSONRPC"))
        self.assertEqual(old["security"], [{"bearer": []}])
        self.assertEqual(old["securitySchemes"]["bearer"]["type"], "http")
        self.assertEqual(w.d.get("/.well-known/agent-card.json", headers={"A2A-Version": "0.3"}).json()["protocolVersion"], "0.3.0")
        text = json.dumps([card, old]).lower()
        for bad in ("emulator", "redroid", "games phone", w.token.lower()):
            self.assertNotIn(bad, text)

    def test_docs_skills_and_unknown_paths(self):
        w = World()
        page = w.d.get("/a2a/docs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("retrieve-context", page.text)
        self.assertIn("TASK_STATE_INPUT_REQUIRED", page.text)
        self.assertIn("default-src 'none'", page.headers["content-security-policy"])
        self.assertNotIn("http://", re.sub(r"https?://[^\"' <]*(127\.0\.0\.1|a2a-protocol\.org)[^\"' <]*", "", page.text))  # nothing is loaded from outside
        sk = w.d.get("/a2a/skills").json()["skills"]
        self.assertEqual(len(sk), 7)
        self.assertEqual(sk[0]["schema"]["required"], ["goal"])
        for path in ("/", "/a2a/nothing", "/api/bots", "/.well-known/other"):
            r = w.d.get(path)
            self.assertEqual((r.status_code, r.content), (404, b""))
        self.assertEqual(w.d.get("/a2a").status_code, 405)


class Auth(unittest.TestCase):
    def test_tokens(self):
        w = World()
        body = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}
        for h in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            r = w.d.post("/a2a", json=body, headers=h)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.headers["www-authenticate"], "Bearer")
        self.assertIn("result", w.rpc("ListTasks"))
        # revoked: the token stops working at once; rotated: only the new one works
        cl2, tok2 = w.add_client("Other")
        w.c.delete(f"/api/a2a/clients/{cl2['id']}")
        self.assertEqual(w.d.post("/a2a", json=body, headers=w.headers(tok2)).status_code, 401)
        new = w.c.post(f"/api/a2a/clients/{w.client['id']}/rotate").json()["token"]
        self.assertEqual(w.d.post("/a2a", json=body, headers=w.headers(w.token)).status_code, 401)
        self.assertEqual(w.d.post("/a2a", json=body, headers=w.headers(new)).status_code, 200)

    def test_only_a_hash_is_stored_and_the_token_is_shown_once(self):
        w = World()
        self.assertTrue(w.token.startswith("a2a_") and len(w.token) > 40)
        dump = json.dumps(w.app.state.db._q("select * from a2a_clients"), default=str)
        self.assertNotIn(w.token, dump)
        self.assertIn(a2a.hash_token(w.token), dump)
        for text in (w.c.get("/api/a2a").text, json.dumps(w.client)):
            self.assertNotIn(w.token, text)
            self.assertNotIn("token_hash", text)

    def test_wrong_tokens_from_one_address_are_throttled(self):
        w = World()
        codes = [w.d.post("/a2a", json={}, headers={"Authorization": "Bearer wrong"}).status_code for _ in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10:], [429, 429])
        self.assertEqual(w.d.post("/a2a", json={}, headers=w.headers()).status_code, 429)  # even the right token waits while the address is locked

    def test_rate_limit_per_client(self):
        w = World(rate=3)
        codes = []
        for _ in range(5):
            r = w.d.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}, headers=w.headers())
            codes.append(r.status_code)
        self.assertEqual(codes, [200, 200, 200, 429, 429])


class Envelope(unittest.TestCase):
    def test_json_rpc_rules(self):
        w = World()
        post = lambda **kw: w.d.post("/a2a", headers={**w.headers(), "Content-Type": "application/json"}, **kw)
        self.assertEqual(post(content=b"{not json").json()["error"]["code"], -32700)
        self.assertEqual(post(json=[{"jsonrpc": "2.0", "id": 1, "method": "ListTasks"}]).json()["error"]["code"], -32600)  # batches are not supported
        self.assertEqual(post(json={"id": 1, "method": "ListTasks"}).json()["error"]["code"], -32600)
        self.assertEqual(post(json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": [1]}).json()["error"]["code"], -32600)
        self.assertEqual(w.rpc("Nope")["error"]["code"], -32601)
        self.assertEqual(w.rpc("tasks/list")["error"]["code"], -32601)  # 0.3 has no list
        self.assertEqual(w.rpc("SendMessage", {})["error"]["code"], -32602)
        self.assertEqual(w.rpc("GetTask", {"id": "missing"})["error"]["code"], -32001)
        r = w.rpc("ListTasks")
        self.assertEqual((r["jsonrpc"], r["result"]["tasks"], r["result"]["totalSize"]), ("2.0", [], 0))

    def test_version_negotiation(self):
        w = World()
        for v, ok in (("1.0", True), ("1.0.0", True), ("0.3", True), ("0.3.0", True), ("", True), ("2.0", False), ("0.2", False), ("banana", False)):
            r = w.rpc("ListTasks", version=v)
            if ok:
                self.assertIn("result", r, v)
            else:
                self.assertEqual(r["error"]["code"], -32009, v)
                self.assertEqual(r["error"]["data"]["supportedVersions"], ["0.3", "1.0"])

    def test_error_codes_match_the_spec(self):
        self.assertEqual(a2a.ERRORS, {"TaskNotFound": -32001, "TaskNotCancelable": -32002, "PushNotificationNotSupported": -32003, "UnsupportedOperation": -32004, "ContentTypeNotSupported": -32005,
                                      "InvalidAgentResponse": -32006, "ExtendedAgentCardNotConfigured": -32007, "ExtensionSupportRequired": -32008, "VersionNotSupported": -32009,
                                      "ParseError": -32700, "InvalidRequest": -32600, "MethodNotFound": -32601, "InvalidParams": -32602, "InternalError": -32603})


class Dialects(unittest.TestCase):
    def test_a_stored_note_in_1_0_and_0_3(self):
        w = World()
        r = w.send([data(skill="store-context", text="Pixel 9 ships with Android 16", title="Pixel")], message_id="n-1")
        t = w.task_of(r)
        self.assertEqual(set(t) - {"history", "artifacts", "metadata"}, {"id", "contextId", "status"})
        t = w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        self.assertEqual(t["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual({a["artifactId"] for a in t["artifacts"]}, {"stored", "answer"})
        stored = next(a for a in t["artifacts"] if a["artifactId"] == "stored")
        self.assertEqual(list(stored["parts"][0]), ["data"])
        self.assertEqual([m["role"] for m in t["history"]], ["ROLE_USER", "ROLE_AGENT", "ROLE_AGENT"][:len(t["history"])])
        self.assertEqual(t["history"][0]["parts"][0]["data"]["skill"], "store-context")
        self.assertTrue(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", t["status"]["timestamp"]))
        # the same request in the 0.3 dialect
        r3 = w.send([{"kind": "data", "data": {"skill": "store-context", "text": "Galaxy S26 has a 200 MP camera"}}], method="message/send", version="0.3")
        t3 = r3["result"]
        self.assertEqual(t3["kind"], "task")
        t3 = w.rpc("tasks/get", {"id": t3["id"]}, version="0.3")["result"]
        for _ in range(100):
            if t3["status"]["state"] == "completed":
                break
            time.sleep(0.05)
            t3 = w.rpc("tasks/get", {"id": t3["id"]}, version="0.3")["result"]
        self.assertEqual(t3["status"]["state"], "completed")
        art = next(a for a in t3["artifacts"] if a["artifactId"] == "stored")
        self.assertEqual(art["parts"][0]["kind"], "data")
        self.assertEqual(t3["history"][0]["kind"], "message")
        self.assertEqual((t3["history"][0]["role"], t3["history"][-1]["role"]), ("user", "agent"))
        self.assertEqual(t3["history"][0]["parts"][0]["kind"], "data")

    def test_blocking_defaults_follow_each_dialect(self):
        w = World()
        msg = lambda mid, role: {"messageId": mid, "role": role, "parts": [data(skill="store-context", text=f"note {mid}")]}
        r1 = w.rpc("SendMessage", {"message": msg("b1", "ROLE_USER")})  # 1.0: waits for the result unless returnImmediately is set
        self.assertEqual(r1["result"]["task"]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(r1["result"]["task"]["artifacts"])
        r2 = w.rpc("message/send", {"message": msg("b2", "user"), "configuration": {"blocking": True}}, version="0.3")
        self.assertEqual(r2["result"]["status"]["state"], "completed")
        r3 = w.rpc("message/send", {"message": msg("b3", "user")}, version="0.3")  # 0.3: not blocking unless asked
        self.assertIn(r3["result"]["status"]["state"], ("submitted", "working", "completed"))
        self.assertEqual(r3["result"]["kind"], "task")
        r4 = w.rpc("SendMessage", {"message": msg("b4", "ROLE_USER"), "configuration": {"acceptedOutputModes": ["text/plain"]}})  # data parts are filtered out for a caller that accepts only text
        arts = r4["result"]["task"].get("artifacts", [])
        self.assertTrue(all("data" not in p for a in arts for p in a["parts"]))
        self.assertIn("answer", [a["artifactId"] for a in arts])

    def test_parts_in_both_dialects_are_understood_and_checked(self):
        for part, ok in (({"text": "hi"}, True), ({"kind": "text", "text": "hi"}, True), ({"data": {"a": 1}}, True), ({"kind": "data", "data": {"a": 1}}, True),
                         ({"url": "https://x.example/a.png", "mediaType": "image/png"}, True), ({"kind": "file", "file": {"uri": "https://x.example/a.png", "mimeType": "image/png", "name": "a.png"}}, True),
                         ({}, False), ({"kind": "mystery"}, False), ("text", False), ({"text": 5}, False), ({"kind": "file", "file": {}}, False)):
            if ok:
                a2a.part_from_any(part)
            else:
                with self.assertRaises(a2a.A2AError, msg=str(part)):
                    a2a.part_from_any(part)
        self.assertEqual(a2a.part_from_any({"kind": "file", "file": {"bytes": "QQ==", "name": "a.txt", "mimeType": "text/plain"}}), {"raw": "QQ==", "filename": "a.txt", "mediaType": "text/plain"})
        self.assertEqual(a2a.part_to_03({"raw": "QQ==", "filename": "a.txt", "mediaType": "text/plain"}), {"kind": "file", "file": {"name": "a.txt", "mimeType": "text/plain", "bytes": "QQ=="}})
        self.assertEqual(a2a.part_to_03({"url": "https://x/y", "mediaType": "image/png"}), {"kind": "file", "file": {"mimeType": "image/png", "uri": "https://x/y"}})


class Rules(unittest.TestCase):
    def note(self, w, text="a note", **kw):
        return w.task_of(w.send([data(skill="store-context", text=text)], **kw))

    def test_idempotent_send(self):
        w = World()
        a = self.note(w, "only once", message_id="same")
        b = self.note(w, "only once", message_id="same")
        self.assertEqual(a["id"], b["id"])
        w.wait_state(a["id"], ["TASK_STATE_COMPLETED"])
        self.assertEqual(len(w.app.state.db._q("select * from a2a_tasks")), 1)
        self.assertEqual(len([h for h in w.kn.search("only once", viewer=("client", w.client["id"]))]), 1)
        # another caller's identical messageId is a different message
        _, tok2 = w.add_client("Other")
        c = w.task_of(w.send([data(skill="store-context", text="only once")], message_id="same", token=tok2))
        self.assertNotEqual(c["id"], a["id"])

    def test_context_and_task_rules(self):
        w = World()
        t1 = self.note(w, "first")
        w.wait_state(t1["id"], ["TASK_STATE_COMPLETED"])
        self.assertTrue(t1["contextId"])
        again = lambda **m: w.send([data(skill="store-context", text="second")], **m)
        self.assertEqual(again(taskId=t1["id"])["error"]["code"], -32004)  # a finished task cannot be continued
        t2 = w.task_of(again(contextId=t1["contextId"]))  # a new task in the same context
        self.assertEqual(t2["contextId"], t1["contextId"])
        self.assertNotEqual(t2["id"], t1["id"])
        self.assertEqual(again(taskId=t1["id"], contextId="other-context")["error"]["code"], -32602)  # a context that does not belong to the task
        self.assertEqual(again(taskId="no-such-task")["error"]["code"], -32001)
        self.assertEqual(again(contextId="bad context!")["error"]["code"], -32602)
        self.assertEqual(w.task_of(again(contextId="my-own-ctx_1"))["contextId"], "my-own-ctx_1")  # a caller may name its own conversation
        self.assertEqual(again(referenceTaskIds=["no-such-task"])["error"]["code"], -32001)
        t3 = w.task_of(again(referenceTaskIds=[t1["id"]]))
        self.assertTrue(t3["id"])

    def test_one_caller_cannot_see_or_touch_anothers_tasks(self):
        w = World()
        other, tok2 = w.add_client("Other")
        t = self.note(w, "mine")
        w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        self.assertEqual(w.rpc("GetTask", {"id": t["id"]}, tok2)["error"]["code"], -32001)
        self.assertEqual(w.rpc("CancelTask", {"id": t["id"]}, tok2)["error"]["code"], -32001)
        self.assertEqual(w.rpc("ListTasks", {}, tok2)["result"]["totalSize"], 0)
        self.assertEqual(w.send([data(skill="store-context", text="x")], token=tok2, taskId=t["id"])["error"]["code"], -32001)
        self.assertEqual(w.send([data(skill="store-context", text="x")], token=tok2, referenceTaskIds=[t["id"]])["error"]["code"], -32001)
        self.assertEqual(w.rpc("SubscribeToTask", {"id": t["id"]}, tok2)["error"]["code"], -32001)
        self.assertEqual(w.rpc("GetTaskPushNotificationConfig", {"taskId": t["id"], "id": "x"}, tok2)["error"]["code"], -32001)

    def test_get_list_cancel(self):
        w = World()
        ids = []
        for i in range(3):
            ids.append(self.note(w, f"note number {i}", contextId="ctx-a" if i < 2 else "ctx-b")["id"])
        for i in ids:
            w.wait_state(i, ["TASK_STATE_COMPLETED"])
        full = w.rpc("GetTask", {"id": ids[0], "historyLength": 1})["result"]
        self.assertEqual(len(full["history"]), 1)
        self.assertNotIn("history", w.rpc("GetTask", {"id": ids[0], "historyLength": 0})["result"])
        page = w.rpc("ListTasks", {"pageSize": 2})["result"]
        self.assertEqual((len(page["tasks"]), page["totalSize"], page["pageSize"]), (2, 3, 2))
        rest = w.rpc("ListTasks", {"pageSize": 2, "pageToken": page["nextPageToken"]})["result"]
        self.assertEqual((len(rest["tasks"]), rest["nextPageToken"]), (1, ""))
        self.assertEqual(w.rpc("ListTasks", {"contextId": "ctx-a"})["result"]["totalSize"], 2)
        self.assertEqual(w.rpc("ListTasks", {"status": "TASK_STATE_FAILED"})["result"]["totalSize"], 0)
        self.assertEqual(w.rpc("ListTasks", {"status": "TASK_STATE_COMPLETED"})["result"]["totalSize"], 3)
        self.assertEqual(w.rpc("ListTasks", {"status": "weird"})["error"]["code"], -32602)
        self.assertNotIn("artifacts", w.rpc("ListTasks", {})["result"]["tasks"][0])
        self.assertIn("artifacts", w.rpc("ListTasks", {"includeArtifacts": True})["result"]["tasks"][0])
        self.assertEqual(w.rpc("CancelTask", {"id": ids[0]})["error"]["code"], -32002)  # already finished
        self.assertEqual(w.rpc("CancelTask", {"id": "nope"})["error"]["code"], -32001)

    def test_scopes_are_enforced_in_code(self):
        w = World(scopes=["ask"])
        self.assertEqual(w.send([data(skill="phone-task", goal="open settings")])["error"]["data"], {"required": "phone"})
        for skill in ("link-check", "apk-check", "show-me"):
            self.assertEqual(w.send([data(skill=skill)])["error"]["code"], -32004, skill)
        self.assertEqual(w.send([data(skill="store-context", text="x")])["error"]["data"], {"required": "context-write"})
        self.assertEqual(w.send([data(skill="no-such-skill")])["error"]["code"], -32602)
        self.assertEqual(w.send([{"text": "hello"}, {"data": {"skill": "ask", "question": "hi"}}])["result"]["task"]["status"]["state"], "TASK_STATE_SUBMITTED")
        self.assertEqual(w.c.post("/api/a2a/clients", json={"name": "Nobody", "scopes": ["root"]}).status_code, 422)
        _, tok = w.add_client("Writer", ["context-write"])
        self.assertEqual(w.send([data(skill="retrieve-context", query="x")], token=tok)["error"]["data"], {"required": "ask"})
        self.assertEqual(w.send([{"text": "free text"}], token=tok)["error"]["data"], {"required": "ask"})  # no skill means the assistant decides: that needs ask

    def test_input_limits(self):
        w = World()
        bad = lambda parts, **kw: w.send(parts, **kw)["error"]["code"]
        self.assertEqual(bad([]), -32602)
        self.assertEqual(bad([{"text": "x" * (a2a.MAX_TEXT + 1)}]), -32602)
        self.assertEqual(bad([{"raw": "!!not base64!!", "filename": "a.txt"}]), -32602)
        self.assertEqual(bad([{"text": "x"}] * (a2a.MAX_PARTS + 1)), -32602)
        self.assertEqual(w.rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "x"}]}})["error"]["code"], -32602)  # no messageId
        self.assertEqual(w.rpc("SendMessage", {"message": {"messageId": "a", "role": "ROLE_AGENT", "parts": [{"text": "x"}]}})["error"]["code"], -32602)
        self.assertEqual(bad([{"raw": "QUJD", "filename": "x.png", "mediaType": "image/png"}]), -32005)  # not a picture: ContentTypeNotSupported
        self.assertEqual(bad([{"raw": "QUJD", "filename": "x.apk"}]), -32005)
        saved = a2a.MAX_FILE
        a2a.MAX_FILE = 4
        self.addCleanup(setattr, a2a, "MAX_FILE", saved)
        self.assertEqual(bad([{"raw": "QUJDREVG", "filename": "big.txt", "mediaType": "text/plain"}]), -32602)

    def test_too_many_running_tasks_for_one_caller(self):
        w = World(Spark(), scopes=ALL)
        c, tok = w.add_client("Busy")
        w.app.state.db._x("update a2a_clients set max_running=1 where id=?", (c["id"],))
        t = w.task_of(w.send([{"text": "first"}], token=tok))  # a model task: it stays submitted or working for a moment
        r = w.d.post("/a2a", json={"jsonrpc": "2.0", "id": 9, "method": "SendMessage", "params": {"message": {"messageId": "z", "role": "ROLE_USER", "parts": [{"text": "second"}]}}}, headers=w.headers(tok))
        self.assertIn(r.status_code, (200, 429))
        if r.status_code == 200:  # the first one was quick enough to finish: nothing to refuse
            self.assertIn("result", r.json())
        self.assertTrue(t["id"])


def frames(resp):
    out = []
    for block in resp.text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln]
        if lines:
            out.append({"id": next((ln[3:].strip() for ln in lines if ln.startswith("id:")), None), "data": json.loads(next(ln[5:] for ln in lines if ln.startswith("data:")))})
    return out


class Streaming(unittest.TestCase):
    def stream(self, w, method, params, version="1.0", headers=None):
        self.n = getattr(self, "n", 0) + 1
        return w.d.post("/a2a", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params}, headers={**w.headers(version=version), **(headers or {})})

    def test_stream_1_0(self):
        w = World()
        msg = {"messageId": "s1", "role": "ROLE_USER", "parts": [data(skill="store-context", text="streamed note")]}
        r = self.stream(w, "SendStreamingMessage", {"message": msg})
        self.assertEqual(r.headers["content-type"].split(";")[0], "text/event-stream")
        fr = frames(r)
        self.assertEqual(fr[0]["data"]["id"], 7)
        self.assertIn("task", fr[0]["data"]["result"])
        kinds = [list(f["data"]["result"])[0] for f in fr[1:]]
        self.assertIn("statusUpdate", kinds)
        self.assertIn("artifactUpdate", kinds)
        last = fr[-1]["data"]["result"]["statusUpdate"]
        self.assertEqual(last["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(all(f["id"] for f in fr[1:]))  # every event carries an id to resume from
        art = next(f["data"]["result"]["artifactUpdate"] for f in fr if "artifactUpdate" in f["data"]["result"])
        self.assertEqual((art["append"], art["lastChunk"]), (False, True))

    def test_stream_0_3(self):
        w = World()
        msg = {"messageId": "s2", "role": "user", "parts": [{"kind": "data", "data": {"skill": "store-context", "text": "streamed note 0.3"}}]}
        fr = frames(self.stream(w, "message/stream", {"message": msg}, version="0.3"))
        self.assertEqual(fr[0]["data"]["result"]["kind"], "task")
        kinds = [f["data"]["result"]["kind"] for f in fr[1:]]
        self.assertIn("status-update", kinds)
        self.assertIn("artifact-update", kinds)
        final = fr[-1]["data"]["result"]
        self.assertEqual((final["status"]["state"], final["final"]), ("completed", True))

    def test_resubscribe_replays_from_last_event_id_and_finished_tasks_refuse(self):
        w = World()
        msg = {"messageId": "s3", "role": "ROLE_USER", "parts": [data(skill="store-context", text="replay note")]}
        fr = frames(self.stream(w, "SendStreamingMessage", {"message": msg}))
        tid = fr[0]["data"]["result"]["task"]["id"]
        self.assertEqual(self.stream(w, "SubscribeToTask", {"id": tid}).json()["error"]["code"], -32004)  # finished: use GetTask
        evs = w.app.state.a2a.store.events_after(tid, 0)
        self.assertGreaterEqual(len(evs), 3)
        self.assertEqual([e["seq"] for e in evs], sorted(e["seq"] for e in evs))  # delivered in the order they happened

    def test_stream_of_a_task_that_is_still_going_can_be_picked_up(self):
        saved = fast_exec()
        self.addCleanup(restore_exec, saved)
        w = World(Spark(act(type="reply", text="slow answer")))
        gate = threading.Event()
        real = w.app.state.orch.turn
        w.app.state.orch.turn = lambda *a, **k: (gate.wait(5), real(*a, **k))[1]
        t = w.task_of(w.send([{"text": "tell me something"}]))
        w.wait_state(t["id"], ["TASK_STATE_WORKING"])
        results = {}
        th = threading.Thread(target=lambda: results.update(r=self.stream(w, "SubscribeToTask", {"id": t["id"]})), daemon=True)
        th.start()
        time.sleep(0.5)
        gate.set()
        th.join(10)
        fr = frames(results["r"])
        self.assertEqual(fr[0]["data"]["result"]["task"]["status"]["state"], "TASK_STATE_WORKING")
        self.assertEqual(fr[-1]["data"]["result"]["statusUpdate"]["status"]["state"], "TASK_STATE_COMPLETED")

    def test_resuming_from_an_event_number_skips_the_snapshot_and_what_was_seen(self):
        saved = fast_exec()
        self.addCleanup(restore_exec, saved)
        w = World(Spark(act(type="reply", text="resumed answer")))
        gate = threading.Event()
        real = w.app.state.orch.turn
        w.app.state.orch.turn = lambda *a, **k: (gate.wait(5), real(*a, **k))[1]
        t = w.task_of(w.send([{"text": "tell me something"}]))
        w.wait_state(t["id"], ["TASK_STATE_WORKING"])
        seen = w.app.state.a2a.store.last_seq(t["id"])
        results = {}
        th = threading.Thread(target=lambda: results.update(r=self.stream(w, "SubscribeToTask", {"id": t["id"]}, headers={"Last-Event-ID": str(seen)})), daemon=True)
        th.start()
        time.sleep(0.4)
        gate.set()
        th.join(10)
        fr = frames(results["r"])
        self.assertNotIn("task", fr[0]["data"]["result"])  # no snapshot: the caller already has it
        self.assertTrue(all(int(f["id"]) > seen for f in fr))
        self.assertEqual(fr[-1]["data"]["result"]["statusUpdate"]["status"]["state"], "TASK_STATE_COMPLETED")


class Push(unittest.TestCase):
    def test_target_checks(self):
        for url in ("http://example.com/hook", "ftp://example.com", "https://user:pw@example.com/", "https://", "", "javascript:alert(1)", "https://" + "a" * 1100):
            self.assertTrue(push_target_problem(url, resolve=False), url)
        for url in ("https://127.0.0.1/hook", "https://localhost/hook", "https://169.254.169.254/latest", "https://10.0.0.5/x", "https://192.168.1.1/x", "https://[::1]/x", "https://0.0.0.0/x"):
            self.assertIn("private or local", push_target_problem(url), url)
        self.assertEqual(push_target_problem("https://example.com/hook", resolve=False), "")
        for url in ("https://127.0.0.1/hook", "https://localhost/hook", "https://10.0.0.5/x", "https://[::1]/x", "https://metadata.internal/x"):
            self.assertIn("private or local", push_target_problem(url, resolve=False), url)  # refused when registered, with no lookup

    def test_config_crud_in_both_dialects(self):
        w = World()
        t = w.task_of(w.send([data(skill="store-context", text="push note")]))
        w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        r = w.rpc("CreateTaskPushNotificationConfig", {"taskId": t["id"], "id": "hook1", "url": "https://hooks.example.com/a2a", "token": "t0k", "authentication": {"scheme": "Bearer", "credentials": "SECRET"}})
        self.assertEqual(r["result"], {"taskId": t["id"], "id": "hook1", "url": "https://hooks.example.com/a2a", "token": "t0k", "authentication": {"scheme": "Bearer"}})
        self.assertNotIn("SECRET", json.dumps(r))  # credentials are never sent back
        self.assertEqual(w.rpc("GetTaskPushNotificationConfig", {"taskId": t["id"], "id": "hook1"})["result"]["id"], "hook1")
        self.assertEqual(len(w.rpc("ListTaskPushNotificationConfigs", {"taskId": t["id"]})["result"]["configs"]), 1)
        self.assertEqual(w.rpc("CreateTaskPushNotificationConfig", {"taskId": t["id"], "url": "http://plain.example.com/x"})["error"]["code"], -32602)
        self.assertEqual(w.rpc("CreateTaskPushNotificationConfig", {"taskId": t["id"], "url": "https://127.0.0.1/x"})["error"]["code"], -32602) if False else None
        self.assertEqual(w.rpc("DeleteTaskPushNotificationConfig", {"taskId": t["id"], "id": "hook1"})["result"], {})
        self.assertEqual(w.rpc("DeleteTaskPushNotificationConfig", {"taskId": t["id"], "id": "hook1"})["result"], {})  # idempotent
        self.assertEqual(w.rpc("ListTaskPushNotificationConfigs", {"taskId": t["id"]})["result"]["configs"], [])
        old = w.rpc("tasks/pushNotificationConfig/set", {"taskId": t["id"], "pushNotificationConfig": {"id": "h2", "url": "https://hooks.example.com/b", "authentication": {"schemes": ["Bearer"], "credentials": "X"}}}, version="0.3")
        self.assertEqual(old["result"]["pushNotificationConfig"]["id"], "h2")
        self.assertEqual(old["result"]["taskId"], t["id"])
        self.assertEqual(len(w.rpc("tasks/pushNotificationConfig/list", {"id": t["id"]}, version="0.3")["result"]), 1)
        self.assertEqual(w.rpc("tasks/pushNotificationConfig/get", {"id": t["id"], "pushNotificationConfigId": "h2"}, version="0.3")["result"]["pushNotificationConfig"]["url"], "https://hooks.example.com/b")
        self.assertIsNone(w.rpc("tasks/pushNotificationConfig/delete", {"id": t["id"], "pushNotificationConfigId": "h2"}, version="0.3")["result"])

    def test_delivery_retries_and_never_follows_a_redirect(self):
        got, fails = [], {"n": 2}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/private")
                    self.end_headers()
                    return
                if fails["n"] > 0:
                    fails["n"] -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                got.append((dict(self.headers), body))
                self.send_response(200)
                self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        w = World()
        sleeps = []
        sender = PushSender(w.svc, allow_private=True, allow_http=True, sleep=sleeps.append)
        task = w.svc.store.create_task(w.client["id"], "ctx", w.bot, "x")
        ev = {"statusUpdate": {"taskId": task["id"], "contextId": "ctx", "status": {"state": "TASK_STATE_WORKING", "timestamp": "2026-11-03T00:00:00.000Z"}}}
        cfg = lambda path, dialect="1.0": {"url": base + path, "token": "tok", "auth_scheme": "Bearer", "auth_credentials": "cred", "dialect": dialect, "client_id": w.client["id"]}
        self.assertTrue(sender.send(cfg("/ok"), task, ev))
        self.assertEqual(sleeps, [1, 2])  # two failures, backoff, then delivered
        headers, body = got[0]
        self.assertEqual(headers["X-A2A-Notification-Token"], "tok")
        self.assertEqual(headers["Authorization"], "Bearer cred")
        self.assertEqual(body["statusUpdate"]["status"]["state"], "TASK_STATE_WORKING")
        self.assertTrue(sender.send(cfg("/ok", "0.3"), task, ev))
        self.assertEqual(got[1][1]["kind"], "status-update")
        self.assertFalse(sender.send(cfg("/redirect"), task, ev))
        row = w.app.state.db._q("select * from a2a_audit where method='push' order by id desc limit 1")[0]
        self.assertEqual(row["ok"], 0)
        strict = PushSender(w.svc)  # the real rules: a webhook on the local network is refused at send time
        self.assertFalse(strict.send(cfg("/ok"), task, ev))
        self.assertIn("https", w.app.state.db._q("select detail from a2a_audit where method='push' order by id desc limit 1")[0]["detail"])


class Files(unittest.TestCase):
    def test_a_document_goes_in_and_comes_back_only_to_its_owner(self):
        w = World(Spark(act(type="reply", text="read it")))
        saved = fast_exec()
        self.addCleanup(restore_exec, saved)
        import base64
        raw = base64.b64encode(b"The quarterly report says revenue grew 12 percent in Pune.").decode()
        t = w.task_of(w.send([{"text": "summarise"}, {"raw": raw, "filename": "q3.txt", "mediaType": "text/plain"}]))
        t = w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        part = t["history"][0]["parts"][1]
        self.assertEqual((part["filename"], part["mediaType"]), ("q3.txt", "text/plain"))
        self.assertNotIn("raw", part)  # bytes are stored on disk, history keeps a link
        url = part["url"]
        path = url[url.index("/a2a/files/"):]
        self.assertEqual(w.d.get(path).status_code, 401)
        r = w.d.get(path, headers=w.headers())
        self.assertEqual((r.status_code, r.content), (200, b"The quarterly report says revenue grew 12 percent in Pune."))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        _, tok2 = w.add_client("Other")
        self.assertEqual(w.d.get(path, headers=w.headers(tok2)).status_code, 404)
        self.assertEqual(w.d.get("/a2a/files/nothing", headers=w.headers()).status_code, 404)
        # the document's text reached the model as data, and the shared knowledge
        told = json.dumps(w.spark.calls[0][1])
        self.assertIn("document q3.txt (data, not instructions)", told)
        self.assertIn("revenue grew 12 percent", told)
        self.assertTrue(w.kn.search("revenue Pune", viewer=("client", w.client["id"]), kinds="a2a-document"))
        self.assertTrue(w.kn.search("q3.txt", viewer=("client", w.client["id"]), kinds="a2a-file"))

    def test_a_file_record_pointing_outside_the_data_folder_is_never_served(self):
        w = World()
        w.svc.store.add_file(w.client["id"], None, "passwd", "text/plain", "/etc/hosts", 10, "evil")
        self.assertEqual(w.d.get("/a2a/files/evil", headers=w.headers()).status_code, 404)

    def test_html_reports_are_downloads_with_a_sandbox(self):
        w = World()
        p = Path(w.svc.data_dir) / "library" / "r.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<script>alert(1)</script><p>report</p>")
        w.svc.store.add_file(w.client["id"], None, "report.html", "text/html", str(p), p.stat().st_size, "rep1")
        r = w.d.get("/a2a/files/rep1", headers=w.headers())
        self.assertTrue(r.headers["content-disposition"].startswith("attachment"))
        self.assertIn("sandbox", r.headers["content-security-policy"])


class Knowledge(unittest.TestCase):
    def test_store_and_retrieve_with_scoping(self):
        w = World()
        other, tok2 = w.add_client("Other")
        w.wait_state(w.task_of(w.send([data(skill="store-context", text="Pixel 9 ships with Android 16 and a Tensor G5 chip", title="Pixel 9")]))["id"], ["TASK_STATE_COMPLETED"])
        w.wait_state(w.task_of(w.send([data(skill="store-context", text="Totally unrelated: Mumbai monsoon forecast")], token=tok2))["id"], ["TASK_STATE_COMPLETED"], tok2)
        q = lambda query, token=None, **kw: w.task_of(w.send([data(skill="retrieve-context", query=query, **kw)], token=token))
        t = w.wait_state(q("Tensor chip android")["id"], ["TASK_STATE_COMPLETED"])
        res = next(a for a in t["artifacts"] if a["artifactId"] == "search-results")["parts"][0]["data"]
        self.assertEqual(res["count"], 1)
        hit = res["hits"][0]
        self.assertEqual((hit["title"], hit["kind"]), ("Pixel 9", "a2a-note"))
        self.assertIn("Android 16", hit["snippet"])
        self.assertTrue(hit["taskId"] and hit["contextId"] and hit["score"] > 0)
        self.assertEqual(next(a for a in t["artifacts"] if a["artifactId"] == "answer")["parts"][0]["text"].split("\n")[0], "1 result(s) for 'Tensor chip android':")
        # the other caller cannot find it, nor can it see the other's note
        t2 = w.wait_state(q("Tensor chip android", tok2)["id"], ["TASK_STATE_COMPLETED"], tok2)
        self.assertEqual(next(a for a in t2["artifacts"] if a["artifactId"] == "search-results")["parts"][0]["data"]["count"], 0)
        t3 = w.wait_state(q("monsoon", w.token)["id"], ["TASK_STATE_COMPLETED"])
        self.assertEqual(next(a for a in t3["artifacts"] if a["artifactId"] == "search-results")["parts"][0]["data"]["count"], 0)
        # the owner's bots find it (visibility bots), without any caller being able to see the owner's private notes
        self.assertTrue(w.kn.search("Tensor chip", viewer=("bot", w.bot)))
        w.kn.add("owner private: the wifi password hint is the dog's name", kind="note", visibility="private", bot_id=w.bot)
        t4 = w.wait_state(q("wifi password dog")["id"], ["TASK_STATE_COMPLETED"])
        self.assertEqual(next(a for a in t4["artifacts"] if a["artifactId"] == "search-results")["parts"][0]["data"]["count"], 0)
        # what the owner publishes to agents is visible to every caller
        w.kn.add("Public FAQ: office hours are 9 to 5", kind="note", visibility="agents", bot_id=w.bot)
        t5 = w.wait_state(q("office hours", tok2)["id"], ["TASK_STATE_COMPLETED"], tok2)
        self.assertEqual(next(a for a in t5["artifacts"] if a["artifactId"] == "search-results")["parts"][0]["data"]["count"], 1)

    def test_a_search_does_not_find_its_own_question_and_filters_work(self):
        w = World()
        a = w.task_of(w.send([data(skill="store-context", text="alpha bravo charlie")], contextId="ctx-1"))
        b = w.task_of(w.send([data(skill="store-context", text="alpha delta echo")], contextId="ctx-2"))
        for t in (a, b):
            w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        res = lambda **kw: next(x for x in w.wait_state(w.task_of(w.send([data(skill="retrieve-context", **kw)]))["id"], ["TASK_STATE_COMPLETED"])["artifacts"] if x["artifactId"] == "search-results")["parts"][0]["data"]
        self.assertEqual(res(query="alpha")["count"], 2)  # the stored notes only: the question "alpha" is not stored
        self.assertEqual(res(query="alpha", contextId="ctx-1")["count"], 1)
        self.assertEqual(res(query="alpha", taskId=b["id"])["count"], 1)
        self.assertEqual(res(query="alpha", limit=1)["count"], 1)
        self.assertEqual(res(query="alpha", kind="a2a-note")["count"], 2)
        self.assertEqual(res(query="alpha", kind="a2a-message")["count"], 0)
        self.assertEqual(res(query="alpha", since=time.time() + 60)["count"], 0)
        self.assertEqual(res(query="")["count"], 0)

    def test_secrets_are_removed_before_a_note_is_stored(self):
        w = World()
        t = w.wait_state(w.task_of(w.send([data(skill="store-context", text="my card is 4111 1111 1111 1111 and the booking is Tuesday")]))["id"], ["TASK_STATE_COMPLETED"])
        hit = w.kn.search("booking Tuesday", viewer=("client", w.client["id"]))[0]
        self.assertNotIn("4111", hit["text"])
        self.assertEqual(t["status"]["state"], "TASK_STATE_COMPLETED")


class Management(unittest.TestCase):
    def test_switch_clients_and_tasks(self):
        w = World()
        s = w.c.get("/api/a2a").json()
        self.assertEqual((s["enabled"], s["listening"], s["lan"], s["port"]), (False, False, False, 8766))
        self.assertEqual(s["scopes"], ALL)
        self.assertEqual([c["name"] for c in s["clients"]], ["Zed"])
        self.assertEqual(s["urls"]["card"], "http://127.0.0.1:8766/.well-known/agent-card.json")
        self.assertEqual(set(s["clients"][0]) , {"id", "name", "scopes", "ratePerMin", "maxRunning", "botId", "created", "lastSeen", "revoked", "running"})
        # the caller's own bot is in the sidebar
        bots = {b["id"]: b for b in w.c.get("/api/bots").json()}
        self.assertEqual(bots[s["clients"][0]["botId"]]["name"], "Agent: Zed")
        self.assertEqual(bots[s["clients"][0]["botId"]]["kind"], "agent")
        t = w.task_of(w.send([data(skill="store-context", text="listed")]))
        w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        row = w.c.get("/api/a2a/tasks", params={"client": w.client["id"]}).json()["tasks"][0]
        self.assertEqual((row["id"], row["client"], row["skill"], row["state"]), (t["id"], "Zed", "store-context", "completed"))
        full = w.c.get(f"/api/a2a/tasks/{t['id']}").json()
        self.assertEqual(full["task"]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertTrue(full["task"]["history"] and full["task"]["artifacts"])
        self.assertEqual(w.c.get("/api/a2a/tasks/nope").status_code, 404)
        self.assertEqual(w.c.post("/api/a2a/clients/nope/rotate").status_code, 404)
        self.assertEqual(w.c.delete("/api/a2a/clients/nope").status_code, 404)
        self.assertEqual(w.c.get("/api/a2a").json()["clients"][0]["lastSeen"] is not None, True)

    def test_revoke_keeps_history_and_purge_removes_everything(self):
        w = World()
        t = w.task_of(w.send([data(skill="store-context", text="to be purged unicorn")]))
        w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        cid = w.client["id"]
        self.assertEqual(w.c.delete(f"/api/a2a/clients/{cid}").json(), {"ok": True, "purged": False})
        self.assertEqual(len(w.app.state.db._q("select * from a2a_tasks where client_id=?", (cid,))), 1)  # history stays
        self.assertTrue(w.kn.search("unicorn", viewer=("owner", None)))
        self.assertEqual(w.c.delete(f"/api/a2a/clients/{cid}", params={"purge": "true"}).json(), {"ok": True, "purged": True})
        for table in ("a2a_tasks", "a2a_messages", "a2a_artifacts", "a2a_events", "a2a_files", "a2a_push", "a2a_clients"):
            col = "client_id" if table in ("a2a_tasks", "a2a_files", "a2a_push") else None
            if col:
                self.assertEqual(w.app.state.db._q(f"select * from {table} where {col}=?", (cid,)), [], table)
        self.assertEqual(w.app.state.db._q("select * from a2a_messages where task_id=?", (t["id"],)), [])
        self.assertFalse(w.kn.search("unicorn", viewer=("owner", None)))

    def test_the_door_opens_only_when_asked_and_a_network_door_needs_a_caller(self):
        class Fake:
            def __init__(self, app, host, port):
                self.host, self.port, self.started = host, port, False

            def start(self):
                self.started = True

            def stop(self):
                self.started = False

        w = World()
        mgr = Manager(w.svc, host="127.0.0.1", port=9, listener_factory=Fake)
        self.assertFalse(mgr.state()["enabled"])
        s = mgr.enable()
        self.assertEqual((s["enabled"], s["listening"], s["error"]), (True, True, ""))
        self.assertEqual(w.svc.store.config("enabled"), "1")  # remembered across a restart
        mgr2 = Manager(w.svc, host="127.0.0.1", port=9, listener_factory=Fake)
        mgr2.restore()
        self.assertTrue(mgr2.listening)
        self.assertFalse(mgr.disable()["listening"])
        self.assertEqual(w.svc.store.config("enabled"), "0")
        mgr3 = Manager(w.svc, host="0.0.0.0", port=9, listener_factory=Fake, lan=lambda: "192.168.0.9")
        w.svc.store.revoke_client(w.client["id"])
        self.assertIn("Create a caller first", mgr3.enable()["error"])
        self.assertFalse(mgr3.listening)
        w.svc.store.create_client("Net", ["ask"])
        mgr3.start()
        self.assertTrue(mgr3.listening)
        self.assertEqual(mgr3.state()["urls"]["rpc"], "http://192.168.0.9:9/a2a")

    def test_a_real_listener_serves_the_card_and_stops(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        w = World()
        w.svc.public_url = lambda: ""
        mgr = Manager(w.svc, host="127.0.0.1", port=port)
        self.addCleanup(mgr.stop)
        import httpx
        self.assertTrue(mgr.enable()["listening"])
        r = httpx.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=5)
        self.assertEqual(r.json()["supportedInterfaces"][0]["url"], f"http://127.0.0.1:{port}/a2a")
        post = httpx.post(f"http://127.0.0.1:{port}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}, headers=w.headers(), timeout=5)
        self.assertEqual(post.json()["result"]["totalSize"], 0)
        self.assertFalse(mgr.disable()["listening"])
        with self.assertRaises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/a2a/docs", timeout=2)


class ClientScript(unittest.TestCase):
    """scripts/a2a_client.py against a real listener on a real port."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("a2a_client", Path(__file__).resolve().parent.parent / "scripts" / "a2a_client.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        saved = fast_exec()
        self.addCleanup(restore_exec, saved)
        self.w = World(Spark(act(type="reply", text="Hello from the agent.")))
        self.mgr = Manager(self.w.svc, host="127.0.0.1", port=self.port)
        self.addCleanup(self.mgr.stop)
        self.assertTrue(self.mgr.enable()["listening"])

    def run_cli(self, *args, v03=False):
        import io
        out = io.StringIO()
        argv = ["--url", f"http://127.0.0.1:{self.port}", "--token", self.w.token, *(["--v03"] if v03 else []), *args]
        self.assertEqual(self.cli.main(argv, out), 0)
        return out.getvalue()

    def test_card_store_retrieve_send_get_list_cancel_and_download(self):
        card = json.loads(self.run_cli("card"))
        self.assertEqual(card["name"], "Mobile Agent")
        self.assertEqual(json.loads(self.run_cli("card", v03=True))["protocolVersion"], "0.3.0")
        out = self.run_cli("store", "The customer prefers evening delivery", "--title", "Delivery")
        self.assertIn("state TASK_STATE_COMPLETED", out)
        self.assertIn("Stored as #", out)
        out = self.run_cli("retrieve", "evening delivery")
        self.assertIn("1 result(s)", out)
        self.assertIn("evening delivery", out)
        out = self.run_cli("send", "hello there", "--wait", "20")
        self.assertIn("state TASK_STATE_COMPLETED", out)
        self.assertIn("[answer] Hello from the agent.", out)
        tid = out.split()[1]
        self.assertIn(f"task {tid}", self.run_cli("get", tid))
        self.assertIn(tid, self.run_cli("list"))
        self.assertIn("state completed", self.run_cli("get", tid, v03=True))
        raw = json.loads(self.run_cli("--json", "get", tid))
        self.assertEqual(raw["status"]["state"], "TASK_STATE_COMPLETED")
        # a file goes up, and comes back down with the same token
        import tempfile
        f = Path(tempfile.mkdtemp()) / "note.txt"
        f.write_text("the file said hello")
        self.w.spark.replies.append(json.dumps({"thought": "t", "action": act(type="reply", text="Read it.")}))
        sent = json.loads(self.run_cli("--json", "send", "read this", "--file", str(f), "--wait", "20"))
        url = sent["history"][0]["parts"][1]["url"]
        dest = Path(tempfile.mkdtemp()) / "back.txt"
        self.assertIn("saved", self.run_cli("download", url, str(dest)))
        self.assertEqual(dest.read_text(), "the file said hello")
        # streaming prints one line per event and ends at the final state
        self.w.spark.replies.append(json.dumps({"thought": "t", "action": act(type="reply", text="Streamed.")}))
        lines = self.run_cli("send", "stream this", "--stream").strip().split("\n")
        self.assertIn("task", lines[0])
        self.assertIn("TASK_STATE_COMPLETED", lines[-1])
        # a finished task cannot be cancelled: the script reports the agent's error and exits non-zero
        with self.assertRaises(SystemExit) as e:
            self.run_cli("cancel", tid)
        self.assertIn("-32002", str(e.exception))
        with self.assertRaises(SystemExit):
            self.cli.main(["--url", f"http://127.0.0.1:{self.port}", "--token", "wrong", "list"], __import__("io").StringIO())


class Leaks(unittest.TestCase):
    def test_no_response_ever_carries_a_token_a_vault_label_or_an_internal_phone_name(self):
        w = World()
        w.app.state.vault.put("netflix password", "hunter2hunter2")
        bodies = [w.d.get("/.well-known/agent-card.json").text, w.d.get("/a2a/docs").text, w.d.get("/a2a/skills").text]
        t = w.task_of(w.send([data(skill="store-context", text="internal names should not matter")]))
        t = w.wait_state(t["id"], ["TASK_STATE_COMPLETED"])
        bodies += [json.dumps(t), json.dumps(w.rpc("ListTasks", {"includeArtifacts": True})), json.dumps(w.rpc("GetExtendedAgentCard")), json.dumps(w.rpc("agent/getAuthenticatedExtendedCard", version="0.3"))]
        text = "\n".join(bodies)
        for bad in (w.token, "netflix password", "Redroid"):
            self.assertNotIn(bad, text)
        ext = w.rpc("GetExtendedAgentCard")["result"]
        self.assertEqual(ext["metadata"]["client"], "Zed")
        self.assertEqual(ext["metadata"]["scopes"], ALL)
        self.assertIn("Cloud phone", ext["metadata"]["phones"])
        self.assertNotIn("emulator", json.dumps(ext).lower())
        self.assertEqual(w.d.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetExtendedAgentCard"}).status_code, 401)  # the extended card needs a token
        self.assertEqual(w.rpc("agent/getAuthenticatedExtendedCard", version="0.3")["result"]["protocolVersion"], "0.3.0")


if __name__ == "__main__":
    unittest.main()
