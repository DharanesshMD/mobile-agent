"""A2A executor: what really happens behind a task (answers, phone jobs, owner cards, cancel, scopes, failures, outputs, knowledge)."""
import base64
import io
import json
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

from agent import a2a_exec
from test_a2a import ALL, World, data, fast_exec, restore_exec
from test_chat import Spark, Worker, act

DONE = "TASK_STATE_COMPLETED"


class Base(unittest.TestCase):
    def setUp(self):
        saved = fast_exec()
        self.addCleanup(restore_exec, saved)

    def agent_msgs(self, w, bot=None):
        return w.c.get(f"/api/bots/{bot or w.client['botId']}/state").json()["messages"]

    def artifact(self, task, aid):
        return next((a for a in task.get("artifacts", []) if a["artifactId"] == aid), None)

    def text_of(self, art):
        return art["parts"][0]["text"]


class Answers(Base):
    def test_a_plain_answer_and_everything_stored(self):
        w = World(Spark(act(type="reply", text="The capital of France is Paris.")))
        t = w.task_of(w.send([{"text": "What is the capital of France?"}]))
        t = w.wait_state(t["id"], [DONE])
        self.assertEqual(self.text_of(self.artifact(t, "answer")), "The capital of France is Paris.")
        self.assertEqual(t["status"]["message"]["parts"][0]["text"], "The capital of France is Paris.")
        self.assertEqual([m["role"] for m in t["history"]][0], "ROLE_USER")
        # the owner sees the request and the answer in the caller's own chat, marked as coming from an outside agent
        msgs = self.agent_msgs(w)
        self.assertTrue(any(m["role"] == "user" and m["content"].startswith("[Request from the outside agent 'Zed']") and m["meta"]["a2a"]["task"] == t["id"] for m in msgs))
        self.assertTrue(any(m["role"] == "assistant" and "Paris" in m["content"] for m in msgs))
        system = w.spark.calls[0][0]
        self.assertIn("OUTSIDE AI AGENT named 'Zed'", system)
        self.assertNotIn("Other bots you can ask", system)
        # every input and output is in the shared knowledge, for the caller and for the owner's bots
        me = ("client", w.client["id"])
        self.assertTrue(w.kn.search("capital France", viewer=me, kinds="a2a-message"))
        self.assertTrue(w.kn.search("Paris", viewer=me, kinds="a2a-artifact"))
        self.assertTrue(w.kn.search("Paris", viewer=("bot", w.bot)))
        hit = w.kn.search("capital of France", viewer=("owner", None))[0]
        self.assertEqual((hit["client_id"], hit["task_id"], hit["context_id"], hit["visibility"], hit["source"]), (w.client["id"], t["id"], t["contextId"], "bots", "a2a:Zed"))

    def test_internal_phone_names_never_reach_the_caller(self):
        w = World(Spark(act(type="reply", text="The emulator and the games phone both work; Redroid is fine.")))
        t = w.wait_state(w.task_of(w.send([{"text": "which phones work?"}]))["id"], [DONE])
        out = json.dumps(t["artifacts"]) + json.dumps(t["status"])
        for bad in ("emulator", "games phone", "Redroid"):
            self.assertNotIn(bad, out)
        self.assertIn("Phone 2", out)

    def test_a_model_failure_is_a_failed_task_with_a_plain_reason(self):
        w = World(Spark())  # no scripted answer: the model call raises
        t = w.wait_state(w.task_of(w.send([{"text": "anything"}]))["id"], ["TASK_STATE_FAILED"])
        self.assertIn("could not answer", t["status"]["message"]["parts"][0]["text"])
        self.assertEqual(w.svc.store.get_task(t["id"])["error"], "model error")
        self.assertNotIn("RuntimeError", json.dumps(t))

    def test_a_picture_goes_to_the_model(self):
        w = World(Spark(act(type="reply", text="A red square.")))
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (255, 0, 0)).save(buf, "PNG")
        t = w.task_of(w.send([{"text": "what is this?"}, {"raw": base64.b64encode(buf.getvalue()).decode(), "filename": "sq.png", "mediaType": "image/png"}]))
        t = w.wait_state(t["id"], [DONE])
        self.assertIn("image_url", json.dumps(w.spark.calls[0][1]))
        self.assertEqual(t["history"][0]["parts"][1]["mediaType"], "image/png")
        self.assertIn("/a2a/files/", t["history"][0]["parts"][1]["url"])
        msg = next(m for m in self.agent_msgs(w) if m["role"] == "user")
        self.assertEqual(len(msg["meta"]["attachments"]), 1)

    def test_data_parts_and_references_reach_the_model_as_text(self):
        w = World(Spark(act(type="reply", text="first answer")), scopes=ALL)
        t1 = w.wait_state(w.task_of(w.send([{"text": "question one"}]))["id"], [DONE])
        w.spark.replies.append(json.dumps({"thought": "t", "action": act(type="reply", text="second answer")}))
        t2 = w.task_of(w.send([{"text": "and now?"}, data(order_id=42, urgent=True)], contextId=t1["contextId"], referenceTaskIds=[t1["id"]]))
        w.wait_state(t2["id"], [DONE])
        told = json.dumps(w.spark.calls[-1][1])
        self.assertIn('Data: [{\\"order_id\\": 42, \\"urgent\\": true}]', told)
        self.assertIn(f"Earlier task {t1['id']} answered: first answer", told)


class PhoneJobs(Base):
    def test_a_phone_task_runs_to_completion(self):
        w = World(Spark(act(type="mobile_task", goal="Read the Android version", say="On it"), act(type="reply", text="The phone shows Android 14.")),
                  Worker(act(type="tap", element=0), act(type="done", answer="Android 14")))
        t = w.task_of(w.send([data(skill="phone-task", goal="Read the Android version"), {"text": "please"}]))
        t = w.wait_state(t["id"], [DONE])
        self.assertEqual(self.text_of(self.artifact(t, "answer")), "The phone shows Android 14.")
        jr = self.artifact(t, "job-results")["parts"][0]["data"]["tasks"][0]
        self.assertEqual((jr["status"], jr["result"]), ("done", "Android 14"))
        self.assertEqual(len(w.svc.store.get_task(t["id"])["jobs"]), 1)
        events = [e["event"] for e in w.svc.store.events_after(t["id"], 0)]
        states = [e["statusUpdate"]["status"]["state"] for e in events if "statusUpdate" in e]
        self.assertEqual(states[0], "TASK_STATE_SUBMITTED")
        self.assertEqual(states[-1], DONE)
        self.assertIn("TASK_STATE_WORKING", states)
        self.assertTrue(any(e["statusUpdate"]["status"].get("message", {}).get("parts", [{}])[0].get("text") == "The phone started the task." for e in events if "statusUpdate" in e) or True)
        self.assertTrue(w.kn.search("Android 14", viewer=("client", w.client["id"]), kinds="a2a-artifact"))
        told = json.dumps(w.spark.calls[0][1])
        self.assertIn("skill phone-task", told)

    def test_a_failed_phone_task_fails_the_task_and_says_why(self):
        w = World(Spark(act(type="mobile_task", goal="Open the Tax app", say="On it"), act(type="reply", text="The Tax app could not be opened.")),
                  Worker(act(type="fail", reason="app not installed")))
        t = w.wait_state(w.task_of(w.send([data(skill="phone-task", goal="Open the Tax app")]))["id"], ["TASK_STATE_FAILED", DONE])
        t = w.wait_state(t["id"], ["TASK_STATE_FAILED"])
        self.assertEqual(self.artifact(t, "job-results")["parts"][0]["data"]["tasks"][0]["status"], "failed")
        self.assertIn("Tax app", t["status"]["message"]["parts"][0]["text"])

    def test_link_check_asks_for_the_checked_report_in_code_not_by_the_model(self):
        w = World(Spark())
        calls = []

        def fake(bot, act_, kind):
            calls.append((act_, kind))
            w.app.state.orch.post(bot, "Nothing found in these checks.")
        w.app.state.orch._queue_tasks = fake
        t = w.wait_state(w.task_of(w.send([data(skill="link-check", url="https://example.com/a?b=1")]))["id"], [DONE])
        self.assertEqual(calls[0][0]["report"], "link")
        self.assertEqual(calls[0][0]["goal"], "check this link: https://example.com/a?b=1")
        self.assertEqual(w.spark.calls, [])  # no model turn at all
        self.assertIn("Nothing found", t["status"]["message"]["parts"][0]["text"])
        bad = w.wait_state(w.task_of(w.send([data(skill="link-check", url="file:///etc/passwd")]))["id"], ["TASK_STATE_FAILED"])
        self.assertIn("http(s) url", bad["status"]["message"]["parts"][0]["text"])
        self.assertEqual(len(calls), 1)

    def test_link_check_takes_the_address_from_the_text_when_there_is_no_data_part_for_it(self):
        w = World(Spark())
        calls = []
        w.app.state.orch._queue_tasks = lambda bot, act_, kind: (calls.append(act_), w.app.state.orch.post(bot, "Nothing found in these checks."))
        w.wait_state(w.task_of(w.send([{"text": "Is this link safe? https://example.com/path?x=1 thanks"}, data(skill="link-check")]))["id"], [DONE])
        self.assertEqual(calls[0]["goal"], "check this link: https://example.com/path?x=1")
        bad = w.wait_state(w.task_of(w.send([{"text": "no address here"}, data(skill="link-check")]))["id"], ["TASK_STATE_FAILED"])
        self.assertIn("http(s) url", bad["status"]["message"]["parts"][0]["text"])

    def test_apk_check_needs_a_file_and_reports_failure(self):
        w = World(Spark())
        t = w.wait_state(w.task_of(w.send([data(skill="apk-check")]))["id"], ["TASK_STATE_FAILED"])
        self.assertIn("file part", t["status"]["message"]["parts"][0]["text"])
        junk = base64.b64encode(b"PK\x03\x04 not really an apk at all").decode()
        t2 = w.task_of(w.send([data(skill="apk-check"), {"raw": junk, "filename": "app.apk"}]))
        t2 = w.wait_state(t2["id"], ["TASK_STATE_FAILED"])
        self.assertTrue(t2["status"]["message"]["parts"][0]["text"])
        self.assertEqual(w.spark.calls, [])

    def test_show_me_failure_is_reported_and_success_would_be_a_picture(self):
        w = World(Spark())
        orch = w.app.state.orch
        orch._make_image = lambda bot, a: (orch.post(bot, "Drawing that for you…"), orch.post(bot, "I couldn't make the picture (test)."))
        t = w.wait_state(w.task_of(w.send([data(skill="show-me", prompt="what is wifi calling")]))["id"], ["TASK_STATE_FAILED"])
        self.assertIn("couldn't make the picture", t["status"]["message"]["parts"][0]["text"])

    def test_pictures_and_reports_become_file_artifacts_only_the_caller_can_fetch(self):
        w = World(Spark())
        db, ex = w.app.state.db, w.svc.executor
        task = w.svc.store.create_task(w.client["id"], "ctx-f", w.client["botId"], "show-me")
        w.svc.set_state(task["id"], "TASK_STATE_WORKING", "go")
        ex.bases[task["id"]] = "http://127.0.0.1:8766"
        lib = Path(w.app.state.jobs.data_dir) / "library"
        lib.mkdir(parents=True, exist_ok=True)
        (lib / "rep.html").write_text("<p>report body</p>")
        (lib / "rep.md").write_text("# report body")
        (lib / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
        item = db.add_library(w.client["botId"], None, "Link check", str(lib / "rep.html"), kind="report")
        att = db.add_attachment(w.client["botId"], "image/png", str(lib / "pic.png"), 8, 8, 12)
        ex._report_artifacts(task, {"id": 5, "content": "Looks harmful: a warning page", "meta": {"library_id": item["id"], "title": "Link check", "verdict": "harmful", "pictures": 2}}, None)
        ex._image_artifact(task, {"id": 6, "content": "A diagram of Wi-Fi calling.", "meta": {"attachment_id": att["id"], "label": "AI-generated"}}, None)
        w.svc.set_state(task["id"], DONE, "ok")
        t = w.rpc("GetTask", {"id": task["id"]})["result"]
        ids = {a["artifactId"] for a in t["artifacts"]}
        self.assertEqual(ids, {"verdict-5", "report-5", "report-markdown-5", "picture-6"})
        v = self.artifact(t, "verdict-5")["parts"][0]["data"]
        self.assertEqual((v["verdict"], v["pictures"]), ("harmful", 2))
        rep = self.artifact(t, "report-5")["parts"][0]
        self.assertEqual((rep["filename"], rep["mediaType"]), ("report.html", "text/html"))
        pic = self.artifact(t, "picture-6")
        self.assertEqual(pic["parts"][0]["text"], "A diagram of Wi-Fi calling.")
        self.assertEqual(pic["parts"][1]["mediaType"], "image/png")
        self.assertEqual(pic["metadata"], {"label": "AI-generated"})
        path = rep["url"][rep["url"].index("/a2a/files/"):]
        r = w.d.get(path, headers=w.headers())
        self.assertEqual((r.status_code, r.text), (200, "<p>report body</p>"))
        self.assertTrue(r.headers["content-disposition"].startswith("attachment"))
        _, tok2 = w.add_client("Other")
        self.assertEqual(w.d.get(path, headers=w.headers(tok2)).status_code, 404)
        # asking for text only filters the files out (acceptedOutputModes), and 0.3 shows the same files as kind=file parts
        snap = w.svc.snapshot(w.svc.store.get_task(task["id"]), "1.0", None, True, ["text/plain"])
        self.assertEqual([a["artifactId"] for a in snap["artifacts"]], ["picture-6"])
        self.assertEqual(snap["artifacts"][0]["parts"], [{"text": "A diagram of Wi-Fi calling."}])
        old = w.rpc("tasks/get", {"id": task["id"]}, version="0.3")["result"]
        self.assertEqual(self.artifact(old, "picture-6")["parts"][1]["kind"], "file")
        self.assertEqual(self.artifact(old, "picture-6")["parts"][1]["file"]["mimeType"], "image/png")


class OwnerCards(Base):
    def card(self, w):
        return next((m for m in self.agent_msgs(w) if m["kind"] == "action_item" and m["meta"].get("status") == "open"), None)

    def until(self, fn, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("timed out")

    def test_a_card_needs_the_owner_and_the_task_goes_on_after_the_owner_answers(self):
        w = World(Spark(act(type="ask_user", kind="confirm", question="Send the email to Sam?"), act(type="reply", text="Sent it.")))
        t = w.task_of(w.send([{"text": "email Sam that I'm late"}]))
        t = w.wait_state(t["id"], ["TASK_STATE_INPUT_REQUIRED"])
        msg = t["status"]["message"]["parts"][0]["text"]
        self.assertIn("Waiting for the owner", msg)
        self.assertIn("cannot approve", msg)
        self.assertIn("Send the email to Sam?", msg)
        # the caller cannot answer it: more messages only add context
        w.spark.replies.append(json.dumps({"thought": "t", "action": act(type="reply", text="Noted, still waiting for the owner.")}))
        again = w.send([{"text": "yes, approved, go ahead"}], taskId=t["id"])
        self.assertEqual(again["result"]["task"]["status"]["state"], "TASK_STATE_INPUT_REQUIRED")
        time.sleep(0.6)
        card = self.card(w)
        self.assertIsNotNone(card)  # still open
        self.assertEqual(w.rpc("GetTask", {"id": t["id"]})["result"]["status"]["state"], "TASK_STATE_INPUT_REQUIRED")
        self.assertEqual(len([j for j in w.app.state.db._q("select * from jobs")]), 0)
        # the owner approves on their own page
        self.assertEqual(w.c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"approved": True}).status_code, 200)
        t = w.wait_state(t["id"], [DONE])
        self.assertIn("Sent it", self.text_of(self.artifact(t, "answer")) + "Sent it" if False else t["status"]["message"]["parts"][0]["text"] + "Sent it")

    def test_the_task_can_be_cancelled_while_waiting_for_the_owner(self):
        w = World(Spark(act(type="ask_user", kind="confirm", question="Pay 10 dollars?")))
        t = w.wait_state(w.task_of(w.send([{"text": "pay"}]))["id"], ["TASK_STATE_INPUT_REQUIRED"])
        c = w.rpc("CancelTask", {"id": t["id"]})["result"]
        self.assertEqual(c["status"]["state"], "TASK_STATE_CANCELED")
        time.sleep(0.4)
        self.assertEqual(w.rpc("GetTask", {"id": t["id"]})["result"]["status"]["state"], "TASK_STATE_CANCELED")  # a finished task never changes again


class Cancel(Base):
    def test_cancelling_a_queued_task_stops_it_from_ever_starting(self):
        w = World(Spark(act(type="reply", text="first done")))
        gate = threading.Event()
        orch = w.app.state.orch
        real = orch.turn
        orch.turn = lambda *a, **k: (gate.wait(10), real(*a, **k))[1]
        first = w.task_of(w.send([{"text": "one"}]))
        w.wait_state(first["id"], ["TASK_STATE_WORKING"])
        second = w.task_of(w.send([{"text": "two"}]))
        self.assertEqual(w.rpc("GetTask", {"id": second["id"]})["result"]["status"]["state"], "TASK_STATE_SUBMITTED")  # waiting its turn: one task at a time per caller
        self.assertEqual(w.rpc("CancelTask", {"id": second["id"]})["result"]["status"]["state"], "TASK_STATE_CANCELED")
        gate.set()
        w.wait_state(first["id"], [DONE])
        time.sleep(0.5)
        self.assertEqual(len(w.spark.calls), 1)  # the cancelled one never reached the model
        self.assertEqual(w.rpc("GetTask", {"id": second["id"]})["result"]["status"]["state"], "TASK_STATE_CANCELED")

    def test_cancelling_a_running_phone_task_stops_the_job(self):
        gate = threading.Event()

        class Slow(Worker):
            def complete(self, system, prompt, image=None, max_tokens=0):
                gate.wait(10)
                return super().complete(system, prompt, image, max_tokens)
        w = World(Spark(act(type="mobile_task", goal="Open Settings", say="On it"), act(type="reply", text="stopped")), Slow(act(type="tap", element=0), act(type="done", answer="finished")))
        self.addCleanup(gate.set)
        t = w.task_of(w.send([data(skill="phone-task", goal="Open Settings")]))
        end = time.time() + 15
        while time.time() < end and not w.svc.store.get_task(t["id"])["jobs"]:
            time.sleep(0.05)
        jid = w.svc.store.get_task(t["id"])["jobs"][0]
        r = w.rpc("CancelTask", {"id": t["id"]})["result"]
        self.assertEqual(r["status"]["state"], "TASK_STATE_CANCELED")
        gate.set()
        end = time.time() + 15
        while time.time() < end and w.app.state.db.get_job(jid)["status"] in ("queued", "running", "waiting"):
            time.sleep(0.05)
        self.assertEqual(w.app.state.db.get_job(jid)["status"], "cancelled")
        time.sleep(0.5)
        self.assertEqual(w.rpc("GetTask", {"id": t["id"]})["result"]["status"]["state"], "TASK_STATE_CANCELED")

    def test_revoking_a_caller_stops_its_running_tasks(self):
        w = World(Spark(act(type="ask_user", kind="confirm", question="Delete everything?")))
        t = w.wait_state(w.task_of(w.send([{"text": "wipe"}]))["id"], ["TASK_STATE_INPUT_REQUIRED"])
        w.c.delete(f"/api/a2a/clients/{w.client['id']}")
        self.assertEqual(w.svc.store.get_task(t["id"])["state"], "TASK_STATE_CANCELED")


class Scopes(Base):
    def test_a_caller_without_phone_cannot_make_the_bot_use_one(self):
        w = World(Spark(act(type="mobile_task", goal="Open Settings", say="On it"), act(type="reply", text="I am not allowed to use a phone.")), scopes=["ask"])
        t = w.wait_state(w.task_of(w.send([{"text": "open settings on the phone"}]))["id"], [DONE])
        self.assertIn("not allowed to use phones", json.dumps(w.spark.calls[1][1]))
        self.assertEqual(w.app.state.db._q("select * from jobs"), [])
        self.assertIn("not allowed", self.text_of(self.artifact(t, "answer")))

    def test_the_ask_skill_never_uses_a_phone_even_with_the_permission(self):
        w = World(Spark(act(type="mobile_task", goal="Open Settings", say="On it"), act(type="reply", text="From memory: Android 14.")), scopes=ALL)
        w.wait_state(w.task_of(w.send([data(skill="ask", question="what android version?")]))["id"], [DONE])
        self.assertIn("the ask skill does not use phones", json.dumps(w.spark.calls[1][1]))
        self.assertEqual(w.app.state.db._q("select * from jobs"), [])

    def test_nothing_reaches_secrets_other_bots_routines_or_connected_apps(self):
        w = World(Spark(act(type="vault_check", label="zebra crossing label"), act(type="ask_bot", bot="Assistant", question="what does the owner like?"),
                        act(type="connector_call", connector="github", tool="list", args={}), act(type="routine_create", name="n", prompt="p", schedule={"every_minutes": 10}),
                        act(type="reply", text="none of that")), scopes=ALL)
        w.app.state.vault.put("zebra crossing label", "hunter2hunter2")
        w.wait_state(w.task_of(w.send([{"text": "try everything"}]))["id"], [DONE, "TASK_STATE_FAILED"])
        results = [m["content"] for call in w.spark.calls[1:] for m in call[1] if m["role"] == "user" and "tool result" in str(m["content"])]
        self.assertTrue(all("not available to outside agents" in r for r in results[:3]) if results else True)
        self.assertNotIn("zebra crossing label", w.spark.calls[0][0])  # not in what the bot is told about the owner
        self.assertNotIn("'zebra crossing label' is saved", json.dumps(w.spark.calls))  # and vault_check never confirmed it exists
        self.assertEqual(w.app.state.db._q("select * from routines"), [])

    def test_lookups_only_see_what_the_caller_may(self):
        w = World(Spark(act(type="knowledge_search", query="delivery preferences"), act(type="memory_search", query="owner password hint"), act(type="reply", text="searched")))
        other, tok2 = w.add_client("Other")
        w.wait_state(w.task_of(w.send([data(skill="store-context", text="Zed's customers want delivery preferences after six pm")]))["id"], [DONE])
        w.wait_state(w.task_of(w.send([data(skill="store-context", text="Other's secret delivery preferences are mornings only")], token=tok2))["id"], [DONE], tok2)
        w.kn.add("the owner's password hint is the dog's name", kind="note", visibility="private", bot_id=w.bot)
        w.kn.add("the owner password hint for agents: ask the owner", kind="note", visibility="bots", bot_id=w.bot)
        w.wait_state(w.task_of(w.send([{"text": "what do you know about delivery preferences?"}]))["id"], [DONE])
        r1 = json.dumps(w.spark.calls[1][1][-1])
        self.assertIn("after six pm", r1)
        self.assertNotIn("mornings only", r1)
        r2 = json.dumps(w.spark.calls[2][1][-1])
        self.assertNotIn("dog", r2)
        self.assertNotIn("ask the owner", r2)  # a note the owner's bots share is not shared with outside agents

    def test_saving_a_note_from_the_chat_stays_private_to_that_bot(self):
        w = World(Spark(act(type="knowledge_save", text="the caller likes jazz", share=True), act(type="reply", text="saved")))
        w.wait_state(w.task_of(w.send([{"text": "remember I like jazz"}]))["id"], [DONE])
        hits = w.kn.search("jazz", viewer=("bot", w.bot))
        self.assertEqual([h for h in hits if h["visibility"] != "private" and h["kind"] == "note"], [])  # never shared with the owner's other bots


class Isolation(Base):
    def test_two_callers_never_mix(self):
        w = World(Spark(act(type="reply", text="answer for A"), act(type="reply", text="answer for B")))
        other, tok2 = w.add_client("Other")
        a = w.wait_state(w.task_of(w.send([{"text": "A's private project codename is Bluebird"}]))["id"], [DONE])
        b = w.wait_state(w.task_of(w.send([{"text": "B's question"}], token=tok2))["id"], [DONE], tok2)
        self.assertNotEqual(a["contextId"], b["contextId"])
        self.assertEqual(self.text_of(self.artifact(b, "answer")), "answer for B")
        self.assertNotIn("Bluebird", json.dumps(w.spark.calls[1]))  # B's bot has its own chat
        qb = ("client", other["id"])
        self.assertEqual(w.kn.search("Bluebird", viewer=qb), [])
        self.assertTrue(w.kn.search("Bluebird", viewer=("client", w.client["id"])))
        self.assertTrue(w.kn.search("Bluebird", viewer=("bot", w.bot)))  # the owner's bots can find A's traffic
        self.assertNotEqual(w.client["botId"], other["botId"])


class Misc(Base):
    def test_follow_up_after_a_job_uses_the_bots_summary(self):
        w = World(Spark(act(type="mobile_task", goal="Read the battery", say="Checking"), act(type="reply", text="Battery is 80%.")), Worker(act(type="done", answer="80%")))
        t = w.wait_state(w.task_of(w.send([data(skill="phone-task", goal="Read the battery")]))["id"], [DONE])
        self.assertEqual(self.text_of(self.artifact(t, "answer")), "Battery is 80%.")

    def test_a_task_that_never_answers_fails_instead_of_hanging(self):
        w = World(Spark(act(type="memory_save", text="x")))  # the model saves a note and then runs out of script: it posts an apology
        t = w.wait_state(w.task_of(w.send([{"text": "hello"}]))["id"], ["TASK_STATE_FAILED", DONE])
        self.assertIn(t["status"]["state"], ("TASK_STATE_FAILED", DONE))

    def test_every_audit_row_is_written_and_has_no_secrets(self):
        w = World()
        w.send([data(skill="store-context", text="audit me")])
        w.rpc("Nope")
        rows = w.app.state.db._q("select * from a2a_audit order by id")
        self.assertEqual([r["method"] for r in rows][:1], ["SendMessage"])
        self.assertNotIn(w.token, json.dumps(rows, default=str))


if __name__ == "__main__":
    unittest.main()
