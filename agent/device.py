"""Android device driver over ADB: observe (screenshot, UI tree) and act (tap, swipe, type, keys, launch)."""
import io
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from PIL import Image


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


def parse_accounts(dumpsys_account: str) -> list[tuple[str, str]]:
    """(name, type) pairs from `dumpsys account`, e.g. ("me@gmail.com", "com.google")."""
    return sorted({(m.group(1).strip(), m.group(2).strip()) for m in ACCOUNT_RE.finditer(dumpsys_account)})


class Device:
    """Reads the screen through uiautomator2's resident on-device service when available (about 0.05 s per UI tree and a
    fast screenshot), and falls back to plain ADB (`uiautomator dump`, `screencap`) otherwise. Taps, swipes and typing stay
    on `adb shell input`, which measured faster. Set PHONE_READER=adb to force the fallback."""

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

    def _run(self, *args: str) -> bytes:
        return subprocess.run([*self.base, *args], check=True, capture_output=True, timeout=60).stdout

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

    def prepare(self, width: int = 720, height: int = 1600, density: int = 300) -> None:
        """Light profile: a smaller screen (cheaper to render, capture and send to the model) and no animations (faster to settle)."""
        self._run("shell", "wm", "size", f"{width}x{height}")
        self._run("shell", "wm", "density", str(density))
        for key in self.ANIMATION_SETTINGS:
            self._run("shell", "settings", "put", "global", key, "0")

    def restore(self) -> None:
        self._run("shell", "wm", "size", "reset")
        self._run("shell", "wm", "density", "reset")
        for key in self.ANIMATION_SETTINGS:
            self._run("shell", "settings", "put", "global", key, "1")

    def launcher_apps(self) -> list[str]:
        out = self._run("shell", "cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
                        "-c", "android.intent.category.LAUNCHER").decode(errors="replace")
        return parse_launcher_packages(out)

    def accounts(self) -> list[tuple[str, str]]:
        return parse_accounts(self._run("shell", "dumpsys", "account").decode(errors="replace"))

    def open_app(self, package: str) -> None:
        if package == "com.android.settings":  # Settings has no launcher entry on AOSP-based images
            return self.open_intent("android.settings.SETTINGS")
        self._run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

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
