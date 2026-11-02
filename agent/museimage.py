"""Muse Image 1.0 (Meta Model API): make a picture that explains something, or edit one (annotate a phone screenshot, add numbered arrows).

POST /v1/images/generations {"model","prompt","n","size","output_format"} and POST /v1/images/edits {"model","prompt","images":[{"image_url": data URL}],"n"}
answer {"data":[{"b64_json"}], "output_format", "usage"}. `size` only sets the aspect ratio (the pictures come back at about 1600 px), the edit endpoint has no mask and
may answer WEBP whatever was asked, so every result is decoded, shrunk and re-encoded by agent.images.normalize (PNG or JPEG, at most 1568 px, no metadata).
A blocked prompt is HTTP 400 `content_policy_violation`; a busy backend is 503 (retried). One image costs $0.01 and only a returned picture is billed.
"""
import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request

from agent.model import RETRY_DELAYS

MODEL = "muse-image-1.0"
PRICE_PER_IMAGE = 0.01
TIMEOUT_S = 180  # a picture takes about 15 s; a busy backend can take longer
MAX_PROMPT = 1500
SIZES = {"square": "1024x1024", "wide": "1792x1024", "tall": "1024x1792"}  # the aspect ratio is what counts
DEFAULT_DAILY_CAP = 20
KEEP = " Change nothing else: keep every control, label, text and the on/off state of every switch exactly as in the original picture. Only add the annotation."


class ImageRefused(Exception):
    """Muse Image's content policy blocked the request, or our own rules did. The message can be shown to the owner."""


class ImageApiError(RuntimeError):
    """The picture could not be made (network, server, or a bad request). The message is short and safe to show."""


FORGERY = re.compile(
    r"(?i)\b(fake|forged?|counterfeit|fabricat\w*|spoof\w*|mock ?up of a real)\b.{0,60}\b(passport|id card|identity|driver'?s? licen[cs]e|licen[cs]e|receipt|invoice|bank statement|"
    r"certificate|diploma|prescription|boarding pass|ticket|check|cheque|payment screenshot|transaction|chat screenshot|screenshot of a (chat|conversation|message))")


def daily_cap() -> int:
    try:
        return max(0, int(os.environ.get("MUSE_IMAGE_DAILY_CAP", DEFAULT_DAILY_CAP)))
    except ValueError:
        return DEFAULT_DAILY_CAP


def policy_problem(prompt: str) -> str | None:
    """Our own first line of defence, before the provider's: pictures that only exist to deceive (forged documents, receipts, payment or chat screenshots)."""
    if FORGERY.search(prompt):
        return "I don't make pictures of fake documents, receipts or screenshots meant to look real. I can draw a clearly labelled illustration of how such a thing is laid out instead."
    return None


def size_for(name: str | None) -> str:
    name = str(name or "").strip().lower()
    if name in SIZES:
        return SIZES[name]
    if re.fullmatch(r"\d{3,4}x\d{3,4}", name):
        w, h = map(int, name.split("x"))
        if 1 / 4 <= w / h <= 4:
            return name
    return SIZES["square"]


class MuseImage:
    def __init__(self, api_key: str | None = None, base: str | None = None, model: str = MODEL, transport=None):
        self.api_key = api_key or os.environ.get("META_API_KEY", "")
        self.base = (base or os.environ.get("META_API_BASE") or "https://api.meta.ai/v1").rstrip("/")
        self.model = model
        self.transport = transport  # tests: a function (path, body) -> dict, in place of the network
        self.last_usage: dict = {}
        if not self.api_key and transport is None:
            raise ImageApiError("META_API_KEY is not set (put it in .env)")

    def _post(self, path: str, body: dict) -> dict:
        if self.transport is not None:
            return self.transport(path, body)
        req = urllib.request.Request(f"{self.base}{path}", json.dumps(body).encode(), {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(len(RETRY_DELAYS) + 1):
            last = attempt == len(RETRY_DELAYS)
            try:
                return json.load(urllib.request.urlopen(req, timeout=TIMEOUT_S))
            except urllib.error.HTTPError as e:
                text = e.read()[:600].decode(errors="replace")
                if e.code == 400 and "content_policy" in text:
                    raise ImageRefused("Muse Image's content policy blocked that picture.") from e
                if e.code in (429, 503) and not last:  # busy: wait and ask again (500 is a failure that retrying does not fix)
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise ImageApiError(f"HTTP {e.code}: {text[:200]}") from e
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:
                if not last:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise ImageApiError(str(e)) from e
        raise ImageApiError("unreachable")

    def _picture(self, data: dict) -> bytes:
        self.last_usage = data.get("usage") or {}
        items = data.get("data") or []
        if not items or not items[0].get("b64_json"):
            raise ImageApiError("Muse Image answered without a picture.")
        try:
            return base64.b64decode(items[0]["b64_json"])
        except Exception as e:
            raise ImageApiError("Muse Image sent a picture that could not be decoded.") from e

    def generate(self, prompt: str, size: str | None = None) -> bytes:
        """A new picture from a description. Returns the picture's bytes (PNG); the caller normalizes and stores it."""
        prompt = prompt.strip()[:MAX_PROMPT]
        if not prompt:
            raise ImageRefused("There was nothing to draw.")
        return self._picture(self._post("/images/generations", {"model": self.model, "prompt": prompt, "n": 1, "size": size_for(size), "output_format": "png"}))

    def edit(self, image: bytes, instruction: str, mime: str = "image/png") -> bytes:
        """Change a picture as told (annotate it, highlight a toggle, add numbered arrows). Returns the new picture's bytes (any format: normalize it)."""
        instruction = instruction.strip()[:MAX_PROMPT]
        if not instruction:
            raise ImageRefused("There was nothing to change.")
        instruction += KEEP  # the edit model redraws the whole picture: without this it can flip a switch or reword a label
        url = f"data:{mime};base64," + base64.b64encode(image).decode()
        return self._picture(self._post("/images/edits", {"model": self.model, "prompt": instruction, "images": [{"image_url": url}], "n": 1}))
