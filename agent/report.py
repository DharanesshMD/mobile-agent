"""Build a report (Markdown and one self-contained HTML page) from what a phone job captured.

The page has no scripts and no outside requests: text is escaped, pictures are inline JPEGs. It states what was NOT checked, always: a clean result
is "nothing found in these checks", never "safe".
"""
import base64
import html
import re
import time

REPORT_TYPES = ("link", "apk", "matrix", "guide", "plain")
TITLES = {"link": "Link check", "apk": "App check", "matrix": "Look and feel", "guide": "Step-by-step guide", "plain": "Report"}
NOT_CHECKED = {
    "link": ["Files the page offers to download (nothing was downloaded or opened)", "What happens after signing in or paying (nothing was typed)",
             "Pages that look different for other visitors, countries or devices", "Outside reputation lists beyond what the browser itself showed"],
    "apk": ["Network traffic and what the app sends out", "Behaviour after the first 20 seconds, or after signing in", "The app's code (this is not a malware scanner)",
            "Purchases, ads and accounts inside the app"],
    "matrix": ["Screens other than the ones shown", "Other devices, Android versions and languages", "Whether every string in the app is translated (only what was on screen)"],
    "guide": ["Other phones, brands and Android versions: menus can be named or placed differently", "Anything that needed a sign-in or a password (the owner does that)"],
    "plain": [],
}
VERDICT_LABEL = {"harmful": "Looks harmful", "suspicious": "Looks suspicious", "nothing_found": "Nothing found in these checks"}
VERDICT_COLOR = {"harmful": "#d64545", "suspicious": "#d9912b", "nothing_found": "#2e9d6a"}

CSS = """:root{color-scheme:light dark;--bg:#fff;--fg:#17181c;--dim:#5d6270;--card:#f4f5f8;--line:#d9dce4}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#eceef3;--dim:#9aa0ae;--card:#181b22;--line:#2a2e38}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:28px 0 8px}
.dim{color:var(--dim);font-size:14px}.badge{display:inline-block;color:#fff;border-radius:999px;padding:4px 14px;font-weight:650;margin:10px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px}
.card img{width:100%;height:auto;border-radius:8px;display:block}.card b{display:block;margin:8px 0 2px}ol.steps{padding-left:0;list-style:none;counter-reset:s}
ol.steps li{counter-increment:s;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;margin:0 0 14px}
ol.steps li:before{content:counter(s);display:inline-block;width:26px;height:26px;border-radius:50%;background:var(--fg);color:var(--bg);text-align:center;font-weight:650;margin-right:8px}
ol.steps img{max-width:320px;width:100%;border-radius:8px;display:block;margin-top:8px}ul{margin:6px 0}"""


URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)


LEAD = re.compile(r"^(?:please\s+)?(?:create|make|write|produce|build)\s+(?:me\s+)?(?:an?\s+)?(?:step[- ]by[- ]step\s+)?(?:guide|report|walkthrough)\b", re.I)


def _strip_filler(x: str) -> str:
    """"Create a guide with screenshots for my mom: how to open Keep" -> "open Keep"; "make a guide to turn on dark mode" -> "turn on dark mode"."""
    m = LEAD.match(x)
    if not m:
        return x
    rest = x[m.end():]
    rest = rest.split(":", 1)[1] if ":" in rest else re.sub(r"^\s*(?:with screenshots\s*)?(?:to|for|on)\s+", "", rest, flags=re.I)
    return re.sub(r"^how to\s+", "", rest.strip(), flags=re.I).strip() or x


def short_subject(goal: str) -> str:
    """What the task is about, in a few words: the first numbered part (the others become "+ N more"), without "create a guide to ..." filler."""
    first = goal.strip().splitlines()[0] if goal.strip() else ""
    parts = [x.strip() for x in re.split(r"(?:^|\s)\d\)\s+", first) if x.strip()]
    more = len(parts) - 1 if len(parts) > 1 else 0
    x = (parts[0] if parts else first).rstrip(" .;,")
    x = _strip_filler(x)
    if x:
        x = x[0].upper() + x[1:]
    if len(x) > 64:  # cut at a word, not in the middle of one
        x = x[:64].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return x + (f" + {more} more" if more else "")


def title_for(kind: str, goal: str) -> str:
    """A short title: for a link check the address itself, otherwise what the task is about."""
    if kind == "link" and (m := URL_RE.search(goal)):
        return f"{TITLES['link']}: {m.group(0)[:80]}"
    return f"{TITLES.get(kind, 'Report')}: {short_subject(goal)}".rstrip(": ")


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def build(kind: str, title: str, goal: str, status: str, answer: str, captures: list[dict], verdict: dict | None, phone: str = "", facts: list[str] | None = None,
          mask=lambda s: s, when: float | None = None) -> tuple[str, str]:
    """Returns (markdown, html). `mask` hides account addresses in every line of text."""
    kind = kind if kind in REPORT_TYPES else "plain"
    when_s = time.strftime("%d %b %Y %H:%M", time.localtime(when or time.time()))
    title, goal, answer = mask(title), mask(goal), mask(answer or "")
    facts = [mask(f) for f in (facts or [])]
    not_checked = list(NOT_CHECKED[kind]) + [mask(x) for x in (verdict or {}).get("not_checked", [])]
    checked = [mask(x) for x in (verdict or {}).get("checked", [])]
    reasons = [mask(x) for x in (verdict or {}).get("reasons", [])]
    meta = f"{TITLES[kind]} · {when_s}" + (f" · {phone}" if phone else "") + f" · task {status}"

    md = [f"# {title}", "", f"_{meta}_", ""]
    if verdict:
        md += [f"**{VERDICT_LABEL[verdict['level']]}**", ""] + [f"- {r}" for r in reasons] + [""]
    if answer:
        md += [answer, ""]
    if facts:
        md += ["## Details", ""] + [f"- {f}" for f in facts] + [""]
    if captures:
        md += ["## Screens", ""] + [f"{i}. **{mask(c['title'])}**" + (f": {mask(c['note'])}" if c["note"] else "") for i, c in enumerate(captures, 1)] + ["", "_(The pictures are in the HTML version.)_", ""]
    if checked:
        md += ["## What was checked", ""] + [f"- {x}" for x in checked] + [""]
    md += ["## What was NOT checked", ""] + ([f"- {x}" for x in not_checked] or ["- Only what is shown above."]) + [""]

    def img(c: dict) -> str:
        return f'<img alt="{_esc(mask(c["title"]))}" src="data:image/jpeg;base64,{base64.b64encode(c["jpeg"]).decode()}">'

    body = [f"<h1>{_esc(title)}</h1>", f'<div class="dim">{_esc(meta)}</div>']
    if verdict:
        body.append(f'<div class="badge" style="background:{VERDICT_COLOR[verdict["level"]]}">{_esc(VERDICT_LABEL[verdict["level"]])}</div>')
        if reasons:
            body.append("<ul>" + "".join(f"<li>{_esc(r)}</li>" for r in reasons) + "</ul>")
    if answer:
        body.append(f"<p>{_esc(answer)}</p>")
    if facts:
        body.append("<h2>Details</h2><ul>" + "".join(f"<li>{_esc(f)}</li>" for f in facts) + "</ul>")
    if captures and kind == "guide":
        body.append("<h2>Steps</h2><ol class=\"steps\">" + "".join(
            f"<li><b>{_esc(mask(c['title']))}</b>" + (f"<div>{_esc(mask(c['note']))}</div>" if c["note"] else "") + img(c) + "</li>" for c in captures) + "</ol>")
    elif captures:
        body.append("<h2>Screens</h2><div class=\"grid\">" + "".join(
            f'<div class="card">{img(c)}<b>{_esc(mask(c["title"]))}</b>' + (f'<span class="dim">{_esc(mask(c["note"]))}</span>' if c["note"] else "") + "</div>" for c in captures) + "</div>")
    if checked:
        body.append("<h2>What was checked</h2><ul>" + "".join(f"<li>{_esc(x)}</li>" for x in checked) + "</ul>")
    body.append("<h2>What was NOT checked</h2><ul>" + ("".join(f"<li>{_esc(x)}</li>" for x in not_checked) or "<li>Only what is shown above.</li>") + "</ul>")
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(title)}</title><style>{CSS}</style></head><body><main>{''.join(body)}</main></body></html>")
    return "\n".join(md), page
