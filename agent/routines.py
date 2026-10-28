"""Routines: recurring tasks a bot runs on a schedule, so the bot messages the owner first.

A schedule is {"every_minutes": N} (5 to 10080) or {"daily_at": "HH:MM"} in the server's local time. When a routine is due the
scheduler gives the orchestrator a note ("routine X fired: ...") and the bot answers or starts a phone job on its own. After a long
downtime a routine runs once, not once per missed slot.
"""
import threading
import time
from datetime import datetime, timedelta

from agent.db import Database
from agent.orchestrator import Orchestrator

MIN_MINUTES, MAX_MINUTES = 5, 10080
TICK_SECONDS = 15


def parse_schedule(raw: dict) -> dict:
    """Validated schedule dict; raises ValueError with a message the model or owner can act on."""
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    if "every_minutes" in raw:
        try:
            n = int(raw["every_minutes"])
        except (TypeError, ValueError):
            raise ValueError("every_minutes must be a whole number") from None
        if not MIN_MINUTES <= n <= MAX_MINUTES:
            raise ValueError(f"every_minutes must be between {MIN_MINUTES} and {MAX_MINUTES}")
        return {"every_minutes": n}
    if "daily_at" in raw:
        try:
            t = datetime.strptime(str(raw["daily_at"]).strip(), "%H:%M")
        except ValueError:
            raise ValueError("daily_at must look like 07:30 (24-hour)") from None
        return {"daily_at": t.strftime("%H:%M")}
    raise ValueError('schedule needs "every_minutes" or "daily_at"')


def next_run(schedule: dict, after: float) -> float:
    """The first fire time strictly after `after` (epoch seconds)."""
    if "every_minutes" in schedule:
        return after + schedule["every_minutes"] * 60
    hh, mm = map(int, schedule["daily_at"].split(":"))
    now = datetime.fromtimestamp(after).astimezone()
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.timestamp()


def describe(schedule: dict) -> str:
    if "every_minutes" in schedule:
        n = schedule["every_minutes"]
        return "every day" if n == 1440 else f"every {n // 60} h" if n % 60 == 0 and n >= 60 else f"every {n} min"
    return f"daily at {schedule['daily_at']}"


class Scheduler:
    def __init__(self, db: Database, orch: Orchestrator, clock=time.time):
        self.db, self.orch, self.clock = db, orch, clock
        self._stop = threading.Event()

    def add(self, bot_id: str, name: str, prompt: str, schedule: dict) -> dict:
        sched = parse_schedule(schedule)
        if not name.strip() or not prompt.strip():
            raise ValueError("name and prompt are required")
        return self.db.routine_add(bot_id, name.strip()[:60], prompt.strip()[:1000], sched, next_run(sched, self.clock()))

    def fire(self, routine: dict, manual: bool = False) -> None:
        """Hand the routine to the bot as a system note; the bot decides to reply or start a phone job."""
        r = routine
        if not manual:  # reschedule first so a slow turn cannot cause a double fire
            self.db.routine_update(r["id"], next_run=next_run(r["schedule"], self.clock()), last_run=self.clock())
        else:
            self.db.routine_update(r["id"], last_run=self.clock())
        note = (f"The owner's routine '{r['name']}' ({describe(r['schedule'])}) has just fired. The owner did not message you: you are reaching out first. "
                f"Routine instructions: {r['prompt']}\nDo it now. If it needs the phone, start a mobile_task; otherwise reply with the result, briefly.")
        self.orch.turn(r["bot_id"], note)

    def tick(self) -> int:
        """Fire everything due. Returns how many fired."""
        due = self.db.routines_due(self.clock())
        for r in due:
            threading.Thread(target=self.fire, args=(r,), daemon=True, name=f"routine-{r['id']}").start()
            self.db.routine_update(r["id"], next_run=next_run(r["schedule"], self.clock()))  # claim it before the thread runs
        return len(due)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(TICK_SECONDS):
                try:
                    self.tick()
                except Exception:  # the scheduler must outlive a bad row
                    pass
        threading.Thread(target=loop, daemon=True, name="routines").start()

    def stop(self) -> None:
        self._stop.set()
