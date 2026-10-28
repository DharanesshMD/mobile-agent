"""Meta Model API client (OpenAI-style chat completions with image input)."""
import base64
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class ModelError(RuntimeError):
    pass


class MetaClient:
    def __init__(self, model: str = "muse-wonder-1.0", effort: str = "minimal",
                 api_key: str | None = None, base: str | None = None):
        self.model = model
        self.effort = effort
        self.api_key = api_key or os.environ.get("META_API_KEY", "")
        self.base = (base or os.environ.get("META_API_BASE") or "https://api.meta.ai/v1").rstrip("/")
        self.tokens = {"prompt": 0, "completion": 0, "cached": 0}
        if not self.api_key:
            raise ModelError("META_API_KEY is not set (put it in .env)")

    def complete(self, system: str, user_text: str, image_png: bytes | None = None, max_tokens: int = 2000) -> str:
        content: list[dict] = [{"type": "text", "text": user_text}]
        if image_png:
            url = "data:image/png;base64," + base64.b64encode(image_png).decode()
            content.append({"type": "image_url", "image_url": {"url": url}})
        return self.chat(system, [{"role": "user", "content": content}], max_tokens)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(f"{self.base}{path}", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                return json.load(urllib.request.urlopen(req, timeout=120))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ModelError(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from e
            except (OSError, http.client.HTTPException, json.JSONDecodeError) as e:  # dropped connection, timeout, bad body
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ModelError(str(e)) from e
        raise ModelError("unreachable")

    def _count(self, usage: dict) -> None:
        self.tokens["prompt"] += usage.get("prompt_tokens", usage.get("input_tokens", 0))
        self.tokens["completion"] += usage.get("completion_tokens", usage.get("output_tokens", 0))
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        self.tokens["cached"] += details.get("cached_tokens", 0)

    def chat(self, system: str, messages: list[dict], max_tokens: int = 2000) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "reasoning_effort": self.effort,
                "messages": [{"role": "system", "content": system}, *messages]}
        data = self._post("/chat/completions", body)
        self._count(data.get("usage", {}))
        return data["choices"][0]["message"].get("content") or ""

    def search(self, query: str, size: str = "low") -> dict:
        """Web search grounding (Responses API only; $2.50 per 1,000 queries). Returns {"text", "sources": [{"title", "url", "snippet"}]}:
        the grounded answer, then the pages it cited followed by other pages it considered."""
        data = self._post("/responses", {"model": self.model, "input": query, "include": ["web_search_call.results"],
                                         "tools": [{"type": "web_search", "search_context_size": size}]})
        self._count(data.get("usage", {}))
        texts, cited, seen = [], [], []
        for item in data.get("output", []):
            if item.get("type") == "message":
                texts.append("")
                for block in item.get("content", []):
                    texts[-1] += block.get("text", "")  # the model narrates its searching in earlier messages; only the last one is the answer
                    cited += [{"title": a.get("title", ""), "url": a["url"], "snippet": ""} for a in block.get("annotations", []) if a.get("url")]
            elif item.get("type") == "web_search_call":
                seen += [{"title": r.get("title", ""), "url": r["url"], "snippet": r.get("snippet", "")} for r in item.get("results", []) if r.get("url")]
        by_url: dict[str, dict] = {}
        for src in cited + seen:
            by_url.setdefault(src["url"], src)
        snippets = {r["url"]: r["snippet"] for r in seen}
        return {"text": (texts[-1] if texts else "").strip(), "sources": [{**v, "snippet": v["snippet"] or snippets.get(v["url"], "")} for v in by_url.values()][:6]}

    def transcribe(self, wav: bytes) -> str:
        """Speech to text with Muse Voice Transcribe (`POST /v1/asr/transcribe`, multipart). The audio must be a mono 16-bit PCM WAV at 16 or 24 kHz,
        at most 10 minutes and 32 MB; push-to-talk mode returns one transcript for the whole clip."""
        import httpx
        settings = json.dumps({"mode": "PUSH_TO_TALK", "model": self.model, "audioEncoding": "WAV"})
        for attempt in range(3):
            try:
                r = httpx.post(f"{self.base}/asr/transcribe", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=120,
                               files={"request": (None, settings, "application/json"), "audio": ("speech.wav", wav, "audio/wav")})
            except httpx.HTTPError as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ModelError(str(e)) from e
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise ModelError(f"HTTP {r.status_code}: {r.text[:300]}")
            return str(r.json().get("transcript", "")).strip()
        raise ModelError("unreachable")
