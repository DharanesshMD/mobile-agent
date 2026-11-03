"""Follow up fast when a task is waiting for the owner.

A card (an approval, a choice, a code, a hand-over) holds its phone task until the owner answers. The card is posted at once, but a chat that has scrolled or a
tab in the background hides it, so a short reminder is posted as a normal chat message (it also lights the bot's unread mark) after 45 seconds, again after 3 minutes
and a last time after 10. Three reminders at most, none for a card that was answered, declined or expired. The older 10-minute alert in Master's chat stays as it is.
"""
import logging
import threading
import time

from agent.db import Database
from agent.hub import Hub

log = logging.getLogger("agent.orch")
AFTER_S = (45, 180, 600)  # seconds after the card was created
CHECK_S = 5
HOW = {
    "confirm": "Approve or decline on the card above.",
    "choice": "Pick an option on the card above.",
    "text": "Type your answer on the card above.",
    "otp": "Enter the code on the card above, or use the one from your phone if it shows up there.",
    "handoff": "Do your part on the phone, then press Done on the card above.",
    "secret": "Add it on the card above (it is stored encrypted and never shown again).",
}
LEAD = ("Waiting for you", "Still waiting for you", "This is still open and the task cannot go on without you")


def clip(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class Nudger:
    def __init__(self, db: Database, hub: Hub, clock=time.time, after: tuple = AFTER_S):
        self.db, self.hub, self.clock, self.after = db, hub, clock, after
        self.sent: dict[str, int] = {}  # card id -> reminders posted
        self._stop = threading.Event()

    def check(self) -> int:
        now, posted = self.clock(), 0
        open_ids = set()
        for it in self.db.open_items():
            open_ids.add(it["id"])
            k = self.sent.get(it["id"], 0)
            if k >= len(self.after) or now - it["created"] < self.after[k]:
                continue
            bot = self.db.get_bot(it["bot_id"])
            if not bot:
                continue
            q = clip(it["payload"].get("question", ""))
            text = f"{LEAD[k]}: {q}\n{HOW.get(it['kind'], 'Answer on the card above.')}"
            msg = self.db.add_message(bot["id"], "assistant", "text", text, {"nudge": it["id"]})
            self.hub.publish(bot["id"], {"type": "message", "message": msg})
            self.sent[it["id"]] = k + 1
            posted += 1
            log.info("reminded the owner about card %s (%s, reminder %d)", it["id"], it["kind"], k + 1)
        for gone in [i for i in self.sent if i not in open_ids]:
            del self.sent[gone]  # answered, declined or expired
        return posted

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(CHECK_S):
                try:
                    self.check()
                except Exception:
                    log.exception("reminder check failed")
        threading.Thread(target=loop, daemon=True, name="owner-reminders").start()

    def stop(self) -> None:
        self._stop.set()
