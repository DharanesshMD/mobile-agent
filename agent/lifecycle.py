"""Phone power: boot, sleep and wake, so an idle phone costs almost nothing and the UI always says what state it is in.

States (`power`): `on` (ready to use), `sleeping` (frozen in memory, resumes in about a second), `off` (stopped, boots in about a minute) and
`waking` (resuming or booting). An idle phone sleeps after SLEEP_AFTER_IDLE_MIN minutes without a task, owner control or owner input, and is
stopped after STOP_AFTER_PAUSED_MIN more. Anything that needs the phone (a task, an owner click, a routine) wakes it first.

`EmulatorBackend` is for the local Android emulator on the Mac: sleep is SIGSTOP on the emulator process (no CPU, memory kept) and wake is
SIGCONT; stop is `adb emu kill`; boot starts `scripts/emulator.sh`. A cloud backend (docker pause / stop / start) fits the same interface.
"""
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from agent.config import SLEEP_AFTER_IDLE_MIN, STOP_AFTER_PAUSED_MIN

ROOT = Path(__file__).resolve().parent.parent
BOOT_TIMEOUT = 240


class EmulatorBackend:
    """Controls the local emulator process. `adb` is the command prefix, for example ["adb", "-s", "emulator-5554"]."""

    def __init__(self, adb: list[str], avd: str = "agent34ps", script: Path | None = None, log: Path | None = None):
        self.adb, self.avd = adb, avd
        self.script = script or ROOT / "scripts" / "emulator.sh"
        self.log = log or ROOT / "data" / "emulator.log"

    @staticmethod
    def available(avd: str = "agent34ps") -> bool:
        return (ROOT / "scripts" / "emulator.sh").exists() and (Path.home() / ".android" / "avd" / f"{avd}.avd").exists()

    def _adb(self, *args: str, timeout: float = 8) -> str:
        try:
            return subprocess.run([*self.adb, *args], capture_output=True, text=True, timeout=timeout).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def pids(self) -> list[int]:
        # Anchored at the start of the command line: a shell whose command text merely mentions the emulator must never match.
        out = subprocess.run(["pgrep", "-f", rf"^[^ ]*qemu-system[^ ]* .*-avd {re.escape(self.avd)}( |$)"], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    def status(self) -> str:
        """running, paused (SIGSTOP) or stopped."""
        pids = self.pids()
        if not pids:
            return "stopped"
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pids[0])], capture_output=True, text=True).stdout.strip()
        return "paused" if state.startswith("T") else "running"

    def booted(self) -> bool:
        return self._adb("shell", "getprop", "sys.boot_completed", timeout=4) == "1"

    def pause(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_SLEEP", timeout=4)  # screen off first, so apps stop playing and drawing
        for pid in self.pids():
            os.kill(pid, signal.SIGSTOP)

    def resume(self) -> None:
        for pid in self.pids():
            os.kill(pid, signal.SIGCONT)
        for _ in range(40):  # adb needs a moment to see the device again
            if self.booted():
                break
            time.sleep(0.5)
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=4)
        self._adb("shell", "wm", "dismiss-keyguard", timeout=4)

    def kick_network(self) -> None:
        """After a long freeze, open connections are dead (apps show playback or network errors): restart Wi-Fi so they reconnect."""
        self._adb("shell", "svc", "wifi", "disable", timeout=6)
        self._adb("shell", "svc", "wifi", "enable", timeout=6)

    def stop(self) -> None:
        for pid in self.pids():
            os.kill(pid, signal.SIGCONT)  # a frozen process cannot shut down
        self._adb("emu", "kill", timeout=8)
        for _ in range(30):
            if not self.pids():
                return
            time.sleep(0.5)
        for pid in self.pids():
            os.kill(pid, signal.SIGKILL)

    def start(self) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log, "ab") as out:
            subprocess.Popen(["/bin/bash", str(self.script), self.avd], stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, "PATH": f"{Path(os.environ.get('ANDROID_HOME', '/opt/homebrew/share/android-commandlinetools'))}/platform-tools:"
                                                         f"{os.environ.get('PATH', '')}"})


class Lifecycle:
    def __init__(self, backend, busy=lambda: False, on_ready=lambda cold: None, clock=time.time,
                 idle_s: float = SLEEP_AFTER_IDLE_MIN * 60, stop_s: float = STOP_AFTER_PAUSED_MIN * 60, tick_s: float = 10):
        self.backend, self.busy, self.on_ready, self.clock = backend, busy, on_ready, clock
        self.idle_s, self.stop_s, self.tick_s = idle_s, stop_s, tick_s
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop_thread = threading.Event()
        self.last_activity = clock()
        self.detail, self.boot_started, self.last_boot_s = "", 0.0, 60.0
        s = backend.status()
        self.power = {"running": "on", "paused": "sleeping"}.get(s, "off")
        self.since = clock()
        if self.power == "on":
            self._ready.set()

    # --- state ---------------------------------------------------------------------------------------------------
    def _set(self, power: str, detail: str = "") -> None:
        with self._lock:
            self.power, self.detail, self.since = power, detail, self.clock()
            if power == "on":
                self._ready.set()
            else:
                self._ready.clear()

    def touch(self) -> None:
        self.last_activity = self.clock()

    def info(self) -> dict:
        now = self.clock()
        out = {"power": self.power, "since": self.since, "detail": self.detail}
        if self.power == "on" and not self.busy():
            out["sleeps_in"] = max(0, round(self.idle_s - (now - self.last_activity)))
        if self.power == "sleeping":
            out["stops_in"] = max(0, round(self.stop_s - (now - self.since)))
        if self.power == "waking" and self.boot_started:
            out["elapsed"] = round(now - self.boot_started)
            out["expected"] = round(self.last_boot_s)
        return out

    # --- transitions (each runs in its own thread so the API never blocks) -----------------------------------------
    def wake(self) -> str:
        """Start waking if needed and return the current power state right away."""
        with self._lock:
            self.touch()
            if self.power in ("on", "waking"):
                return self.power
            cold = self.power == "off"
            slept = self.clock() - self.since
            self._set("waking", "Starting the phone…" if cold else "Waking the phone…")
            self.boot_started = self.clock()
        threading.Thread(target=self._wake, args=(cold, slept), daemon=True, name="phone-wake").start()
        return "waking"

    def _wake(self, cold: bool, slept: float) -> None:
        try:
            if cold:
                self.backend.start()
                self._set("waking", "Booting Android…")
                deadline = self.clock() + BOOT_TIMEOUT
                while not self.backend.booted():
                    if self.clock() > deadline:
                        raise TimeoutError("the phone did not finish booting")
                    time.sleep(2)
                self._set("waking", "Getting the phone ready…")
                self.last_boot_s = self.clock() - self.boot_started
            else:
                self.backend.resume()
                if slept > 90 and hasattr(self.backend, "kick_network"):
                    self._set("waking", "Reconnecting to the network…")
                    self.backend.kick_network()
            self.on_ready(cold)
            self.touch()
            self._set("on")
        except Exception as e:  # leave a state the owner can retry from
            self._set("off" if self.backend.status() == "stopped" else "sleeping", f"Could not wake the phone: {e}")

    def ensure_awake(self, timeout: float = 300) -> bool:
        """Block until the phone is on (used by phone tasks). False if it did not wake in time."""
        self.wake()
        return self._ready.wait(timeout)

    def sleep(self) -> bool:
        with self._lock:
            if self.power != "on" or self.busy():
                return False
            self.backend.pause()
            self._set("sleeping")
            return True

    def stop(self) -> bool:
        with self._lock:
            if self.power == "waking":
                return False
            self.backend.stop()
            self._set("off")
            return True

    def tick(self) -> None:
        """Idle policy: sleep after a quiet period, stop after a long sleep."""
        now = self.clock()
        actual = self.backend.status()  # the emulator can be stopped or resumed from outside (the terminal, a crash)
        with self._lock:
            if self.power == "on" and actual == "stopped":
                self._set("off", "The phone stopped unexpectedly")
            elif self.power == "sleeping" and actual == "stopped":
                self._set("off")
            elif self.power == "off" and actual == "running":
                self._set("on")
                self.touch()
        if self.power == "on" and not self.busy() and now - self.last_activity >= self.idle_s:
            self.sleep()
        elif self.power == "sleeping" and now - self.since >= self.stop_s:
            self.stop()

    def start(self) -> None:
        def loop() -> None:
            while not self._stop_thread.wait(self.tick_s):
                try:
                    self.tick()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True, name="phone-lifecycle").start()

    def close(self) -> None:
        self._stop_thread.set()
