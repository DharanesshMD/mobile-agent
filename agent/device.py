"""Android device driver over ADB: observe (screenshot, UI tree) and act (tap, swipe, type, keys, launch)."""
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class Element:
    id: int
    label: str
    center: tuple[int, int]
    clickable: bool


from agent.apps import parse_launcher_packages

ACCOUNT_RE = re.compile(r"Account \{name=([^,}]+), type=([^}]+)\}")


def parse_accounts(dumpsys_account: str) -> list[tuple[str, str]]:
    """(name, type) pairs from `dumpsys account`, e.g. ("me@gmail.com", "com.google")."""
    return sorted({(m.group(1).strip(), m.group(2).strip()) for m in ACCOUNT_RE.finditer(dumpsys_account)})


class Device:
    def __init__(self, serial: str | None = None, adb: str = "adb"):
        self.base = [adb, *(["-s", serial] if serial else [])]

    def _run(self, *args: str) -> bytes:
        return subprocess.run([*self.base, *args], check=True, capture_output=True, timeout=60).stdout

    def size(self) -> tuple[int, int]:
        m = re.search(r"(\d+)x(\d+)", self._run("shell", "wm", "size").decode())
        return int(m.group(1)), int(m.group(2))

    def screenshot(self) -> bytes:
        return self._run("exec-out", "screencap", "-p")

    def elements(self, limit: int = 60) -> list[Element]:
        root = self._dump()
        out: list[Element] = []
        if root is None:
            return out
        seen: set[tuple] = set()
        for node in root.iter("node"):
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            label = node.get("text") or node.get("content-desc") or ""
            clickable = node.get("clickable") == "true"
            if not m or not (label or clickable):
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            key = (label, center)
            if key in seen:
                continue
            seen.add(key)
            label = label or node.get("resource-id", "").split("/")[-1] or node.get("class", "").split(".")[-1]
            out.append(Element(len(out), label, center, clickable))
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

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._run("shell", "input", "swipe", *map(str, (x1, y1, x2, y2, ms)))

    def type_text(self, text: str) -> None:
        self._run("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))

    def key(self, name: str) -> None:
        self._run("shell", "input", "keyevent", name)

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

    def open_intent(self, action: str) -> None:
        self._run("shell", "am", "start", "-a", action)
