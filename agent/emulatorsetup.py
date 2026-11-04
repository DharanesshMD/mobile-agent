"""One-click Android emulator setup: download the SDK command-line tools, install the emulator and a Play Store system image, create the AVD "agent34ps",
then restart the app so it finds the phone by itself (EmulatorBackend.available() turns true and the service wakes it).

The SDK lives where ANDROID_HOME points (default: data/android-sdk, which is the data volume in Docker) and the AVD in ANDROID_AVD_HOME (data/android-avd).
Both are saved in data/settings.env so a restart finds them. The owner must tick the SDK licence box: that is the licence acceptance, nothing is accepted for them.
The slow parts (network, the installer) are injectable so the tests need neither."""
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

log = logging.getLogger("agent.emulatorsetup")
TOOLS_BUILD = "11076708"  # Google's command-line tools build (a version the sdkmanager of this era understands)
AVD = "agent34ps"
LICENSE_URL = "https://developer.android.com/studio/terms"
APPROX_GB = 2.5


def host_os() -> str:
    return "mac" if sys.platform == "darwin" else "linux"


def abi() -> str:
    return "arm64-v8a" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def image_package() -> str:
    return f"system-images;android-34;google_apis_playstore;{abi()}"


def supported() -> bool:
    """Google ships the emulator for Mac (Intel and Apple Silicon), Windows and Linux x86_64, but not for Linux on ARM: that is what a Docker container on an Apple Silicon Mac is."""
    return not (host_os() == "linux" and abi() == "arm64-v8a")


def tools_url() -> str:
    return f"https://dl.google.com/android/repository/commandlinetools-{host_os()}-{TOOLS_BUILD}_latest.zip"


def sdk_root() -> Path:
    return Path(os.environ.get("ANDROID_HOME") or "data/android-sdk").resolve()


def avd_home() -> Path:
    return Path(os.environ.get("ANDROID_AVD_HOME") or Path.home() / ".android" / "avd")


def has_kvm() -> bool:
    return os.path.exists("/dev/kvm")


def download(url: str, dest: Path, progress: Callable[[int], None]) -> None:
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            got += len(chunk)
            if total:
                progress(int(got * 100 / total))


def run_streaming(cmd: list[str], progress: Callable[[int, str], None], stdin: str = "", env: dict | None = None) -> int:
    """Run a command, feeding stdin, reading the installer's "NN%" lines as progress."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env={**os.environ, **(env or {})})
    try:
        p.stdin.write(stdin); p.stdin.close()
    except OSError:
        pass
    tail = ""
    for line in p.stdout:
        m = re.search(r"(\d{1,3})%", line)
        text = re.sub(r"\[[= ]*\]\s*", "", line).strip()
        if text:
            tail = text
        progress(int(m.group(1)) if m else -1, text)
    return p.wait()


class EmulatorInstaller:
    def __init__(self, save: Callable[[str, str], None], restart: Callable[[], None], *, fetch=download, run=run_streaming, available: Callable[[], bool] | None = None):
        self.save, self.restart, self.fetch, self.run = save, restart, fetch, run
        self.available = available or (lambda: False)
        self.lock = threading.Lock()
        self.s = {"state": "idle", "step": "", "pct": 0, "message": "", "error": ""}

    def state(self) -> dict:
        with self.lock:
            return dict(self.s)

    def _set(self, **kw) -> None:
        with self.lock:
            self.s.update(kw)

    def start(self, accepted: bool) -> dict:
        if not supported():
            raise ValueError("Google has no Android emulator for Linux on ARM (this container on an Apple Silicon Mac). Connect a real phone, or run the app on the host.")
        if not accepted:
            raise ValueError("Tick the box to accept the Android SDK licence first.")
        with self.lock:
            if self.s["state"] in ("running", "restarting"):
                return dict(self.s)
            self.s = {"state": "running", "step": "Starting", "pct": 0, "message": "", "error": ""}
        threading.Thread(target=self._work, daemon=True, name="emulator-setup").start()
        return self.state()

    def _work(self) -> None:
        try:
            root = sdk_root()
            home = avd_home()
            root.mkdir(parents=True, exist_ok=True)
            home.mkdir(parents=True, exist_ok=True)
            env = {"ANDROID_HOME": str(root), "ANDROID_SDK_ROOT": str(root), "ANDROID_AVD_HOME": str(home)}
            sm = root / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
            if not sm.exists():
                self._set(step="Downloading the Android command-line tools", pct=0)
                z = root / "cmdline-tools.zip"
                self.fetch(tools_url(), z, lambda p: self._set(pct=p))
                self._set(step="Unpacking the command-line tools", pct=0)
                tmp = root / "cmdline-tools" / "_unpack"
                shutil.rmtree(tmp, ignore_errors=True)
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(tmp)
                dest = root / "cmdline-tools" / "latest"
                shutil.rmtree(dest, ignore_errors=True)
                (tmp / "cmdline-tools").rename(dest)
                shutil.rmtree(tmp, ignore_errors=True)
                z.unlink(missing_ok=True)
                for f in (dest / "bin").glob("*"):
                    f.chmod(0o755)
            sdkm = [str(sm), f"--sdk_root={root}"]
            self._set(step="Accepting the SDK licence you ticked", pct=0)
            if self.run([*sdkm, "--licenses"], lambda p, t: self._set(message=t), stdin="y\n" * 60, env=env) != 0:
                raise RuntimeError("the licence step failed")
            pkgs = ["emulator", image_package()] + ([] if shutil.which("adb") else ["platform-tools"])
            self._set(step=f"Installing the emulator and the Android 14 Play Store image (about {APPROX_GB} GB)", pct=0)
            def prog(p: int, t: str) -> None:
                self._set(message=t[:140], **({"pct": p} if p >= 0 else {}))
            if self.run([*sdkm, *pkgs], prog, env=env) != 0:
                raise RuntimeError("the installer could not download the emulator (check the internet connection and disk space)")
            self._set(step="Creating the phone", pct=100, message="")
            am = str(root / "cmdline-tools" / "latest" / "bin" / "avdmanager")
            if self.run([am, "create", "avd", "-n", AVD, "-k", image_package(), "-d", "pixel_6", "--force"], lambda p, t: self._set(message=t[:140]), stdin="no\n", env=env) != 0:
                raise RuntimeError("could not create the phone")
            self.save("ANDROID_HOME", str(root))
            self.save("ANDROID_AVD_HOME", str(home))
            if not self.available():
                raise RuntimeError("installed, but the app does not see the phone yet")
            self._set(state="restarting", step="Restarting the app to start the phone", pct=100, message="")
            log.info("emulator installed under %s; restarting", root)
            self.restart()
        except Exception as e:
            log.warning("emulator setup failed: %s", e)
            self._set(state="failed", error=str(e) or type(e).__name__)


def restart_app() -> None:
    """Start the same command again in this process (PID and ports are reused); the new start finds the emulator by itself."""
    import time
    time.sleep(1.5)  # let the page read "restarting" first
    os.execv(sys.executable, sys.orig_argv)  # the exact command this process was started with, so `python -m agent.service` stays a module run
