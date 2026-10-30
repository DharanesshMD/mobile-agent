"""The games phone: Android 13 arm64 in a Redroid container inside a Lima VM, controlled over adb at localhost:5565.

`LimaRedroidBackend` has the same methods `Lifecycle` uses on `EmulatorBackend`, so the sleep and wake policy, the health watch and the UI work unchanged.
Sleep is `docker pause` (about 0.2 s, adb carries on afterwards), stop shuts the container AND the Lima VM down (frees about 1-2 GB of the Mac's RAM;
a cold start is then about 15 s, against about 5 s when only the container was stopped). Everything goes through `scripts/virtual-android/redroid.sh`.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERIAL = "localhost:5565"  # not 5555: on the Mac that is the official emulator's own adb port
VM = "rd"
GAMES = "games"  # the phone id used by the bot and the job target (`virtual:games`)


def tool_path() -> str:
    """The service may start with a minimal PATH: Lima and adb live in Homebrew's and the SDK's folders."""
    sdk = os.environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools")
    return os.pathsep.join(["/opt/homebrew/bin", f"{sdk}/platform-tools", os.environ.get("PATH", "")])


class LimaRedroidBackend:
    def __init__(self, serial: str = SERIAL, vm: str = VM, script: Path | None = None, log: Path | None = None, run=None, popen=None):
        self.serial, self.vm = serial, vm
        self.adb = ["adb", "-s", serial]
        self.script = script or ROOT / "scripts" / "virtual-android" / "redroid.sh"
        self.log = log or ROOT / "data" / "virtual-android.log"
        self._run, self._popen = run or subprocess.run, popen or subprocess.Popen  # replaceable in tests
        self.env = {**os.environ, "PATH": tool_path()}

    @staticmethod
    def available(vm: str = VM, run=None) -> bool:
        """Is Lima installed and the games VM created? (Nothing is started.)"""
        path = tool_path()
        if not shutil.which("limactl", path=path):
            return False
        try:
            out = (run or subprocess.run)(["limactl", "list", "--format", "{{.Name}}"], capture_output=True, text=True, timeout=15, env={**os.environ, "PATH": path})
        except (subprocess.TimeoutExpired, OSError):
            return False
        return vm in out.stdout.split()

    def _script(self, *args: str, timeout: float = 60) -> str:
        try:
            out = self._run(["/bin/bash", str(self.script), *args], capture_output=True, text=True, timeout=timeout, env=self.env)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return (out.stdout or "").strip()

    def _adb(self, *args: str, timeout: float = 8) -> str:
        try:
            return self._run([*self.adb, *args], capture_output=True, text=True, timeout=timeout, env=self.env).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    # --- the contract Lifecycle uses ---------------------------------------------------------------------------------
    def status(self) -> str:
        """running, paused (docker pause) or stopped (the container or the whole VM is down)."""
        word = self._script("state", timeout=30)
        return word if word in ("running", "paused") else "stopped"

    def _connect(self) -> None:
        self._adb("connect", self.serial, timeout=10)  # after a VM restart the old connection is dead

    def booted(self) -> bool:
        if self._adb("shell", "getprop", "sys.boot_completed", timeout=4) != "1":
            self._connect()
            return False
        return "size" in self._adb("shell", "wm", "size", timeout=5).lower()

    def healthy(self) -> bool:
        return "size" in self._adb("shell", "wm", "size", timeout=8).lower()

    def pause(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_SLEEP", timeout=4)  # screen off first, so the game stops drawing
        self._script("pause")

    def resume(self) -> bool:
        """Unfreeze and wait until adb answers again. False if the container is gone or never answers (the caller then boots it again)."""
        self._script("resume")
        for i in range(60):  # up to 30 s; adb normally answers at once
            if self.status() == "stopped":
                return False
            if self.booted():
                break
            time.sleep(0.5)
        else:
            return False
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=4)
        self._adb("shell", "wm", "dismiss-keyguard", timeout=4)
        return True

    def stop(self) -> None:
        self._script("shutdown", timeout=120)

    def start(self) -> None:
        """Boots the VM and the container in the background; `Lifecycle` polls `booted()`."""
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "ab") as out:
            self._popen(["/bin/bash", str(self.script), "start"], stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True, env=self.env)
