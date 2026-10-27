"""In-memory event log per bot, replayed to SSE clients. Events carry whole message objects, so clients upsert by id."""
import threading
import time


class Hub:
    def __init__(self, keep: int = 500):
        self.keep = keep
        self._events: dict[str, list[dict]] = {}
        self._base: dict[str, int] = {}
        self._lock = threading.Lock()

    def publish(self, bot_id: str, event: dict) -> int:
        with self._lock:
            log = self._events.setdefault(bot_id, [])
            base = self._base.setdefault(bot_id, 0)
            seq = base + len(log)
            log.append({**event, "seq": seq, "ts": round(time.time(), 2)})
            if len(log) > self.keep:
                drop = len(log) - self.keep
                del log[:drop]
                self._base[bot_id] = base + drop
            return seq

    def since(self, bot_id: str, after: int) -> list[dict]:
        with self._lock:
            log, base = self._events.get(bot_id, []), self._base.get(bot_id, 0)
            start = max(after + 1 - base, 0)
            return log[start:]

    def last_seq(self, bot_id: str) -> int:
        with self._lock:
            return self._base.get(bot_id, 0) + len(self._events.get(bot_id, [])) - 1
