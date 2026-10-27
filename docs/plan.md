# Mobile Agent: plan and feasibility

Date: 27 Oct 2026. Hackathon build window: Oct 27 to Nov 4 (submission due Nov 4, 9 PM ET).
Status: planning. Items marked (verify) are not yet tested.

## 1. Idea

An always-on, proactive agent that runs on a self-hosted Android environment in the cloud.
The user talks to it from a web chatbot or a mobile client with always-on voice. The LLM looks at
the Android screen, taps and types through the device, and reports results back.

## 2. Hackathon constraints that shape the design

- Must use Meta's models: Muse Spark (reasoning, vision), Muse Voice Transcribe (speech to text),
  Muse Wonder / Muse Image, SAM 3.1 (segmentation), or open weights Muse Spark Open / Muse Glimmer.
- Judging: Impact 30%, Use of Meta tooling 25%, Technical depth 20%, Demo 25%. Demo video is 3 minutes.
- New work only. Everything in this repo is written during the event. The earlier `android-personal-agent`
  project (Oct 14) is not reused as code; its design notes are reference only.
- Public repo is required at submission time. Keep this repo private until then, with no secrets committed.
- Avoid listed anti-projects: medical or health advice, political content, basic RAG, simple image analyzers.
- Prize bonus: open-weight models served on partner inference (Baseten or Modal) are eligible for the
  $10K "Best Build on Meta Open Weight Models". Best Agentic Use-Case is $5K.

## 3. Platform feasibility

| Surface | Feasibility | Notes |
|---|---|---|
| Android agent environment (cloud) | Good | Container or VM images exist, controllable over ADB. |
| iOS agent environment (cloud) | Not feasible | No legal or practical way to host iOS in the cloud. Skip. |
| Android client app | Good | Foreground service with a wake word, push-to-talk fallback. |
| iOS client app | Limited | iOS blocks always-on background listening. Only worth doing as a web app or later. |
| Web chatbot | Good | Fastest path. Build first. |

Decision: Android only for the agent. Web chatbot first, Android client second, no iOS.

## 4. Android OS options for the agent device

| Option | What it is | Fit | Risks |
|---|---|---|---|
| Redroid | Android in a Docker container, arm64 and amd64 | Best first choice. Lightweight, scriptable, many instances per host. | Needs host kernel with binder support (verify per VPS). Official images have no Google services; GApps or MicroG needed. Weaker fidelity for apps that detect emulators. |
| Cuttlefish (AOSP) | Google's official virtual device, KVM based | Good fidelity, supports newer API levels. | Needs KVM or nested virtualization, heavier, more setup. |
| Waydroid | Android container for Linux desktops | Poor fit. Designed for Wayland desktops, not headless cloud. | Extra display stack. |
| Android-x86, BlissOS, LineageOS in QEMU | Full OS in a VM | Fallback if containers fail. | Slow, needs nested virtualization. |
| Real spare phone | Physical device on a stand | Best app compatibility. | Not "cloud", weak for a demo story, but a good backup. |

Recommendation: start with Redroid on an arm64 or amd64 Linux VPS. Spike it on day 1. If binder is not
available on the chosen host, move to Cuttlefish or a Linux VM with a kernel that supports it.
Keep the agent layer independent of the OS so the device backend can be swapped.

Known limits for a self-hosted Android device:
- Apps with strong anti-emulator or device-integrity checks (banking, payments, WhatsApp) may refuse to run
  or may risk account bans. Do not plan the demo around them.
- Prefer apps that work without device certification: a browser, Maps, YouTube web, Settings, Files, Gmail
  web, a notes app, and apps the project builds itself.
- Add a clean approval step before anything consequential (send, pay, delete).

## 5. Agent control layer

Two ways to let the LLM operate the device:
1. ADB plus UI hierarchy: `adb shell uiautomator dump` or an accessibility helper app gives a text tree of the
   screen, and `adb shell input` taps, swipes and types.
2. Screenshots plus a vision model: Muse Spark reads the screenshot and picks coordinates. Works when the
   accessibility tree is missing.

Open-source references to study (verify before depending on them): Mobilerun (formerly DroidRun), which pairs an
accessibility Portal app with LLM providers. Decision: write a thin own loop (observe, plan, act, verify) on ADB
so Meta models are central and the code is clearly new work. Borrow ideas, not code.

Loop per step: capture screen (tree + screenshot) -> Muse Spark returns one structured action -> execute ->
re-observe to verify -> stop on goal, failure budget, or approval needed.

## 6. System architecture

- Android device host: Redroid container, reachable only through a private tunnel. No public ADB.
- Agent service (Python, FastAPI): task queue, agent loop, device driver, model client, event stream.
- Model client: Meta Model API for Muse Spark (vision and planning) and Muse Voice Transcribe. Optional
  open-weight Muse Spark Open on Baseten or Modal for the open-weight bonus.
- Web app: chat, live device view (screenshot stream), approval cards, task history.
- Android client: foreground service wake word, push-to-talk, speaks answers, sends commands.
- Proactive mode: scheduled and event triggers (for example "check this every morning and tell me if it
  changed"), run by a scheduler in the agent service.
- Storage: SQLite or Postgres for tasks, steps and screenshots; object storage for screenshots.
- Security: single-owner pairing code, token auth on every API, device isolated from the host, secrets in env only.

## 7. Cost and credits

- Meta Model API credit ($150) once applied. Do not call the API before the credit shows, or the card is charged.
- Modal credit ($100) and Baseten credit ($100) for open-weight inference and hosting (apply).
- Android host: a small VPS. Check the cost before committing. Free or cheap arm64 tiers exist (verify availability).

## 8. Milestones (8 days)

1. Day 1 (Oct 27 to 28): Redroid boots on a host, ADB works from the agent service, screenshot and tap from a script.
2. Day 2: Agent loop with a Meta model completes a 3-step task (open browser, search, read result).
3. Day 3: Web chatbot with live device view and task history.
4. Day 4: Voice in via Muse Voice Transcribe, voice out. Approval cards.
5. Day 5: Android client with wake word and push-to-talk.
6. Day 6: Proactive tasks (scheduled checks, notifications).
7. Day 7: Hardening, error recovery, demo scenarios, make repo public-ready (no secrets).
8. Day 8 (Nov 4): Record 3-minute demo, submit before 9 PM ET.

## 9. Open questions

- Indie or startup track?
- Which hosting for the Android device (VPS provider, arch)? Needs binder support check.
- Which demo scenarios are most impressive and safe (no banking, no medical, no political)?
- Wake word engine for the Android client (on-device open-source detector vs push-to-talk only).
- Does Muse Code usage draw from the $150 credit?

## 10. Risks

- Redroid cannot run on the chosen host: fall back to Cuttlefish, or a physical phone.
- Hackathon rule on prior work: keep this repo free of code from earlier projects.
- Model access errors some participants reported (`model_not_found`): verify model IDs on first call.
- Always-on voice on Android is battery and policy sensitive; keep push-to-talk as the safe path.
