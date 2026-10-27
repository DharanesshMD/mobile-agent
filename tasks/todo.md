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
- [x] SQLite store, bots/messages, orchestrator (Spark 1.3) with computer_task + ask_user, background worker jobs, SSE chat
- [x] Web UI like the reference: bot sidebar, chat with action cards, right panel Details/Library/Computer, full-screen take control
- [ ] Vault (AES-GCM), secret input, sensitive-text detector, type_secret with approval
- [ ] Sleep/wake lifecycle (docker pause/stop), routines scheduler, library artifacts
- [ ] Android client, voice (Muse Voice Transcribe), demo scenarios, 3-minute video
