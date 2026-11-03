"""Save a finished report: the HTML page and its Markdown twin in the Library, and a card in the chat."""
from pathlib import Path

from agent import report as report_mod
from agent.artifacts import Artifacts


def save(db, hub, data_dir, bot_id: str, job_id: str | None, kind: str, title: str, goal: str, status: str, answer: str, art: Artifacts, phone: str = "",
         extra_facts: list[str] | None = None) -> dict:
    """Build the report from what was captured, store it and tell the chat. Returns the Library item."""
    facts = list(art.facts) + list(extra_facts or [])
    md, page = report_mod.build(kind, title, goal, status, answer, art.captures, art.verdict, phone, facts, mask=db.mask)
    lib_dir = Path(data_dir) / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{job_id or 'check'}-{__import__('uuid').uuid4().hex[:6]}-report"
    path = lib_dir / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    path.with_suffix(".md").write_text(md, encoding="utf-8")
    item = db.add_library(bot_id, job_id, title, str(path), kind="report")
    db.knowledge.add(md, kind="report", visibility="bots", bot_id=bot_id, title=title, source=item["id"])  # the report's words can be found by every bot
    level = art.verdict["level"] if art.verdict else None
    text = (f"{report_mod.VERDICT_LABEL[level]}. " + "; ".join(art.verdict["reasons"][:3])) if level else f"{title} ({len(art.captures)} picture{'' if len(art.captures) == 1 else 's'})."
    msg = db.add_message(bot_id, "assistant", "report", text, {"library_id": item["id"], "title": title[:100], "verdict": level, "pictures": len(art.captures), "report": kind})
    hub.publish(bot_id, {"type": "message", "message": msg})
    hub.publish(bot_id, {"type": "refresh", "what": "library"})
    return item
