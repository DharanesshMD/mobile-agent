"""Facts read straight from the phone (no model, no screenshots), used to prove a task really finished.

A check is {"kind": "app_installed", "query": "clash of clans"}, {"kind": "account"} (a Google account is signed in) or
{"kind": "foreground", "query": "instagram"} or {"kind": "battery"} (the charge level). `run_check` returns (ok, detail); the detail is safe to show the orchestrator (it never contains account names).
"""
import re

KINDS = ("app_installed", "account", "foreground", "battery")


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


def parse_crashes(crash_log: str) -> list[tuple[str, str]]:
    """(package, pid) of every app crash in `logcat -b crash`: a Java crash prints "Process: pkg, PID: n", a native one "pid: n ... >>> pkg <<<"."""
    out = [(m.group(1), m.group(2)) for m in re.finditer(r"Process: ([\w.]+), PID: (\d+)", crash_log)]
    out += [(m.group(2), m.group(1)) for m in re.finditer(r"pid: (\d+), tid: \d+, name: .*? >>> ([\w.]+) <<<", crash_log)]
    return out


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
            if not hasattr(device, "accounts"):
                return False, "this phone does not report its accounts: ask the owner"
            google = [a for a in device.accounts() if a[1] == "com.google"]
            return bool(google), (f"{len(google)} Google account(s) signed in" if google else "no Google account is signed in")
        if kind == "foreground":
            pkg = device.foreground()
            return bool(pkg) and (not query or _norm(query) in _norm(pkg)), f"the app in front is {pkg or 'unknown'}"
        if kind == "battery":
            if not hasattr(device, "battery"):
                return False, "this phone does not report its battery to the server: ask its worker to read it from the screen"
            level, charging = device.battery()
            return level is not None, (f"battery is at {level}%" + (", charging" if charging else "") if level is not None else "could not read the battery")
    except Exception as e:
        return False, f"could not check ({type(e).__name__})"
    return False, f"unknown check '{kind}'; use one of: {', '.join(KINDS)}"
