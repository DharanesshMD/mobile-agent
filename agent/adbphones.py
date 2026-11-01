"""Extra adb phones: any adb endpoint the owner manages (a real phone over USB debugging, BlueStacks with adb enabled, a cloud phone).

Configured as `ADB_PHONES="name=serial,name=serial"` (environment) or in `data/phones.json` (`{"name": "serial"}`). Each one that answers `adb -s <serial> get-state`
gets its own bot, lane and live view, like the games phone, but no power management: it is always on while adb reaches it, and a job on a phone that has gone offline fails
honestly. The serial is always named in every adb call, and a serial that would clash with the shared emulator or the games phone is refused.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

RESERVED_NAMES = {"games", "emulator", "master", "phone", "shared", "default", "main"}
CLASHING_SERIALS = {"emulator-5554", "localhost:5565", "127.0.0.1:5565", "[::1]:5565"}  # the shared emulator and the games phone
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")
SERIAL_RE = re.compile(r"^[\w.:\[\]-]{3,60}$")
STATE_TTL = 10  # seconds a get-state answer is reused: the bot list is polled every few seconds


def parse_adb_phones(spec: str | dict | None) -> tuple[dict[str, str], list[str]]:
    """({name: serial}, problems) from "a=serial,b=serial" or a dict. Names are lowercased; bad entries are reported, never silently used."""
    if not spec:
        return {}, []
    pairs = list(spec.items()) if isinstance(spec, dict) else [tuple(part.split("=", 1)) if "=" in part else (part, "") for part in re.split(r"[,\n;]", spec) if part.strip()]
    out, problems = {}, []
    for raw_name, raw_serial in pairs:
        name, serial = str(raw_name).strip().lower(), str(raw_serial).strip()
        if not NAME_RE.match(name):
            problems.append(f"'{name}': the name must be 1 to 20 letters, digits, - or _")
        elif name in RESERVED_NAMES:
            problems.append(f"'{name}' is a reserved name")
        elif not SERIAL_RE.match(serial):
            problems.append(f"'{name}': '{serial}' is not an adb serial")
        elif serial.lower() in CLASHING_SERIALS or serial.lower().startswith("emulator-"):
            problems.append(f"'{name}': {serial} clashes with the shared emulator or the games phone")
        elif serial in out.values():
            problems.append(f"'{name}': {serial} is already used by another name")
        elif name in out:
            problems.append(f"'{name}' is listed twice")
        else:
            out[name] = serial
    return out, problems


def load_adb_phones(env: dict | None = None, path: str | Path = "data/phones.json") -> tuple[dict[str, str], list[str]]:
    """The environment value wins over the file; both are merged name by name."""
    env = os.environ if env is None else env
    found, problems = {}, []
    p = Path(path)
    if p.exists():
        try:
            from_file, bad = parse_adb_phones(json.loads(p.read_text()))
            found.update(from_file)
            problems += bad
        except (OSError, ValueError) as e:
            problems.append(f"{p}: {e}")
    from_env, bad = parse_adb_phones(env.get("ADB_PHONES", ""))
    found.update(from_env)
    return found, problems + bad


class AdbPhones:
    """The configured extra phones and whether adb reaches each one right now (cached for a few seconds)."""

    def __init__(self, config: dict[str, str], run=None, clock=time.time):
        self.config, self._run, self._clock = dict(config), run or subprocess.run, clock
        self._state: dict[str, tuple[float, bool]] = {}
        self._play: dict[str, bool] = {}
        self._lock = threading.Lock()
        self.device_factory = None  # (serial) -> a Device-like object; the app sets agent.device.Device

    def device(self, name: str):
        """The Device of one extra phone, with `has_play` detected while it is online."""
        dev = self.device_factory(self.config[name])
        dev.has_play = self.has_play(name)
        return dev

    def names(self) -> list[str]:
        return sorted(self.config)

    def serial(self, name: str) -> str:
        return self.config[name]

    def _adb(self, name: str, *args: str, timeout: float = 4) -> str:
        try:
            out = self._run(["adb", "-s", self.config[name], *args], capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return (out.stdout or "").strip()

    def online(self, name: str, fresh: bool = False) -> bool:
        """Does `adb -s <serial> get-state` say "device"? (A phone that does not answer in a few seconds counts as offline.)"""
        if name not in self.config:
            return False
        with self._lock:
            hit = self._state.get(name)
        if hit and not fresh and self._clock() - hit[0] < STATE_TTL:
            return hit[1]
        up = self._adb(name, "get-state") == "device"
        with self._lock:
            self._state[name] = (self._clock(), up)
        return up

    def has_play(self, name: str) -> bool:
        """Is the Play Store installed on it? Detected once while the phone is online (`pm list packages com.android.vending`)."""
        if name in self._play:
            return self._play[name]
        if not self.online(name):
            return False
        have = "com.android.vending" in self._adb(name, "shell", "pm", "list", "packages", "com.android.vending", timeout=8)
        self._play[name] = have
        return have
