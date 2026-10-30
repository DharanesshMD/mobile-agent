# Demo-first plan (29 Oct 2026), pending owner approval
Goal: a 3-minute video that shows the idea working end to end on the Mac emulator, a public repo, and a public replay link that works without a live phone.
Deadline Wed 4 Nov 9 PM ET = Thu 5 Nov 6:30 AM IST. Submit by Wed 4 Nov evening IST, so there is a night of buffer. Feature freeze Mon 2 Nov noon.

Story for the judges (Impact 30): an always-on assistant that does things on a phone while you are away, asks before it sends or pays, never sees your passwords,
and messages you first. Meta tooling (25): Muse Spark (chat, tools, web search), Muse Wonder (phone), Muse Voice Transcribe (mic), open-weight Spark on Baseten if time allows.

## Owner tasks (only the owner can do these)
- [ ] Today: Google sign-in once on the emulator, on a phone hotspot or with ProtonVPN on. Use a throwaway Google account, not a personal one (it appears in the video). Then check `/api/cloud-account`
- [ ] Pick the two hero scenarios (proposals below)
- [ ] Rotate the exposed API key before the repo goes public; make the repo public on submission day

## Thu 29 Oct: unblock and baseline
- [ ] Sign-in done (owner), account shows in `/api/cloud-account`, Play Store installs one free app through the agent
- [ ] Run `scripts/scenarios.py` on all scenarios; fix anything that fails; record pass/fail and time in docs/quality-log.md
- [ ] Commit the steering and restart-recovery changes

## Fri 30 Oct: two hero scenarios, rehearsed
Proposed (safe topics only):
- Hero A, voice to action: speak "text Mom I'll be late and set an alarm for 6" into the mic. Shows Voice Transcribe, Wonder on the phone, the confirm card before the text is sent, and steering ("make it 6:30") while it runs.
- Hero B, proactive and safe: a 7:30 routine messages first with a web-search brief (Spark web search); the bot signs in to an app with a vault secret (approval card, the agent never sees the value); the master bot answers "what needs me?".
- [ ] Rehearse each 5 times; log steps, time and failures; fix the top failure each round
- [ ] Polish only the screens the video shows (empty states, copy, the phone power card, landscape if a game is used)
- [ ] A stable demo bot setup script (`scripts/demo_reset.py`): fresh data, known bots, routine, vault entry, phone in a known state

## Sat 31 Oct: replay mode and the public link
- [ ] Recorder: each finished job already keeps `data/runs/<job>/step*.png` and timing; export a session (chat messages, steps with thoughts, screenshots as small JPEGs) to one folder of static files
- [ ] Replay page: the same chat and phone panel UI, driven by the exported session (play, pause, speed); read-only, no model calls, no keys; `?replay=<name>` on the existing web/index.html or a small `web/replay.html`
- [ ] Host as a static site (GitHub Pages from the repo, no risk to the production server); include the two hero sessions and one failure-and-recovery session
- [ ] Open-weight spike (about 2 h, drop if it fails): Muse Spark Open on Baseten for the orchestrator behind a config switch

## Sun 1 Nov: how judges meet it
- [ ] README rewrite: what it is, the demo video, the replay link, architecture picture, how to run, safety design, which Meta models do what, honest limits (local emulator today, cloud phone is the deployment target)
- [ ] Phone-friendly web chat (installable PWA with the mic) as the mobile client for the demo; the native Android client with SMS/OTP relay is out of scope, listed as next step
- [ ] Security sweep: secrets scan of the repo and git history, `.env`, `data/`, `vault.key` ignored, LICENSE, remove the spike output; `uv run python -m unittest discover -s tests` green
- [ ] Video script and shot list (3 minutes: 20 s problem, 2 hero scenarios, 20 s safety, 15 s models and architecture, 10 s close)

## Mon 2 Nov: freeze and dress rehearsal
- [ ] Freeze at noon; only bug fixes after that
- [ ] Full run-through with a screen recorder, fix what breaks, capture b-roll (power states, vault card, steering)

## Tue 3 Nov: record
- [ ] Record on a quiet network, three takes per hero scenario; voice-over; captions; export 1080p under the size limit
- [ ] Upload the video, test the replay link on a phone

## Wed 4 Nov: submit
- [ ] Make the repo public, final README check from a clean clone, submit the form by evening IST, keep the night as buffer

## Cut list if time runs short (in this order)
Open-weight Baseten spike, PWA polish, landscape and game demo, master bot in the video, the second failure-and-recovery replay.
