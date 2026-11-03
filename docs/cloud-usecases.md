# Use cases people offload to a cloud Android phone (research, 2 Nov 2026)

Question from the owner: what do people want to hand to a phone instead of doing it on a paired phone? Then build and test those, one query each.

## What the market shows (sources are mostly vendor blogs; treat as directional)
- Phones are used for three things: **managing many social accounts**, **always-on apps and games (AFK)**, and **automation and testing** (QA, app testing, ad verification, geo checks, AI-agent development).
  Sources: [pixelscan overview](https://pixelscan.net/blog/best-cloud-phone-android-providers/), [DEV Community: phones for developers](https://dev.to/vietnam/cloud-phones-for-developers-building-real-mobile-environments-not-just-running-apps-1ilc), [GeeLark press piece](https://www.wboc.com/online_features/press_releases/geelark-cloud-phone-replaces-device-farms-with-cloud-based-social-media-operations/article_255e5231-c727-5891-a8b8-723c4120e613.html), [Redfinger/GeeLark/VMOS comparison](https://www.conbersa.ai/learn/redfinger-vs-geelark).
- The first two groups sit in terms-of-service grey areas (bulk accounts, proxy tricks, game AFK farming, anti-detect). **We do not build those**: they get accounts banned, which is the opposite of our safety promise.
- Mainstream demand for phone agents is "save time on tasks inside apps": messaging, bookings, calendar, contacts, posting, applications ([Ericsson survey via TechAfrica](https://techafricanews.com/2026/09/17/ericsson-survey-agentic-ai-smartphone-users-2030/), [BlueStacks AI assistants post](https://www.bluestacks.com/blog/bluestacks-exclusives/ai-personal-assistant-apps-en.html)).
  Google is building app automation on the user's own phone ([Engadget](https://engadget.com/2170770/gemini-intelligence-brings-app-automation-to-android)). Our difference: a separate, always-on phone you can hand work to, so a paired phone, battery, data and privacy stay out of it.
- Disposable sandboxes are an established practice for suspicious files ([ANY.RUN Android sandbox](https://hackread.com/analyze-mobile-threats-any-run-android-os-sandbox/)); for app QA, teams pay for device clouds ([BrowserStack, TestGrid, Firebase Test Lab](https://dev.to/digitalgrowthpro/cloud-based-android-testing-comparing-infrastructure-options-for-development-teams-42e)).
- Not found: a ranked survey of "most wanted automations". The list below is judged on the reasons to offload, not on a poll.

## Why something belongs on the phone (the test every use case must pass)
1. It must **keep running while your phone is off, locked or away** (always-on, scheduled, waiting).
2. Or it is **risky or messy** and you do not want it on a paired phone (unknown links, APKs, apps you may not keep).
3. Or it needs a **clean, repeatable phone** (testing, screenshots, guides) with none of your data in the picture.
4. And it must be **allowed**: no bulk accounts, no game automation, no fake identities, nothing the app's rules forbid.

## The 10 use cases (one query each)
| # | One query | Why the phone | Needs built (new) | Evidence plan |
|---|---|---|---|---|
| 1 | "Is this link safe?" (paste a suspicious text/link or a screenshot of it) | Risky: never open it on a paired phone | `safe_check`: open in the phone's Chrome, record the redirect chain, final address, whether a password form or a browser warning appears, screenshots, a verdict with reasons | Google's public Safe Browsing test pages and a harmless page; the verdict and screenshots in the chat |
| 2 | "Check this APK before I install it" (attach an APK, or a package name) | Risky | `inspect_app`: version, size, target SDK, permissions with the dangerous ones flagged, signing certificate, install in a throwaway phone, launch, crash check, uninstall | An F-Droid APK and a deliberately permission-heavy app |
| 3 | "Watch this and tell me when it changes" (a price, a slot, a status, a score) | Always-on: runs while your phone is off | `watch`: a scheduled check that stays silent until the condition is met or the value changes, then messages first with a screenshot | A public page with a value the owner can change (a local test page) and one real public page |
| 4 | "Run my app's smoke test every night and tell me if it breaks" | Clean phone, scheduled, repeatable (indie developers are the hackathon's audience) | `qa_run`: install the APK, launch, exercise it (steps in plain English plus the standard Android monkey exerciser), catch crashes and ANRs from the log, screenshots, a report | Shattered Pixel Dungeon and our own Mobile Agent APK; a forced crash for the failure path |
| 5 | "Show me my app in dark mode, large text and Hindi, on a small and a big screen" | Clean phone, repeatable | `set_device`: dark mode, font scale, per-app language, screen size and orientation; screenshots of each state in one grid | Settings app and a sample app |
| 6 | "Make a step-by-step guide to set up X with screenshots" (support teams, creators, helping a parent) | Clean phone: no personal data in the pictures | The `report` artifact: numbered steps, each with a screenshot and a caption, exported as Markdown/HTML in the Library | "Turn on dark mode and set the screen timeout", "set up Google Keep" |
| 7 | "Reproduce this bug on a clean phone and send me the logs" (paste the report) | Clean phone, no risk to your data | `qa_run` variant: follow the steps, capture the log slice around a crash, screenshots, a short write-up | A planted failing step on a sample app |
| 8 | "Record a clean 20-second screen video of this flow" (app-store previews, demos, tutorials) | Clean phone; also useful for our own demo video | `screen_record`: Android's screenrecord, pulled into the Library | The Settings flow |
| 9 | "Test-drive these three apps and tell me which is simplest to set up" | Try apps without installing them on your phone | `inspect_app` + guided first-run per app, steps and time counted, permissions requested, a comparison table, uninstall at the end | Three free open-source note apps from F-Droid |
| 10 | "Sign me up for this service; the SMS code goes to my phone" (hybrid with the paired phone) | The phone does the long flow; your paired phone only supplies the SIM | OTP relay (server side built 3 Nov 2026, see [otp-relay.md](otp-relay.md)): the paired phone watches its notifications for one code, you approve THAT code with one click on the card, it is typed on the phone; the digits can always be typed by hand instead | A service that texts a code to the owner's number (owner picks); app side verified on virtual phones only |
| 11 | "I can't figure this out, show me" (a setting, a screen, a concept) | The agent explains with a picture instead of a wall of text, using your paired phone screen only when it is awake and shows no account address | `make_image` (Muse Image 1.0): an illustration from a prompt, or an edit of your picture or the phone's screenshot (numbered arrows, a highlighted switch), labelled AI-generated | "I don't get what Wi-Fi calling is. Show me with a picture." and, with a Settings > Display screenshot, "Where do I tap to turn on dark theme? Show me."; a plain question (battery percentage) must stay text |
| 12 | Another AI agent hires this one: an inbox agent finds a suspicious link and asks "Is this link safe?" over A2A | The outside agent has no phone; it borrows one, gets a typed answer, and every bot of the owner remembers the exchange | A2A service (`agent/a2a*.py`), skill `link-check` | Real run 3 Nov 2026: submitted, working, artifacts (verdict, report in Markdown and HTML, screenshot, answer), completed in 35 s; `retrieve-context` found it; Master recalled it; an agent without the `phone` scope was refused with error -32004 |

Paired-phone assists (the client app) beyond #10: notification catch-up ("what did I miss") and a morning brief that uses the paired phone's battery and calendar. They are extras, not the focus.

## Build order (risk-first, because time and usage are limited)
Batch 1 (shared pieces first, then the use cases that need little else): the `report` artifact (steps + screenshots in the Library), `set_device`, `inspect_app`, then #1, #2, #5, #6.
Batch 2: #3 `watch` (routine semantics), #4/#7 `qa_run`, #8 `screen_record`.
Batch 3: #9, #10 (client app SMS relay), notification catch-up.

## Safety rules that apply to all of them
- Never send the owner's files or secrets out; links and APKs run only on the phone, which is reset afterwards.
- No game automation (game guard), no bulk or fake accounts, no proxy or identity tricks.
- Installing or opening anything the owner supplied happens on a phone only, never on the Mac, and never on the paired phone.
- Reports state what was NOT checked (a clean verdict on a link means "nothing found in these checks", not "safe").

## Batch 1 built and tested (2 Nov 2026)
Shared pieces: `capture` (named screenshot with a caption), `verdict`, `set_device` (dark mode, text size, screen size, per-app language; undone when the task ends; never on a paired phone),
`inspect_app`, the `report` (Markdown plus one self-contained HTML page with inline pictures, no scripts; served with a sandboxing CSP) in the Library, a report card in the chat, an `.apk` upload,
and `check_apk`. Account screens (Chrome's first-run page lists the phone's Google account) are never captured. A guide or a look-and-feel task restores dark mode, text size and screen timeout afterwards.

| # | Query used | Result | Time | Notes |
|---|---|---|---|---|
| 1 | "Is this link safe?" Google's phishing test page | **Looks harmful** (Chrome's red warning, address unchanged, nothing typed) | 36 s | Observe-only guard held; Chrome data cleared afterwards |
| 1 | Google's malware test page | **Looks harmful** | 38 s | |
| 1 | https://example.com | **Nothing found in these checks** | 44 s | First run was refused because Chrome's "Stay signed out" button was not on the allow-list, and its picture would have shown the owner's Google address: both fixed, and that report was deleted |
| 2 | "Check this APK before I put it on my phone" (Shattered Pixel Dungeon, F-Droid) | **Nothing found in these checks** | 32 s | Phone 2 already had the game, so the emulator was used (an existing app is never touched); uninstalled afterwards |
| 2 | Our own Mobile Agent APK | **Looks suspicious** (sees every installed app, draws over other apps, debug build) | 28 s | Ran on Phone 2; removed afterwards |
| 5 | "Show me Settings > Display in normal, dark mode + large text, and a small screen" | 3 labelled pictures in one report | 37 s | The first run started from whatever the phone was in; it now sets a baseline first. Phone restored (checked with adb) |
| 6 | "Make a guide for my mom: dark mode and a 2-minute screen timeout" | 7 plain-language steps, each with a picture | 88 s | The worker added "undo" steps and left the timeout changed; settings are now snapshotted and restored, and the prompt says not to add undo steps |
| 6 | "Guide for my dad: install Google Keep and write a note" | Ran out of its step budget in the install (3 steps captured); the retry ran out too because every capture was refused (Keep's avatar has a hidden "Signed in as <address>" description that the account guard wrongly counted) | 185 s + 246 s | Guard narrowed to visible addresses and the chooser phrases; guides and look-and-feel tasks now get 15 extra steps |
| 6 | "New guide for my sister: add a note called Dentist in Google Keep" (Keep already installed) | **6 plain steps, each with a picture** | 86 s | The test notes (Shopping x2, Dentist) were left in the throwaway account's Keep |

Known limits: a link check reads the address bar and the page the way a person would; it cannot see downloads, other regions, or what happens after sign-in (every report says so).
Per-app language only works for apps that declare locales (Android 13+); the report says when an app does not support it. Not built in batch 1: #3 watch, #4/#7 QA runs, #8 screen recording, #9 app comparison, #10 SMS relay.

## Use case #11 "Show me" built and tested live (3 Nov 2026, Muse Image 1.0)
Built: `agent/museimage.py` (client), action `make_image` in the orchestrator (generate, or edit of the owner's picture, a Library picture or the bot's phone screen), chat message kind `image` with an AI-generated / AI-edited badge,
the prompt under it and a download link, Library kind `image`, daily cap (`MUSE_IMAGE_DAILY_CAP`, default 20), a forged-document refusal before the provider is asked, 22 tests in `tests/test_museimage.py`.
Live, real models, cost about $0.07 in all (7 images incl. the first API probes):
| Query | Result | Time |
|---|---|---|
| "I don't get what Wi-Fi calling is. Show me with a picture." | A clear two-column diagram (Wi-Fi calling vs cellular, 3 numbered steps), caption in words, labelled AI-generated | 31 s from message to picture (about 16 s is Muse Image) |
| Settings > Display screenshot attached, "Where do I tap to turn on dark theme? Show me." | The real screenshot with a red numbered arrow and the label "Tap here to turn on Dark theme", labelled AI-edited | 22 s |
| "What's the battery percentage?" | Plain text answer (100% and charging), no picture made | a few seconds |
Known limit, stated in the prompt and in the default caption: the edit endpoint redraws the whole picture. In the live edit the Dark theme switch came back drawn ON although it was OFF in the screenshot, even with "change nothing else" in the instruction.
So the picture shows WHERE to tap, never the current state. A pixel-exact version would draw the arrow ourselves from the UI tree's element positions (a possible follow-up).


## Use case #12 "Another agent hires this one" run live over A2A (3 Nov 2026)
The caller is a registered client ("Inbox agent", scopes ask, phone, context-write) using `scripts/a2a_client.py` against the A2A door on 127.0.0.1:8766.
| Step | What happened |
|---|---|
| `send "Is this link safe? https://testsafebrowsing.appspot.com/s/phishing.html" --skill link-check --stream` | Task submitted, then working. The first try failed at once, honestly: the skill wanted a data part with the address. Now it also takes the first address in the text. |
| Phone | The shared phone opened the page in a throwaway browser; Chrome's red warning appeared; nothing was typed or tapped on the page |
| Result (35 s in all) | Artifacts: `verdict` (data), `report` in Markdown and HTML (files the caller downloads with its token), a screenshot, the job result and the `answer` text: "Looks harmful". State: completed |
| `retrieve "what did you find about the phishing test page"` | The caller's own history came back ranked: the verdict, the report, the message |
| Master, in the owner's chat: "What did the Inbox agent ask you to check, and what did you find?" | Master answered from the shared store: asked about that link; found "Looks harmful", opened in a throwaway browser |
| A second agent with only the `ask` scope sends a phone task | Refused with `-32004: not allowed to use the phone-task skill (permission 'phone' is missing)` |
Not shown here: approval cards (they stay with the owner: the task would sit in `input-required`), push webhooks, the 0.3 dialect against a third-party client.
