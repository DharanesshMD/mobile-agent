"""Day-1 spike: prove we can observe and act on an Android device over ADB.

Observe: screenshot (PNG) + UI hierarchy (uiautomator XML parsed into tappable elements).
Act: tap, type, key events, launch an app.
"""
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ADB = "adb"


def adb(*args: str, binary: bool = False) -> bytes | str:
    out = subprocess.run([ADB, *args], check=True, capture_output=True).stdout
    return out if binary else out.decode()


def screenshot(path: Path) -> Path:
    path.write_bytes(adb("exec-out", "screencap", "-p", binary=True))
    return path


def ui_elements() -> list[dict]:
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    root = ET.fromstring(adb("exec-out", "cat", "/sdcard/ui.xml"))
    elements = []
    for node in root.iter("node"):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        label = node.get("text") or node.get("content-desc") or node.get("resource-id")
        if label and (node.get("clickable") == "true" or node.get("text")):
            elements.append({"label": label, "center": ((x1 + x2) // 2, (y1 + y2) // 2),
                             "clickable": node.get("clickable") == "true"})
    return elements


def tap(x: int, y: int) -> None:
    adb("shell", "input", "tap", str(x), str(y))


def key(name: str) -> None:
    adb("shell", "input", "keyevent", name)


def launch(action: str) -> None:
    adb("shell", "am", "start", "-a", action)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "spike/out")
    out.mkdir(parents=True, exist_ok=True)
    print("device:", adb("get-state").strip(), adb("shell", "getprop", "ro.build.version.release").strip())
    key("KEYCODE_HOME")
    launch("android.settings.SETTINGS")
    adb("shell", "sleep", "2")
    print("screenshot:", screenshot(out / "settings.png"))
    items = ui_elements()
    print(f"{len(items)} labelled elements, first 8:")
    for e in items[:8]:
        print("  ", e)
    target = next((e for e in items if "network" in e["label"].lower()), None)
    if target:
        print("tapping", target["label"], target["center"])
        tap(*target["center"])
        adb("shell", "sleep", "1")
        print("screenshot:", screenshot(out / "after_tap.png"))
