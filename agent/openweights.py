"""Open-weight orchestrator backend: Muse Spark Open on Baseten Model APIs (OpenAI-compatible Chat Completions).

`ORCHESTRATOR_BACKEND=baseten` runs the chat, memory, routing and planning model on open weights; the phone worker stays Muse Wonder and speech to text stays
Muse Voice Transcribe on Meta's API. The client has the same `chat()` / `complete()` interface and the same retry behaviour as `MetaClient` (it is a subclass).
Web search is a Meta API tool, not part of an open model, so `search()` goes to Muse Spark 1.3 (the "web options" card), and pictures follow `OPEN_WEIGHT_VISION*`.
"""
import os
import re
import time

from agent.config import OPEN_WEIGHT_BASE_URL, OPEN_WEIGHT_MODEL, ORCHESTRATOR_MODEL
from agent.model import MetaClient, ModelError

NO_PICTURES = "[a picture was attached, but this model backend is not set up to read pictures, so say so instead of guessing what it shows]"
THINK = re.compile(r"<think>.*?</think>", re.S)  # some open reasoning models print their thinking before the answer


def has_images(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) and any(b.get("type") == "image_url" for b in m["content"]) for m in messages)


def strip_images(messages: list[dict]) -> list[dict]:
    """The same messages with every picture replaced by a short note (a model that cannot read pictures must not be sent them)."""
    out = []
    for m in messages:
        if isinstance(m.get("content"), list):
            parts = [b["text"] if b.get("type") == "text" else NO_PICTURES for b in m["content"]]
            m = {**m, "content": "\n".join(p for p in parts if p)}
        out.append(m)
    return out


def _on(value: str | None, default: bool) -> bool:
    return default if value in (None, "") else value.strip().lower() in ("1", "on", "true", "yes")


class OpenWeightClient(MetaClient):
    def __init__(self, model: str | None = None, api_key: str | None = None, base: str | None = None, vision: bool | None = None,
                 vision_fallback: str | None = None, json_mode: bool | None = None, env=None):
        env = os.environ if env is None else env
        self.api_key = api_key or env.get("BASETEN_API_KEY", "")
        if not self.api_key:
            raise ModelError("BASETEN_API_KEY is not set (put it in .env), or set ORCHESTRATOR_BACKEND=meta")
        self.model = model or env.get("OPEN_WEIGHT_MODEL") or OPEN_WEIGHT_MODEL
        self.base = (base or env.get("OPEN_WEIGHT_BASE_URL") or OPEN_WEIGHT_BASE_URL).rstrip("/")
        self.effort = ""
        self.vision = vision if vision is not None else _on(env.get("OPEN_WEIGHT_VISION"), True)
        self.fallback_meta = (vision_fallback if vision_fallback is not None else env.get("OPEN_WEIGHT_VISION_FALLBACK", "")).strip().lower() == "meta"
        self.json_mode = json_mode if json_mode is not None else _on(env.get("OPEN_WEIGHT_JSON_MODE"), False)
        self.tokens = {"prompt": 0, "completion": 0, "cached": 0}
        self.calls, self.seconds = 0, 0.0
        self._meta: MetaClient | None = None

    def _meta_client(self) -> MetaClient:
        if self._meta is None:
            self._meta = MetaClient(ORCHESTRATOR_MODEL)  # Muse Spark 1.3 on Meta's API: pictures when the open model cannot read them, and web search
        return self._meta

    def chat(self, system: str, messages: list[dict], max_tokens: int = 2000) -> str:
        pictures = has_images(messages)
        if pictures and not self.vision:
            if self.fallback_meta:
                return self._meta_client().chat(system, messages, max_tokens)
            messages = strip_images(messages)
        body = {"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "system", "content": system}, *messages]}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.time()
        try:
            data = self._post("/chat/completions", body)
        except ModelError as e:
            if pictures and "HTTP 400" in str(e) and self.fallback_meta:  # the open model refused the picture: Spark 1.3 reads it
                return self._meta_client().chat(system, messages, max_tokens)
            raise
        self.calls += 1
        self.seconds += time.time() - t0
        self._count(data.get("usage", {}))
        text = data["choices"][0]["message"].get("content") or ""
        return THINK.sub("", text).strip()

    def search(self, query: str, size: str = "low") -> dict:
        return self._meta_client().search(query, size)  # web search grounding is a Meta API tool

    def transcribe(self, wav: bytes) -> str:
        raise ModelError("speech to text runs on Muse Voice Transcribe (Meta's API), not on the open-weight backend")


def make_model_factory(env=None):
    """`factory(model_name) -> client`. Meta's API for everything, except the orchestrator model name when ORCHESTRATOR_BACKEND=baseten."""
    env = os.environ if env is None else env
    backend = (env.get("ORCHESTRATOR_BACKEND") or "meta").strip().lower()
    if backend not in ("meta", "baseten"):
        raise ModelError(f"ORCHESTRATOR_BACKEND must be meta or baseten, not {backend!r}")
    if backend == "meta":
        return lambda name: MetaClient(name)
    OpenWeightClient(env=env)  # fail at start-up, not on the first chat, when the key is missing
    print(f"Orchestrator on open weights: {env.get('OPEN_WEIGHT_MODEL') or OPEN_WEIGHT_MODEL} via {env.get('OPEN_WEIGHT_BASE_URL') or OPEN_WEIGHT_BASE_URL} "
          f"(worker stays Muse Wonder)", flush=True)
    return lambda name: OpenWeightClient(env=env) if name == ORCHESTRATOR_MODEL else MetaClient(name)
