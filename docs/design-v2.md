# Design v2: chat-first agent with a background cloud phone

Date: 28 Oct 2026. Status: proposal, waiting for owner sign-off. Supersedes the "web chatbot beside a phone view" layout in plan.md sections 6 and 12.
Items marked (verify) have not been tested.

## 1. The flow the owner described (from the reference recording)

The reference is a desktop chat app with a bot list. What it shows, and what we take from it:

- Left sidebar: a list of bots (agents), each a conversation with a last-message preview, search, new bot, and the profile at the bottom.
- Centre: the chat. Agent messages, a status line while it works ("Clicking around" with an animated dot), messages relayed between bots
  ("Message from <other bot>"), and an inline question card with a choice button (the action item). Composer: attach, mic, voice.
- Right panel with three tabs. Details: routines (recurring tasks on a schedule). Library: artifacts the bot produced (screenshots, files) grouped by
  This week / Older. Mobile: a small live thumbnail of the bot's screen with an Open button.
- Open goes full screen with a banner "You're in control" and an "I'm done" button. The owner takes the mouse, then hands control back.

What we change: the screen is an Android phone, not a desktop browser; and the cloud phone sleeps when nobody needs it.
Originality note: use this as layout inspiration only. Use our own names, assets and code (hackathon new-work rule). The recording shows personal
details (phone number, resume content); keep it and any screenshots of it out of the public repo and the demo.
I could not transcribe the audio (Whisper is not installed), so this design is based on the visuals and your written description.

## 2. How a request flows

1. The owner writes or speaks a request in a bot's chat (web or Android).
2. The orchestrator (Muse Spark 1.3) answers directly if it can: reasoning, drafting, questions, memory lookups.
3. If the task needs a phone, it calls the `mobile_task` tool. That creates a background job, wakes the cloud phone if it sleeps, and returns at once.
   The chat shows "Working on it" and a live status line. The owner can keep chatting or leave.
4. The Mobile Worker (Muse Wonder, the existing graph) drives the phone: observe, plan, act, verify. Only this worker uses Wonder.
5. When the worker needs something it cannot get from the screen, it pauses and sends a typed request to the orchestrator. The chat shows it as an
   action item card (section 4). The owner answers in chat; the job resumes.
6. The owner can click Mobile on the right at any moment to watch live, press Open for full screen, take control, and press "I'm done" to give it back.
7. When the job ends, the orchestrator writes the result in chat, saves artifacts (key screenshots, files) to the Library, and stores a short memory.
8. After the idle timeout the phone sleeps (section 7).

## 3. Models and who does what

| Role | Model | Why |
|---|---|---|
| Orchestrator: reasoning, chat replies, tool choice, summaries | `muse-spark-1.3` | Cheaper and fast for text and planning |
| Mobile Worker: screen understanding, tap, swipe, type, drag | `muse-wonder-1.0` | Listed with computer use; in our tests it completed Settings tasks that Spark 1.3 looped on |
| Speech to text (mic) | `muse-voice-transcribe-1.0` | Only Meta speech model; no TTS, so spoken replies use the client's own TTS |
| Search grounding | Spark with search grounding | Priced per 1,000 queries |
| Retrieval | none from Meta (no embeddings model in the list) | Start with SQLite full-text search (FTS5); add a local embedding model later if needed |

Rule: anything that touches the phone screen goes to Wonder. Everything else is Spark. Models are named only in one config file.

## 4. Orchestrator: tools, prompts, human in the loop

LangGraph supervisor graph, one thread per bot conversation, checkpointed in SQLite so a job or a question survives a restart.

Tools the orchestrator knows (each has a name, description and JSON schema in the system prompt):

| Tool | Purpose |
|---|---|
| `mobile_task(goal, app_hint)` | Start a background phone job. Returns a job id. |
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

Status: built. `agent/vault.py` (AES-256-GCM, id and owner as associated data, key from `VAULT_KEY` or an owner-only `data/vault.key`), `agent/sensitive.py`
(card with Luhn, Aadhaar-like, PAN, "password is", tokens). Differences from the plan above: the server redacts pasted secrets before they are stored or sent to a model
(the owner then re-enters them in the Vault tab, so a bypassed browser check cannot leak); the "trust this secret for this app" switch is not built, every use asks.
Secrets are referenced by label; unknown labels are reported to the worker without asking the owner.

## 6. Mobile panel and takeover

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
- Wake triggers: a message that needs the phone, a routine firing, the owner opening Mobile or taking control, an incoming OTP request.
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
`POST /api/mobile/control` (take) and `/release`, `GET /api/mobile/state` (ready, paused, waking). Existing run, pairing and cloud-account endpoints stay.

## 10. Milestones (deadline Wed 4 Nov, 9 PM ET; about 7 days left)

1. Today and tomorrow: SQLite store, bots and messages, orchestrator graph on Spark with `mobile_task` and `ask_user`, worker jobs in the background, SSE chat.
2. New web UI like the reference: sidebar of bots, chat with status lines and action cards, right panel with Details, Library and Mobile, full-screen take control.
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


## 12. Build status (28 Oct 2026): milestones 1 and 2

Done and verified: SQLite store (bots, messages, jobs, action items, library, FTS5 memory), orchestrator on Muse Spark 1.3 (JSON tool protocol: reply, mobile_task,
ask_user, memory_search, memory_save), worker requests (`need_info`, answered from memory when possible, otherwise asked in chat, and remembered), background job
manager with a FIFO queue on one shared phone, owner takeover that pauses the worker at the next step, cancel, restart recovery, library screenshots, SSE event stream,
and the new web UI (bot sidebar, chat, action cards, Details/Library/Mobile, full-screen watch and take control). 49 automated tests pass.

Live check with real models in the browser: "check the phone's Settings and tell me the Android version and security patch" ran in the background (7 steps, 138 s),
Spark reported "Android version is 14, security patch is September 5, 2023", which matches the device. Take control paused the agent and I'm done resumed it.
A choice card and cancel also worked live.

Known issues: about 20 s per phone step (emulator plus model latency); cancel takes effect at the next step; the live thumbnail polls every 2.5 s; one choice card was answered
during testing without a deliberate click from me (cause unknown, possibly my automated click on a neighbouring tab); the web UI has no vault, voice or routines yet.

## Routines (built)
`agent/routines.py`: a routine is `{name, prompt, schedule}` with `every_minutes` (5 to 10080) or `daily_at` (HH:MM, server local time). A scheduler thread ticks every 15 s,
claims due routines (next run moves first, so no double fire), and gives the orchestrator a system note; the bot replies or starts a phone job, so it messages the owner first.
After downtime a routine fires once. The orchestrator can create, list and delete routines in chat; the Details tab has the form, Run now, Pause and Delete.
Not built: per-routine time zones, retry after a failed phone job, a notification when the browser tab is closed (needs the Android client or web push).

## Vault update: credentials only through a secure input card
There is no direct "add secret" form or API. A credential enters only through an action card of kind `secret` (masked input, plus a name when none was given). The value goes
straight to the vault, the card turns into "Saved as '<label>'" and its input disappears; the value is never kept on the item, in messages or in logs. Cards come from three
places: the orchestrator's `request_secret`, the worker's `need_secret`, and automatically when a pasted secret is redacted from chat. The Vault tab only lists labels and kinds,
deletes, and shows recent use. Verification: the orchestrator's `vault_check` and the worker's `need_secret` (continues at once if the label is saved) answer yes or no, never the value.

Routines are created only through chat (the orchestrator's `routine_create`); the API and Details tab only list, run now, pause and delete them.

## Deleting a bot
Removed: the chat, its phone tasks with their screenshots and traces (`data/runs/<job>`), routines (they stop), remembered facts, library screenshots, open cards. A running phone task is
stopped first and allowed to finish before its data goes. Kept: saved secrets (they belong to the owner and are shared by all bots), the phone with its apps and Google account.
The only bot can also be deleted: it is replaced by a fresh Assistant, and the owner may tick "also erase all saved secrets" (secrets and the use history), offered only then because
other bots depend on them otherwise. The dialog lists the counts first (`GET /api/bots/{id}/delete-preview`). Late messages for a deleted bot are dropped.

## Bots talking to each other
`ask_bot {bot, question}` in the orchestrator: one bot asks another by name and gets the answer back in the same turn. The other bot answers from its own focus and memory
(its memory is searched for the question first), and cannot start phone tasks, ask the owner or use the vault while answering. Both chats get a dashed "bot note" so the owner
sees every exchange. Chains are limited to three bots and never loop back to a bot already in the chain. Not built: bots handing each other phone tasks, or messaging when no question is pending.

## Master bot
One special bot (`kind = master`, created at startup with an introduction, pinned on top, cannot be deleted, can be renamed). Its model reads a digest of every other bot on each turn
(focus or NOT SET, last messages, running or failed tasks, questions waiting for the owner and for how long, routines, recent remembered facts; capped at 4,500 characters) and can use `ask_bot`
for details. It can run phone tasks itself (`mobile_task`, as its own job on the shared phone) or hand one to the bot that fits (`delegate_task`: the job runs and reports in that bot's chat, with a note in the master chat when it finishes). Notifications need no model: `agent/master.py` `Watcher` checks every 60 s and posts each problem once (key stored in
`alerts`) as an "alert" card in the master chat: a bot without a focus, a card unanswered for 10 minutes, a phone task that failed in the last 24 h. The page shows an unread dot per bot,
a count in the tab title and, once the owner has allowed it, a browser notification for new master alerts. Not built: push to the Android client, alerts for routines that stopped firing,
alerts for a secret that is needed but not saved.

## Landscape apps
Games and video apps that force landscape rotate the phone by themselves (auto-rotate is locked, so only apps that insist do). `Device.size()` now returns the rotated size (uiautomator2
`window_size`; `dumpsys input` SurfaceOrientation on the adb path), the worker takes each step's size from the screenshot itself, shows landscape pictures at the same short side as portrait
ones (800x360, not 360x160) and gets a one-line note. The worker has a `rotate` action for apps that do not force it, and the owner has a rotate button in the control dock
(`POST /api/device/rotate`). The page follows the phone: when a landscape frame arrives, the bot list collapses into a drawer (the ☰ button), the phone panel widens and the live view and
thumbnail switch to 20:9; it all returns when the app closes. The launcher itself never rotates, so going Home also brings the portrait layout back.

## Phone power (built, local emulator backend)
`agent/lifecycle.py`. Power states: `on`, `sleeping`, `off`, `waking`. Idle policy: sleep after 5 minutes without a task, owner control or owner input (watching the live view does not count, so an open
tab cannot keep it awake); fully off after 60 more minutes asleep. `EmulatorBackend`: sleep is screen-off plus SIGSTOP on the emulator process (0 % CPU, memory kept), wake is SIGCONT (about 1 s), off is
`adb emu kill`, boot starts `scripts/emulator.sh` (measured 21 s to a usable phone with the light profile re-applied). After a nap longer than 90 s Wi-Fi is toggled so apps reconnect (a long-frozen
emulator leaves stale connections, which showed up as a YouTube "An error occurred (Playback ID ...)" screen). The server also notices when the emulator is stopped or started from outside.
Wake triggers: a phone task (including routines and delegated tasks) waits for the phone, the owner's Wake or Start button, taking control, and opening a task. A sleeping phone answers frames and input with 409
instead of hanging adb. The page shows the state everywhere: the sidebar chip, the Mobile tab (asleep, off, or starting with a progress bar, and "Sleeps in N min / Sleep now" while on) and the full-screen view.
`PHONE_SLEEP=off` disables it. A cloud backend (docker pause, stop, start) only has to implement the same six methods. Not built: waking from an incoming SMS or OTP request.

Phone power, lessons from testing: freezing only the emulator (SIGSTOP) kills it if it stays frozen for about a minute: the emulator's network helper `netsimd` pings it, gives up and the emulator
crashes on resume. So the helper is frozen and resumed with it (verified: 110 s frozen, adb back in 1 s). `resume()` now waits until adb answers and `wm size` works (Android sets boot_completed
early, and the window service can still be restarting), and if the emulator is gone or never recovers the phone is restarted automatically ("Restarting the phone"). A task submitted while the phone sleeps wakes it first
(measured: asleep for 105 s, task sent, phone on 4 s later, task done).

## Steering while a query runs

The owner can keep typing while the bot works; nothing in the composer is ever locked.

- **Chat turns:** each message starts a turn, turns for one bot run one at a time. A running turn checks for newer owner messages after every model call and tool result; if one arrived it throws away its pending decision, reloads the chat and decides again with everything said so far, so the owner gets one reply that covers both. `Orchestrator._answered` records the newest owner message a turn has seen, so the queued turn for the second message is skipped instead of answering twice.
- **Phone jobs:** `steer_task` (change or add to the running or queued job) and `stop_task` (cancel it). The note goes to `JobManager.steer`, is appended to the job's goal, and the worker picks it up in `observe` before its next step (`steer` callback in `graph.run`). A separate new request still waits for the current one ("one job per bot").
- **UI:** while thinking or a job runs, the composer placeholder says it can take more.

## Task queue, dependencies and checks (30 Oct 2026)
The phone is shared and does one task at a time. What changed so a stuck or half-done task can no longer freeze everything:

- **Queue.** `JobManager` keeps an ordered list of queued jobs. A new request while one runs is queued (up to 5 per bot), not refused; repeats of the same request are ignored (`same_goal`). Each queued job's status line says what it waits for ("Queued (1): insta bot's task is waiting for you").
- **Dependencies.** `mobile_task` takes `after` (a job id), and `mobile_plan` queues 2 to 4 steps in order. A step runs when the one before it is `done`, is skipped (status `skipped`, owner told) if that one failed or was stopped, and does not block other tasks while it waits. A step starts with the result of the one before it.
- **Checks, not trust.** `agent/checks.py` reads facts straight from the phone (app installed, a Google account signed in, app in front). A task can carry `verify`: if the worker says done but the check fails, the job is `failed`, so dependents do not run. The orchestrator also has `phone_check` to plan (what is missing?) and to confirm a result. The job-finished note makes it judge the report against the goal and say plainly whether it worked, partly worked or failed; one retry is allowed.
- **No frozen phone.** A task waiting for the owner gives the phone up after 15 min (card `expired`, job failed, next task runs). Owner control with no input for 10 min ends by itself. After a server restart, running and waiting jobs fail with a message in their chat, and jobs that never started go back in the queue.
- **Phone always available.** `Lifecycle.ensure_healthy` wakes the phone and restarts it if adb or the window service does not answer; every task calls it first. If the phone dies mid-task the job restarts it and runs once more. A background check every minute restarts an idle phone that stopped answering twice.
- Limits and timeouts are in `agent/config.py` (`OWNER_WAIT_MIN`, `HOLD_IDLE_MIN`, `MAX_ACTIVE_PER_BOT`).

## Secure screens in the live view (30 Oct 2026)
Password, purchase and some banking screens set `FLAG_SECURE`: Android returns a blank picture for any screenshot (the status and navigation bars still show). The owner needs to see the screen to type on it, so the live view (`/api/device/screenshot`) falls back to the emulator's own picture (`adb emu screenrecord screenshot`, about 0.1 s, `Device.host_frame`) when the normal frame is blank in the middle (`is_blank`). The agent's own screenshots do not use this path: on those screens the worker sees a blank picture on purpose, so it never sees what the owner types. A real phone has no host-side capture, so there the view stays blank on secure screens; a cloud phone would need its own equivalent.

## Long waits, handoffs from chat, retry cap (30 Oct 2026)
- **Waiting is free.** `{"type":"wait","seconds":30}` (1 to 60) waits in 0.25 s slices, so Stop works at once. Waits do not use the step budget (`state["waits"]`), only a hard cap of 4x the budget and a wall-clock limit of `MAX_JOB_MINUTES` (30) that does not count the time the owner spends on a handoff. A 573 MB game download had used up all 25 steps at 9% because every short wait cost a step and a model call.
- **Say "done" in chat.** A handoff card ("your turn on the phone") is normally answered with Take control and I'm done. The orchestrator now also sees open cards and can `resume_task` when the owner says they finished (only handoffs: approvals, choices, codes and secrets still need their own card).
- **One retry, never a loop.** After a task ends unfinished the orchestrator may retry once (`MAX_AUTO_RETRIES`); a second automatic retry is refused with a message to the owner. The count resets when the owner writes again or a task finishes.

## Routines: reliability (30 Oct 2026)
A routine is checked every 15 s, fires the bot with a system note, and the bot decides to reply or queue a phone task. What keeps it dependable when nobody is watching:
- **No pile-up.** Tasks started in a routine's turn (and their retries) carry its id (`jobs.routine`). A routine whose last run is still queued, running or waiting is not started again: the scheduled fire is skipped (`last_status` "skipped: the last run is still working", no model call) and "Run now" answers 409.
- **Missed times.** A daily routine more than 2 h late (the server was off) waits for tomorrow and tells the owner in the bot's chat; an every-N-minutes routine runs once after downtime. Routines live in SQLite, so they survive restarts.
- **Visible outcome.** `routines.last_status` (started, replied, done, failed, max_steps, skipped, missed) shows in the Details tab as "last run: ...".
- **Rides out network blips.** Model calls retry rate limits, server errors and network failures (DNS, dropped connection) for about a minute (1, 2, 4, 8, 15, 30 s) instead of failing a task on the first hiccup; a 30-minute install had died on a 3-second DNS failure.
- It all sits on the queue: a routine's phone task wakes a sleeping phone, queues behind other tasks, times out if it needs the owner, is verified and may be retried once.
