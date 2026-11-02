"""Worker actions that only make sense on a phone the agent owns (a cloud phone): capture a named screenshot, record a verdict, change the phone's display
settings for a look-and-feel check, and read an installed app's permissions. They never run on the owner's own phone (set_device), and none of them types or taps.
"""
import re

from agent.artifacts import Artifacts

TOOLS = ("capture", "verdict", "set_device", "inspect_app")

# Observe-only mode (a link check): the page is hostile until proven otherwise, so the only taps allowed are on the browser's own first-run and warning screens.
OBSERVE_TAP_OK = ("stay signed out", "use without an account", "no thanks", "no, thanks", "not now", "skip", "got it", "details", "more details", "back to safety",
                  "accept & continue", "accept and continue", "close", "dismiss")
OBSERVE_REASON = ("this is an observe-only link check: nothing is typed, and the page is not tapped, scrolled for forms or downloaded from. "
                  "Read what the page shows, capture it, record the verdict and finish with done.")


def observe_block(action: dict, elements: list) -> str | None:
    """None when the action is fine in an observe-only link check, else the reason it is refused."""
    kind = action.get("type")
    if kind in ("type", "type_secret"):
        return OBSERVE_REASON
    if kind == "tap":
        e = next((e for e in elements if "element" in action and e.id == int(action["element"])), None)
        label = (e.label if e else "").strip().lower()
        return None if label and any(label == ok or label.startswith(ok) for ok in OBSERVE_TAP_OK) else OBSERVE_REASON
    if kind == "key" and action.get("key") not in ("back", "home"):
        return OBSERVE_REASON
    return None


EMAIL_ONLY = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
ACCOUNT_PHRASES = re.compile(r"choose an account|continue as |sign in to get your bookmarks", re.I)  # "Signed in as <address>" is also an avatar's hidden description in some apps, so it is not a reason by itself


def shows_account(state: dict) -> bool:
    """Does the screen show an account address or an account chooser (Chrome's first-run page lists the phone's Google account)? Such a screen is never kept in a report.
    An address that only sits inside a longer description (an avatar button's "Google Account: Name (address)", which is not drawn on screen) does not count."""
    for e in (state.get("obs") or {}).get("elements", []):
        label = str(e.get("label", "")).replace("\u2060", "").strip()
        if EMAIL_ONLY.fullmatch(label) or ACCOUNT_PHRASES.search(label):
            return True
    return False


def run(c: dict, state: dict, action: dict) -> str:
    """Run a cloud-tool action. Returns the outcome line the model sees; raises ValueError with a reason it can act on."""
    art: Artifacts = c.setdefault("artifacts", Artifacts())
    kind = action["type"]
    if kind == "capture":
        if shows_account(state):
            raise ValueError("this screen shows an account address or an account chooser, so it is not kept in a report: dismiss it first (Stay signed out) and capture the page instead")
        n = art.add_capture(c["device"].screenshot(), str(action.get("title", "")), str(action.get("note", "")), state.get("step", 0))
        return f"captured picture {n}"
    if kind == "verdict":
        art.set_verdict(action.get("level", ""), _list(action.get("reasons")), _list(action.get("checked")), _list(action.get("not_checked")))
        return f"verdict recorded: {art.verdict['level']}"
    if kind == "set_device":
        return set_device(c["device"], art, action)
    if kind == "inspect_app":
        return inspect_app(c["device"], art, str(action.get("package", "")))
    raise ValueError(f"unknown tool {kind!r}")


def _list(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value] if isinstance(value, (list, tuple)) else []


# --- set_device -----------------------------------------------------------------------------------------------------------------------------------
FONT_SCALES = {"small": 0.85, "normal": 1.0, "large": 1.3, "largest": 1.5, "huge": 1.8}
SIZES = {"small": (540, 1200, 240), "normal": (720, 1600, 300), "big": (1080, 2400, 420), "tablet": (1200, 1920, 240)}


def set_device(device, art: Artifacts, action: dict) -> str:
    """Change the phone's display for a look-and-feel check and register the undo. Only phones the agent owns have these methods (not the owner's real phone)."""
    if not hasattr(device, "set_night"):
        raise ValueError("set_device is not available on this phone: it only works on the agent's own cloud phones, never on the owner's phone")
    done = []
    if "dark_mode" in action:
        want = action["dark_mode"] in (True, "on", "yes", "true", "dark")
        before = device.night_mode()
        device.set_night(want)
        art.on_finish(lambda b=before: device.set_night(b))
        done.append("dark mode " + ("on" if want else "off"))
    if "font_scale" in action:
        raw = action["font_scale"]
        scale = FONT_SCALES.get(str(raw).lower()) if str(raw).lower() in FONT_SCALES else float(raw)
        if not 0.5 <= scale <= 2.0:
            raise ValueError("font_scale must be between 0.5 and 2.0 (or small, normal, large, largest, huge)")
        before = device.font_scale()
        device.set_font_scale(scale)
        art.on_finish(lambda b=before: device.set_font_scale(b))
        done.append(f"font scale {scale}")
    if "size" in action:
        name = str(action["size"]).lower()
        if name not in SIZES:
            raise ValueError(f"size must be one of: {', '.join(SIZES)}")
        w, h, dpi = SIZES[name]
        art.on_finish(device.reset_display)
        device.set_display(w, h, dpi)
        done.append(f"screen {name} ({w}x{h} at {dpi} dpi)")
    if "language" in action:
        package, tag = str(action.get("package", "")), str(action["language"])
        if not package:
            raise ValueError("language needs the app's package (set_device works per app)")
        ok = device.set_app_language(package, tag)
        if not ok:
            raise ValueError(f"{package} does not support a per-app language here (Android 13+ apps that declare locales only), so it stays as it is")
        art.on_finish(lambda: device.set_app_language(package, ""))
        done.append(f"{package} language {tag}")
    if not done:
        raise ValueError("set_device needs at least one of: dark_mode, font_scale, size, language (with package)")
    art.facts.append("Phone settings tried: " + ", ".join(done))
    return "set " + ", ".join(done) + " (restored when the task ends)"


def snapshot(device, art: Artifacts) -> None:
    """Remember the settings a guide or a look-and-feel task may change (dark mode, text size, screen timeout) and put them back when it ends:
    the phone is the agent's shared one, and a how-to must leave it as it was."""
    if not hasattr(device, "set_night"):
        return
    for get, put in (("night_mode", "set_night"), ("font_scale", "set_font_scale"), ("screen_timeout", "set_screen_timeout")):
        if hasattr(device, get) and hasattr(device, put):
            try:
                before = getattr(device, get)()
            except Exception:
                continue
            art.on_finish(lambda b=before, f=getattr(device, put): f(b))


# --- inspect_app ----------------------------------------------------------------------------------------------------------------------------------
DANGEROUS = {
    "READ_SMS": "reads your text messages", "RECEIVE_SMS": "receives your text messages", "SEND_SMS": "sends text messages (can cost money)",
    "READ_CONTACTS": "reads your contacts", "WRITE_CONTACTS": "changes your contacts", "READ_CALL_LOG": "reads your call history", "CALL_PHONE": "places phone calls",
    "RECORD_AUDIO": "uses the microphone", "CAMERA": "uses the camera", "ACCESS_FINE_LOCATION": "knows your exact location", "ACCESS_COARSE_LOCATION": "knows your rough location",
    "ACCESS_BACKGROUND_LOCATION": "tracks your location in the background", "READ_EXTERNAL_STORAGE": "reads your files", "WRITE_EXTERNAL_STORAGE": "changes your files",
    "READ_MEDIA_IMAGES": "reads your photos", "READ_MEDIA_VIDEO": "reads your videos", "READ_CALENDAR": "reads your calendar", "WRITE_CALENDAR": "changes your calendar",
    "GET_ACCOUNTS": "sees the accounts on the phone", "SYSTEM_ALERT_WINDOW": "draws over other apps", "REQUEST_INSTALL_PACKAGES": "can install other apps",
    "BIND_ACCESSIBILITY_SERVICE": "can read and control the screen", "BIND_NOTIFICATION_LISTENER_SERVICE": "reads your notifications", "BIND_DEVICE_ADMIN": "can lock or wipe the phone",
    "QUERY_ALL_PACKAGES": "sees every app you have installed", "READ_PHONE_STATE": "reads the phone number and device identity", "BODY_SENSORS": "reads body sensors",
    "ACTIVITY_RECOGNITION": "knows when you walk or drive", "USE_BIOMETRIC": "uses fingerprint or face unlock", "MANAGE_EXTERNAL_STORAGE": "access to all files",
    "FOREGROUND_SERVICE_SPECIAL_USE": "runs in the background with a notification",
}


def dangerous_permissions(requested: list[str]) -> list[tuple[str, str]]:
    out = []
    for perm in requested:
        short = perm.rsplit(".", 1)[-1]
        if short in DANGEROUS and (perm.startswith("android.permission.") or "." not in perm):
            out.append((short, DANGEROUS[short]))
    return sorted(set(out))


def parse_package_dump(dump: str, package: str = "") -> dict:
    """The facts `dumpsys package <pkg>` prints that matter for a quick review."""
    info = {"permissions": [], "version": "", "target_sdk": "", "installer": "", "components": 0, "key_id": ""}
    section = ""
    for raw in dump.splitlines():
        line = raw.strip()
        if line.startswith("versionName=") and not info["version"]:
            info["version"] = line.split("=", 1)[1]
        elif "targetSdk=" in line and not info["target_sdk"]:
            info["target_sdk"] = line.split("targetSdk=", 1)[1].split()[0]
        elif line.startswith("installerPackageName=") and not info["installer"]:
            val = line.split("=", 1)[1]
            info["installer"] = "" if val in ("null", "") else val
        elif line.startswith("signatures=") and not info["key_id"]:
            m = re.search(r"signatures:\[([0-9a-f]+)", line)
            info["key_id"] = m.group(1) if m else ""
        elif line.endswith("permissions:") and ("requested" in line or "install" in line):
            section = "perm"
        elif line.endswith(":") and section == "perm" and "permissions" not in line:
            section = ""
        elif section == "perm" and line.startswith(("android.permission.", "com.", "org.")):
            info["permissions"].append(line.split(":", 1)[0].split()[0])
    info["permissions"] = sorted(set(info["permissions"]))
    if package:
        info["components"] = len(set(re.findall(rf"{re.escape(package)}/[\w.$]+", dump)))
    return info


def inspect_app(device, art: Artifacts, package: str) -> str:
    """Read an installed app's permissions and facts. Static: nothing is launched."""
    if not package or "/" in package or " " in package:
        raise ValueError("inspect_app needs a package name such as com.example.app")
    if not hasattr(device, "package_dump"):
        raise ValueError("inspect_app is not available on this phone")
    info = parse_package_dump(device.package_dump(package), package)
    if not info["version"] and not info["permissions"]:
        raise ValueError(f"{package} is not installed on this phone")
    danger = dangerous_permissions(info["permissions"])
    lines = [f"{package} {info['version'] or ''}".strip(), f"target SDK {info['target_sdk'] or 'unknown'}, {len(info['permissions'])} permissions requested, {info['components']} components"]
    if info["installer"]:
        lines.append(f"installed by {info['installer']}")
    if info["key_id"]:
        lines.append(f"signing key id {info['key_id']}")
    lines += [f"{name}: {meaning}" for name, meaning in danger]
    art.facts += lines
    return "; ".join(lines)[:600]
