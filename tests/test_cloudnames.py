"""The phones have generic names for the owner ("Cloud phone", "Cloud phone 2"), and the panel shows only phones that are working."""
import pathlib
import re
import unittest

from agent import master, phones
from agent.db import Database

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Names(unittest.TestCase):
    def test_an_old_games_phone_bot_is_renamed_and_keeps_its_history(self):
        db = Database(":memory:")
        old = db.create_bot("Games phone", "The bot of the games phone", kind="virtual", phone_id="games")
        db.add_message(old["id"], "assistant", "text", "an earlier message")
        bot = master.ensure_games_bot(db, None)
        self.assertEqual(bot["id"], old["id"])
        self.assertEqual(bot["name"], "Cloud phone 2")
        self.assertNotIn("games", bot["persona"].lower())
        self.assertEqual([m["content"] for m in db.messages(old["id"], 10)], ["an earlier message"])
        self.assertEqual(master.ensure_games_bot(db, None)["name"], "Cloud phone 2")  # a second start changes nothing

    def test_a_new_second_phone_bot_has_the_generic_name_and_no_games_wording(self):
        bot = master.ensure_games_bot(Database(":memory:"), None)
        self.assertEqual(bot["name"], "Cloud phone 2")
        for text in (bot["persona"], master.GAMES_INTRO):
            self.assertNotIn("games phone", text.lower())
            self.assertNotIn("for games", text.lower())
        self.assertIn("no Google Play", master.GAMES_INTRO)
        self.assertIn("refuse to run on virtual devices", master.GAMES_INTRO)  # the honest-limits sentence stays

    def test_nothing_the_model_reads_or_the_owner_sees_calls_the_phone_the_games_phone(self):
        offenders = []
        for path in list((ROOT / "agent").glob("*.py")) + list((ROOT / "prompts").glob("*.md")) + [ROOT / "README.md", ROOT / "web" / "index.html", ROOT / "web" / "replay.html"]:
            lines = [ln for ln in path.read_text().splitlines()
                     if "== \"Games phone\"" not in ln and "never say \"emulator\"" not in ln]  # the old-name migration and the naming rule itself name it on purpose
            if re.search(r"games phone", "\n".join(lines), re.I):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_the_directory_and_the_prompts_name_the_phones_for_the_model(self):
        self.assertIn("Cloud phone (the shared emulator)", phones.EMULATOR)
        self.assertTrue(phones.GAMES.startswith("Cloud phone 2"))
        prompt = (ROOT / "prompts" / "orchestrator.md").read_text()
        self.assertIn('"Cloud phone" (the shared one with Google Play), "Cloud phone 2"', prompt)
        self.assertIn('never say "emulator", "games phone" or "Redroid"', prompt)


class Panel(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "web" / "index.html").read_text()

    def test_the_sidebar_is_a_close_button_an_avatar_pill_tabs_and_one_card(self):
        for needle in ('id="panelClose"', 'class="av xl"', 'role="tablist"', 'data-tab="mobile"', 'role="tabpanel"', 'cs.id = "connectSlot"', "function phoneUp(", "function paintWork("):
            self.assertIn(needle, self.html)
        self.assertNotIn("Not paired", self.html)  # no empty 'your phone' tile
        for gone in ('id="benchIdle"', 'id="benchStrip"', 'class="benchstrip"', "function idleChip(", "function drawPatch(", 'id="patch"'):
            self.assertNotIn(gone, self.html)      # nothing about phone state above the chat, no idle chips, no patch line

    def test_only_working_phones_get_a_chip_and_the_second_phone_stays_hidden_until_it_runs(self):
        body = self.html[self.html.index("const workingView"):self.html.index("async function renderTab()")]
        self.assertIn('d.p.kind === "real") return !!v.up', body)           # a physical phone shows only while it is connected
        self.assertIn('v.lamp === "on" || v.lamp === "wait"', body)         # a chip means a lane is working or waiting for you
        self.assertIn("openConnect", body)                                  # the connect dialog hook
        self.assertIn("if (realUp) cs.replaceChildren()", body)             # the button is gone while a phone is connected
        self.assertIn("It starts by itself when you give it a task.", self.html)  # an asleep second phone is a calm sentence, not a card

    def test_the_first_phone_is_called_cloud_phone_in_the_panel(self):
        self.assertIn('name: "Cloud phone", lane: "cloud phone"', self.html)
        self.assertIn('"cloud phone 2"', self.html)


if __name__ == "__main__":
    unittest.main()
