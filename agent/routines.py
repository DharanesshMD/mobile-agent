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
MISSED_GRACE_S = 2 * 3600  # a daily routine more than this late (the server was off) waits for tomorrow instead of firing at a strange hour


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
        self.on_change = lambda bot_id: None  # the app hooks this to refresh open Details tabs

    def add(self, bot_id: str, name: str, prompt: str, schedule: dict) -> dict:
        sched = parse_schedule(schedule)
        if not name.strip() or not prompt.strip():
            raise ValueError("name and prompt are required")
        r = self.db.routine_add(bot_id, name.strip()[:60], prompt.strip()[:1000], sched, next_run(sched, self.clock()))
        self.on_change(bot_id)
        return r

    def busy(self, routine: dict) -> bool:
        return self.db.routine_busy(routine["id"])

    def fire(self, routine: dict, manual: bool = False) -> str:
        """Hand the routine to the bot as a system note; the bot decides to reply or start a phone job. Returns what happened.
        A routine whose last run is still queued or working is not started again: runs never pile up."""
        r = routine
        if not manual:  # reschedule first so a slow turn cannot cause a double fire
            self.db.routine_update(r["id"], next_run=next_run(r["schedule"], self.clock()))
        if self.busy(r):
            self.db.routine_update(r["id"], last_status="skipped: the last run is still working")
            self.on_change(r["bot_id"])
            return "busy"
        self.db.routine_update(r["id"], last_run=self.clock(), last_status="started")
        note = (f"The owner's routine '{r['name']}' ({describe(r['schedule'])}) has just fired. The owner did not message you: you are reaching out first. "
                f"Routine instructions: {r['prompt']}\nDo it now. If it needs the phone, start a mobile_task; otherwise reply with the result, briefly.")
        self.on_change(r["bot_id"])
        self.orch.turn(r["bot_id"], note, routine=r["id"])
        if not self.busy(r) and (self.db.get_routine(r["id"]) or {}).get("last_status") == "started":
            self.db.routine_update(r["id"], last_status="replied")  # it answered in chat without needing the phone
            self.on_change(r["bot_id"])
        return "started"

    def tick(self) -> int:
        """Fire everything due. Returns how many fired."""
        now, due, fired = self.clock(), self.db.routines_due(self.clock()), 0
        for r in due:
            self.db.routine_update(r["id"], next_run=next_run(r["schedule"], now))  # claim it before anything else runs
            if "daily_at" in r["schedule"] and now - r["next_run"] > MISSED_GRACE_S:
                self.db.routine_update(r["id"], last_status="missed: the server was off at that time")
                self.orch.post(r["bot_id"], f"I missed the routine '{r['name']}' ({describe(r['schedule'])}) because the server was off. It will run again at its next time.",
                               "bot_note", {"routine": r["id"]})
                self.on_change(r["bot_id"])
                continue
            threading.Thread(target=self.fire, args=(r,), daemon=True, name=f"routine-{r['id']}").start()
            fired += 1
        return fired

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
