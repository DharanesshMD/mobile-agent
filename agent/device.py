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

    def _dump(self, tries: int = 3):
        """uiautomator can exit 0 without writing a file (mid-animation) and leave a stale one, so delete first."""
        for _ in range(tries):
            self._run("shell", "rm", "-f", "/sdcard/ui.xml")
            try:
                self._run("shell", "uiautomator", "dump", "/sdcard/ui.xml")
                data = self._run("exec-out", "cat", "/sdcard/ui.xml")
            except subprocess.CalledProcessError:
                data = b""
            try:
                if data.strip():
                    return ET.fromstring(data)
            except ET.ParseError:
                pass
            time.sleep(0.5)
        return None

    def tap(self, x: int, y: int) -> None:
        self._run("shell", "input", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._run("shell", "input", "swipe", *map(str, (x1, y1, x2, y2, ms)))

    def type_text(self, text: str) -> None:
        self._run("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))

    def key(self, name: str) -> None:
        self._run("shell", "input", "keyevent", name)

    def open_app(self, package: str) -> None:
        if package == "com.android.settings":  # Settings has no launcher entry on AOSP-based images
            return self.open_intent("android.settings.SETTINGS")
        self._run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")

    def open_url(self, url: str) -> None:
        self._run("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", shlex.quote(url))

    def open_intent(self, action: str) -> None:
        self._run("shell", "am", "start", "-a", action)
