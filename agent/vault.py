"""Encrypted vault for secrets the owner wants the agent to type (passwords, card numbers, IDs).

AES-256-GCM per record. The record id and owner are bound as associated data, so a ciphertext copied to another row fails to
decrypt. The key comes from VAULT_KEY (base64, 32 bytes); without it a key file is created next to the database (data/ is
gitignored). Values leave this module only through `reveal`, which writes an audit row without the value.
"""
import base64
import os
import secrets
import time
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.db import Database

OWNER = "owner"
KINDS = ("password", "card", "id", "note")
HIDDEN = "connector:"  # labels the connectors keep their keys under; hidden from the Vault tab and from every bot


def load_key(key_file: str | Path = "data/vault.key") -> bytes:
    """VAULT_KEY from the environment, else a key file that is created on first use with owner-only permissions."""
    env = os.environ.get("VAULT_KEY", "").strip()
    if env:
        key = base64.b64decode(env)
    else:
        path = Path(key_file)
        if path.exists():
            key = base64.b64decode(path.read_text().strip())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(base64.b64encode(key).decode())
    if len(key) != 32:
        raise ValueError("VAULT_KEY must be 32 bytes, base64 encoded")
    return key


class Vault:
    def __init__(self, db: Database, key: bytes):
        self.db, self._aes = db, AESGCM(key)
        self.on_change = lambda: None  # the app hooks this to refresh open Vault tabs

    @staticmethod
    def _aad(sid: str) -> bytes:
        return f"{sid}:{OWNER}".encode()

    def put(self, label: str, value: str, kind: str = "password", internal: bool = False) -> dict:
        label, kind = label.strip(), kind if kind in KINDS else "note"
        if not label or not value:
            raise ValueError("label and value are required")
        if label.lower().startswith(HIDDEN) != internal:
            raise ValueError(f"names starting with '{HIDDEN}' are reserved for connectors")
        if self.db._one("select id from vault where lower(label)=lower(?)", (label,)):
            raise ValueError(f"a secret named '{label}' already exists")
        sid, nonce = uuid.uuid4().hex[:10], secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, value.encode(), self._aad(sid))
        self.db._x("insert into vault values(?,?,?,?,?,?,?)", (sid, OWNER, label[:60], kind, nonce, ct, time.time()))
        self.on_change()
        return self.get(sid)

    def get(self, sid: str) -> dict | None:
        return self.db._one("select id,label,kind,created from vault where id=?", (sid,))

    def find(self, label: str) -> dict | None:
        if label.strip().lower().startswith(HIDDEN):
            return None  # connector keys are not part of what the owner or a bot sees in the Vault
        return self.db._one("select id,label,kind,created from vault where lower(label)=lower(?)", (label.strip(),))

    def entries(self) -> list[dict]:
        return self.db._q("select id,label,kind,created from vault where lower(label) not like ? order by label", (HIDDEN + "%",))

    def drop_internal(self, label: str) -> None:
        """Remove a connector's key (the owner disconnected it or reconnected with a new one)."""
        with self.db.lock:
            self.db.conn.execute("delete from vault where lower(label)=lower(?) and lower(label) like ?", (label.strip(), HIDDEN + "%"))
            self.db.conn.commit()

    def labels(self) -> list[str]:
        return [s["label"] for s in self.entries()]

    def delete(self, sid: str) -> bool:
        with self.db.lock:
            n = self.db.conn.execute("delete from vault where id=?", (sid,)).rowcount
            self.db.conn.commit()
        self.on_change()
        return n > 0

    def erase_all(self) -> int:
        """Delete every secret and the audit trail. Used when the owner wipes everything with the last bot."""
        with self.db.lock:
            n = self.db.conn.execute("delete from vault").rowcount
            self.db.conn.execute("delete from vault_audit")
            self.db.conn.commit()
        self.on_change()
        return n

    def reveal(self, label: str, job_id: str | None = None, app: str = "", internal: bool = False) -> str:
        """Decrypt for use. Writes an audit row (label, job, app, time) and never the value."""
        if label.strip().lower().startswith(HIDDEN) and not internal:
            raise KeyError(label)  # a phone job can never ask for a connector's key
        row = self.db._one("select * from vault where lower(label)=lower(?)", (label.strip(),))
        if not row:
            raise KeyError(label)
        try:
            value = self._aes.decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(row["id"])).decode()
        except InvalidTag:
            raise ValueError("secret could not be decrypted (wrong key or tampered data)") from None
        self.db._x("insert into vault_audit(secret_id,label,job_id,app,at) values(?,?,?,?,?)",
                   (row["id"], row["label"], job_id, app[:80], time.time()))
        self.on_change()
        return value

    def audit(self, limit: int = 50) -> list[dict]:
        return self.db._q("select label,job_id,app,at from vault_audit order by id desc limit ?", (limit,))


class SecretUse:
    """What one phone job may do with the vault: list labels, and type a value after the owner approved it."""

    def __init__(self, vault: Vault, job_id: str | None):
        self.vault, self.job_id = vault, job_id

    def labels(self) -> list[str]:
        return self.vault.labels()

    def reveal(self, label: str, app: str = "") -> str:
        return self.vault.reveal(label, self.job_id, app)
