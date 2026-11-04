import json
import secrets
import unittest

from agent import sensitive
from agent.db import Database
from agent.graph import run
from agent.vault import SecretUse, Vault, load_key
from test_chat import Spark, Worker, act, msgs, open_card, say, setup, texts, until
from test_loop import FakeDevice, FakeModel, png, reply

SECRET = "hunter2-Correct-Horse"


def fresh() -> tuple[Database, Vault]:
    db = Database(":memory:")
    return db, Vault(db, secrets.token_bytes(32))


class Crypto(unittest.TestCase):
    def test_roundtrip_and_nothing_readable_in_the_database(self):
        db, v = fresh()
        rec = v.put("netflix password", SECRET)
        self.assertNotIn("value", rec)
        self.assertEqual(v.reveal("Netflix Password"), SECRET)  # labels are case-insensitive
        raw = json.dumps([bytes(r["ciphertext"]).hex() for r in db._q("select ciphertext from vault")]) + json.dumps(db._q("select label,kind from vault"))
        self.assertNotIn(SECRET, raw)
        self.assertNotIn(SECRET.encode(), bytes(db._q("select ciphertext from vault")[0]["ciphertext"]))

    def test_each_record_gets_its_own_nonce(self):
        db, v = fresh()
        v.put("a", SECRET)
        v.put("b", SECRET)
        n = [bytes(r["nonce"]) for r in db._q("select nonce from vault")]
        self.assertNotEqual(n[0], n[1])

    def test_ciphertext_moved_to_another_row_does_not_decrypt(self):
        db, v = fresh()
        a, b = v.put("a", SECRET), v.put("b", "other-value")
        row = db._one("select nonce, ciphertext from vault where id=?", (a["id"],))
        db._x("update vault set nonce=?, ciphertext=? where id=?", (row["nonce"], row["ciphertext"], b["id"]))
        with self.assertRaises(ValueError):
            v.reveal("b")

    def test_wrong_key_fails_and_duplicates_are_refused(self):
        db, v = fresh()
        v.put("a", SECRET)
        with self.assertRaises(ValueError):
            v.put("A", "x")
        with self.assertRaises(ValueError):
            Vault(db, secrets.token_bytes(32)).reveal("a")
        with self.assertRaises(KeyError):
            v.reveal("missing")

    def test_audit_records_use_but_never_the_value(self):
        db, v = fresh()
        v.put("bank", SECRET)
        v.reveal("bank", "job1", "Chase")
        rows = v.audit()
        self.assertEqual([(r["label"], r["job_id"], r["app"]) for r in rows], [("bank", "job1", "Chase")])
        self.assertNotIn(SECRET, json.dumps(rows))

    def test_delete_removes_the_secret(self):
        _, v = fresh()
        rec = v.put("x", SECRET)
        self.assertTrue(v.delete(rec["id"]))
        self.assertFalse(v.delete(rec["id"]))
        self.assertEqual(v.entries(), [])

    def test_key_file_is_created_once_with_owner_only_permissions(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "vault.key")
        os.environ.pop("VAULT_KEY", None)
        k1, k2 = load_key(path), load_key(path)
        self.assertEqual((k1, len(k1)), (k2, 32))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class Detector(unittest.TestCase):
    def kinds(self, text):
        return sensitive.redact(text)[1]

    def test_catches_cards_ids_passwords_and_tokens(self):
        self.assertEqual(self.kinds("my card is 4111 1111 1111 1111 exp 12/29"), ["card number"])
        self.assertEqual(self.kinds("4111-1111-1111-1111"), ["card number"])
        self.assertEqual(self.kinds("aadhaar 2345 6789 0123"), ["ID number"])
        self.assertEqual(self.kinds("PAN ABCDE1234F"), ["ID number"])
        self.assertEqual(self.kinds("the password is Sup3rSecret!"), ["password"])
        self.assertEqual(self.kinds("key sk-abcdefghijklmnop1234"), ["secret token"])

    def test_redaction_removes_the_value_and_keeps_the_sentence(self):
        text, kinds = sensitive.redact("pay with 4111 1111 1111 1111 please, password: abc12345")
        self.assertNotIn("4111", text)
        self.assertNotIn("abc12345", text)
        self.assertIn("[card number hidden]", text)
        self.assertIn("please", text)
        self.assertEqual(kinds, ["card number", "password"])

    def test_ordinary_text_is_left_alone(self):
        for t in ("call me on 5551234567", "set an alarm for 5:17", "order 1234567890123 shipped", "my pin code area is Chennai",
                  "open https://example.com/some/long/path/that/goes/on/forever.html", "2026-10-29 15:00 meeting"):
            self.assertEqual(sensitive.redact(t)[0], t, t)


class Worker_(unittest.TestCase):
    def run_secret(self, handler, label="bank", app="Chase"):
        _, v = fresh()
        v.put("bank", SECRET)
        dev = FakeDevice([png(1), png(2), png(3)])
        model = FakeModel([reply(type="type_secret", label=label, app=app), reply(type="done", answer="in")])
        events: list = []
        res = run("g", dev, model, settle=0, emit=events.append, handler=handler, secrets=SecretUse(v, "job1"))
        return res, dev, model, events, v

    def test_approved_secret_is_typed_but_never_seen_by_model_history_or_events(self):
        seen: list = []
        res, dev, model, events, v = self.run_secret(lambda p: seen.append(p) or {"approved": True})
        self.assertEqual((res.status, dev.typed), ("done", [SECRET]))
        self.assertEqual(seen, [{"kind": "type_secret", "label": "bank", "text": "Type your saved secret 'bank' into Chase?"}])
        self.assertNotIn(SECRET, json.dumps([model.calls, res.history, events, seen]))
        self.assertIn("Saved secrets (labels only): bank", model.calls[0])
        self.assertIn("typed saved secret 'bank'", model.calls[1])
        self.assertEqual([(r["label"], r["job_id"], r["app"]) for r in v.audit()], [("bank", "job1", "Chase")])

    def test_a_retry_of_the_same_secret_in_the_same_app_does_not_ask_again(self):
        _, v = fresh()
        v.put("bank", SECRET)
        dev = FakeDevice([png(1), png(2), png(3), png(4)])
        model = FakeModel([reply(type="type_secret", label="bank", app="Chase"), reply(type="type_secret", label="bank", app="Chase"), reply(type="done", answer="in")])
        asked: list = []
        res = run("g", dev, model, settle=0, emit=lambda e: None, handler=lambda p: asked.append(p) or {"approved": True}, secrets=SecretUse(v, "job1"))
        self.assertEqual((res.status, len(asked), dev.typed), ("done", 1, [SECRET, SECRET]))  # one card, two typings
        self.assertEqual(len(v.audit()), 2)  # both uses are still on the audit trail

    def test_a_secret_goes_through_the_private_path_when_the_device_has_one(self):
        _, v = fresh()
        v.put("bank", SECRET)
        dev = FakeDevice([png(1), png(2)])
        private: list = []
        dev.type_private = private.append
        model = FakeModel([reply(type="type_secret", label="bank", app="Chase"), reply(type="done", answer="in")])
        res = run("g", dev, model, settle=0, emit=lambda e: None, handler=lambda p: {"approved": True}, secrets=SecretUse(v, "job1"))
        self.assertEqual((res.status, private, hasattr(dev, "typed")), ("done", [SECRET], False))  # never through type_text, which may use the clipboard

    def test_declined_secret_ends_the_run_and_types_nothing(self):
        res, dev, _, _, v = self.run_secret(lambda p: {"approved": False})
        self.assertEqual(res.status, "denied")
        self.assertFalse(hasattr(dev, "typed"))
        self.assertEqual(v.audit(), [])

    def test_unknown_label_is_reported_to_the_model_without_asking_the_owner(self):
        asked: list = []
        res, dev, model, _, _ = self.run_secret(lambda p: asked.append(p) or {"approved": True}, label="nope")
        self.assertEqual((res.status, asked), ("done", []))
        self.assertIn("no saved secret 'nope' (saved: bank)", model.calls[1])

    def test_without_a_vault_the_action_is_an_error_not_a_crash(self):
        dev = FakeDevice([png(1), png(2)])
        model = FakeModel([reply(type="type_secret", label="x"), reply(type="done", answer="ok")])
        res = run("g", dev, model, settle=0, handler=lambda p: {"approved": True})
        self.assertEqual(res.status, "done")
        self.assertIn("no saved secret", model.calls[1])


class NeedSecret(unittest.TestCase):
    def test_saved_label_continues_without_asking_the_owner(self):
        _, v = fresh()
        v.put("bank", SECRET)
        asked: list = []
        model = FakeModel([reply(type="need_secret", label="Bank", why="sign in"), reply(type="done", answer="ok")])
        res = run("g", FakeDevice([png(1), png(2)]), model, settle=0, handler=lambda p: asked.append(p) or {}, secrets=SecretUse(v, "j"))
        self.assertEqual((res.status, asked), ("done", []))
        self.assertIn("secret 'Bank' is already saved", model.calls[1])

    def test_missing_label_asks_for_a_secure_input_then_continues_to_type_secret(self):
        _, v = fresh()
        seen: list = []

        def handler(p):
            seen.append(p)
            if p["kind"] == "need_secret":
                v.put(p["label"], SECRET, p["secret_kind"])  # what the secure input card does
                return {"saved": p["label"]}
            return {"approved": True}

        dev = FakeDevice([png(1), png(2), png(3), png(4)])
        model = FakeModel([reply(type="need_secret", label="bank", kind="card", why="pay"), reply(type="type_secret", label="bank"), reply(type="done", answer="ok")])
        res = run("g", dev, model, settle=0, handler=handler, secrets=SecretUse(v, "j"))
        self.assertEqual((res.status, dev.typed), ("done", [SECRET]))
        self.assertEqual([p["kind"] for p in seen], ["need_secret", "type_secret"])
        self.assertEqual(seen[0]["secret_kind"], "card")
        self.assertNotIn(SECRET, json.dumps([model.calls, res.history, seen]))

    def test_declined_secret_input_ends_the_run(self):
        _, v = fresh()
        res = run("g", FakeDevice([png(1)]), FakeModel([reply(type="need_secret", label="bank")]), settle=0, handler=lambda p: {"saved": ""}, secrets=SecretUse(v, "j"))
        self.assertEqual(res.status, "denied")


class Api(unittest.TestCase):
    def test_pasted_card_is_redacted_before_the_log_and_the_model(self):
        spark = Spark(act(type="reply", text="Please add it from the Vault panel."))
        c, _, bot, app = setup(spark)
        m = c.post(f"/api/bots/{bot}/messages", json={"text": "use card 4111 1111 1111 1111 for the order"}).json()
        self.assertEqual(m["content"], "use card [card number hidden] for the order")
        self.assertEqual(m["meta"], {"sensitive": ["card number"]})
        until(lambda: "Please add it from the Vault panel." in texts(c, bot, "assistant"))
        self.assertNotIn("4111", json.dumps(msgs(c, bot)) + json.dumps(spark.calls) + json.dumps(app.state.db._q("select * from messages")))

    def test_secret_card_saves_to_the_vault_and_never_keeps_the_value(self):
        c, _, bot, app = setup(Spark())
        item = app.state.orch.make_item(bot, None, {"kind": "secret", "secret_kind": "password", "question": "Enter it"})
        self.assertEqual(item["payload"]["label"], "")
        url = f"/api/items/{item['id']}/answer"
        self.assertEqual(c.post(url, json={"secret": SECRET}).status_code, 422)  # a name is needed
        r = c.post(url, json={"secret": SECRET, "label": "netflix"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(SECRET, json.dumps(msgs(c, bot)) + json.dumps(app.state.db._q("select * from items")) + r.text)
        card = next(m for m in msgs(c, bot) if m["kind"] == "action_item")
        self.assertEqual((card["meta"]["status"], card["meta"]["answer"]), ("answered", "Saved as 'netflix'"))
        lst = c.get("/api/vault")
        self.assertEqual([x["label"] for x in lst.json()], ["netflix"])
        self.assertNotIn(SECRET, lst.text + c.get("/api/vault/audit").text)
        self.assertEqual(app.state.vault.reveal("netflix"), SECRET)
        self.assertEqual(c.post("/api/vault", json={"label": "x", "value": "y"}).status_code, 405)  # no direct way in
        dup = app.state.orch.make_item(bot, None, {"kind": "secret", "label": "Netflix"})
        self.assertEqual(c.post(f"/api/items/{dup['id']}/answer", json={"secret": "other"}).status_code, 409)
        self.assertEqual(c.delete(f"/api/vault/{lst.json()[0]['id']}").status_code, 200)
        self.assertEqual(c.get("/api/vault").json(), [])

    def test_declining_a_secret_card_saves_nothing(self):
        c, _, bot, app = setup(Spark())
        item = app.state.orch.make_item(bot, None, {"kind": "secret", "label": "bank"})
        c.post(f"/api/items/{item['id']}/answer", json={"approved": False})
        self.assertEqual(c.get("/api/vault").json(), [])

    def test_pasting_a_secret_in_chat_offers_a_secure_input_card(self):
        c, _, bot, _ = setup(Spark(act(type="reply", text="ok")))
        say(c, bot, "my card is 4111 1111 1111 1111")
        card = until(lambda: open_card(c, bot))
        self.assertEqual((card["meta"]["kind"], card["meta"]["secret_kind"]), ("secret", "card"))
        self.assertNotIn("4111", json.dumps(msgs(c, bot)))

    def test_orchestrator_requests_and_verifies_secrets_without_seeing_values(self):
        spark = Spark(act(type="vault_check", label="netflix"), act(type="request_secret", label="netflix", kind="password"),
                      act(type="vault_check", label="Netflix"), act(type="request_secret", label="netflix"), act(type="reply", text="Already saved."))
        c, _, bot, app = setup(spark)
        say(c, bot, "log me into netflix")
        card = until(lambda: open_card(c, bot))
        self.assertIn("[tool result for vault_check] 'netflix' is not saved; saved: nothing", json.dumps(spark.calls[1][1]))
        self.assertEqual((card["meta"]["kind"], card["meta"]["label"]), ("secret", "netflix"))
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"secret": SECRET})
        until(lambda: spark.calls and len(spark.calls) >= 3)  # the orchestrator is told the owner saved it
        say(c, bot, "again")
        until(lambda: "Already saved." in texts(c, bot, "assistant"))
        self.assertIn("'netflix' is saved (password)", json.dumps(spark.calls))
        self.assertIn("already saved", json.dumps(spark.calls))
        self.assertNotIn(SECRET, json.dumps(spark.calls))

    def test_end_to_end_secret_card_then_typing(self):
        spark = Spark(act(type="mobile_task", goal="sign in to bank using the saved secret 'bank'", say="ok"), act(type="reply", text="signed in"))
        worker = Worker(act(type="type_secret", label="bank", app="Chase"), act(type="done", answer="ok"))
        c, phone, bot, app = setup(spark, worker)
        app.state.vault.put("bank", SECRET)
        say(c, bot, "sign in")
        card = until(lambda: open_card(c, bot))
        self.assertEqual(card["meta"]["kind"], "confirm")
        self.assertEqual(card["content"], "Type your saved secret 'bank' into Chase?")
        self.assertEqual(phone.typed, [])  # nothing typed before approval
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"approved": True})
        until(lambda: "signed in" in texts(c, bot, "assistant"))
        self.assertEqual(phone.typed, [SECRET])
        dump = json.dumps(msgs(c, bot)) + json.dumps(app.state.db._q("select * from items")) + json.dumps(worker.prompts) + json.dumps(spark.calls)
        self.assertNotIn(SECRET, dump)
        self.assertEqual(len(c.get("/api/vault/audit").json()), 1)

    def test_a_secret_is_not_accepted_as_a_plain_text_answer(self):
        spark = Spark(act(type="ask_user", kind="text", question="Which card?"))
        c, _, bot, _ = setup(spark)
        say(c, bot, "buy it")
        card = until(lambda: open_card(c, bot))
        r = c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"value": "4111 1111 1111 1111"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("Vault", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
