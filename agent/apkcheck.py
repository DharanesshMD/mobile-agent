"""Check an APK the owner shares before it goes anywhere near their phone.

Static first (the manifest and the signing certificate are read with the Android SDK's own tools; nothing in the file is run on this computer), then an install on a throwaway
cloud phone, a 20-second launch with the crash log watched, and an uninstall. The answer is a verdict with reasons and a list of what was NOT checked: a clean result means
"nothing found in these checks", never "safe".
"""
import os
import re
import subprocess
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from agent import cloudtools
from agent.artifacts import Artifacts

MAX_APK_BYTES = 150 * 1024 * 1024
APK_MIME = "application/vnd.android.package-archive"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
WATCH_S = 20
HIGH_RISK = {  # permissions that together make an app powerful enough to be worth a second look
    "READ_SMS", "SEND_SMS", "RECEIVE_SMS", "BIND_ACCESSIBILITY_SERVICE", "BIND_DEVICE_ADMIN", "REQUEST_INSTALL_PACKAGES", "BIND_NOTIFICATION_LISTENER_SERVICE",
    "READ_CALL_LOG", "SYSTEM_ALERT_WINDOW", "MANAGE_EXTERNAL_STORAGE", "QUERY_ALL_PACKAGES", "ACCESS_BACKGROUND_LOCATION"}


class ApkError(ValueError):
    """The file is not an APK we can check; the message is shown to the owner."""


def sdk_tool(name: str) -> str:
    """apkanalyzer lives under cmdline-tools, apksigner under the newest build-tools."""
    root = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/opt/homebrew/share/android-commandlinetools")
    if name == "apkanalyzer":
        path = root / "cmdline-tools" / "latest" / "bin" / "apkanalyzer"
    else:
        builds = sorted((root / "build-tools").glob("*"), key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", p.name)])
        path = builds[-1] / name if builds else root / "build-tools" / name
    if not path.exists():
        raise ApkError(f"The Android SDK tool {name} is not installed on this computer, so I cannot read the APK.")
    return str(path)


def looks_like_apk(path: str | Path) -> None:
    """Cheap checks before anything reads the file: it is a ZIP with an AndroidManifest.xml and some code. Raises ApkError."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"PK\x03\x04":
                raise ApkError("That file is not an APK (it is not a ZIP archive).")
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            bomb = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as e:
        raise ApkError("That file is a damaged archive, not an APK.") from e
    if "AndroidManifest.xml" not in names:
        raise ApkError("That archive has no AndroidManifest.xml, so it is not an APK.")
    if bomb > 4 * 1024 * 1024 * 1024:
        raise ApkError("That archive unpacks to more than 4 GB; I will not open it.")


def _run(cmd: list[str], timeout: float = 90) -> str:
    env = {**os.environ}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise ApkError((r.stderr or r.stdout or "the SDK tool failed").strip().splitlines()[-1][:200])
    return r.stdout


def parse_manifest(xml: str) -> dict:
    """Facts from `apkanalyzer manifest print`: package, version, SDK levels, permissions, how many components, and two flags that matter."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ApkError("I could not read that APK's manifest.") from e
    sdk = root.find("uses-sdk")
    app = root.find("application")
    perms = sorted({p.get(ANDROID_NS + "name", "") for p in root.findall("uses-permission") if p.get(ANDROID_NS + "name")})
    comps = sum(len(app.findall(t)) for t in ("activity", "service", "receiver", "provider")) if app is not None else 0
    return {"package": root.get("package", ""), "version": root.get(ANDROID_NS + "versionName", ""),
            "min_sdk": (sdk.get(ANDROID_NS + "minSdkVersion") if sdk is not None else "") or "", "target_sdk": (sdk.get(ANDROID_NS + "targetSdkVersion") if sdk is not None else "") or "",
            "permissions": perms, "components": comps,
            "debuggable": app is not None and app.get(ANDROID_NS + "debuggable") == "true",
            "cleartext": app is not None and app.get(ANDROID_NS + "usesCleartextTraffic") == "true"}


def parse_certs(out: str) -> list[str]:
    return re.findall(r"certificate SHA-256 digest:\s*([0-9a-fA-F]{64})", out)


def analyze(path: str | Path) -> dict:
    """Read an APK without running it. Raises ApkError with a message that is safe to show."""
    looks_like_apk(path)
    info = parse_manifest(_run([sdk_tool("apkanalyzer"), "manifest", "print", str(path)]))
    if not info["package"]:
        raise ApkError("That APK has no package name.")
    try:
        certs = parse_certs(_run([sdk_tool("apksigner"), "verify", "--print-certs", str(path)]))
    except ApkError as e:
        certs = []
        info["signature_problem"] = str(e)
    info["certs"] = certs
    info["size_mb"] = round(Path(path).stat().st_size / 1e6, 1)
    return info


def judge(info: dict) -> tuple[str, list[str]]:
    """(level, reasons) from the static facts alone. Never "harmful": a static read cannot prove that; it says what the app is able to do."""
    reasons = []
    high = [p for p in (x.rsplit(".", 1)[-1] for x in info["permissions"]) if p in HIGH_RISK]
    danger = dict(cloudtools.dangerous_permissions(info["permissions"]))
    if len(high) >= 2:
        reasons.append("It asks for several powerful permissions: " + "; ".join(f"{p.lower().replace('_', ' ')} ({danger.get(p, 'powerful')})" for p in high[:5]))
    if info.get("signature_problem"):
        reasons.append("Its signature could not be verified: " + info["signature_problem"])
    elif not info["certs"]:
        reasons.append("It carries no signing certificate.")
    if info["debuggable"]:
        reasons.append("It is marked debuggable, which normal store apps are not.")
    try:
        if info["target_sdk"] and int(info["target_sdk"]) < 28:
            reasons.append(f"It targets an old Android version ({info['target_sdk']}), so it skips newer privacy protections.")
    except ValueError:
        pass
    return ("suspicious" if reasons else "nothing_found"), reasons


def facts_lines(info: dict) -> list[str]:
    danger = cloudtools.dangerous_permissions(info["permissions"])
    lines = [f"{info['package']} {info['version']}".strip(), f"{info['size_mb']} MB, target Android API {info['target_sdk'] or 'unknown'}, minimum {info['min_sdk'] or 'unknown'}, "
             f"{len(info['permissions'])} permissions requested, {info['components']} components"]
    lines += [f"Signing certificate SHA-256: {c}" for c in info.get("certs", [])[:2]]
    lines += [f"{name.lower().replace('_', ' ')}: {meaning}" for name, meaning in danger]
    return lines


INSTALL_REASONS = {
    "INSTALL_FAILED_NO_MATCHING_ABIS": "it is built only for chips this test phone does not have (the app ships 32-bit or other-architecture libraries only)",
    "INSTALL_FAILED_DEPRECATED_SDK_VERSION": "it targets an Android version that is too old for this phone to accept",
    "INSTALL_FAILED_OLDER_SDK": "it needs a newer Android version than this test phone has",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "a copy signed by someone else is already there",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "it has no valid signature, so Android will not install it",
}


def install_reason(e: Exception) -> str:
    """A plain-English reason an app could not be installed on the test phone, for the report."""
    code = getattr(e, "code", "")
    return INSTALL_REASONS.get(code) or (f"Android refused to install it ({code.replace('INSTALL_', '').replace('_', ' ').lower()})" if code else "Android refused to install it")


def _what_it_shows(describe, png: bytes) -> str:
    """One plain sentence about the launched app's screen from the vision model; empty when there is none or it fails (the pictures still go in the report)."""
    if not describe:
        return ""
    try:
        return str(describe(png) or "").strip()[:240]
    except Exception:
        return ""


def sandbox_run(device, art: Artifacts, apk: str, info: dict, watch_s: float | None = None, sleep=time.sleep, clock=time.time, describe=None) -> list[str]:
    """Install on the throwaway phone, launch, watch the crash log and the screen for `watch_s` seconds, and always uninstall. Returns the lines to add to the report."""
    package, notes, installed = info["package"], [], False
    watch_s = WATCH_S if watch_s is None else watch_s
    try:
        had = package in device.packages()
        if had:
            raise ApkError(f"{package} is already installed on that phone; I will not touch an app that was there before. Remove it first or check it on another phone.")
        try:
            device.install_apks([apk])
        except ValueError as e:  # not installable on the test phone: the static read still stands, and the report says what was not run
            if not hasattr(e, "code"):
                raise
            info["not_run"] = install_reason(e)
            return [f"I could not run it on the test phone: {info['not_run']}. This verdict is from reading the file only."]
        installed = True
        try:
            before = {(p, i) for p, i in device.crashes()}
        except Exception:
            before = set()
        device.open_app(package)
        end, fresh, shot = clock() + watch_s, [], False
        sleep(min(5, watch_s))
        while True:
            try:
                fresh = [(p, i) for p, i in device.crashes() if p == package and (p, i) not in before]
            except Exception:
                fresh = []
            if not shot:
                art.add_capture(device.screenshot(), "After launch", f"{package} a few seconds after opening")
                shot = True
            if fresh or clock() >= end:
                break
            sleep(2)
        front = device.foreground() if hasattr(device, "foreground") else ""
        if fresh:
            notes.append(f"It crashed {len(fresh)} time(s) while launching.")
        elif front == package:
            notes.append(f"It launched and was still on screen after {round(watch_s)} s with no crash.")
            png = device.screenshot()
            art.add_capture(png, f"After {round(watch_s)} seconds", "Still running")
            seen = _what_it_shows(describe, png)
            if seen:
                info["screen"] = seen
                notes.append(f"What it showed: {seen}")
        else:
            notes.append(f"It launched, but a different app was in front after {round(watch_s)} s ({front or 'unknown'}); it may have closed itself or opened another app.")
        return notes
    finally:
        if installed:
            try:
                device.uninstall(package)
            except Exception as e:
                notes.append(f"I could not uninstall it from the test phone: {e}")
