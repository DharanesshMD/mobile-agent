"""Other ways to do something: web research that ends in a choice for the owner, never in an action.

`research` runs up to three searches, reads the top pages (and an obvious terms or privacy page of the unofficial candidates), asks the model to pick at most four
options, and then builds each option's risk facts in CODE from the text that was really read: a risk tag appears only with the sentence it came from, and what could not
be found is said so. Official routes (the store listing, the publisher's site, Google Play Games on PC) come first and are marked; third-party sites are marked unofficial.
Nothing here opens a page, logs in or plays: a pick only opens a link on a phone for the owner, who signs in themselves (agent/orchestrator.py `web_pick`).
"""
import json
import re
import urllib.parse

from agent import gamepolicy, websearch

MAX_OPTIONS = 4  # third-party options the model may pick
MAX_SHOWN = 5  # the most buttons a card shows besides "None": official routes first
NAMED_SITES = {  # a service the owner names is read at its own address (its homepage and its terms), not only through articles about it
    "easyfun": ("EasyFun.gg", "https://www.easyfun.gg/"), "cloudmoon": ("CloudMoon", "https://web.cloudmoonapp.com/"),
    "yaksgames": ("YaksGames", "https://www.yaksgames.com/"), "now.gg": ("now.gg", "https://now.gg/")}
SKIP_RESULT = re.compile(r"\.(pdf|docx?|zip|rar)(\?|$)|^ftp\.|/book-search/", re.I)
MAX_PAGES = 5
NONE_LABEL = "None, I'll decide later"

# tag -> pattern over the text of the pages that were read. A tag is reported only when its sentence is found.
RISKS: dict[str, re.Pattern] = {
    "login with a third-party account or a game ID": re.compile(r"(log ?in|sign ?in)[^.]{0,40}(with|using|via)[^.]{0,30}(google|facebook|apple|supercell id|e-?mail|game id)", re.I),
    "shares data with marketing or advertising partners": re.compile(r"(marketing|advertis\w*)\s+(partners?|networks?|companies|providers)|share[^.]{0,100}(advertis|marketing)", re.I),
    "the terms put no liability on the site for bans or account loss": re.compile(r"(not|no)\s+(be\s+)?(responsible|liable|liability)[^.]{0,140}(ban|suspend|terminat|account)", re.I),
    "time limits or a cost": re.compile(r"\b\d+\s*(min(ute)?s?|hours?)\s*(per|a|/|each)\s*(day|session|week)|free (time|minutes|plan|tier|trial)\b|subscription (plan|fee|required)|monthly (plan|fee)|(upgrade|go)\s+(to\s+)?(pro|premium|vip)\b|per month", re.I),
    "offers app (APK) downloads from outside the official store": re.compile(r"\bapk\b[^.]{0,60}download|download[^.]{0,60}\bapk\b|/download/[^\s]*\.?(apk)?|mod apk", re.I),
    "mentions Windows (a desktop client or a Windows requirement)": re.compile(r"\bwindows\s*(10|11|pc)\b|\bfor windows\b|\bon windows\b", re.I),
    "the terms mention bots or automation": re.compile(r"\b(bots?|automation|automated)\b[^.]{0,100}(ban|prohibit|forbid|not allowed)|(ban|prohibit|forbid)[^.]{0,100}\b(bots?|automation)\b", re.I),
}
OFFICIAL_DOMAINS = ("play.google.com", "apps.apple.com", "supercell.com", "nianticlabs.com", "scopely.com", "pokemon.com", "google.com")
OPERATOR = re.compile(r"(operated by|provided by|owned by|©\s*\d{4}|copyright\s*\d{4})[^.\n]{0,80}(ltd|limited|inc|llc|gmbh|corp|company|co\.)", re.I)

SLUGS = {"com.supercell.clashofclans": "clashofclans", "com.supercell.clashroyale": "clashroyale", "com.supercell.brawlstars": "brawlstars", "com.supercell.hayday": "hayday",
         "com.supercell.boombeach": "boombeach", "com.supercell.squadbusters": "squadbusters"}


def official_routes(game: str) -> list[dict]:
    """The publisher's own routes for a blocked game: curated, never searched for. Each one says what is not known."""
    pkg = next((p for p, n in gamepolicy.NAMES.items() if n == game), "")
    if not pkg:
        return []
    out = [{"name": f"{game} on Google Play (official)", "url": f"https://play.google.com/store/apps/details?id={pkg}", "official": True,
            "what": "The official store listing: install it on your own Android phone and play there.", "facts": [], "missing": []}]
    if pkg in SLUGS:
        out.append({"name": f"{game} on supercell.com (official)", "url": f"https://supercell.com/en/games/{SLUGS[pkg]}/", "official": True,
                    "what": "The publisher's own page: news, support and store links.", "facts": [], "missing": []})
    out.append({"name": "Google Play Games on PC (official)", "url": "https://play.google.com/googleplaygames", "official": True,
                "what": "Google's own way to play Android games on a Windows PC.", "facts": [("mentions Windows (a desktop client or a Windows requirement)", "Google Play Games on PC runs on Windows PCs, not on this Mac")],
                "missing": [f"whether {game} is on its list (not checked)"]})
    return out


def domain(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").removeprefix("www.")


def is_official(url: str) -> bool:
    d = domain(url)
    return any(d == o or d.endswith("." + o) for o in OFFICIAL_DOMAINS)


def sentence(text: str, m: re.Match) -> str:
    start = max(text.rfind(".", 0, m.start()) + 1, m.start() - 90)
    end = m.end() + 80
    return " ".join(text[start:end].split())[:200]


def extract_facts(texts: list[str], urls: list[str] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """(risk tag, the sentence it came from) for every risk found in the pages that were read, and the checks that found nothing."""
    blob = "\n".join([*texts, *(urls or [])])  # the address itself counts: a /download/ path is a fact about the page
    facts: list[tuple[str, str]] = []
    missing: list[str] = []
    m = OPERATOR.search(blob)
    if m:
        facts.append(("operator", sentence(blob, m)))
    else:
        missing.append("who runs it")
    for tag, rx in RISKS.items():
        m = rx.search(blob)
        if m:
            facts.append((tag, sentence(blob, m)))
        else:
            missing.append(tag)
    return facts, missing


def pick_prompt(goal: str, results: list[dict], pages: dict[str, str]) -> str:
    cands = "\n".join(f"{i + 1}. {r['title']} | {r['url']} | {r['snippet'][:200]}" for i, r in enumerate(results))
    excerpts = "\n".join(f"[{u}] {t[:500]}" for u, t in pages.items())
    return (f"The owner wants another way to do this: {goal}\n\nWeb search results (title | url | snippet):\n{cands}\n\nStart of the pages that were read:\n{excerpts}\n\n"
            "Pick at most 4 DIFFERENT options that really offer it, only from the results above (never invent a site or a url). Prefer the service's OWN site over articles, reviews or forums about it; skip PDFs and files. Official routes (the publisher's store listing, "
            "its own site, Google Play Games on PC) are added separately: do not list them. For each give: name, url (exactly as in the results), what (one plain line, only what "
            "the results say), unofficial (true if it is a third party). Reply with ONLY JSON: {\"options\":[{\"name\":\"...\",\"url\":\"https://...\",\"what\":\"...\",\"unofficial\":true}]}. "
            "If none really offers it, reply {\"options\":[]}.")


def parse_options(raw: str, results: list[dict]) -> list[dict]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("options", [])
    except (ValueError, AttributeError):
        return []
    known = {r["url"]: r for r in results}
    out, seen = [], set()
    for it in items if isinstance(items, list) else []:
        url = str(it.get("url", ""))
        if url not in known or domain(url) in seen:  # only addresses the search really returned, one option per site
            continue
        seen.add(domain(url))
        name = " ".join(str(it.get("name") or known[url]["title"]).split())
        if domain(url) not in name.lower():  # a long page title is cut at a word and names the site, so the buttons can be told apart
            name = (name[:44].rsplit(" ", 1)[0] if len(name) > 44 else name) + f" ({domain(url)})"
        out.append({"name": name[:70], "url": url, "official": is_official(url),
                    "what": " ".join(str(it.get("what") or known[url]["snippet"]).split())[:160]})
    return out[:MAX_OPTIONS]


def research(query: str, goal: str, model, provider=None, fetch=None, game: str = "", log=lambda *a: None, notes: list | None = None) -> list[dict]:
    """The options for the owner to choose from (official first), each with facts that come only from pages that were read."""
    provider = provider or websearch.choose_provider(model)
    fetch = fetch or websearch.fetch_page
    notes = notes if notes is not None else []
    results: list[dict] = []
    failed = 0
    for q in websearch.plan_queries(query, goal):
        log(q)
        try:
            results += provider.search(q)
        except Exception as e:
            failed += 1  # one failed search is not the end: the others still count
            if failed == 1:
                notes.append(f"the web search ({provider.name}) did not work: {str(e)[:100]}")
    if not results:
        notes.append("so the options below are only the official routes I already know, not what the web says")
    asked = f"{query} {goal}".lower()
    for key, (title, url) in NAMED_SITES.items():  # the owner named this service: read the service itself
        if key in asked and not any(domain(r["url"]) == domain(url) for r in results):
            results.insert(0, {"title": f"{title} (the service itself)", "url": url, "snippet": "Named in the request."})
    uniq, seen = [], set()
    for r in results:
        if r["url"] not in seen and r["url"].startswith("https://") and not SKIP_RESULT.search(r["url"]):
            seen.add(r["url"])
            uniq.append(r)
    uniq = uniq[:8]
    pages: dict[str, str] = {}
    links: dict[str, list] = {}
    for r in uniq[:MAX_PAGES]:
        try:
            pages[r["url"]], links[r["url"]] = fetch(r["url"])
        except Exception:
            continue  # a page that cannot be read is simply not a source
    options = official_routes(game) if game else []
    chosen: list[dict] = []
    if uniq:
        try:
            chosen = parse_options(model.chat("You compare ways to do something on the web and only report what the results say.", [{"role": "user", "content": pick_prompt(goal or query, uniq, pages)}]), uniq)
        except Exception:
            chosen = []
        if not chosen:  # the model failed or gave nothing usable: the top results, plainly
            chosen = [{"name": (r["title"][:44].rsplit(" ", 1)[0] if len(r["title"]) > 44 else r["title"]) + f" ({domain(r['url'])})", "url": r["url"], "official": is_official(r["url"]), "what": r["snippet"][:160]} for r in uniq[:3]]
    for o in chosen:
        if o["official"]:
            o.update(facts=[], missing=[])
            options.append(o)
            continue
        texts = [pages[o["url"]]] if o["url"] in pages else []
        for pol in websearch.policy_links(o["url"], links.get(o["url"], []), 2):  # an obvious terms or privacy page of the site itself
            try:
                texts.append(fetch(pol)[0])
                o.setdefault("sources", []).append(pol)
            except Exception:
                continue
        o["facts"], o["missing"] = extract_facts(texts, [o["url"]]) if texts else ([], ["the page could not be read, so nothing is known about its terms"])
        options.append(o)
    seen_d: set[str] = set()
    final = []
    for o in sorted(options, key=lambda x: not x["official"]):  # official routes first
        key = o["url"] if o["official"] else domain(o["url"])  # one option per third-party site; the official routes are different pages of big sites
        if key in seen_d:
            continue
        seen_d.add(key)
        final.append(o)
    return final[:MAX_SHOWN]


def card_text(options: list[dict], intro: str, notes: list | None = None) -> str:
    """The options as the card shows them: what it is, official or not, the risks that were found (with their sentence), what was not found, and the sources."""
    lines = [intro]
    for i, o in enumerate(options, 1):
        head = f"{i}. {o['name']} - {domain(o['url'])} - {'official' if o['official'] else 'UNOFFICIAL third party'}"
        risks = [f"{tag}" + (f' ("{ev[:90]}")' if ev and tag != "operator" else "") for tag, ev in o["facts"] if tag != "operator"]
        op = next((ev for tag, ev in o["facts"] if tag == "operator"), "")
        parts = [o["what"]]
        if op:
            parts.append(f"Operator: {op[:100]}")
        if risks:
            parts.append("Risks found: " + "; ".join(risks[:4]))
        missing = [m for m in o["missing"] if m != "who runs it" or not op][:4]
        if missing and not o["official"]:
            parts.append("Not found in what I read: " + ", ".join(missing))
        elif o["official"] and o["missing"]:
            parts.append("Not checked: " + ", ".join(o["missing"]))
        lines.append(head + "\n   " + "\n   ".join(parts) + "\n   Source: " + " ".join([o["url"], *o.get("sources", [])][:3]))
    if notes:
        lines.append("Note: " + "; ".join(notes) + ".")
    lines.append("Nothing runs until you pick. A pick only opens the link or shows it to you; you sign in yourself, and I never play it for you.")
    return "\n".join(lines)
