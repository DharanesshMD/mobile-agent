"""The phones have neutral names for the owner ("Phone", "Phone 2", "Phone 3" ...), and the panel shows only phones that are working."""
import pathlib
import re
import unittest

from agent import master, phones
from agent.db import Database

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Names(unittest.TestCase):
    def test_an_old_games_phone_bot_is_renamed_and_keeps_its_history(self):
        for old_name in ("Games phone", "Cloud phone 2"):
            db = Database(":memory:")
            old = db.create_bot(old_name, "The bot of the old name", kind="virtual", phone_id="games")
            db.add_message(old["id"], "assistant", "text", "an earlier message")
            bot = master.ensure_games_bot(db, None)
            self.assertEqual(bot["id"], old["id"])
            self.assertEqual(bot["name"], "Phone 2")
            self.assertNotIn("games", bot["persona"].lower())
            self.assertEqual([m["content"] for m in db.messages(old["id"], 10)], ["an earlier message"])
            self.assertEqual(master.ensure_games_bot(db, None)["name"], "Phone 2")  # a second start changes nothing

    def test_a_name_the_owner_chose_is_left_alone(self):
        db = Database(":memory:")
        old = db.create_bot("Arcade", "mine", kind="virtual", phone_id="games")
        self.assertEqual(master.ensure_games_bot(db, None)["name"], "Arcade")
        self.assertEqual(master.migrate_phone_names(db), [])
        self.assertEqual(db.get_bot(old["id"])["name"], "Arcade")

    def test_a_new_second_phone_bot_has_the_generic_name_and_no_games_wording(self):
        bot = master.ensure_games_bot(Database(":memory:"), None)
        self.assertEqual(bot["name"], "Phone 2")
        for text in (bot["persona"], master.GAMES_INTRO):
            self.assertNotIn("games phone", text.lower())
            self.assertNotIn("for games", text.lower())
        self.assertIn("no Google Play", master.GAMES_INTRO)
        self.assertIn("refuse to run here", master.GAMES_INTRO)  # the honest-limits sentence stays

    def test_the_directory_and_the_prompts_name_the_phones_for_the_model(self):
        self.assertTrue(phones.EMULATOR.startswith("Phone: "))
        self.assertTrue(phones.GAMES.startswith("Phone 2: "))
        prompt = (ROOT / "prompts" / "orchestrator.md").read_text()
        self.assertIn('the owner knows the phones only as "Phone", "Phone 2", "Phone 3"', prompt)


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
        self.assertIn('d.p.kind === "real") return !!v.up', body)           # a paired phone shows only while it is connected
        self.assertIn('v.lamp === "on" || v.lamp === "wait"', body)         # a chip means a lane is working or waiting for you
        self.assertIn("openConnect", body)                                  # the connect dialog hook
        self.assertIn("if (realUp) cs.replaceChildren()", body)             # the button is gone while a phone is connected
        self.assertIn("It starts by itself when you give it a task.", self.html)  # an asleep second phone is a calm sentence, not a card

    def test_the_first_phone_is_called_phone_in_the_panel(self):
        self.assertIn('name: "Phone", lane: "phone"', self.html)
        self.assertIn('"phone 2"', self.html)


if __name__ == "__main__":
    unittest.main()
