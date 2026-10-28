# Reference study: a desktop agent app, and what to build for Android

Date: 28 Oct 2026. Source: the owner's screen recording plus a read-only static look at the installed desktop app's package (file layout, process
structure, and the names of the concepts in its API schema). This is a design reference only. Nothing was copied: no code, prompts, assets or
identifiers are used here, and the extracted copy was deleted. Everything below is described in our own words, and our implementation is our own.

## How the desktop app is put together
- An Electron client with several processes: the app shell, a React UI, a **local execution daemon** (runs tools on the user's own machine),
  and an **agent coordinator** process. Native helpers handle window capture, on-screen text recognition, notification settings and device checks.
- It is a client of a **hosted backend** (agents, cloud machines, automations, library, connectors). The agent logic and the cloud machines live there,
  so the client alone does not reveal how tasks are run; only the shape of the product.
- Cloud machines **hibernate** and wake on demand (the same idea as our sleep and wake design).
- Prompts to the user are a "widget" with a question and options that the user answers (our action cards).

## Product concepts the schema shows
| Concept | What it does | Our status | Android adaptation |
|---|---|---|---|
| Many bots, each a conversation | Separate agents with their own history, tools and visibility or sharing | Built (bots, persona) | Same; sharing later |
| Cloud machine per bot | Hibernates, wakes on demand | Designed, not built | Cloud Android device; pause or stop when idle |
| Local tools on the user's own machine with a permission card, a permission ceiling and stored permissions | The agent can use the owner's device only within granted limits | Not built | The Android client is the local executor: SMS or OTP, notifications, contacts, location, each with a per-tool grant and a ceiling |
| Auto-approve rules | The user saves "always allow this" to stop repeated prompts | Not built | "Always allow this app or action" on action cards, revocable |
| Credential requests, connector grants, vault | Secrets and access are requested through cards, never typed to the model | Vault designed | Same, plus per-use approval |
| Hand back the machine | Owner takes control, then returns it | Built (take control, I'm done) | Same |
| Automations (routines) with policies | Scheduled work and rules about what it may do alone | Not built | Scheduler plus per-routine policy (what needs approval) |
| Proactive messages and presence | The bot messages first; delivery depends on whether the user is present | Not built | Push notification when the client is closed; in-chat when open |
| Library of pinned items and attachments | Saved screenshots, documents, files | Built (screenshots) | Add pins and file attachments |
| Proposed memories and skills reviewed by the user | The agent suggests what to remember; the user accepts | Memory built, no review | Add an accept or reject card for saved memories |
| Templates | Reusable bot setups | Not built | Bot templates (persona, tools, routines) |
| Connectors (chat apps, MCP tools, accounts) | Reach services through APIs instead of clicking | Not built | API or intent paths first, UI automation as the fallback |
| Voice calls and meetings | Voice interaction | Not built | Muse Voice Transcribe plus the phone's own text to speech |

Left out on purpose: payment cards and money movement (financial risk, and against our rules), team and organization administration.

## What to build for Android, in order
1. **Fast paths over UI steps.** Android lets an agent do many things without tapping: intents and deep links (alarm, timer, compose SMS, insert contact or calendar
   event, navigate, dial, share) and `cmd` or content queries. This is both faster and lighter than UI automation. Already added: `open_settings`, `find_app`, `open_url`.
2. **Local-executor phone client** (the equivalent of their local daemon): per-tool grants with a ceiling, OTP relay, notifications, push for proactive messages.
3. **Permission memory:** "always allow" rules and per-routine policies, to cut approval prompts without losing safety.
4. **Memory review cards, bot templates, library pins.**

## Cost note
The desktop app offloads heavy work to its backend. We should do the same: keep the phone cheap (a light container or tuned emulator) and keep models doing
only what needs a model.
