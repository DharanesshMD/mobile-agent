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

    def chat(self, system: str, messages: list[dict], max_tokens: int = 2000) -> str:
        body = {"model": self.model, "max_tokens": max_tokens, "reasoning_effort": self.effort,
                "messages": [{"role": "system", "content": system}, *messages]}
        req = urllib.request.Request(f"{self.base}/chat/completions", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                data = json.load(urllib.request.urlopen(req, timeout=120))
                break
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
        usage = data.get("usage", {})
        self.tokens["prompt"] += usage.get("prompt_tokens", 0)
        self.tokens["completion"] += usage.get("completion_tokens", 0)
        self.tokens["cached"] += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return data["choices"][0]["message"].get("content") or ""
