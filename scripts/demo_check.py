"""Preflight before filming: prints green / amber / red for everything the demo needs. Exit code 1 if anything is red.

    .venv/bin/python scripts/demo_check.py [--no-model]

It never prints a key or a secret. The one model call is a few tokens (skip it with --no-model).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000")
EMULATOR = os.environ.get("ANDROID_SERIAL", "emulator-5554")
GAMES = "localhost:5565"
results: list[tuple[str, str, str]] = []  # (level, name, detail)


def add(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))


def sh(*cmd: str, timeout: float = 15) -> str:
    try:
        return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def get(path: str, timeout: float = 8):
    return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))


def adb(serial: str, *args: str, timeout: float = 10) -> str:
    exe = shutil.which("adb") or str(Path(os.environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools")) / "platform-tools" / "adb")
    return sh(exe, "-s", serial, *args, timeout=timeout)


def check_service() -> dict | None:
    pids = [p for p in sh("pgrep", "-f", "--", "-m agent.service").split() if p]
    python_pids = [p for p in pids if "python3 -m agent.service" in sh("ps", "-o", "command=", "-p", p)]
    if len(python_pids) == 1:
        add("green", "one service process", f"pid {python_pids[0]}")
    else:
        add("red", "one service process", f"{len(python_pids)} found (need exactly 1)")
    try:
        bots = get("/api/bots")
        add("green", "service answers", f"{BASE} ({len(bots)} bots)")
        return {"bots": bots}
    except Exception as e:
        add("red", "service answers", f"{BASE}: {type(e).__name__}")
        return None


def check_demo_state(bots: list[dict]) -> None:
    names = {b["name"] for b in bots}
    want = {"Master", "Home assistant", "Phone 2"}
    add("green" if want <= names else "red", "demo bots", ", ".join(sorted(names)) if want <= names else f"missing {', '.join(sorted(want - names))}")
    add("green" if "My phone" in names else "amber", "real-phone bot", "My phone" if "My phone" in names else "none (fine unless a paired phone is filmed)")
    try:
        home = next(b for b in bots if b["name"] == "Home assistant")
        routines = get(f"/api/bots/{home['id']}/routines")
        add("green" if any(r["name"] == "Morning brief" for r in routines) else "red", "Morning brief routine", f"{len(routines)} routine(s)")
        vault = get("/api/vault")
        add("green" if any(v["label"] == "Demo login" for v in vault) else "red", "vault entry 'Demo login'", f"{len(vault)} secret(s)")
        library = get(f"/api/bots/{home['id']}/library")
        add("green" if not library else "amber", "library empty", f"{len(library)} item(s)")
    except Exception as e:
        add("red", "demo seed", f"{type(e).__name__}: {str(e)[:80]}")
    for b in bots:
        if b["kind"] == "master":
            alerts = [m for m in get(f"/api/bots/{b['id']}/state")["messages"] if m["kind"] == "alert"]
            add("green" if not alerts else "amber", "no stale alerts in Master", f"{len(alerts)} alert(s)")


def check_model(skip: bool) -> None:
    key = ""
    env = ROOT / ".env"
    if env.exists():
        m = re.search(r"^META_API_KEY=(.+)$", env.read_text(), re.M)
        key = (m.group(1).strip() if m else "")
    key = key or os.environ.get("META_API_KEY", "")
    if len(key) < 20:
        add("red", "API key present", "META_API_KEY is missing or too short")
        return
    add("green", "API key present", f"{len(key)} characters (not shown)")
    if skip:
        add("amber", "models reachable", "skipped (--no-model)")
        return
    try:
        from agent.model import MetaClient
        out = MetaClient("muse-spark-1.3", api_key=key).chat("Reply with one word.", [{"role": "user", "content": "Say ready."}], max_tokens=300)
        add("green" if out.strip() else "amber", "models reachable", "muse-spark-1.3 answered")
    except Exception as e:
        add("red", "models reachable", f"{type(e).__name__}: {str(e)[:100]}")


def check_phones() -> None:
    try:
        s = get("/api/mobile/state")
        power = s["phone"]["power"]
        add("green" if power == "on" else "amber", "emulator", f"{power}" + ("" if power == "on" else " (it wakes in seconds when a task starts)"))
    except Exception as e:
        add("red", "Phone state", type(e).__name__)
    try:
        g = get("/api/mobile/state?phone=games")
        power = g["phone"]["power"]
        add("green" if power == "on" else "amber", "Phone 2", f"{power}" + ("" if power == "on" else " (starts in about 25 s when needed)"))
    except Exception:
        add("amber", "Phone 2", "no state (not set up?)")
    if adb(EMULATOR, "get-state").strip() == "device":
        pk = adb(EMULATOR, "shell", "pm", "list", "packages")
        for label, pkg in (("Chrome", "com.android.chrome"), ("Calendar", "com.google.android.calendar"), ("Clock", "com.google.android.deskclock")):
            add("green" if f"package:{pkg}" in pk else "red", f"Phone app: {label}")
        tz = adb(EMULATOR, "shell", "getprop", "persist.sys.timezone").strip()
        from agent.device import local_timezone
        mac = local_timezone()
        add("green" if tz == mac else "amber", "time zone", f"Phone {tz or '?'} / Mac {mac or '?'}")
        size = adb(EMULATOR, "shell", "wm", "size")
        add("green" if "720x1600" in size else "amber", "screen profile", size.replace("\n", " ").strip()[:60])
    else:
        add("amber", "Phone adb", "not reachable right now (asleep): app checks skipped")
    if adb(GAMES, "get-state", timeout=5).strip() == "device":
        pk = adb(GAMES, "shell", "pm", "list", "packages")
        for label, pkg in (("Shattered Pixel Dungeon", "com.shatteredpixel.shatteredpixeldungeon"), ("Clash of Clans", "com.supercell.clashofclans")):
            add("green" if f"package:{pkg}" in pk else "red", f"Phone 2 app: {label}")
    else:
        add("amber", "Phone 2 apps", "phone is off: app check skipped")


def check_machine() -> None:
    pressure = sh("memory_pressure")  # "System-wide memory free percentage: 38%": what the Mac can still give out right now
    m_free = re.search(r"free percentage:\s*(\d+)%", pressure)
    free_pct = int(m_free.group(1)) if m_free else None
    if free_pct is not None:
        add("green" if free_pct >= 30 else "amber" if free_pct >= 15 else "red", "memory pressure", f"{free_pct}% free" + ("" if free_pct >= 30 else ": close other apps or reboot before filming"))
    swap = sh("sysctl", "-n", "vm.swapusage")  # total = 5120.00M  used = 4826.00M  free = 294.00M (macOS keeps old swap until a reboot, so this alone is only a warning)
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", swap)
    if m:
        total, used = float(m.group(1)), float(m.group(2))
        pct = round(100 * used / total) if total else 0
        add("green" if pct < 60 else "amber" if pct < 85 or (free_pct or 0) >= 30 else "red", "swap", f"{used:.0f} of {total:.0f} MB used ({pct}%)" + ("" if pct < 60 else ": a reboot before filming clears it"))
    du = shutil.disk_usage("/")
    gb = du.free / 1e9
    add("green" if gb > 20 else "amber" if gb > 8 else "red", "disk", f"{gb:.0f} GB free")
    from agent.device import local_timezone
    add("green" if local_timezone() else "amber", "Mac time zone", local_timezone() or "unknown")
    bs = sh("pgrep", "-f", "BlueStacks.app").split()
    add("green" if not bs else "red", "BlueStacks not running", "" if not bs else "it opens an adb port to the whole network and eats memory: quit it")
    listen = sh("lsof", "-nP", "-iTCP:5555", "-sTCP:LISTEN")
    exposed = [ln for ln in listen.splitlines()[1:] if "127.0.0.1" not in ln and "[::1]" not in ln]
    add("green" if not exposed else "red", "port 5555 only on loopback", "" if not exposed else "something listens on every interface")
    up = ROOT / "data" / "uploads"
    left = [p for p in up.glob("*") if p.is_file()] if up.exists() else []
    add("green" if not left else "amber", "no pictures left from rehearsals", f"{len(left)} file(s)")
    runs = ROOT / "data" / "runs"
    add("green" if not runs.exists() or not any(runs.iterdir()) else "amber", "no old run traces", "")


def main() -> None:
    state = check_service()
    if state:
        check_demo_state(state["bots"])
    check_model("--no-model" in sys.argv)
    check_phones()
    check_machine()
    icon = {"green": "\033[32mGREEN\033[0m", "amber": "\033[33mAMBER\033[0m", "red": "\033[31mRED  \033[0m"}
    for level, name, detail in results:
        print(f"{icon[level]}  {name}" + (f": {detail}" if detail else ""))
    reds = [r for r in results if r[0] == "red"]
    print(f"\n{len([r for r in results if r[0] == 'green'])} green, {len([r for r in results if r[0] == 'amber'])} amber, {len(reds)} red")
    sys.exit(1 if reds else 0)


if __name__ == "__main__":
    main()
