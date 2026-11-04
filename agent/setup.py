"""First-run setup from the page: save the model API key and (in Docker) the computer's Wi-Fi address, and say what is ready.

Values the owner types here are kept in data/settings.env (mode 600, inside the data volume), applied to this process at once and loaded again on every start.
A key is checked against the model API before it is saved, and is never logged or sent back."""
import ipaddress
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException

from agent.logs import audit

log = logging.getLogger("agent.setup")
ALLOWED = {"META_API_KEY", "PHONE_GATEWAY_ADVERTISE_IP", "ANDROID_HOME", "ANDROID_AVD_HOME",
           "ANDROID_SERIAL", "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT", "ANDROID_ADB_SERVER_HOST"}
SOURCE: dict[str, str] = {}  # which names came from the page ("page") rather than the environment


def _read(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ALLOWED and v.strip():
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _write(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    os.replace(tmp, path)


def load_saved(path: str | Path = "data/settings.env") -> None:
    """Apply what the owner saved from the page (it wins over the environment: it is the newer choice)."""
    for k, v in _read(Path(path)).items():
        os.environ[k] = v
        SOURCE[k] = "page"


def _save(path: Path, name: str, value: str) -> None:
    vals = _read(path)
    vals[name] = value
    _write(path, vals)
    os.environ[name] = value
    SOURCE[name] = "page"


def check_key(key: str) -> str | None:
    """None when the model API accepts the key, else a sentence for the owner."""
    base = (os.environ.get("META_API_BASE") or "https://api.meta.ai/v1").rstrip("/")
    req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
    try:
        urllib.request.urlopen(req, timeout=12).close()
        return None
    except urllib.error.HTTPError as e:
        return "The model API did not accept this key." if e.code in (401, 403) else f"The model API answered {e.code}; try again."
    except Exception:
        return "Could not reach the model API from here. Check the internet connection of this computer."


def in_docker() -> bool:
    return os.environ.get("MOBILE_AGENT_DOCKER") == "1"


def register_setup(app: FastAPI, auth, data_dir: str | Path, gateway, phones, installer=None, host_emulator=None) -> None:
    path = Path(data_dir) / "settings.env"
    from agent import emulatorsetup as es
    if installer is None:
        from agent.lifecycle import EmulatorBackend
        installer = es.EmulatorInstaller(lambda k, v: _save(path, k, v), es.restart_app, available=EmulatorBackend.available)
    app.state.emulator_installer = installer
    if host_emulator is None and in_docker():
        from agent.hostemulator import HostEmulator
        host_emulator = HostEmulator(lambda k, v: _save(path, k, v), es.restart_app)
        host_emulator.start()
    app.state.host_emulator = host_emulator

    @app.get("/api/setup", dependencies=[Depends(auth)])
    def setup_state():
        key = bool(os.environ.get("META_API_KEY"))
        gw = gateway.state()
        ip = os.environ.get("PHONE_GATEWAY_ADVERTISE_IP", "")
        docker = in_docker()
        return {"docker": docker,
                "key": {"set": key, "source": SOURCE.get("META_API_KEY", "environment" if key else "")},
                "phone": {"needs_address": docker and not ip, "address": ip, "apk": bool(gw["apk"]["present"]),
                          "connected": len(phones.listing()), "gateway_address": gw["address"]},
                "emulator": {"available": installer.available(), "supported": es.supported(), "docker": docker, "kvm": es.has_kvm(), "gb": es.APPROX_GB, "license_url": es.LICENSE_URL, "host": host_emulator.state() if host_emulator else None,
                             "install": installer.state()}}

    @app.post("/api/setup/key", dependencies=[Depends(auth)])
    def save_key(body: dict = Body(...)):
        key = str(body.get("key", "")).strip()
        if not key or len(key) > 400 or any(c.isspace() for c in key):
            raise HTTPException(400, "Paste the key as one line.")
        problem = check_key(key)
        if problem:
            raise HTTPException(400, problem)
        _save(path, "META_API_KEY", key)
        audit("model api key saved from the setup page")
        return {"ok": True}

    @app.post("/api/setup/emulator", dependencies=[Depends(auth)])
    def install_emulator(body: dict = Body(...)):
        try:
            audit("emulator install started from the setup page")
            return installer.start(body.get("accept") is True)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/setup/address", dependencies=[Depends(auth)])
    def save_address(body: dict = Body(...)):
        raw = str(body.get("ip", "")).strip()
        try:
            a = ipaddress.ip_address(raw)
        except ValueError:
            raise HTTPException(400, "That is not an IP address. It looks like 192.168.1.20.")
        if not a.is_private or a.is_loopback or a.is_link_local:
            raise HTTPException(400, "Use the address of this computer on your Wi-Fi (it starts with 192.168, 10. or 172.16 to 31).")
        _save(path, "PHONE_GATEWAY_ADVERTISE_IP", raw)
        audit("phone gateway address saved from the setup page")
        return {"ok": True}
