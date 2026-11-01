"""Game accounts the agent must never play for the owner.

Some publishers ban accounts they identify as bots, and an agent that taps through a game is a bot. This module lists the apps whose
publisher rules forbid automation, and the worker refuses every in-game input in them (agent/graph.py). Opening such an app, waiting,
taking screenshots, back/home, installing, moving it between phones and describing what is on screen stay allowed.

The built-in list holds only publishers whose public rules say so (source next to each entry). The owner can ADD packages in
`data/policy.json` ({"blocked": ["com.example.game"]}); the built-ins can never be removed from there.
"""
import json
import re
from pathlib import Path

POLICY_FILE = Path("data/policy.json")

# prefix -> (display name or "" for "any app of this publisher", publisher, where the rule is written)
BUILTIN: dict[str, tuple[str, str, str]] = {
    # Supercell Terms of Service, section 1.1 "Use Limitations": no "cheats, exploits, automation software, emulators, bots, hacks, mods"; section 1.2: accounts may be terminated.
    "com.supercell.": ("", "Supercell", "https://supercell.com/en/terms-of-service/"),
    # Pokémon GO Terms of Service (Niantic games, now run by Scopely): section 3.1 (cheating, unofficial third party software), section 3.5 (suspension or termination), section 6 (no bots or automation).
    "com.nianticlabs.": ("", "Niantic", "https://explore.scopely.com/terms/pokemongo/en"),
    "com.nianticproject.": ("", "Niantic", "https://explore.scopely.com/terms/pokemongo/en"),
}

NAMES = {  # well-known titles, for messages and for spotting them in a request
    "com.supercell.clashofclans": "Clash of Clans", "com.supercell.clashroyale": "Clash Royale", "com.supercell.brawlstars": "Brawl Stars",
    "com.supercell.hayday": "Hay Day", "com.supercell.boombeach": "Boom Beach", "com.supercell.squadbusters": "Squad Busters",
    "com.nianticlabs.pokemongo": "Pokémon GO", "com.nianticlabs.monsterhunter": "Monster Hunter Now", "com.nianticproject.ingress": "Ingress",
}
ALIASES = {  # how an owner writes the name in a request
    "clash of clans": "com.supercell.clashofclans", "coc": "com.supercell.clashofclans", "clash royale": "com.supercell.clashroyale",
    "brawl stars": "com.supercell.brawlstars", "hay day": "com.supercell.hayday", "hayday": "com.supercell.hayday",
    "boom beach": "com.supercell.boombeach", "squad busters": "com.supercell.squadbusters", "pokemon go": "com.nianticlabs.pokemongo",
    "pokémon go": "com.nianticlabs.pokemongo", "monster hunter now": "com.nianticlabs.monsterhunter", "ingress": "com.nianticproject.ingress",
}

REASON = ("automated play of {name} is blocked to protect your account: {publisher}'s rules forbid bots and automation and they can ban the account. "
          "Opening the app and describing what is on screen is fine; to play it yourself, use Take control.")
POLICY_PREFIX = "Not played, to protect your account: "  # a task ended by the guard starts like this; the orchestrator does not retry it

_cache: dict = {"stamp": None, "owner": []}


def _owner_list() -> list[str]:
    """Packages the owner added (re-read when the file changes). A broken file adds nothing and never removes a built-in."""
    try:
        stamp = POLICY_FILE.stat().st_mtime_ns
    except OSError:
        return []
    if _cache["stamp"] != stamp:
        try:
            data = json.loads(POLICY_FILE.read_text())
            items = data.get("blocked", []) if isinstance(data, dict) else []
            _cache.update(stamp=stamp, owner=[str(p).strip().lower() for p in items if re.fullmatch(r"[A-Za-z0-9_.]{3,}", str(p).strip())])
        except (OSError, ValueError):
            _cache.update(stamp=stamp, owner=[])
    return _cache["owner"]


def app_name(package: str) -> str:
    return NAMES.get(package.lower()) or package


def blocked(package: str | None) -> str | None:
    """Why the agent must not play this app, or None when it may."""
    if not package:
        return None
    p = package.lower()
    for prefix, (_, publisher, _src) in BUILTIN.items():
        if p.startswith(prefix):
            return REASON.format(name=app_name(package), publisher=publisher)
    for owned in _owner_list():
        if p == owned or p.startswith(owned.rstrip(".") + "."):
            return REASON.format(name=app_name(package), publisher="the publisher's")
    return None


def mentions_blocked(text: str) -> list[str]:
    """Names of blocked apps a request talks about ("open clash of clans and gather my resources" -> ["Clash of Clans"])."""
    low = " " + re.sub(r"[^a-z0-9é ]+", " ", text.lower()) + " "
    found = [app_name(pkg) for alias, pkg in ALIASES.items() if f" {alias} " in low]
    found += [app_name(p) for p in _owner_list() if re.sub(r"[^a-z0-9]+", " ", p.split(".")[-1]) in low and len(p.split(".")[-1]) > 3]
    return sorted(set(found))


INPUT_KEYS_ALLOWED = ("back", "home")  # navigating away is never gameplay


def is_input(action: dict) -> bool:
    """Does this action send input into whatever is on screen? (taps, scrolls, typing and keys other than back/home)"""
    kind = action.get("type")
    if kind in ("tap", "type", "swipe", "scroll", "type_secret"):
        return True
    return kind == "key" and action.get("key") not in INPUT_KEYS_ALLOWED


# --- cloud gaming in a browser --------------------------------------------------------------------------------------------------------------------
# Playing a blocked game through a cloud-gaming site is still automated play of the account, so the guard looks at the page a browser is on.
BROWSERS = ("com.android.chrome", "com.chrome.beta", "com.chrome.dev", "com.chrome.canary", "org.mozilla.firefox", "org.mozilla.firefox_beta", "org.mozilla.fenix",
            "org.mozilla.focus", "com.microsoft.emmx", "com.brave.browser", "com.opera.browser", "com.opera.mini.native", "com.sec.android.app.sbrowser",
            "com.duckduckgo.mobile.android", "com.vivaldi.browser", "com.kiwibrowser.browser", "com.android.browser", "org.chromium.webview_shell",
            "org.chromium.chrome", "com.google.android.apps.chrome")
CLOUD_HOSTS = ("cloudmoon", "easyfun.gg", "yaksgames", "now.gg", "cloudemulator", "redfinger", "ugphone", "geelark")  # sites that stream a phone game to a browser
HINTS = ("play now", "start game", "start playing", "launch game", "loading game", "in queue", "queue position", "streaming", "cloud gaming", "play online", "play in browser", "play for free")
URL_RE = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?([/?#]\S*)?$", re.I)
_CLICK_OK = re.compile(r"close tab|close all tabs|switch or close tabs|new tab|open tab switcher|tab switcher", re.I)
PLAY_CUE = re.compile(r"\b(play|farm|gather|collect|grind|raid|attack|battle|train|upgrade|build|donate|trophy|trophies|resources|loot|win|run)\b", re.I)
CLOUD_CUE = re.compile(r"\b(browser|chrome|firefox|cloud|cloudmoon|easyfun|yaksgames|now\.gg|website|web ?site|online|streaming|stream)\b", re.I)


def _game_in(text: str) -> str | None:
    """The blocked game a piece of page text names (also as a url slug: clash-of-clans, clashofclans)."""
    low = re.sub(r"[^a-z0-9é]+", " ", text.lower())
    padded, compact = f" {low} ", low.replace(" ", "")
    for alias, pkg in ALIASES.items():
        if f" {alias} " in padded or (len(alias) >= 6 and alias.replace(" ", "") in compact):
            return app_name(pkg)
    for owned in _owner_list():
        tail = owned.split(".")[-1]
        if len(tail) > 3 and (re.sub(r"[^a-z0-9]+", " ", tail) in padded or tail in compact):
            return app_name(owned)
    return None


def browser_page(package: str | None, elements: list, size: tuple[int, int], was_blocked: str = "") -> str:
    """The blocked game a browser page is streaming, or "". Conservative on purpose: a search page, an article or a forum about the game is not gameplay.
    A page counts only when its address (the bar at the top) is a cloud-gaming site AND the page is about a blocked game (its address or a play/stream page). With no
    address bar on screen (a full-screen stream hides it) the last verdict stays until the browser shows another address."""
    if not package or package.lower() not in BROWSERS:
        return ""
    height = max(size[1] if size else 0, 1)
    url = next((e.label for e in elements if e.center[1] < height * 0.2 and " " not in e.label.strip() and URL_RE.match(e.label.strip())), "")
    if not url:
        return was_blocked  # no address visible: keep what the last screen showed
    low = url.lower()
    host = re.sub(r"^https?://", "", low).split("/")[0].split("?")[0].split("#")[0]  # only the site itself counts, never a search phrase in the address
    if not any(h in host for h in CLOUD_HOSTS):
        return ""  # a search engine, an article, the publisher's own site: reading is fine
    game = _game_in(low)
    if game:
        return game
    text = " ".join(e.label for e in elements).lower()
    game = _game_in(text)
    if game and (any(h in text for h in HINTS) or len(elements) <= 8):
        return game  # the cloud site's player page for that game: a stream, a queue or its loading screen
    return ""


def browser_click_ok(action: dict, elements: list) -> bool:
    """On a blocked stream page, tapping to close the tab or open another is still allowed (leaving is never gameplay)."""
    if action.get("type") != "tap" or "element" not in action:
        return False
    e = next((x for x in elements if x.id == int(action["element"])), None)
    return bool(e and _CLICK_OK.search(e.label))


def cloud_play_request(text: str) -> str | None:
    """A request to PLAY a blocked game through a browser or a cloud-gaming site ("play clash of clans on cloudmoon in chrome"): the game's name, else None."""
    found = mentions_blocked(text)
    return found[0] if found and PLAY_CUE.search(text) and CLOUD_CUE.search(text) else None


# --- third-party sites picked from "other ways": the agent never types a login there ---------------------------------------------------------------
THIRD_PARTY_FILE = Path("data/third_party_sites.json")  # hosts the owner opened from a web_options card (written by the app, never by the owner)
CREDS_PREFIX = "Not typed, to protect your account: "
POLICY_PREFIXES = (POLICY_PREFIX, CREDS_PREFIX)


def mark_third_party(host: str) -> None:
    host = (host or "").lower().strip(".")
    if not host:
        return
    try:
        known = json.loads(THIRD_PARTY_FILE.read_text()) if THIRD_PARTY_FILE.exists() else []
        if host not in known:
            THIRD_PARTY_FILE.parent.mkdir(parents=True, exist_ok=True)
            THIRD_PARTY_FILE.write_text(json.dumps(sorted({*known, host})))
    except (OSError, ValueError):
        pass


def third_party_hosts() -> tuple[str, ...]:
    try:
        extra = json.loads(THIRD_PARTY_FILE.read_text()) if THIRD_PARTY_FILE.exists() else []
    except (OSError, ValueError):
        extra = []
    return (*CLOUD_HOSTS, *[h for h in extra if isinstance(h, str)])


def _address(elements: list, size: tuple[int, int]) -> str:
    height = max(size[1] if size else 0, 1)
    return next((e.label.strip() for e in elements if e.center[1] < height * 0.2 and " " not in e.label.strip() and URL_RE.match(e.label.strip())), "")


def site_host(url: str) -> str:
    return re.sub(r"^https?://", "", url.lower()).split("/")[0].split("?")[0].split("#")[0]


def browser_site(package: str | None, elements: list, size: tuple[int, int], was: str = "") -> str:
    """The third-party gaming/service host a browser is on ("" if none): nothing is typed there for the owner. Like browser_page, a hidden address bar keeps the last verdict."""
    if not package or package.lower() not in BROWSERS:
        return ""
    url = _address(elements, size)
    if not url:
        return was
    host = site_host(url)
    return host if any(h in host for h in third_party_hosts()) else ""


CREDS_REASON = ("I never type a login, a code or any text on {host}, a third-party site: sign in yourself (I can hand the phone to you) or use the official app. "
                "That includes saved passwords.")
