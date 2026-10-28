# Meta Model API: models and findings

Base URL: `https://api.meta.ai/v1` (OpenAI-style chat completions, plus Responses and Messages).
Auth: `Authorization: Bearer $META_API_KEY`. Key lives in `.env` (gitignored). Never commit it.
Checked 27 Oct 2026 with the team's key: `GET /v1/models` returns all nine models below.

| Model | ID | Use here |
|---|---|---|
| Muse Wonder (preview) | `muse-wonder-1.0` | Main agent brain. Docs list computer use, tool calling, structured output, vision, audio, video. |
| Muse Spark 1.3 (recommended) | `muse-spark-1.3` | Cheaper/faster planner. Vision, tools, structured output. Audio not fully supported; use 1.2 for audio. |
| Muse Spark 1.2 / 1.1 | `muse-spark-1.2`, `muse-spark-1.1` | Fallbacks. 1.2 for audio understanding. |
| `*-contributor` variants | `muse-spark-1.3-contributor`, ... | Lower price, content may be used for training. Avoid for personal data. |
| Muse Voice Transcribe | `muse-voice-transcribe-1.0` | Speech to text: `POST /v1/asr/transcribe` or WebSocket `wss://api.meta.ai/v1/asr/realtime`. No speech synthesis. |
| Muse Image | `muse-image-1.0` | Image generation/editing. Not needed for the agent. |
| SAM 3.1 | `sam-3.1` | Segmentation via `/v1/responses`. Possible UI grounding aid. |
| Muse Spark Open / Muse Glimmer | open weights | Run on Baseten/Modal (`meta-models/muse-spark-open-1026` on Baseten) for the open-weight bonus. |

Context window is 1,048,576 tokens for Wonder and Spark. Pricing: see below.
No text-to-speech in the list, so spoken replies need another route (OS TTS on the client).

## Gotchas
- Reasoning tokens count against `max_tokens`. With `max_tokens: 20` the reply content came back `null`. Use a large limit
  (we used 1500) and `reasoning_effort: "minimal"` for fast UI steps.
- Muse Wonder is unavailable in South Korea. Muse Wonder Full is only in Muse Code, not the API.

## Spike: screenshot grounding (27 Oct 2026)
Goal "open Battery settings" on a 540x1200 downscaled Settings screenshot, JSON action out.
Both models returned valid JSON, and the taps (scaled x2) opened Battery on the emulator.

| Model | Latency | Tokens | Tap (540x1200) | Result |
|---|---|---|---|---|
| muse-spark-1.3 | 3.2 s | 1098 | (221, 950) | Opened Battery, but near the row boundary |
| muse-wonder-1.0 | 4.4 s | 972 | (270, 905) | Opened Battery, near the row center |

One run each, so not a benchmark. Plan: start with Wonder for the agent loop, compare with Spark on a few screens.

## Pricing and rate limits (dev.meta.ai/docs/pricing-rate-limits, read 27 Oct 2026)

USD per 1M tokens, standard mode:

| Model | Cached input | Input | Output |
|---|---|---|---|
| muse-wonder-1.0 | $0.20 | $4.00 | $20.00 |
| muse-spark-1.3 / 1.2 / 1.1 | $0.15 | $1.25 | $4.25 |
| muse-spark-1.3 / 1.2 -contributor | $0.002 | $0.10 | $0.20 |

Other: web search grounding $2.50 per 1,000 queries; Muse Image $0.01 per image; Muse Voice Transcribe $0.18 per hour of audio;
SAM 3.1 $2.50 per 1,000 images. Rate limits are per team: Wonder and Spark 3,000 RPM and 4M TPM; contributor Spark 150 RPM;
Muse Voice Transcribe 256 concurrent streams.

Measured agent cost: about 1.5K input tokens per step with a 540px screenshot plus the UI list, so Wonder is roughly $0.007 per step
and Spark about $0.002. The $150 credit is on the order of 20,000 Wonder steps.

## Agent loop results (27 Oct 2026, Android 14 emulator)

| Task | Model | Result | Steps |
|---|---|---|---|
| Battery percentage via Settings | muse-wonder-1.0 | Done, 100% (correct) | 5 |
| Android version + security patch (About phone) | muse-wonder-1.0 | Done, 14 and 2023-09-05, matches `getprop` | 6 |
| Same task | muse-spark-1.3 | Failed: oscillated scrolling up and down, never tapped the "Android version" row (two runs, 12 and 14 steps) | max |

Conclusion for now: use Wonder as the default agent model. Spark 1.3 at `minimal` reasoning is cheaper but loops, so it is better
suited to simple one-screen decisions or text-only work. One run per cell, not a benchmark.

## Web search grounding (28 Oct 2026)
Responses API only (`POST /v1/responses`, `tools: [{"type": "web_search", "search_context_size": "low"}]`, `include: ["web_search_call.results"]`); it is not available through Chat Completions.
The model searches and browses by itself, narrates in earlier `message` items (we keep only the last) and cites with `url_citation` annotations. Measured with muse-spark-1.3 on one question
("latest stable Python"): low context 9 s, 16.5K input tokens, 0.8K output (about $0.024 with the token price plus the $0.0025 search fee); medium 16 s and 32.8K input tokens, high would cost more.
So `MetaClient.search` defaults to low. Each bot turn may search up to three times (`web_search` action in the orchestrator).

## Speech to text (28 Oct 2026)
`POST /v1/asr/transcribe` with `multipart/form-data`: a `request` part (JSON: `mode` PUSH_TO_TALK, `model` muse-voice-transcribe-1.0, `audioEncoding` WAV) and an `audio` part (mono 16-bit PCM WAV, 16 or 24 kHz,
at most 10 minutes and 32 MB). The response has `transcript` (plus `turns` in other modes). Measured: a 5 second spoken sentence came back correctly in 3 s. Price $0.18 per hour of audio.
The chat microphone records in the browser (MediaRecorder), converts to 16 kHz mono WAV with an OfflineAudioContext, posts it to `/api/transcribe`, and puts the text in the message box for review.
Nothing is stored; audio is capped at 3 minutes in the page. The browser pane inside the Claude desktop app blocks the microphone, so use a normal browser at http://localhost:8000.
