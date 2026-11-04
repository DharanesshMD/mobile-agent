"""The emulator on the computer that runs Docker. A container cannot start it, but it can reach the host's adb server (host.docker.internal:5037, loopback only
on the host), and every adb command, touch and screenshot of this app goes through adb. So: `scripts/host-emulator.sh` on the host installs and starts the
emulator (hardware accelerated), and this watcher notices it, saves the settings and restarts the app once so the phone is used like on localhost."""
import logging
import os
import re
import subprocess
import threading
from typing import Callable

log = logging.getLogger("agent.hostemulator")
HOST = "host.docker.internal"
PORT = 5037
SETTINGS = ("ANDROID_SERIAL", "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ANDROID_ADB_SERVER_HOST")


def probe(run=subprocess.run, host: str = HOST, port: int = PORT) -> str | None:
    """The serial of an emulator the host's adb server lists as ready, else None. `-H` names a remote server, so this never starts or kills one."""
    try:
        out = run(["adb", "-H", host, "-P", str(port), "devices"], capture_output=True, text=True, timeout=6).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"^(emulator-\d+)\s+device\b", out, re.M)
    return m.group(1) if m else None


def configured() -> bool:
    return os.environ.get("ADB_SERVER_SOCKET", "").startswith(f"tcp:{HOST}:")


class HostEmulator:
    def __init__(self, save: Callable[[str, str], None], restart: Callable[[], None], probe_fn=probe, interval: float = 6.0):
        self.save, self.restart, self.probe_fn, self.interval = save, restart, probe_fn, interval
        self.found: str | None = None
        self.applying = False
        self.stop = threading.Event()

    def state(self) -> dict:
        return {"configured": configured(), "serial": os.environ.get("ANDROID_SERIAL", "") if configured() else (self.found or ""), "found": bool(self.found), "applying": self.applying,
                "command": "bash scripts/host-emulator.sh"}

    def check(self) -> None:
        if configured():
            return
        self.found = self.probe_fn()
        if self.found and not self.applying:
            self.applying = True
            log.info("found %s on the host's adb server; connecting to it and restarting", self.found)
            for k, v in {"ANDROID_SERIAL": self.found, "ADB_SERVER_SOCKET": f"tcp:{HOST}:{PORT}", "ANDROID_ADB_SERVER_ADDRESS": HOST,
                         "ANDROID_ADB_SERVER_PORT": str(PORT), "ANDROID_ADB_SERVER_HOST": HOST}.items():
                self.save(k, v)
            self.restart()

    def start(self) -> None:
        def loop():
            while not self.stop.wait(self.interval):
                try:
                    self.check()
                except Exception:
                    log.exception("host emulator check failed")
        threading.Thread(target=loop, daemon=True, name="host-emulator-watch").start()
