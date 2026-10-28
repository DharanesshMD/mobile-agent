# Todo

## Planning
- [x] Create private repo `DharanesshMD/mobile-agent`
- [x] Research Android OS options, write docs/plan.md
- [x] Confirm track: Indie Developer
- [ ] Pick Android host and verify binder support
- [x] Local spike: Android 14 emulator on Mac, ADB screenshot + UI tree + tap (spike/probe.py)
- [ ] Cloud spike later: Redroid or Cuttlefish on a Linux host

## Next
- [x] Agent loop with Meta model (observe, plan, act, verify): agent/loop.py, tested live on emulator
- [ ] Harden loop: memory of seen info, better stuck recovery, richer actions (long press, text field focus)
- [ ] Web chatbot with live device view
- [x] FastAPI service + web chatbot: runs, SSE, approvals, handoff, OTP card, takeover (agent/service.py, web/index.html)
- [ ] Pairing + phone-facing channel (WebSocket/push) so the Android client can receive requests and send OTPs
- [ ] Android client app: pairing, SMS/OTP relay with per-request approval, push-to-talk
- [x] Play Store system image and AVD agent34ps; agent hands off at Play Store sign-in (verified)
- [ ] Sign in with a throwaway Google account (owner), then install and drive an app via the agent
- [x] Server: pairing (`/api/pair`), `/api/me`, cloud-account read/start/delete (single Google login on the cloud phone)
- [ ] Owner: do the real Google sign-in once on the cloud phone, then confirm the account shows in /api/cloud-account
- [ ] Android client: pairing, consent, sign-in-on-cloud-phone screen with live phone view, starter tasks

## Design v2 (docs/design-v2.md), pending owner sign-off
- [x] SQLite store, bots/messages, orchestrator (Spark 1.3) with mobile_task + ask_user, background worker jobs, SSE chat
- [x] Web UI like the reference: bot sidebar, chat with action cards, right panel Details/Library/Mobile, full-screen take control
- [ ] Vault (AES-GCM), secret input, sensitive-text detector, type_secret with approval
- [ ] Sleep/wake lifecycle (docker pause/stop), routines scheduler, library artifacts
- [ ] Android client, voice (Muse Voice Transcribe), demo scenarios, 3-minute video

## Prize plan (28 Oct 2026), ordered by value for the judging rubric
Rubric: Impact 30, Meta tooling 25, Demo 25, Technical depth 20. Deadline Wed 4 Nov, 9 PM ET.

P0, this week:
- [ ] Re-verify dark theme, calculator (not installed), storage with the new actions; build a scenario runner (`scripts/scenarios.py`) that prints pass/fail, steps and time for about 8 tasks
- [ ] Vault (AES-GCM), masked secret input, sensitive-text detector, `type_secret` with approval (design-v2 section 5)
- [ ] Routines and proactive behaviour: scheduler (daily time or every N minutes), the bot messages you first with results and asks when it needs you
- [ ] Voice: mic button records, Muse Voice Transcribe turns it into text (Meta tooling score), spoken replies later via the browser/phone TTS
- [ ] Pick and rehearse two hero demo scenarios (owner to choose; safe topics only), polish the UI around them

P1:
- [ ] Open-weight path: run the orchestrator on Muse Spark Open through Baseten (bonus prize, uses the partner credits)
- [ ] Sleep and wake lifecycle visible in the Mobile panel (emulator backend now, docker backend later)
- [ ] Android client: pairing, chat, action cards, OTP relay

P2:
- [ ] Replay mode for reviewers, README polish, 3-minute video script, record on Nov 3

## Lighter and faster phone (28 Oct 2026)
- [x] Light profile: Mac GPU rendering, 720x1600, animations off (about half the CPU)
- [x] Reference study of the desktop agent app (docs/reference-desktop-agent.md)
- [x] Resident screen reader via uiautomator2 (observe 2.9 s to 0.6 s, step 7.0 s to 4.1 s), adb fallback
- [ ] Model is now the biggest cost per step (2 to 3 s): send the screenshot only when needed (text-only when the UI tree is rich), smaller screenshot
- [ ] Android intent fast paths: alarm, timer, SMS compose, contact and calendar insert, maps, dial, share
- [ ] Cloud device: Redroid (container, no VM) on a Linux host; test binder support first
