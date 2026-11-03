"""Install the Mobile Agent app on the owner's own phone from this computer, over adb (a cable or Wi-Fi debugging).

Why: Google Play Protect blocks an app that declares the Accessibility permission when it is installed from an "internet-sideloading source" (a browser download,
a messaging app, a file manager), in some countries, with only an OK button ("App blocked to protect your device"). An install made by adb is not one of those sources
and commonly works. Google does not document exemptions, so nothing here is a guarantee: the steps report what really happened.

What this module will do, and nothing else: list the owner's phones that adb sees (never the emulator or Cloud phone 2), pair and connect over Wi-Fi debugging, and run ONE
fixed job on a listed phone: install our own APK, lift Android's "restricted settings" for it, open the app's pairing link, and open the Accessibility settings screen.
It never turns the accessibility service on (the owner does that), takes no file path, and never logs or returns a pairing code.
"""
import hashlib
import ipaddress
import logging
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from agent.logs import audit

log = logging.getLogger("agent.install")

PACKAGE = "ai.mobileagent.client"
ACTIVITY = ".MainActivity"
APK_PATH = "data/apk/mobile-agent.apk"
CLASHING = {"emulator-5554", "localhost:5565", "127.0.0.1:5565", "[::1]:5565"}  # the shared emulator and Cloud phone 2
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
LINK_RE = re.compile(r"^mobileagent://pair\?server=http://[0-9A-Za-z.:\[\]-]{3,80}&code=[A-Z0-9-]{4,20}$")
STEPS = (("apk", "Check the app file"), ("install", "Install it on the phone"), ("restricted", "Allow it to use Accessibility (Android 13+ restricted settings)"),
         ("link", "Hand it this computer's pairing link"), ("settings", "Open the Accessibility settings on the phone"))
ADVICE = {  # adb's reason -> what the owner can do
    "INSTALL_FAILED_USER_RESTRICTED": "The phone refused an install from the computer. On Xiaomi (MIUI): Settings > Additional settings > Developer options > turn on Install via USB (it needs a Mi account and mobile data once); "
                                      "if a confirm popup keeps cancelling, turn off MIUI optimization in the same screen. Then try again.",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "A copy signed differently is already installed. Uninstall Mobile Agent on the phone, then try again.",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "A newer version is already installed. Uninstall Mobile Agent on the phone first, or build the app again.",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "The phone is out of storage. Free some space and try again.",
    "INSTALL_FAILED_VERIFICATION_FAILURE": "The phone's package check refused it. Pause Play Protect scanning for a minute (Play Store > profile > Play Protect > settings) and try again.",
    "INSTALL_FAILED_ABORTED": "The install was cancelled on the phone. Tap Install when the phone asks, then try again.",
    "INSTALL_CANCELED_BY_USER": "The install was cancelled on the phone. Tap Install when the phone asks, then try again.",
    "unauthorized": "The phone has not trusted this computer yet. Unlock it and tap Allow on the 'Allow USB debugging?' prompt, then try again.",
    "offline": "The phone is offline. Unplug and plug the cable again, or turn Wireless debugging off and on, then try again.",
    "no devices": "adb cannot see the phone any more. Reconnect it and try again.",
}


class InstallError(ValueError):
    """Bad input or a refusal the owner can read."""


def run_command(argv: list[str], timeout: float = 60, input: str | None = None) -> tuple[int, str]:
    """Run a command (a list: never a shell) and return (exit code, stdout and stderr together)."""
    try:
        p = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "adb was not found on this computer (install the Android platform-tools, or set ADB to its path)"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check_host(host: str) -> str:
    host = (host or "").strip().strip("[]")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not HOST_RE.fullmatch(host) or ".." in host:
        raise InstallError("the address must be an IP address such as 192.168.0.23 (as shown on the phone)")
    return host


def check_port(port) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise InstallError("the port must be a number") from None
    if not 1 <= p <= 65535:
        raise InstallError("the port must be between 1 and 65535")
    return p


def check_code(code: str) -> str:
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise InstallError("the pairing code is the 6 digits the phone shows")
    return code


def endpoint(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class Job:
    def __init__(self, serial: str, name: str):
        self.id, self.serial, self.name, self.created = uuid.uuid4().hex[:10], serial, name, time.time()
        self.state, self.advice, self.final = "running", "", ""
        self.steps = [{"key": k, "label": label, "state": "pending", "detail": ""} for k, label in STEPS]

    def step(self, key: str, state: str, detail: str = "") -> None:
        s = next(s for s in self.steps if s["key"] == key)
        s["state"], s["detail"] = state, detail[:400]

    def view(self) -> dict:
        return {"id": self.id, "serial": self.serial, "phone": self.name, "state": self.state, "steps": [dict(s) for s in self.steps], "advice": self.advice, "final": self.final}


class PhoneInstaller:
    def __init__(self, run=run_command, adb: str = "adb", apk_path: str | Path = APK_PATH, managed=lambda: set(), link=lambda: None, sleep=time.sleep):
        self.run, self.adb, self.apk, self.managed, self.link, self.sleep = run, adb, Path(apk_path), managed, link, sleep
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _adb(self, *args: str, timeout: float = 60, input: str | None = None) -> tuple[int, str]:
        return self.run([self.adb, *args], timeout=timeout, input=input)

    # ---- which phones
    def devices(self) -> list[dict]:
        """The owner's own phones that adb sees. The shared emulator, Cloud phone 2, loopback tunnels and any phone the app already manages are never listed."""
        rc, out = self._adb("devices", "-l", timeout=15)
        skip = {s.lower() for s in CLASHING} | {str(s).lower() for s in (self.managed() or ())}
        phones = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2 or parts[1] not in ("device", "unauthorized", "offline", "no", "authorizing") or line.startswith("*"):
                continue
            serial, state = parts[0], parts[1]
            low = serial.lower()
            if low in skip or low.startswith("emulator-") or re.match(r"^(localhost|127\.\d+\.\d+\.\d+|\[::1\])(:\d+)?$", low):
                continue
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            model = (info.get("model") or info.get("device") or "").replace("_", " ")
            phones.append({"serial": serial, "state": state if state != "no" else "offline", "model": model or "Android phone", "wireless": ":" in serial or "_adb-tls" in serial})
        return phones

    def _listed(self, serial: str) -> dict:
        for d in self.devices():
            if d["serial"] == serial:
                return d
        raise InstallError("that phone is not in the list: check the cable or Wi-Fi debugging and try again")

    # ---- Wi-Fi debugging
    def pair(self, host: str, port, code: str) -> dict:
        host, port, code = check_host(host), check_port(port), check_code(code)
        rc, out = self._adb("pair", endpoint(host, port), timeout=40, input=code + "\n")  # the code goes in on stdin, not on the command line
        text = out.replace(code, "******")
        if rc != 0 or not re.search(r"(?i)successfully paired", text):
            low = text.lower()
            if rc in (124, 127) or any(w in low for w in ("unable", "refused", "timed out", "no route")):
                why = "could not reach the phone at that address and port: use the address and port shown under 'Pair device with pairing code', with the phone on the same Wi-Fi"
            elif "wrong" in low or "password" in low or "failed" in low:
                why = "the code was wrong or has expired: tap 'Pair device with pairing code' on the phone again for a new one"
            else:
                why = "pairing failed" + (f" ({text.strip()[:120]})" if text.strip() else "")
            log.warning("wireless pairing failed: %s", why[:80])
            return {"ok": False, "message": why}
        log.info("wireless pairing succeeded with %s", host)
        found = self.discover(host)
        if found:
            conn = self.connect(found[0], found[1])
            if conn["ok"]:
                return {"ok": True, "serial": conn["serial"], "message": "Paired and connected."}
        return {"ok": True, "needs_port": True, "message": "Paired. Now type the address and port from the main 'Wireless debugging' screen (it is a different port) and press Connect."}

    def discover(self, host: str) -> tuple[str, int] | None:
        """The connect endpoint a paired phone announces on the local network (`adb mdns services`), matched on its address."""
        host = check_host(host)
        rc, out = self._adb("mdns", "services", timeout=15)
        for line in out.splitlines():
            if "_adb-tls-connect" not in line:
                continue
            m = re.search(r"([0-9A-Fa-f.:\[\]]+):(\d{2,5})\s*$", line.strip())
            if m and m.group(1).strip("[]") == host:
                return host, int(m.group(2))
        return None

    def connect(self, host: str, port) -> dict:
        host, port = check_host(host), check_port(port)
        rc, out = self._adb("connect", endpoint(host, port), timeout=20)
        low = out.lower()
        if rc == 0 and ("connected to" in low or "already connected" in low) and not re.search(r"cannot|failed|unable|refused", low):
            return {"ok": True, "serial": endpoint(host, port), "message": "Connected."}
        return {"ok": False, "message": "Could not connect: check Wireless debugging is on, and use the address and port from its main screen (not the pairing one)."}

    # ---- the install job
    def start(self, serial: str) -> dict:
        phone = self._listed(serial)
        if phone["state"] != "device":
            raise InstallError(ADVICE.get(phone["state"], "the phone is not ready"))
        with self._lock:
            if any(j.state == "running" for j in self.jobs.values()):
                raise InstallError("an install is already running: wait for it to finish")
            job = Job(serial, phone["model"])
            self.jobs = {k: v for k, v in list(self.jobs.items())[-19:]}
            self.jobs[job.id] = job
        audit("phone install started", phone=phone["model"], serial=serial)
        threading.Thread(target=self._work, args=(job,), daemon=True, name="phone-install").start()
        return job.view()

    def job(self, job_id: str) -> dict | None:
        j = self.jobs.get(job_id)
        return j.view() if j else None

    def _fail(self, job: Job, key: str, why: str, advice: str = "") -> None:
        job.step(key, "failed", why)
        job.state, job.advice = "failed", advice
        for s in job.steps:
            if s["state"] == "pending":
                s["state"] = "skipped"
        log.warning("install failed at %s: %s", key, why[:120])

    def _work(self, job: Job) -> None:
        s = job.serial
        try:
            job.step("apk", "running")
            if not self.apk.exists():
                return self._fail(job, "apk", "The app file is not built yet.", "Run scripts/build_apk.sh in the project folder (needs the Android SDK), then try again.")
            digest = hashlib.sha256(self.apk.read_bytes()).hexdigest()
            job.step("apk", "done", f"{self.apk.stat().st_size / 1e6:.1f} MB, SHA-256 {digest[:12]}…")

            job.step("install", "running", "If the phone shows an install prompt, tap Install.")
            rc, out = self._adb("-s", s, "install", "-r", "-d", str(self.apk), timeout=240)
            if rc != 0 or "Success" not in out:
                code = (re.search(r"INSTALL_[A-Z_]+", out) or re.search(r"(?i)\b(unauthorized|offline)\b", out) or re.search(r"(?i)(no devices)", out))
                key = code.group(0) if code and code.group(0) in ADVICE else (code.group(0).lower() if code and code.group(0).lower() in ADVICE else "")
                why = (code.group(0) if code else (out.strip().splitlines() or ["the install failed"])[-1])[:200]
                return self._fail(job, "install", f"adb said: {why}", ADVICE.get(key, "Read the message above. Pausing Play Protect scanning for a minute sometimes helps (Play Store > profile > Play Protect > settings)."))
            job.step("install", "done", "Installed.")

            job.step("restricted", "running")
            rc, out = self._adb("-s", s, "shell", "appops", "set", PACKAGE, "ACCESS_RESTRICTED_SETTINGS", "allow", timeout=20)
            if rc == 0 and not re.search(r"(?i)error|exception|unknown|invalid", out):
                job.step("restricted", "done", "Done.")
            else:
                job.step("restricted", "skipped", "Not needed or not allowed on this phone. If the Accessibility switch is grey: App info > three dots > Allow restricted settings.")

            link = self.link()
            if link and LINK_RE.match(link):
                job.step("link", "running")
                rc, out = self._adb("-s", s, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-n", f"{PACKAGE}/{ACTIVITY}", "-d", f"'{link}'", timeout=30)
                if rc == 0 and "Error" not in out:
                    job.step("link", "done", "The app has the pairing link and will ask you to confirm.")
                else:
                    job.step("link", "skipped", "Could not open the app from here: open it yourself and type the pairing code.")
            else:
                job.step("link", "skipped", "Switch on 'Allow phones on this Wi-Fi' above and install again, or type the pairing code in the app.")
            self.sleep(1.5)

            job.step("settings", "running")
            rc, out = self._adb("-s", s, "shell", "am", "start", "-a", "android.settings.ACCESSIBILITY_SETTINGS", timeout=20)
            job.step("settings", "done" if rc == 0 and "Error" not in out else "skipped",
                     "Opened on the phone." if rc == 0 and "Error" not in out else "Open Settings > Accessibility on the phone yourself.")
            job.state = "done"
            job.final = ("Left for you on the phone: turn on Mobile Agent under Accessibility (nothing is turned on for you), then go back to the app and tap Confirm to pair. "
                         "The app shows a notification while it is connected.")
            log.info("install finished on %s", job.name)
        except Exception as e:  # a bug must show up in the dialog, not vanish in a thread
            log.error("install crashed", exc_info=True)
            job.state, job.advice = "failed", f"Something went wrong here ({type(e).__name__})."


def register_install(app, auth, installer: PhoneInstaller) -> None:
    """The owner's routes (local main API, same sign-in as the rest)."""
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel, Field

    class Where(BaseModel):
        host: str = Field(max_length=255)
        port: int | str

    class Pair(Where):
        code: str = Field(max_length=12)

    class Run(BaseModel):
        serial: str = Field(max_length=100)

    def guard(fn, *a):
        try:
            return fn(*a)
        except InstallError as e:
            raise HTTPException(422, str(e)) from None

    @app.get("/api/install/devices", dependencies=[Depends(auth)])
    def install_devices():
        return {"devices": installer.devices(), "apk": installer.apk.exists()}

    @app.post("/api/install/pair", dependencies=[Depends(auth)])
    def install_pair(body: Pair):
        return guard(installer.pair, body.host, body.port, body.code)

    @app.post("/api/install/connect", dependencies=[Depends(auth)])
    def install_connect(body: Where):
        return guard(installer.connect, body.host, body.port)

    @app.post("/api/install/run", dependencies=[Depends(auth)])
    def install_run(body: Run):
        return {"job": guard(installer.start, body.serial)}

    @app.get("/api/install/jobs/{job_id}", dependencies=[Depends(auth)])
    def install_job(job_id: str):
        j = installer.job(job_id)
        if not j:
            raise HTTPException(404, "unknown install")
        return j
