# Open-weight orchestrator: Muse Spark Open on Baseten (2 Nov 2026)

Goal: be eligible for the "Best Build on Meta Open Weight Models" bonus ($10K, open weights served on partner inference) by running the orchestrator (chat, memory,
routing, planning) on Muse Spark Open through Baseten, behind one switch. The phone worker stays Muse Wonder and speech to text stays Muse Voice Transcribe, both on Meta's API.

**Status: code done and tested, live run BLOCKED: there is no Baseten API key in `.env` (only `META_API_KEY`).** The go/no-go needs the key. Decision rule: **GO if at least 9 of 10
decisions are valid JSON and the right action** on `scripts/openweights_check.py`.

> **Model id (2 Nov):** the hackathon's partner-credit notes say to use `meta-models/muse-spark-open-1026` on Baseten, not the id in Baseten's own docs (`meta-models/muse-spark-open`). The default is now `-1026`; if Baseten answers 404 for it, set `OPEN_WEIGHT_MODEL` in `.env` to the other id. Partner credits: Baseten $100 + 2M TPM, per person, reviewed by use case; request link is in section 6 of https://cerebralvalley.ai/e/meta-ai-developer-hackathon/details. Modal ($100, first 1,000) is the fallback host for the open weights.

## What was verified (2 Nov 2026)
- Baseten's changelog: model id `meta-models/muse-spark-open-1026`, served by Baseten Model APIs, OpenAI-compatible, Bearer key; it "accepts text, image, audio, and video inputs".
  Source: https://www.baseten.co/resources/changelog/muse-spark-open-and-nemotron-35-super-vl-available-on-baseten/
- Baseten's Chat Completions reference: base URL `https://inference.baseten.co/v1`, `POST /v1/chat/completions`, `Authorization: Bearer $BASETEN_API_KEY`, `max_tokens` up to 262144,
  `response_format` (text, json_object, json_schema), `tools`, `image_url` parts (URL or base64 data URL). 429 on rate limit (headers `x-ratelimit-*`). Source: https://docs.baseten.co/reference/inference-api/chat-completions.md
- A request without a key to that URL returns `HTTP 401 {"error":"Authentication required."}`: the endpoint is live and the model route is the one we call.
- Not stated anywhere I could read: pricing for this model, context length, whether this model supports `response_format` and tool calls. Our orchestrator asks for JSON in the prompt and parses it
  (it does not use tool calls), so it needs neither; `OPEN_WEIGHT_JSON_MODE=on` is available but off until the check shows it works for this model.

## What the owner must do (about 15 minutes)
1. Create a Baseten account (https://www.baseten.co) and redeem the hackathon's partner credits using the hackathon's own instructions (I could not see them).
2. In Baseten, open Model APIs, find **Muse Spark Open** (`meta-models/muse-spark-open-1026`) and make sure it is enabled for the workspace (accept Meta's licence if asked).
3. Create an API key (Settings, API keys) and put it in `.env` (never commit it):
   ```
   BASETEN_API_KEY=<the key>
   ```
4. Check the model answers (a few cents):
   ```bash
   curl -s https://inference.baseten.co/v1/chat/completions -H "Authorization: Bearer $BASETEN_API_KEY" -H "Content-Type: application/json" \
     -d '{"model":"meta-models/muse-spark-open-1026","messages":[{"role":"user","content":"Reply with the word ok"}],"max_tokens":20}'
   ```
5. Run the comparison (10 real orchestrator decisions on each backend; about $0.06 on Meta, a few cents on Baseten):
   ```bash
   .venv/bin/python scripts/openweights_check.py --runs 1
   ```
   It writes `data/openweights-check.json` and prints valid/correct counts, median seconds and tokens per backend. Paste me the output, or tell me the key is in place and I run it.
6. If GO: add `ORCHESTRATOR_BACKEND=baseten` to `.env` and restart the service (one copy, from the launch config; not while a job is running). The service prints
   `Orchestrator on open weights: meta-models/muse-spark-open-1026 via https://inference.baseten.co/v1 (worker stays Muse Wonder)` at start-up.

## Settings (`.env`, all optional except the key)
| Name | Default | Meaning |
|---|---|---|
| `ORCHESTRATOR_BACKEND` | `meta` | `meta` = Muse Spark 1.3 on Meta's API, `baseten` = Muse Spark Open |
| `BASETEN_API_KEY` | none | required for `baseten`; the service refuses to start without it |
| `OPEN_WEIGHT_MODEL` | `meta-models/muse-spark-open-1026` | model id on Baseten |
| `OPEN_WEIGHT_BASE_URL` | `https://inference.baseten.co/v1` | a dedicated deployment's `.../sync/v1` URL also works |
| `OPEN_WEIGHT_VISION` | `on` | `off` = pictures are replaced by a note ("this backend cannot read pictures") |
| `OPEN_WEIGHT_VISION_FALLBACK` | empty | `meta` = a message that carries a picture goes to Muse Spark 1.3 instead (only for pictures, and only if the owner allows it) |
| `OPEN_WEIGHT_JSON_MODE` | `off` | `on` sends `response_format: json_object` |

What runs where on the open backend: chat, memory, routing, planning, the "other ways" comparison: **Muse Spark Open (Baseten)**. Web search grounding (a Meta API tool) goes to Muse Spark 1.3.
The phone worker is Muse Wonder 1.0 and the microphone is Muse Voice Transcribe 1.0, both Meta API. Say exactly this in the README and the form: the bonus claim covers the orchestrator only.
Check the official rules whether a partly open-weight build qualifies.

## Code
- `agent/openweights.py`: `OpenWeightClient` (a subclass of `MetaClient`: same `chat()`/`complete()`, same retries for 429/5xx/network errors, token counting; no Meta-only `reasoning_effort`; strips `<think>` blocks),
  `make_model_factory()` (returns the open client only for the orchestrator model name; the service uses it), `strip_images`/`has_images`.
- `agent/config.py`: `OPEN_WEIGHT_MODEL`, `OPEN_WEIGHT_BASE_URL`. `agent/service.py`: one line uses the factory. `.env.example`: all settings.
- `scripts/openweights_check.py`: 10 decisions (answer, phone task, phone check, plan, memory, routine, delegate to Phone 2, approval before texting, request a secret, refuse to play a
  blocked game) on a temporary database with fake phones; `--dry-run` proves the harness without calling any model.
- `tests/test_openweights.py`: 16 tests (request shape and Bearer key, retries, token counting, JSON mode, `<think>` removal, missing key, picture handling and fallback, factory, search/transcribe stay on Meta).

## Numbers
| Backend | Valid JSON | Correct action | Median seconds | Tokens (10 calls) | Cost |
|---|---|---|---|---|---|
| Muse Spark 1.3 (Meta API), measured 2 Nov | 10/10 | 10/10 | 2.06 | 40,573 in, 1,217 out | about $0.056 ($1.25 / $4.25 per 1M) |
| Muse Spark Open (Baseten) | not run: no key | | | | pricing not published on the pages I could read |

## What to say once the numbers exist (fill the blanks from the table; do not claim more than it shows)
- Video (10 s, only on GO): "The orchestrator can run on Muse Spark Open, served by Baseten. Same agent, open weights: <n>/10 correct decisions, <x> s median." Show the start-up line and one chat turn.
- README: a section "Open-weight mode" with the switch, the table above, and the honest split (orchestrator open, worker Muse Wonder).
- On NO-GO: keep the code and this page, write "open-weight mode is implemented and tested, but did not reach 9/10 on our decision check (<n>/10), so it is off by default", and do not claim the bonus.
