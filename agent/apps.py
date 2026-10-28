"""Shortcuts that save the phone worker many steps: Settings deep links and installed-app lookup."""
import re

SETTINGS_PAGES = {
    "wifi": "android.settings.WIFI_SETTINGS", "bluetooth": "android.settings.BLUETOOTH_SETTINGS",
    "display": "android.settings.DISPLAY_SETTINGS", "sound": "android.settings.SOUND_SETTINGS",
    "battery": "android.settings.BATTERY_SAVER_SETTINGS", "apps": "android.settings.MANAGE_ALL_APPLICATIONS_SETTINGS",
    "storage": "android.settings.INTERNAL_STORAGE_SETTINGS", "location": "android.settings.LOCATION_SOURCE_SETTINGS",
    "security": "android.settings.SECURITY_SETTINGS", "privacy": "android.settings.PRIVACY_SETTINGS",
    "accounts": "android.settings.SYNC_SETTINGS", "add_account": "android.settings.ADD_ACCOUNT_SETTINGS",
    "date_time": "android.settings.DATE_SETTINGS", "language": "android.settings.LOCALE_SETTINGS",
    "keyboard": "android.settings.INPUT_METHOD_SETTINGS", "accessibility": "android.settings.ACCESSIBILITY_SETTINGS",
    "airplane_mode": "android.settings.AIRPLANE_MODE_SETTINGS", "nfc": "android.settings.NFC_SETTINGS",
    "network": "android.settings.WIRELESS_SETTINGS", "data_usage": "android.settings.DATA_USAGE_SETTINGS",
    "notifications": "android.settings.NOTIFICATION_SETTINGS", "do_not_disturb": "android.settings.ZEN_MODE_SETTINGS",
    "about_phone": "android.settings.DEVICE_INFO_SETTINGS", "developer_options": "android.settings.APPLICATION_DEVELOPMENT_SETTINGS",
    "default_apps": "android.settings.HOME_SETTINGS",
}

ALIASES = {
    "play store": "com.android.vending", "playstore": "com.android.vending", "google play": "com.android.vending",
    "gmail": "com.google.android.gm", "mail": "com.google.android.gm", "phone": "com.google.android.dialer",
    "dialer": "com.google.android.dialer", "messages": "com.google.android.apps.messaging",
    "sms": "com.google.android.apps.messaging", "text": "com.google.android.apps.messaging",
    "camera": "com.android.camera2", "clock": "com.google.android.deskclock", "alarm": "com.google.android.deskclock",
    "drive": "com.google.android.apps.docs", "docs": "com.google.android.apps.docs", "contacts": "com.google.android.contacts",
    "photos": "com.google.android.apps.photos", "maps": "com.google.android.apps.maps", "calendar": "com.google.android.calendar",
    "chrome": "com.android.chrome", "browser": "com.android.chrome", "youtube": "com.google.android.youtube",
    "youtube music": "com.google.android.apps.youtube.music", "settings": "com.android.settings",
}
GENERIC = {"com", "org", "net", "android", "google", "apps", "app", "www", "the", "and"}
LAUNCHER_RE = re.compile(r"^\s+([A-Za-z0-9_.]+)/\S+\s*$", re.M)


def parse_launcher_packages(query_activities_output: str) -> list[str]:
    """Package names from `cmd package query-activities --brief` output."""
    return sorted(set(LAUNCHER_RE.findall(query_activities_output)))


def match_apps(query: str, packages: list[str], limit: int = 5) -> list[str]:
    """Installed launcher packages that plausibly match an app name (alias first, then word or 5-letter-prefix match)."""
    q = query.lower().strip()
    found: list[str] = []
    alias = ALIASES.get(q)
    if alias and alias in packages:
        found.append(alias)
    words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 3 and w not in GENERIC]
    for pkg in packages:
        parts = [p for p in re.findall(r"[a-z0-9]+", pkg.lower()) if p not in GENERIC]
        if pkg not in found and any(w == p or (len(w) >= 5 and len(p) >= 5 and w[:5] == p[:5]) for w in words for p in parts):
            found.append(pkg)
    return found[:limit]


# --- Intent fast paths -----------------------------------------------------------------------------------------------
# One validated `intent` action replaces many UI steps. set_alarm and set_timer complete by themselves. compose_sms,
# compose_email, insert_contact and insert_event only prefill a screen: the worker still taps Send or Save, and asks first
# when something would be sent. dial never places the call.
import shlex
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

INTENT_KINDS = {
    "set_alarm": "hour, minute, label?", "set_timer": "seconds, label?", "dial": "number",
    "compose_sms": "number, body", "compose_email": "to, subject, body", "insert_contact": "name, phone, email?",
    "insert_event": "title, start 'YYYY-MM-DD HH:MM' (phone time zone), minutes?, location?", "maps_search": "query",
    "navigate": "destination", "share_text": "text", "camera": "",
}
PHONE_RE = re.compile(r"^\+?\(?[0-9][0-9 ()\-]{2,20}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def _text(p: dict, key: str, limit: int = 500, required: bool = True) -> str:
    v = p.get(key)
    if v is None or str(v).strip() == "":
        if required:
            raise ValueError(f"missing '{key}'")
        return ""
    v = str(v).strip()
    if len(v) > limit:
        raise ValueError(f"'{key}' is longer than {limit} characters")
    return v


def _int(p: dict, key: str, lo: int, hi: int) -> int:
    try:
        v = int(p[key])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"'{key}' must be a whole number") from None
    if not lo <= v <= hi:
        raise ValueError(f"'{key}' must be between {lo} and {hi}")
    return v


def _number(p: dict, key: str = "number") -> str:
    n = _text(p, key, 24)
    if not PHONE_RE.match(n):
        raise ValueError(f"'{key}' is not a phone number")
    return re.sub(r"[ ()\-]", "", n)


def _es(extra: str, value: str) -> list[str]:
    return ["--es", extra, shlex.quote(value)]


def build_intent(kind: str, p: dict, tz: str = "UTC") -> tuple[list[str], str]:
    """Validated `am start` arguments (already shell-quoted) and a note for the model. Raises ValueError with a helpful message."""
    if kind not in INTENT_KINDS:
        raise ValueError(f"unknown intent kind '{kind}'; use one of: " + "; ".join(f"{k}({v})" for k, v in INTENT_KINDS.items()))
    A = "android.intent.action."
    if kind == "set_alarm":
        h, m = _int(p, "hour", 0, 23), _int(p, "minute", 0, 59)
        label = _text(p, "label", 60, required=False)
        args = ["-a", A + "SET_ALARM", "--ei", "android.intent.extra.alarm.HOUR", str(h), "--ei", "android.intent.extra.alarm.MINUTES", str(m),
                "--ez", "android.intent.extra.alarm.SKIP_UI", "true"]
        if label:
            args += _es("android.intent.extra.alarm.MESSAGE", label)
        return args, f"alarm set for {h:02d}:{m:02d}" + (f" ({label})" if label else "")
    if kind == "set_timer":
        s = _int(p, "seconds", 1, 86400)
        label = _text(p, "label", 60, required=False)
        args = ["-a", A + "SET_TIMER", "--ei", "android.intent.extra.alarm.LENGTH", str(s), "--ez", "android.intent.extra.alarm.SKIP_UI", "true"]
        if label:
            args += _es("android.intent.extra.alarm.MESSAGE", label)
        return args, f"timer started for {s} seconds"
    if kind == "dial":
        n = _number(p)
        return ["-a", A + "DIAL", "-d", shlex.quote("tel:" + n)], f"dialer opened with {n}; the call is not placed"
    if kind == "compose_sms":
        n, body = _number(p), _text(p, "body", 1000)
        return ["-a", A + "SENDTO", "-d", shlex.quote("sms:" + n), *_es("sms_body", body)], \
            "message composer opened with the text filled in; it is not sent until Send is tapped (ask the owner first)"
    if kind == "compose_email":
        to = _text(p, "to", 320)
        if not EMAIL_RE.match(to):
            raise ValueError("'to' is not an email address")
        return ["-a", A + "SENDTO", "-d", shlex.quote("mailto:" + to), *_es("android.intent.extra.SUBJECT", _text(p, "subject", 200)),
                *_es("android.intent.extra.TEXT", _text(p, "body", 4000))], \
            "email composer opened and filled in; it is not sent until Send is tapped (ask the owner first)"
    if kind == "insert_contact":
        args = ["-a", A + "INSERT", "-t", "vnd.android.cursor.dir/contact", *_es("name", _text(p, "name", 100)), *_es("phone", _number(p, "phone"))]
        email = _text(p, "email", 320, required=False)
        if email:
            if not EMAIL_RE.match(email):
                raise ValueError("'email' is not an email address")
            args += _es("email", email)
        return args, "contact form opened and filled in; tap Save"
    if kind == "insert_event":
        title, start = _text(p, "title", 200), _text(p, "start", 20)
        try:
            begin = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
        except ValueError:
            raise ValueError("'start' must look like 2026-10-29 15:00") from None
        minutes = _int({"m": p.get("minutes", 60)}, "m", 1, 1440)
        b = int(begin.timestamp() * 1000)
        args = ["-a", A + "INSERT", "-d", "content://com.android.calendar/events", *_es("title", title),
                "--el", "beginTime", str(b), "--el", "endTime", str(b + minutes * 60000)]
        loc = _text(p, "location", 200, required=False)
        if loc:
            args += _es("eventLocation", loc)
        return args, f"calendar event form opened and filled in for {start}; tap Save (a first-run screen may appear first)"
    if kind == "maps_search":
        return ["-a", A + "VIEW", "-d", shlex.quote("geo:0,0?q=" + quote(_text(p, "query", 200)))], "Maps opened with the search"
    if kind == "navigate":
        return ["-a", A + "VIEW", "-d", shlex.quote("google.navigation:q=" + quote(_text(p, "destination", 200)))], "Maps navigation opened"
    if kind == "share_text":
        return ["-a", A + "SEND", "-t", "text/plain", *_es("android.intent.extra.TEXT", _text(p, "text", 4000))], "share sheet opened"
    return ["-a", "android.media.action.STILL_IMAGE_CAMERA"], "camera opened"
