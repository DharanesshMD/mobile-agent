# Design v2: chat-first agent with a background cloud phone

Date: 28 Oct 2026. Status: proposal, waiting for owner sign-off. Supersedes the "web chatbot beside a phone view" layout in plan.md sections 6 and 12.
Items marked (verify) have not been tested.

## 1. The flow the owner described (from the reference recording)

The reference is a desktop chat app with a bot list. What it shows, and what we take from it:

- Left sidebar: a list of bots (agents), each a conversation with a last-message preview, search, new bot, and the profile at the bottom.
- Centre: the chat. Agent messages, a status line while it works ("Clicking around" with an animated dot), messages relayed between bots
  ("Message from <other bot>"), and an inline question card with a choice button (the action item). Composer: attach, mic, voice.
- Right panel with three tabs. Details: routines (recurring tasks on a schedule). Library: artifacts the bot produced (screenshots, files) grouped by
  This week / Older. Computer: a small live thumbnail of the bot's screen with an Open button.
- Open goes full screen with a banner "You're in control" and an "I'm done" button. The owner takes the mouse, then hands control back.

What we change: the screen is an Android phone, not a desktop browser; and the cloud phone sleeps when nobody needs it.
Originality note: use this as layout inspiration only. Use our own names, assets and code (hackathon new-work rule). The recording shows personal
details (phone number, resume content); keep it and any screenshots of it out of the public repo and the demo.
I could not transcribe the audio (Whisper is not installed), so this design is based on the visuals and your written description.

## 2. How a request flows

1. The owner writes or speaks a request in a bot's chat (web or Android).
2. The orchestrator (Muse Spark 1.3) answers directly if it can: reasoning, drafting, questions, memory lookups.
3. If the task needs a phone, it calls the `computer_task` tool. That creates a background job, wakes the cloud phone if it sleeps, and returns at once.
   The chat shows "Working on it" and a live status line. The owner can keep chatting or leave.
4. The Computer Worker (Muse Wonder, the existing graph) drives the phone: observe, plan, act, verify. Only this worker uses Wonder.
5. When the worker needs something it cannot get from the screen, it pauses and sends a typed request to the orchestrator. The chat shows it as an
   action item card (section 4). The owner answers in chat; the job resumes.
6. The owner can click Computer on the right at any moment to watch live, press Open for full screen, take control, and press "I'm done" to give it back.
7. When the job ends, the orchestrator writes the result in chat, saves artifacts (key screenshots, files) to the Library, and stores a short memory.
8. After the idle timeout the phone sleeps (section 7).

## 3. Models and who does what

| Role | Model | Why |
|---|---|---|
| Orchestrator: reasoning, chat replies, tool choice, summaries | `muse-spark-1.3` | Cheaper and fast for text and planning |
| Computer Worker: screen understanding, tap, swipe, type, drag | `muse-wonder-1.0` | Listed with computer use; in our tests it completed Settings tasks that Spark 1.3 looped on |
| Speech to text (mic) | `muse-voice-transcribe-1.0` | Only Meta speech model; no TTS, so spoken replies use the client's own TTS |
| Search grounding | Spark with search grounding | Priced per 1,000 queries |
| Retrieval | none from Meta (no embeddings model in the list) | Start with SQLite full-text search (FTS5); add a local embedding model later if needed |

Rule: anything that touches the phone screen goes to Wonder. Everything else is Spark. Models are named only in one config file.

## 4. Orchestrator: tools, prompts, human in the loop

LangGraph supervisor graph, one thread per bot conversation, checkpointed in SQLite so a job or a question survives a restart.

Tools the orchestrator knows (each has a name, description and JSON schema in the system prompt):

| Tool | Purpose |
|---|---|
| `computer_task(goal, app_hint)` | Start a background phone job. Returns a job id. |
| `ask_user(kind, question, options, field)` | Create an action item. Kinds: `choice`, `confirm`, `text`, `secret`, `file`, `otp`, `handoff`. |
| `vault_list()` | Names and kinds of stored secrets only. Never values. |
| `memory_search(query)` / `memory_save(text)` | RAG over past tasks, user facts and library notes. |
| `routine_create/list/delete` | Recurring jobs (the Details tab). |
| `web_search(query)` | Grounded search. |
| `library_add(item)` | Save an artifact. |

Worker to orchestrator requests (the worker never talks to the owner directly): `ask`, `handoff`, `request_otp`, plus a new
`need_info {field, why, kind}` for a value the screen asks for that we do not have (for example a city, a salary, a file to upload).
The orchestrator first tries memory and the vault. Only if it cannot answer does it create an action item.

Action item card (in chat): a title, why it is needed, the answer control (buttons for choice/confirm, a field for text, a masked field for secret,
a file picker, a code box for otp, a "Take control" button for handoff), and the job it belongs to. Answering resumes the paused job.

Prompts live in `prompts/` as files: `orchestrator.md` (role, tools, when to delegate, when to ask, how to speak, never reveal or guess secrets, never claim a task
is done until the worker confirms), `worker.md` (the existing phone system prompt), and a per-bot persona file. A bot is a persona, a tool allow-list and a memory
namespace; all bots share the one cloud phone and queue for it (a lock with a fair queue).

## 5. Sensitive data: vault design

Threat model: the owner pastes passwords, ID numbers and card details into chat; the database or a backup leaks; or the model is shown a secret by mistake.

- Storage: SQLite table `vault(id, owner, label, kind, nonce, ciphertext, created)`. AES-256-GCM per record with a random nonce and the record id and
  owner as associated data. The key comes from the environment (`VAULT_KEY`, 32 random bytes, base64) and is never stored with the data. In the cloud, hold it in the
  host's secret manager. Losing the key means losing the secrets; document backup of the key separately.
- Entry: a masked "secret" input in the chat (and an action item of kind `secret`). It posts straight to `POST /api/vault`, not into the message log.
  The chat shows "saved as <label>" only.
- Accidents: the normal composer runs a detector (card numbers with Luhn check, Aadhaar-like 12 digits, PAN pattern, "password is …", long random tokens).
  On a match it offers "save to the vault instead" and redacts the text before it is stored or sent to any model.
- Use: the worker gets a `type_secret {ref}` action. The server decrypts and types the value over ADB. The value is never in a prompt, trace, event or log (the same
  guarantee we already test for OTP codes). The orchestrator only sees labels.
- Policy (default, owner can change): each use of a secret needs a one-tap approval card that names the secret and the app. An optional "trust this secret for this app"
  switch removes the prompt. Passwords for Google sign-in may be stored, but the first sign-in still happens by the owner through the live view.
- Deletion: the owner can delete any secret; deletion removes the row. Audit log records label, time, job and app, never the value.
- Limits: the cloud phone itself will hold the typed value in the app, and a compromised host can read memory. This protects data at rest and from the models.

## 6. Computer panel and takeover

- Thumbnail: JPEG from the phone every 2 to 3 seconds, cached on the server so many viewers do not load ADB (already built).
- Open: full-screen view at about 1 frame per second, faster while the owner holds control.
- Take control: pauses the job at the next safe point (between actions), shows "You're in control · I'm done". Taps, typing and swipes go to the phone.
  "I'm done" returns control; the worker re-observes the screen before its next action.
- Android client: the same panel as a native image view with touch mapping. No embedded browser.
- Library: key screenshots are saved when the worker finishes a milestone (a form submitted, an app installed), with a one-line caption.

## 7. Cloud phone sleep and wake

Goal: the container stays deployed, but the phone costs almost nothing when nobody is using it.

- States: `ready`, `idle`, `paused`, `stopped`, `waking`.
- Idle timer starts when no job is running and no viewer or owner activity for 5 minutes (configurable). Then `docker pause` (freezes all processes, near zero CPU,
  memory kept, resume in under a second). After 60 more minutes: `docker stop` (frees memory; `/data` volume keeps accounts and apps). Wake from stopped means
  `docker start` plus waiting for `sys.boot_completed` (tens of seconds, show "Waking the phone").
- Wake triggers: a message that needs the phone, a routine firing, the owner opening Computer or taking control, an incoming OTP request.
- Lifecycle backend is pluggable: `DockerBackend` (pause, unpause, stop, start) for the cloud host; a local emulator backend that only reports state for the Mac.
- (verify) Redroid needs a Linux kernel with binder; it is not expected to run under Docker Desktop on macOS, so the cloud host must be tested separately.
  `docker pause` on a running Android container is plausible but unverified (clocks and network timeouts after unpause may need handling).

## 8. Data model (SQLite)

`bots(id, name, persona, tools, created)`, `messages(id, bot_id, role, kind, content, job_id, created)` (kinds: text, status, action_item, result),
`jobs(id, bot_id, goal, status, kind, started, ended, result)`, `action_items(id, job_id, kind, payload, status, answer_ref)`,
`library(id, bot_id, job_id, title, path, created)`, `routines(id, bot_id, cron, goal, enabled)`, `vault(...)` as above, `memory(id, bot_id, text)` with an FTS5 index,
`devices` and `cloud_account` (moving from the JSON store), `audit(id, at, who, action, subject)`.

## 9. API additions

`GET/POST /api/bots`, `GET /api/bots/{id}/messages`, `POST /api/bots/{id}/messages`, `GET /api/bots/{id}/events` (SSE: messages, status, action items, job state),
`POST /api/action-items/{id}/answer`, `POST /api/vault` (and list/delete by label), `GET/POST /api/bots/{id}/routines`, `GET /api/bots/{id}/library`,
`POST /api/computer/control` (take) and `/release`, `GET /api/computer/state` (ready, paused, waking). Existing run, pairing and cloud-account endpoints stay.

## 10. Milestones (deadline Wed 4 Nov, 9 PM ET; about 7 days left)

1. Today and tomorrow: SQLite store, bots and messages, orchestrator graph on Spark with `computer_task` and `ask_user`, worker jobs in the background, SSE chat.
2. New web UI like the reference: sidebar of bots, chat with status lines and action cards, right panel with Details, Library and Computer, full-screen take control.
3. Vault with AES-GCM, secret input, sensitive-text detector, `type_secret`, approval cards.
4. Sleep and wake lifecycle (docker backend plus local stub), routines scheduler, library artifacts.
5. Android client: chat, action items, live view, OTP relay, push-to-talk with Muse Voice Transcribe.
6. Demo scenarios and polish; record the 3-minute video on Nov 3; submit before 9 PM ET on Nov 4.

Cut line if time runs short: Android client and routines go first; the demo can run on the web UI.

## 11. Decisions needed from the owner

1. Secrets: may the agent type a stored secret after a one-tap approval (default), or should secrets never be typed by the agent?
2. RAG: SQLite full-text search first (default), or add a local embedding model now?
3. Sleep timers: pause after 5 idle minutes, stop after 60 more (default).
4. One shared cloud phone for all bots with a queue (default), or one phone per bot (more cost, more setup)?
5. Which two demo scenarios (safe: no banking, medical or political content)?
