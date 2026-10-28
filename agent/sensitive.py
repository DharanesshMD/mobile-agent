"""Spot secrets in text typed into the chat, so they are never stored or shown to a model. Pattern based, no model involved."""
import re

CARD_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
AADHAAR_RE = re.compile(r"(?<![\d-])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?![\d-])")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
PASSWORD_RE = re.compile(r"\b(pass(?:word|code)?|pwd|pin)\b\s*(?:is|=|:)\s*(\S+)", re.I)
TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|[A-Za-z0-9_\-]{32,})\b")


def luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_token(s: str) -> bool:
    return bool(re.search(r"\d", s)) and bool(re.search(r"[A-Za-z]", s)) and len(set(s)) > 10


def find(text: str) -> list[tuple[str, int, int]]:
    """(kind, start, end) for each secret-looking span, longest-first and without overlaps."""
    hits: list[tuple[str, int, int]] = []
    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn(digits):
            hits.append(("card number", m.start(), m.end()))
    for m in AADHAAR_RE.finditer(text):
        hits.append(("ID number", m.start(), m.end()))
    for m in PAN_RE.finditer(text):
        hits.append(("ID number", m.start(), m.end()))
    for m in PASSWORD_RE.finditer(text):
        hits.append(("password", m.start(2), m.end(2)))
    for m in TOKEN_RE.finditer(text):
        if _is_token(m.group()):
            hits.append(("secret token", m.start(), m.end()))
    out: list[tuple[str, int, int]] = []
    for h in sorted(hits, key=lambda h: (h[1], -(h[2] - h[1]))):
        if not out or h[1] >= out[-1][2]:
            out.append(h)
    return out


def redact(text: str) -> tuple[str, list[str]]:
    """The text with each secret replaced by [kind hidden], and the distinct kinds found."""
    hits = find(text)
    for kind, s, e in reversed(hits):
        text = text[:s] + f"[{kind} hidden]" + text[e:]
    return text, sorted({k for k, _, _ in hits})
