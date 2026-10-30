"""Facts read straight from the phone (no model, no screenshots), used to prove a task really finished.

A check is {"kind": "app_installed", "query": "clash of clans"}, {"kind": "account"} (a Google account is signed in) or
{"kind": "foreground", "query": "instagram"}. `run_check` returns (ok, detail); the detail is safe to show the orchestrator (it never contains account names).
"""
import re

KINDS = ("app_installed", "account", "foreground")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_packages(packages: list[str], query: str) -> list[str]:
    """Installed packages that match an app name: the whole name first ("clash of clans" -> com.supercell.clashofclans), else its longest word."""
    whole = _norm(query)
    if not whole:
        return []
    hits = [p for p in packages if whole in _norm(p)]
    if not hits:
        word = max(re.findall(r"[a-z0-9]+", query.lower()), key=len, default="")
        hits = [p for p in packages if len(word) >= 4 and word in _norm(p)]
    return sorted(hits)[:8]


def parse_focus(dumpsys_window: str) -> str:
    """The package of the app in front, from `dumpsys window` (mCurrentFocus=Window{... u0 com.pkg/com.pkg.Activity})."""
    m = re.search(r"mCurrentFocus=Window\{[^}]*? ([\w.]+)/", dumpsys_window) or re.search(r"mFocusedApp=.*? ([\w.]+)/", dumpsys_window)
    return m.group(1) if m else ""


def run_check(device, check: dict) -> tuple[bool, str]:
    kind, query = str(check.get("kind", "")), str(check.get("query", "")).strip()
    try:
        if kind == "app_installed":
            if not query:
                return False, "an app name is required"
            hits = find_packages(device.packages(), query)
            return bool(hits), (f"installed: {', '.join(hits[:5])}" if hits else f"no installed app matches '{query}'")
        if kind == "account":
            google = [a for a in device.accounts() if a[1] == "com.google"]
            return bool(google), (f"{len(google)} Google account(s) signed in" if google else "no Google account is signed in")
        if kind == "foreground":
            pkg = device.foreground()
            return bool(pkg) and (not query or _norm(query) in _norm(pkg)), f"the app in front is {pkg or 'unknown'}"
    except Exception as e:
        return False, f"could not check ({type(e).__name__})"
    return False, f"unknown check '{kind}'; use one of: {', '.join(KINDS)}"
