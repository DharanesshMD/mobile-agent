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
