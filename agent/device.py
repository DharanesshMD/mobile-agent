"""Android device driver over ADB: observe (screenshot, UI tree) and act (tap, swipe, type, keys, launch)."""
import io
import os
import re
import shlex
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


def is_blank(img) -> bool:
    """Is the app area black, as `screencap` returns for a secure window? The status and navigation bars are not secure and stay visible, so only the middle counts."""
    w, h = img.size
    return img.convert("L").crop((0, int(h * 0.08), w, int(h * 0.92))).resize((16, 32)).getextrema()[1] <= 4


@dataclass
class Element:
    id: int
    label: str
    center: tuple[int, int]
    clickable: bool
    state: str = ""  # for example "off", "on", "scrollable", "text field": what a screenshot would otherwise show


from agent.apps import parse_launcher_packages

ACCOUNT_RE = re.compile(r"Account \{name=([^,}]+), type=([^}]+)\}")


def _node_state(node) -> str:
    bits = []
    if node.get("checkable") == "true":
        bits.append("on" if node.get("checked") == "true" else "off")
    if node.get("selected") == "true":
        bits.append("selected")
    if node.get("scrollable") == "true":
        bits.append("scrollable")
    if node.get("class", "").endswith("EditText"):
        bits.append("text field")
    if node.get("focused") == "true":
        bits.append("focused")
    if node.get("enabled") == "false":
        bits.append("disabled")
    return ", ".join(bits)


LAUNCH_RE = re.compile(r"^\s*([A-Za-z0-9_.]+/[A-Za-z0-9_.$]+)\s*$", re.M)


def parse_launcher_activity(resolve_output: str) -> str:
    """The component ("pkg/.Activity") from `cmd package resolve-activity --brief`, or "" when the package has no launcher activity."""
    m = LAUNCH_RE.search(resolve_output)
    return m.group(1) if m else ""


def parse_accounts(dumpsys_account: str) -> list[tuple[str, str]]:
    """(name, type) pairs from `dumpsys account`, e.g. ("me@gmail.com", "com.google")."""
    return sorted({(m.group(1).strip(), m.group(2).strip()) for m in ACCOUNT_RE.finditer(dumpsys_account)})


def local_timezone() -> str:
    """The IANA name of this computer's time zone (for example Asia/Kolkata), from /etc/localtime; "" when it cannot be told."""
    try:
        target = os.path.realpath("/etc/localtime")
        return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    except OSError:
        return ""


class Device:
    """Reads the screen through uiautomator2's resident on-device service when available (about 0.05 s per UI tree and a
    fast screenshot), and falls back to plain ADB (`uiautomator dump`, `screencap`) otherwise. Taps, swipes and typing stay
    on `adb shell input`, which measured faster. Set PHONE_READER=adb to force the fallback."""

    has_play = True  # False on a phone without Google Play (Cloud phone 2): the worker then never offers or opens a Play Store install

    def __init__(self, serial: str | None = None, adb: str = "adb", use_u2: bool | None = None):
        self.serial = serial
        self.base = [adb, *(["-s", serial] if serial else [])]
        self.use_u2 = (os.environ.get("PHONE_READER", "u2") != "adb") if use_u2 is None else use_u2
        self._u2 = None
        self._u2_failed = False
        self._size_at: tuple[float, tuple[int, int]] | None = None  # (time, size): rotation can change mid-task, so the size is re-read often
        self.keyboard_open = False

    def _u2_device(self):
        if not self.use_u2 or self._u2_failed:
            return None
        if self._u2 is None:
            try:
                import uiautomator2 as u2
                self._u2 = u2.connect(self.serial)
            except Exception:
                self._u2_failed = True
                return None
        return self._u2

    def reconnect(self) -> None:
        """After a boot or a long sleep the on-device service and cached values are stale: start from scratch."""
        self._u2, self._u2_failed, self._size_at = None, False, None
        self.keyboard_open = False

    def _drop_u2(self) -> None:
        """Only one accessibility connection can exist, so the old `uiautomator dump` works only after the service is stopped."""
        self._u2_failed = True
        try:
            if self._u2 is not None:
                self._u2.stop_uiautomator()
        except Exception:
            pass
        self._stop_service_via_adb()

    def _stop_service_via_adb(self) -> None:
        for pkg in ("com.github.uiautomator.test", "com.github.uiautomator"):
            try:
                self._run("shell", "am", "force-stop", pkg)
            except subprocess.CalledProcessError:
                pass

    def _run(self, *args: str, timeout: float = 60) -> bytes:
        return subprocess.run([*self.base, *args], check=True, capture_output=True, timeout=timeout).stdout

    def size(self) -> tuple[int, int]:
        """Current screen size including rotation (a game in landscape is 1600x720 on a 720x1600 phone). `wm size` always reports the physical
        portrait size, so the rotated size comes from uiautomator2, or from the display rotation on the adb path."""
        now = time.time()
        if self._size_at and now - self._size_at[0] < 0.7:
            return self._size_at[1]
        size = None
        d = self._u2_device()
        if d is not None:
            try:
                w, h = d.window_size()
                size = (int(w), int(h))
            except Exception:
                size = None
        if size is None:
            found = re.findall(r"(\d+)x(\d+)", self._run("shell", "wm", "size").decode())
            w, h = int(found[-1][0]), int(found[-1][1])
            rot = re.search(r"SurfaceOrientation:\s*(\d)", self._run("shell", "dumpsys", "input").decode(errors="replace"))
            size = (h, w) if rot and rot.group(1) in ("1", "3") else (w, h)
        self._size_at = (now, size)
        return size

    def orientation(self) -> str:
        w, h = self.size()
        return "landscape" if w > h else "portrait"

    def rotate(self, orientation: str) -> None:
        """portrait or landscape locks the screen that way; auto hands rotation back to the sensor."""
        if orientation not in ("portrait", "landscape", "auto"):
            raise ValueError("orientation must be portrait, landscape or auto")
        if orientation == "auto":
            self._run("shell", "settings", "put", "system", "accelerometer_rotation", "1")
        else:
            self._run("shell", "settings", "put", "system", "accelerometer_rotation", "0")
            self._run("shell", "settings", "put", "system", "user_rotation", "1" if orientation == "landscape" else "0")
        self._size_at = None
        time.sleep(0.6)  # the display takes a moment to turn

    def screenshot(self) -> bytes:
        d = self._u2_device()
        if d is not None:
            try:
                buf = io.BytesIO()
                d.screenshot().save(buf, "PNG")
                return buf.getvalue()
            except Exception:
                pass  # fall through to adb for this call
        return self._run("exec-out", "screencap", "-p")

    def frame(self):
        """The screen as a PIL image, for live viewers: skips the PNG encode and decode of `screenshot` (about 30 ms less)."""
        d = self._u2_device()
        if d is not None:
            try:
                return d.screenshot()
            except Exception:
                pass
        return Image.open(io.BytesIO(self._run("exec-out", "screencap", "-p")))

    def host_frame(self):
        """The screen as the emulator itself draws it. Android blanks `screencap` for secure windows (password and purchase screens set FLAG_SECURE),
        but the emulator window still shows them. For the owner's live view ONLY: the agent's own screenshots stay blank there on purpose, so it never
        sees what the owner types. Raises on a device that is not an emulator."""
        with tempfile.TemporaryDirectory() as d:
            self._run("emu", "screenrecord", "screenshot", d)
            shots = sorted(Path(d).glob("*.png"))
            if not shots:
                raise RuntimeError("the phone did not save a screenshot")
            img = Image.open(shots[-1])
            img.load()
            return img

    def elements(self, limit: int = 70) -> list[Element]:
        root = None
        d = self._u2_device()
        if d is not None:
            try:
                root = ET.fromstring(d.dump_hierarchy())
            except Exception:
                self._drop_u2()
        if root is None:
            root = self._dump()
            if root is None and not self.use_u2:  # a resident service left over from an earlier session blocks `uiautomator dump`
                self._stop_service_via_adb()
                root = self._dump()
        out: list[Element] = []
        self.keyboard_open = False
        if root is None:
            return out
        seen: set[tuple] = set()
        height = max((int(b[3]) for b in (re.findall(r"\d+", n.get("bounds", "")) for n in root.iter("node")) if len(b) == 4), default=0)
        for node in root.iter("node"):
            pkg = (node.get("package") or "").lower()
            if "inputmethod" in pkg:  # keyboard keys would crowd the list and push real fields off it
                self.keyboard_open = True
                continue
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            label = node.get("text") or node.get("content-desc") or ""
            clickable = node.get("clickable") == "true"
            if not m or not (label or clickable):
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            if pkg == "com.android.systemui" and height and y2 <= height * 0.06:  # clock, signal and battery icons
                continue
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            key = (label, center)
            if key in seen:
                continue
            seen.add(key)
            label = label or node.get("resource-id", "").split("/")[-1] or node.get("class", "").split(".")[-1]
            out.append(Element(len(out), label, center, clickable, _node_state(node)))
            if len(out) >= limit:
                break
        return out

    def _dump(self, tries: int = 2, device_timeout: int = 6):
        """uiautomator waits for the app to go idle, which can stall for tens of seconds while a page loads or animates.
        Cap it on the device, retry once, and give up (the model then works from the screenshot alone).
        It can also exit 0 without writing a file and leave a stale one, so delete first."""
        for _ in range(tries):
            try:
                self._run("shell", "rm", "-f", "/sdcard/ui.xml")
                self._run("shell", "timeout", "-s", "KILL", str(device_timeout), "uiautomator", "dump", "/sdcard/ui.xml")
                data = self._run("exec-out", "cat", "/sdcard/ui.xml")
            except subprocess.CalledProcessError:
                data = b""
            try:
                if data.strip():
                    return ET.fromstring(data)
            except ET.ParseError:
                pass
            time.sleep(0.3)
        return None

    def tap(self, x: int, y: int) -> None:
        self._run("shell", "input", "tap", str(x), str(y))

    def touch(self, kind: str, x: int, y: int) -> None:
        """One live touch event (down, move or up) for follow-the-finger dragging. uiautomator2 injects it in about 18 ms;
        `input motionevent` (Android 12+) is the fallback."""
        d = self._u2_device()
        if d is not None:
            try:
                getattr(d.touch, kind)(x, y)
                return
            except Exception:
                pass
        self._run("shell", "input", "motionevent", kind.upper(), str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._run("shell", "input", "swipe", *map(str, (x1, y1, x2, y2, ms)))

    def type_text(self, text: str) -> None:
        """Appends to the focused field. uiautomator2 is about 4x faster than `adb input text`, bypasses keyboard autocorrect and
        suggestions, and handles non-ASCII text (Tamil, accents); adb is the fallback."""
        d = self._u2_device()
        if d is not None:
            try:
                d.send_keys(text)
                return
            except Exception:
                pass
        self._run("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))

    def key(self, name: str) -> None:
        self._run("shell", "input", "keyevent", name)

    ANIMATION_SETTINGS = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")

    def is_emulator(self) -> bool:
        return self._run("shell", "getprop", "ro.kernel.qemu").decode().strip() == "1" or \
            self._run("shell", "getprop", "ro.boot.qemu").decode().strip() == "1"

    def sync_timezone(self, zone: str | None = None) -> str:
        """Put the phone in the owner's time zone (default: this computer's), so a clock it reads or a time it reports is local, not UTC. Idempotent, needs no root."""
        zone = zone or local_timezone()
        if zone:
            self._run("shell", "service", "call", "alarm", "3", "s16", zone)
        return zone

    def prepare(self, width: int = 720, height: int = 1600, density: int = 300) -> None:
        """Light profile: a smaller screen (cheaper to render, capture and send to the model) and no animations (faster to settle)."""
        self._run("shell", "wm", "size", f"{width}x{height}")
        self._run("shell", "wm", "density", str(density))
        for key in self.ANIMATION_SETTINGS:
            self._run("shell", "settings", "put", "global", key, "0")
        try:
            self.sync_timezone()
        except Exception:  # a time zone is a nicety, never a reason to fail the start-up
            pass

    def restore(self) -> None:
        self._run("shell", "wm", "size", "reset")
        self._run("shell", "wm", "density", "reset")
        for key in self.ANIMATION_SETTINGS:
            self._run("shell", "settings", "put", "global", key, "1")

    # --- a cloud phone's look and feel (cloudtools.set_device): all of these are undone when the task ends ---------------------------------------------
    def night_mode(self) -> bool:
        return "yes" in self._run("shell", "cmd", "uimode", "night").decode(errors="replace").lower()

    def set_night(self, on: bool) -> None:
        self._run("shell", "cmd", "uimode", "night", "yes" if on else "no")

    def font_scale(self) -> float:
        raw = self._run("shell", "settings", "get", "system", "font_scale").decode().strip()
        try:
            return float(raw)
        except ValueError:  # "null": the default
            return 1.0

    def set_font_scale(self, scale: float) -> None:
        self._run("shell", "settings", "put", "system", "font_scale", str(scale))

    def screen_timeout(self) -> int:
        raw = self._run("shell", "settings", "get", "system", "screen_off_timeout").decode().strip()
        return int(raw) if raw.lstrip("-").isdigit() else 0

    def set_screen_timeout(self, ms: int) -> None:
        self._run("shell", "settings", "put", "system", "screen_off_timeout", str(ms))

    def _display_now(self) -> tuple[str, str]:
        size = self._run("shell", "wm", "size").decode(errors="replace")
        density = self._run("shell", "wm", "density").decode(errors="replace")
        pick = lambda text, key: (re.search(rf"{key}:\s*(\S+)", text) or [None, ""])[1]
        return (pick(size, "Override size") or pick(size, "Physical size"), pick(density, "Override density") or pick(density, "Physical density"))

    def set_display(self, width: int, height: int, density: int) -> None:
        """Another screen size and density for a look-and-feel check; reset_display() puts back what it was before the first change."""
        if getattr(self, "_display_before", None) is None:
            self._display_before = self._display_now()
        self._run("shell", "wm", "size", f"{width}x{height}")
        self._run("shell", "wm", "density", str(density))
        self._size_at = None

    def reset_display(self) -> None:
        before = getattr(self, "_display_before", None)
        if before:
            self._run("shell", "wm", "size", before[0])
            self._run("shell", "wm", "density", before[1])
            self._display_before = None
        self._size_at = None

    def set_app_language(self, package: str, tag: str) -> bool:
        """Per-app language (Android 13+). Empty tag puts the app back on the system language. False when the app does not offer it."""
        self._run("shell", "cmd", "locale", "set-app-locales", shlex.quote(package), "--locales", shlex.quote(tag) if tag else "''")
        got = self._run("shell", "cmd", "locale", "get-app-locales", shlex.quote(package)).decode(errors="replace").lower()
        return (not tag) or tag.lower().split("-")[0] in got

    def package_dump(self, package: str) -> str:
        return self._run("shell", "dumpsys", "package", shlex.quote(package)).decode(errors="replace")

    def clear_app(self, package: str) -> None:
        """Stop an app and wipe its data (a throwaway browser after a link check)."""
        self._run("shell", "am", "force-stop", shlex.quote(package))
        self._run("shell", "pm", "clear", shlex.quote(package))

    def uninstall(self, package: str) -> None:
        self._run("shell", "pm", "uninstall", shlex.quote(package))

    def launcher_apps(self) -> list[str]:
        out = self._run("shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
                        "-c", "android.intent.category.LAUNCHER").decode(errors="replace")
        return parse_launcher_packages(out)

    def accounts(self) -> list[tuple[str, str]]:
        return parse_accounts(self._run("shell", "dumpsys", "account").decode(errors="replace"))

    def packages(self) -> list[str]:
        out = self._run("shell", "pm", "list", "packages").decode(errors="replace")
        return [line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:")]

    def apk_paths(self, package: str) -> list[str]:
        """Where the APK files of an installed app are on the phone (a base APK and any splits), from `pm path`. Empty if it is not installed."""
        out = self._run("shell", "pm", "path", package).decode(errors="replace")
        return [line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:")]

    def pull(self, remote: str, local: str) -> None:
        self._run("pull", remote, str(local), timeout=300)

    def push(self, local: str, remote: str) -> None:
        """Copy a file to the phone (a picture the owner shared). The folder is created first."""
        self._run("shell", "mkdir", "-p", shlex.quote(remote.rsplit("/", 1)[0]))
        self._run("push", str(local), remote, timeout=120)

    def media_scan(self, remote: str) -> None:
        """Tell Android a new picture exists so the gallery and the photo pickers of other apps list it."""
        self._run("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", shlex.quote("file://" + remote))

    def remove_file(self, remote: str) -> None:
        self._run("shell", "rm", "-f", shlex.quote(remote))
        self.media_scan(remote)  # the gallery drops it too

    def install_apks(self, paths: list[str]) -> None:
        """Install an app from its APK files (a base APK plus splits go in together), replacing an older copy. Only the app, never anyone's data."""
        out = self._run("install-multiple", "-r", *map(str, paths), timeout=300).decode(errors="replace")
        if "Success" not in out:
            raise ValueError("install failed: " + out.strip()[-200:])

    def foreground(self) -> str:
        """Package name of the app in front."""
        from agent.checks import parse_focus
        return parse_focus(self._run("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'").decode(errors="replace"))

    def third_party_packages(self) -> list[str]:
        """Packages the owner or the agent installed (not the system image): the apps worth knowing about when choosing a phone."""
        out = self._run("shell", "pm", "list", "packages", "-3").decode(errors="replace")
        return sorted(line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:"))

    def battery(self) -> tuple[int | None, bool]:
        """(charge level in percent, is it charging) from `dumpsys battery`."""
        out = self._run("shell", "dumpsys", "battery").decode(errors="replace")
        level = re.search(r"^\s*level:\s*(\d+)", out, re.M)
        return (int(level.group(1)) if level else None, bool(re.search(r"^\s*(AC|USB|Wireless) powered:\s*true", out, re.M)))

    def crashes(self) -> list[tuple[str, str]]:
        """(package, pid) of the app crashes Android has logged since the phone booted."""
        from agent.checks import parse_crashes
        return parse_crashes(self._run("shell", "logcat", "-b", "crash", "-d").decode(errors="replace"))

    def open_app(self, package: str) -> None:
        if package == "com.android.settings":  # Settings has no launcher entry on AOSP-based images
            return self.open_intent("android.settings.SETTINGS")
        try:
            out = self._run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            if b"No activities found" not in out:
                return
        except subprocess.CalledProcessError:  # monkey exits 251 on some images (the Redroid phone (Cloud phone 2)) although the app is installed
            pass
        component = parse_launcher_activity(self._run("shell", "cmd", "package", "resolve-activity", "--brief", "-c", "android.intent.category.LAUNCHER", package).decode(errors="replace"))
        if not component:
            raise ValueError(f"{package} has no launcher activity (it is not installed, or it is not an app you can open)")
        self.start_intent(["-n", component])

    def open_url(self, url: str) -> None:
        self._run("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url))

    def start_intent(self, args: list[str]) -> None:
        """`am start` with pre-quoted arguments. am reports failures in its output, not always in the exit code."""
        out = self._run("shell", "am", "start", *args).decode(errors="replace")
        if "Error" in out:
            raise ValueError(out.strip().splitlines()[-1][:160])

    def timezone(self) -> str:
        tz = self._run("shell", "getprop", "persist.sys.timezone").decode().strip()
        return tz or "UTC"

    def next_alarm(self) -> str | None:
        """Time of the next alarm clock from `dumpsys alarm` (for example '2026-10-29 06:45:00'), or None."""
        m = re.search(r"Next alarm clock information:.*?time:\d+ = (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)",
                      self._run("shell", "dumpsys", "alarm").decode(errors="replace"), re.S)
        return m.group(1) if m else None

    def has_timer(self) -> bool:
        return "com.android.deskclock.action.TIMER_EXPIRED" in self._run("shell", "dumpsys", "alarm").decode(errors="replace")

    def open_intent(self, action: str) -> None:
        self._run("shell", "am", "start", "-a", action)
