"""Hide the phones' account addresses in what the app stores and shows.

A job result, a library title, a status line or a chat note can quote the Google address that is signed in on a phone (a contacts task reads it off the screen).
`AccountMasker` replaces those addresses with `d***@gmail.com`. It only knows the addresses of the accounts that really are on the phones (and the linked cloud
account), so an email address the owner asked for (a contact's) is left alone.

Limit: this is text only. A screenshot of a phone screen can still show the address, so check the pictures before they are shown or published.
"""
import re
import threading
import time

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
REFRESH_S = 120


def mask_email(address: str) -> str:
    """d***@gmail.com: the first letter and the domain stay, so the owner still recognises the account."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}" if name else address


class AccountMasker:
    """`provider` returns the addresses to hide (called in a background thread, at most once per REFRESH_S, never while a phone is asleep: the provider decides)."""

    def __init__(self, provider=None, clock=time.time):
        self.provider, self.clock = provider, clock
        self.known: set[str] = set()
        self._at = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def add(self, *addresses: str) -> None:
        with self._lock:
            self.known |= {a.strip().lower() for a in addresses if a and "@" in a}

    def _refresh(self) -> None:
        try:
            self.add(*(self.provider() or []))
        except Exception:  # a phone that does not answer must never break saving a message
            pass
        finally:
            self._busy = False

    def apply(self, text: str) -> str:
        if not text or "@" not in text:
            return text
        if self.provider and not self._busy and self.clock() - self._at > REFRESH_S:
            self._busy, self._at = True, self.clock()
            threading.Thread(target=self._refresh, daemon=True, name="mask-refresh").start()
        if not self.known:
            return text
        return EMAIL_RE.sub(lambda m: mask_email(m.group(0)) if m.group(0).lower() in self.known else m.group(0), text)
