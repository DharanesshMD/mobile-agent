"""Small JSON-file store for paired devices and the cloud-phone Google account. Tokens are kept only as hashes."""
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.data = {"devices": {}, "cloud_account": None, "consent": None}
        if self.path and self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def add_device(self, name: str) -> tuple[str, str]:
        token = "dev_" + secrets.token_urlsafe(32)
        did = uuid.uuid4().hex[:10]
        with self._lock:
            self.data["devices"][did] = {"id": did, "name": name[:60], "hash": _hash(token),
                                         "created": int(time.time()), "last_seen": None}
            self._save()
        return did, token

    def find_device(self, token: str) -> dict | None:
        h = _hash(token)
        with self._lock:
            for d in self.data["devices"].values():
                if secrets.compare_digest(d["hash"], h):
                    if not d["last_seen"] or time.time() - d["last_seen"] > 60:
                        d["last_seen"] = int(time.time())
                        self._save()
                    return {"id": d["id"], "name": d["name"]}
        return None

    def devices(self) -> list[dict]:
        with self._lock:
            return [{k: v for k, v in d.items() if k != "hash"} for d in self.data["devices"].values()]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            gone = self.data["devices"].pop(device_id, None) is not None
            if gone:
                self._save()
            return gone

    def cloud_account(self) -> dict | None:
        return self.data["cloud_account"]

    def set_cloud_account(self, email: str | None) -> None:
        with self._lock:
            cur = self.data["cloud_account"]
            if email and (not cur or cur["email"] != email):
                self.data["cloud_account"] = {"email": email, "linked_at": int(time.time())}
            elif not email:
                self.data["cloud_account"] = None
            else:
                return
            self._save()

    def record_consent(self, email: str | None) -> None:
        with self._lock:
            self.data["consent"] = {"at": int(time.time()), "email_hint": email}
            self._save()

    def consent(self) -> dict | None:
        return self.data["consent"]
