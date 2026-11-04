"""Phone power: boot, sleep and wake, so an idle phone costs almost nothing and the UI always says what state it is in.

States (`power`): `on` (ready to use), `sleeping` (frozen in memory, resumes in about a second), `off` (stopped, boots in about a minute) and
`waking` (resuming or booting). An idle phone sleeps after SLEEP_AFTER_IDLE_MIN minutes without a task, owner control or owner input, and is
stopped after STOP_AFTER_PAUSED_MIN more. Anything that needs the phone (a task, an owner click, a routine) wakes it first.

`EmulatorBackend` is for the local Android emulator on the Mac: sleep is SIGSTOP on the emulator process (no CPU, memory kept) and wake is
SIGCONT; stop is `adb emu kill`; boot starts `scripts/emulator.sh`. A cloud backend (docker pause / stop / start) fits the same interface.
"""
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from agent.config import SLEEP_AFTER_IDLE_MIN, STOP_AFTER_PAUSED_MIN

ROOT = Path(__file__).resolve().parent.parent
BOOT_TIMEOUT = 420  # a cold boot takes about a minute, but 4+ minutes when the Mac is short on memory or busy


class EmulatorBackend:
    """Controls the local emulator process. `adb` is the command prefix, for example ["adb", "-s", "emulator-5554"]."""

    def __init__(self, adb: list[str], avd: str = "agent34ps", script: Path | None = None, log: Path | None = None):
        self.adb, self.avd = adb, avd
        self.script = script or ROOT / "scripts" / "emulator.sh"
        self.log = log or ROOT / "data" / "emulator.log"

    @staticmethod
    def available(avd: str = "agent34ps") -> bool:
        return (ROOT / "scripts" / "emulator.sh").exists() and (Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd") / f"{avd}.avd").exists()

    def _adb(self, *args: str, timeout: float = 8) -> str:
        try:
            return subprocess.run([*self.adb, *args], capture_output=True, text=True, timeout=timeout).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return ""

    def pids(self) -> list[int]:
        # Anchored at the start of the command line: a shell whose command text merely mentions the emulator must never match.
        out = subprocess.run(["pgrep", "-f", rf"^[^ ]*qemu-system[^ ]* .*-avd {re.escape(self.avd)}( |$)"], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    @staticmethod
    def netsim_pids() -> list[int]:
        """The emulator's network helper pings the emulator; if only the emulator is frozen it gives up on it after about a minute and the emulator
        dies when it wakes. So the helper is frozen and resumed together with the emulator."""
        out = subprocess.run(["pgrep", "-x", "netsimd"], capture_output=True, text=True).stdout
        return [int(p) for p in out.split()]

    def status(self) -> str:
        """running, paused (SIGSTOP) or stopped."""
        pids = self.pids()
        if not pids:
            return "stopped"
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pids[0])], capture_output=True, text=True).stdout.strip()
        return "paused" if state.startswith("T") else "running"

    def booted(self) -> bool:
        """Usable, not just booted: Android sets boot_completed early, and after a long freeze the window service can still be restarting."""
        return self._adb("shell", "getprop", "sys.boot_completed", timeout=4) == "1" and "size" in self._adb("shell", "wm", "size", timeout=5).lower()

    def healthy(self) -> bool:
        """Does the phone answer right now? adb up and the window service alive (a hung or half-dead emulator fails this)."""
        return "size" in self._adb("shell", "wm", "size", timeout=8).lower()

    def pause(self) -> None:
        self._adb("shell", "input", "keyevent", "KEYCODE_SLEEP", timeout=4)  # screen off first, so apps stop playing and drawing
        for pid in self.pids() + self.netsim_pids():
            os.kill(pid, signal.SIGSTOP)

    def resume(self) -> bool:
        """Unfreeze and wait until adb really answers again. False if the emulator is gone or never recovers (the caller then reboots it)."""
        for pid in self.netsim_pids() + self.pids():
            os.kill(pid, signal.SIGCONT)
        for i in range(180):  # up to 90 s: after a long freeze on a busy Mac this is still much faster than a cold boot
            if not self.pids():
                return False
            if i == 6:
                self._adb("reconnect", "offline", timeout=4)  # nudge adb if it still thinks the device is offline
            if self.booted():
                break
            time.sleep(0.5)
        else:
            return False
        self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=4)
        self._adb("shell", "wm", "dismiss-keyguard", timeout=4)
        return True

    def network_ok(self) -> bool:
        """Does the phone still reach the internet after a freeze? A short pause usually leaves it connected, and then there is nothing to repair."""
        return "1 received" in self._adb("shell", "ping", "-c", "1", "-W", "2", "1.1.1.1", timeout=6)

    def kick_network(self) -> None:
        """After a long freeze, open connections are dead (apps show playback or network errors): restart Wi-Fi so they reconnect."""
        self._adb("shell", "svc", "wifi", "disable", timeout=6)
        self._adb("shell", "svc", "wifi", "enable", timeout=6)

    def stop(self) -> None:
        for pid in self.netsim_pids() + self.pids():
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
                 idle_s: float = SLEEP_AFTER_IDLE_MIN * 60, stop_s: float = STOP_AFTER_PAUSED_MIN * 60, tick_s: float = 10,
                 health_every_s: float = 60):
        self.backend, self.busy, self.on_ready, self.clock = backend, busy, on_ready, clock
        self.idle_s, self.stop_s, self.tick_s, self.health_every_s = idle_s, stop_s, tick_s, health_every_s
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._stop_thread = threading.Event()
        self.last_activity = clock()
        self.detail, self.boot_started, self.last_boot_s = "", 0.0, 60.0
        self.last_resume_s, self.waking_cold = 4.0, False  # how long waking a paused phone took last time (the progress estimate), and whether the wake in progress is a boot
        self._unhealthy, self._health_at = 0, 0.0
        s = backend.status()
        self.power = {"running": "on", "paused": "sleeping"}.get(s, "off")
        self.since = clock()
        if self.power == "on":
            self._ready.set()

    # --- state ---------------------------------------------------------------------------------------------------
    def _set(self, power: str, detail: str = "") -> None:
        if power != self.power:
            logging.getLogger("agent.phone").info("%s: %s -> %s %s", type(self.backend).__name__, self.power, power, detail[:80])
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
            out["expected"] = round(self.last_boot_s if self.waking_cold else self.last_resume_s)
        return out

    # --- transitions (each runs in its own thread so the API never blocks) -----------------------------------------
    def wake(self) -> str:
        """Start waking if needed and return the current power state right away."""
        with self._lock:
            self.touch()
            if self.power in ("on", "waking"):
                return self.power
            cold = self.power == "off" or self.backend.status() == "stopped"  # a sleeping phone whose process is gone must boot
            slept = self.clock() - self.since
            self._set("waking", "Starting the phone…" if cold else "Waking the phone…")
            self.boot_started, self.waking_cold = self.clock(), cold
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
            elif self.backend.resume() is False:
                # It did not come back (it crashed while frozen, or adb never recovered): restart it instead of leaving the owner stuck.
                self._set("waking", "Restarting the phone…")
                self.backend.stop()
                self._wake(True, 0)
                return
            elif slept > 90 and hasattr(self.backend, "kick_network") and not (hasattr(self.backend, "network_ok") and self.backend.network_ok()):
                self._set("waking", "Reconnecting to the network…")  # only when the connection really went stale
                self.backend.kick_network()
            self.on_ready(cold)
            if not cold:
                self.last_resume_s = max(1.0, self.clock() - self.boot_started)
            self.touch()
            self._set("on")
        except Exception as e:  # leave a state the owner can retry from
            self._set("off" if self.backend.status() == "stopped" else "sleeping", f"Could not wake the phone: {e}")

    def ensure_awake(self, timeout: float = 300) -> bool:
        """Block until the phone is on (used by phone tasks). False if it did not wake in time."""
        self.wake()
        return self._ready.wait(timeout)

    def ensure_healthy(self, timeout: float = 300) -> bool:
        """Block until the phone is on AND answering. A phone that is on but not responding is restarted: tasks and owner actions must never hit a dead phone."""
        if not self.ensure_awake(timeout):
            return False
        healthy = getattr(self.backend, "healthy", None)
        if healthy is None or healthy():
            return True
        return self.recover(timeout)

    def recover(self, timeout: float = 300) -> bool:
        """Restart a phone that stopped answering and wait until it is on again. Safe to call from several threads: one restarts it, the rest wait."""
        with self._lock:
            starter = self.power != "waking"
            if starter:
                self._set("waking", "Restarting the phone…")
                self.boot_started = self.clock()
        if starter:
            try:
                self.backend.stop()
            except Exception:
                pass
            self._wake(True, 0)  # boots it, waits until it answers, and leaves a state the owner can retry from if that fails
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
        self._watch_health(now)
        if self.power == "on" and not self.busy() and now - self.last_activity >= self.idle_s:
            self.sleep()
        elif self.power == "sleeping" and now - self.since >= self.stop_s:
            self.stop()

    def _watch_health(self, now: float) -> None:
        """An idle phone that is 'on' but stopped answering twice in a row (checked once a minute) is restarted in the background."""
        healthy = getattr(self.backend, "healthy", None)
        if healthy is None or self.power != "on" or self.busy() or now - self._health_at < self.health_every_s:
            return
        self._health_at = now
        if healthy():
            self._unhealthy = 0
            return
        self._unhealthy += 1
        if self._unhealthy >= 2:
            self._unhealthy = 0
            threading.Thread(target=self.recover, daemon=True, name="phone-recover").start()

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
