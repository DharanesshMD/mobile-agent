"""Phones are Phone, Phone 2, Phone 3 ...: nothing the owner reads or a model is told says what a phone is made of, older spellings still work,
and an existing install is renamed once without touching a name the owner chose."""
import ast
import json
import pathlib
import re
import unittest

from agent import master, names
from agent.db import Database
from test_chat import Spark, act, say, setup, until

ROOT = pathlib.Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"in the cloud|real android|cloud phone|emulator|virtual|container|redroid|\blima\b|\bown phone|\breal phone|physical|games phone|cloud mobile", re.I)
# literals that are internal identifiers, not words anyone reads
INTERNAL = re.compile(r"^(cloud phone 2|cloud phone|games phone|emulator|virtual|virtual:|virtual:games|emulator-|emulator-5554|redroid\.sh|virtual-android|virtual-android\.log|emulator\.sh|emulator\.log|\.lima|virtuals|is_emulator|cloudemulator)$"
                      r"|virtual table|emulator-5554|Physical (size|density)|sdk_\|gphone|redroid\.sh|virtual-android|emulator\.|\\bredroid|\\b\(\?:|games\[ -\]phone|cloud phone 2\|cloud phone", re.I)
NEUTRAL_FILES = ["agent/orchestrator.py", "agent/phones.py", "agent/master.py", "agent/jobs.py", "agent/service.py", "agent/escalate.py", "agent/a2a.py", "agent/a2a_exec.py",
                 "agent/a2a_rpc.py", "agent/a2a_server.py", "agent/chat_api.py", "agent/remote.py", "agent/cloudtools.py", "agent/loop.py", "agent/graph.py", "agent/gateway.py",
                 "agent/phoneinstall.py", "agent/otprelay.py", "agent/apkcheck.py", "agent/report.py", "agent/reportsave.py", "agent/artifacts.py", "agent/options.py",
                 "agent/adbphones.py", "agent/moveapp.py", "agent/websearch.py", "agent/images.py", "agent/museimage.py", "agent/knowledge.py", "agent/connectors.py"]


def literals(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant) and isinstance(n.body[0].value.value, str)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            yield n.lineno, n.value


class Sweep(unittest.TestCase):
    def test_no_python_string_the_owner_or_a_model_reads_names_what_a_phone_is_made_of(self):
        bad = []
        for rel in NEUTRAL_FILES:
            for line, text in literals(ROOT / rel):
                if FORBIDDEN.search(text) and not INTERNAL.search(text):
                    bad.append(f"{rel}:{line}: {text[:70]!r}")
        self.assertEqual(bad, [])

    def test_prompts_and_docs_pages_say_phone_phone_2_phone_3(self):
        for rel in ("prompts/orchestrator.md", "prompts/master.md", "prompts/worker_request.md"):
            for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                if line.startswith("- NAMES:"):
                    continue  # the naming rule lists the words it forbids
                self.assertIsNone(FORBIDDEN.search(line), f"{rel}:{n}: {line[:80]}")

    def test_the_page_never_shows_the_old_words(self):
        bad = []
        for rel in ("web/index.html", "web/connect.js", "web/replay.html"):
            for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                code = re.sub(r"//.*$", "", line) if not re.search(r"https?://", line) else line
                if re.search(r"cloud phone|physical|container|redroid|games phone|\bown phone|\breal phone|virtual (android|phone|device)", code, re.I):
                    bad.append(f"{rel}:{n}")
        self.assertEqual(bad, [])

    def test_the_replay_demo_data_uses_the_neutral_names(self):
        for rel in ("web/replay/index.json", "web/replay/demo/session.json"):
            text = (ROOT / rel).read_text()
            json.loads(text)
            for m in re.finditer(r'"([^"]*)"', text):
                v = m.group(1)
                if v not in ("emulator",):  # "emulator" alone is the replay's internal phone key
                    self.assertIsNone(re.search(r"cloud phone|emulator|virtual", v, re.I), (rel, v))

    def test_the_directory_and_default_names_for_every_phone(self):
        from agent import phones
        db = Database(":memory:")
        master.ensure_games_bot(db, None)
        one = master.ensure_phone_bot(db, None, "p1", {"name": "2107113SI", "model": "2107113SI"})
        two = master.ensure_phone_bot(db, None, "p2", {"name": "Pixel 8"})
        self.assertEqual((one["name"], two["name"]), ("2107113SI", "Pixel 8"))  # a connected phone is named after the device
        self.assertEqual(master.ensure_phone_bot(db, None, "p1", {})["name"], "2107113SI")  # once per phone
        three = master.ensure_phone_bot(db, None, "p3", {"name": "Pixel 8"})
        self.assertEqual(three["name"], "Pixel 8 2")  # two phones never share a name
        self.assertEqual(master.ensure_phone_bot(db, None, "p4", {"name": "Phone 2"})["name"], "Phone 2 2")  # and never take the fixed names
        text = phones.render([{"key": "emulator", "label": names.SHARED, "bot": "x", "status": "ready", "can": phones.EMULATOR, "known_bad": [], "runs": []}], None, master=True)
        self.assertIsNone(FORBIDDEN.search(text.replace("Phone 2", "")) , text[:200])


class Aliases(unittest.TestCase):
    def test_the_new_and_the_older_spellings_name_the_same_phones(self):
        for spoken in ("phone", "Phone", "cloud phone", "Cloud Phone", "emulator", "shared emulator", "main", "Phone 1"):
            self.assertEqual(names.alias_key(spoken), "emulator", spoken)
        for spoken in ("phone 2", "Phone 2", "cloud phone 2", "second cloud phone", "games", "virtual", "phone2"):
            self.assertEqual(names.alias_key(spoken), "games", spoken)
        for spoken in ("phone 3", "Pixel", "", None, "phones"):
            self.assertIsNone(names.alias_key(spoken))
        self.assertEqual(names.next_paired_name([]), "Phone 3")
        self.assertEqual(names.next_paired_name(["Phone 3", "Phone 5", "Phone 4"]), "Phone 6")
        self.assertEqual(names.next_paired_name(["Phone 2", "Phone"]), "Phone 3")

    def test_phone_check_and_check_apk_accept_both_spellings(self):
        c, _, bot, app = setup(Spark())
        orch = app.state.orch
        for on in ("phone", "cloud phone", "emulator"):
            self.assertEqual(orch._phone_key(bot, on), "emulator")
        for on in ("phone 2", "cloud phone 2", "games"):
            self.assertEqual(orch._phone_key(bot, on), "games")
        err = orch._phone_key(bot, "tablet")
        self.assertTrue(err.startswith("error: unknown phone 'tablet'; use phone, phone 2"), err)

    def test_delegating_to_phone_2_by_an_older_name_still_finds_its_bot(self):
        db = Database(":memory:")
        games = master.ensure_games_bot(db, None)
        from agent.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        orch.db = db
        for spoken in ("Phone 2", "cloud phone 2", "games"):
            self.assertEqual(orch._find_bot(spoken, ())["id"], games["id"], spoken)


class Migration(unittest.TestCase):
    def test_the_second_phone_loses_its_old_name_and_numbered_phones_get_their_device_name_back_once(self):
        db = Database(":memory:")
        games = db.create_bot("Cloud phone 2", "old persona", kind="virtual", phone_id="games")
        numbered = db.create_bot("Phone 3", "p", kind="phone", phone_id="d1")  # an earlier version numbered connected phones
        mine = db.create_bot("Dad's phone", "p", kind="phone", phone_id="d2")  # chosen by the owner
        model = db.create_bot("2107113SI", "p", kind="phone", phone_id="d3")  # already right
        out = master.migrate_phone_names(db, {"d1": "2107113SI", "d2": "Pixel 7", "d3": "2107113SI"})
        self.assertEqual(sorted(out), ["2107113SI 2", "Phone 2"])  # d3 already carries the name, so d1 gets the next free one
        self.assertEqual(db.get_bot(games["id"])["name"], "Phone 2")
        self.assertEqual(db.get_bot(numbered["id"])["name"], "2107113SI 2")
        self.assertEqual((db.get_bot(mine["id"])["name"], db.get_bot(model["id"])["name"]), ("Dad's phone", "2107113SI"))
        self.assertEqual(master.migrate_phone_names(db, {"d1": "2107113SI"}), [])  # once
        db.rename_bot(numbered["id"], "Phone 3")  # the owner chooses that name later
        self.assertEqual(master.migrate_phone_names(db, {"d1": "2107113SI"}), [])  # and it stays
        self.assertEqual(db.get_bot(numbered["id"])["name"], "Phone 3")

    def test_library_labels_are_renamed_and_chat_history_is_left_alone(self):
        db = Database(":memory:")
        bot = db.create_bot("Assistant", "", kind="bot")
        db.add_library(bot["id"], "j1", "Link check on Cloud phone 2", "/x.png")
        db.add_library(bot["id"], "j2", "Guide on Cloud phone", "/y.png")
        msg = db.add_message(bot["id"], "assistant", "text", "Ran it on Cloud phone 2")
        master.migrate_phone_names(db)
        self.assertEqual(sorted(r["title"] for r in db.library(bot["id"])), ["Guide on Phone", "Link check on Phone 2"])
        self.assertEqual(db.get_message(msg["id"])["content"], "Ran it on Cloud phone 2")  # history is not rewritten
        self.assertEqual(master.migrate_phone_names(db), [])  # idempotent

    def test_history_is_kept_and_the_app_starts_with_the_migration(self):
        c, _, bot, app = setup(Spark())
        db = app.state.db
        old = db.create_bot("Cloud phone 2", "x", kind="virtual", phone_id="games2")
        db.add_message(old["id"], "assistant", "text", "kept")
        master.migrate_phone_names(db)
        self.assertEqual(db.get_bot(old["id"])["name"], "Phone 2")
        self.assertEqual([m["content"] for m in db.messages(old["id"], 5)], ["kept"])


class InChat(unittest.TestCase):
    def test_what_the_model_is_told_about_the_phones_uses_the_new_names_only(self):
        spark = Spark(act(type="reply", text="ok"))
        c, _, bot, app = setup(spark)
        say(c, bot, "hello")
        until(lambda: spark.calls)
        system = spark.calls[0][0]
        self.assertIn("Phone: Google Play", system)
        directory = system[system.index("Phones you can know about"):]
        self.assertIsNone(FORBIDDEN.search(directory), FORBIDDEN.search(directory) and directory[FORBIDDEN.search(directory).start() - 40:FORBIDDEN.search(directory).end() + 40])


if __name__ == "__main__":
    unittest.main()
