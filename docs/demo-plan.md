# Demo and submission plan v2 (2 Nov 2026, 02:30 IST), pending owner approval

Replaces the 29 Oct plan. The product grew a lot since then (several phones, automatic routing, game-account safety, image input, real-phone app), so the story, the
hero scenarios and the work list are re-cut around what is built and verified (434 tests, QA run 10/12, docs/qa-run-2026-10-31.md).

## 0. Facts and what is NOT verified
Recorded earlier (docs/plan.md, tasks/todo.md; **the owner must re-check them against the official rules page, I could not read it from here**):
- Meta Global AI Developer Hackathon, Indie Developer track. Judging rubric: **Impact 30, Use of Meta tooling 25, Demo 25, Technical depth 20**. 3-minute demo video.
- Must use Meta's models; everything is new work from the event; public repo at submission; avoid the listed anti-projects (medical/health advice, political content, basic RAG, simple image analysers).
- Bonuses: **Best Build on Meta Open Weight Models** ($10K, open weights served on partner inference such as Baseten or Modal), **Best Agentic Use-Case** ($5K). Indie 1st place $150K.
- Checked on the official Cerebral Valley page and participant guide (3 Nov): deadline **Thu 5 Nov 07:30 IST (= Wed 4 Nov 9 PM EST; US daylight time ended 1 Nov, so IST is +10.5 h, not +9.5 h)**, judging 5-9 Nov, finalists and winners 10 Nov. Judges therefore look at the video, the repo and any link
  **after** the deadline, on their own machines, without our phone: a replay link and a clear README matter as much as the live product.
- Not found in any source I could reach: exact submission form fields, video length/size/format limits, brand rules for using Meta's names. Check the official page and fill section 7.

Time left: about 76 hours from now to the deadline. Recommended submission: **Wed 4 Nov by 18:00 IST** (13.5 h of buffer).

## 0b. Official facts, read from the Cerebral Valley page and participant guide on 3 Nov
- **Submit on the CV platform ("Submit Project") by Wed 4 Nov 9:00 PM EST = Thu 5 Nov 07:30 IST.** Late submissions are not considered. One submission per team; list everyone who worked on it; the repo must be public and the video link must work logged out.
- Three-minute demo video, shown to judges together with the code and the live product. The demo must show only what was built during the hackathon, and judges must be able to see what that is (a README line stating it; the git history starts 27 Oct). Unclear original work means disqualification.
- Rubric confirmed: Impact 30, Meta AI tooling 25, Technical depth and execution 20, Demo 25. Stage 1 is pass/fail (fits a theme, really uses Meta Model API or open weights); Stage 3 top 5 per track are judged live. Judging 5-8 Nov, closing ceremony Mon 9 Nov 12:00 PM ET.
- Themes: "Build for Your World" (our fit) and "Push the Frontier". Indie prizes: 1st $150K, 2nd $100K, 3rd $75K, 4th $50K, 5th $25K.
- Bonus prizes we can aim at: **Best Creative Build $5K (built with Muse Image or Muse Wonder: we use both)**, **Best Agentic Use-Case $5K**, **Best Build on Open Weights $10K** (Muse Spark Open or Muse Glimmer, model `meta-models/muse-spark-open-1026` on Baseten), **Most Viral $20K (post the demo on X and submit the post link on the platform; engagement counted until 8 Nov EOD)**, Community Vote $10K (public gallery, one vote per verified account).
- Credits: claim the $150 Meta API credits **by Wed 4 Nov 9:00 PM ET** (each participant claims their own; a card is required). Baseten $100 and Modal $100 credits are requested separately and reviewed.

## 1. The story (one sentence, then three proofs)
"Mobile Agent is an always-on assistant with its own phones: it picks the phone that can do the job, finds another way when one fails, and protects you from itself
(approvals, a vault it never reads, and a rule that it never plays games that ban bots)."

| Rubric | What the judge must see | Evidence we have |
|---|---|---|
| Impact (30) | A real daily problem solved: phone tasks done while you are away, from voice, text or a picture; safe by design; works on apps that refuse virtual phones | Hero A and C in the video, QA run, game-safety doc, the honest-failure behaviour |
| Meta tooling (25) | Each Meta model doing a distinct job, visibly | Spark 1.3: chat, memory, routing, web search, **image understanding**; Wonder 1.0: the phone worker (computer use); Voice Transcribe: the mic; (bonus) Spark Open on Baseten |
| Demo (25) | Clear, live, no dead time, a story with a beginning and an end | The 3-minute script below, rehearsed, cut to the beat, captions, "sped up" labels |
| Technical depth (20) | Real engineering, not a wrapper | Three kinds of phone behind one interface, per-phone parallel queues, crash detection and automatic escalation, verification gates, vault, game guard (code-enforced), prompt-injection rule for images, 434 tests, measured numbers |

## 2. The 3-minute video (shot list)
| Time | Beat | On screen | Narration (draft) | Proves |
|---|---|---|---|---|
| 0:00-0:15 | Hook | Master chat, three phones tiles (emulator, Phone 2, your phone) | "Some apps refuse to run on virtual phones. Agents that tap blindly can get your account banned. This agent has three phones and a conscience." | Impact |
| 0:15-1:10 | **Hero A: it finds a way** | Type "open Clash of Clans" in the Assistant. The emulator crashes (counter 3/3), a message "moving it to Phone 2", the app opens there (age screen). Then "gather my resources": it refuses (account protection), offers Take control; you tap once yourself. Cut to Shattered Pixel Dungeon: the agent plays a few moves **in parallel** with an emulator task | "It saw the crash, moved the app, opened it, and told me why it will not play for me." | Tech depth, safety, parallel lanes |
| 1:10-2:00 | **Hero B: see it, say it** | Paste a screenshot or a photo of an event flyer, say into the mic "put this in my calendar and remind me an hour before". Spark reads the picture, a confirm card appears, approve, Wonder fills the calendar on the phone, steer mid-run ("make it 6:30") | "Voice, a picture, then the phone does it, and it asks before it saves." | Meta tooling (3 models), approval |
| 2:00-2:30 | **Hero C: proactive and safe** | The morning routine message arrives first (web-search brief); Master "what needs me?"; a sign-in with a vault secret: approval card, the agent never sees the value; an image with hidden instructions is ignored | "It messages first, asks before it sends, and never reads my passwords." | Impact, safety |
| 2:30-2:50 | Architecture and numbers | One diagram: phones, lanes, guard, vault; the numbers: 434 tests, QA 10/12, 5 s boot of Phone 2, 4.3 s per step | "Built on Muse Spark, Muse Wonder and Voice Transcribe" (+ "and the open-weight Spark on Baseten" if done) | Tech depth, Meta tooling |
| 2:50-3:00 | Close | Repo and replay link on screen | "Free, open source, runs on your Mac." | Demo |

Rules for the recording: one scenario per take, sped-up segments labelled "4x", captions on, no music without a licence, no real contacts/emails on screen (throwaway Google account only),
no Clash of Clans gameplay shown or implied, the model names said exactly as Meta writes them.

## 3. Readiness work (priority order)
P0 = must be done before recording.

| # | Work | Why | Effort |
|---|---|---|---|
| P0-1 | `scripts/demo_reset.py`: clean database, named bots (Master, Assistant, Phone 2, "My phone"), one routine, one vault entry, calendar and Chrome empty, apps preinstalled, phones in a known state | Repeatable takes | 1.5 h |
| P0-2 | QA leftovers: rename the `sdk_gphone64_arm64` bot to "My phone"; hide the Google address in job results and Library titles; show routine times in local time; the phone bot greets "connected" only when it is; clear the stale "needs attention" cards; the empty "Assistant has no focus yet" card | First impressions on camera | 3 h |
| P0-3 | Rehearse Hero A, B, C five times each from a reset; log time and failures; fix the top failure each round (Hero B calendar flow and Hero C routine are not rehearsed yet) | The demo is 25 % of the score | 4-5 h |
| P0-4 | README rewrite (it still says "Status: planning"), LICENSE (none today; suggest MIT), one architecture picture, a 5-minute quick start tested from a clean clone, docs index | Judges open the repo first | 2 h |
| P0-5 | Security sweep: **rotate the exposed API key** (owner), delete the local `backup-before-trailer-removal` branch, secret scan of the tree and history (the current key is not in the git history: checked), remove personal data from docs, `.env.example` complete | Public repo | 1 h |
| P0-6 | Video: script lock, record, edit, captions, export 1080p, upload, test the link while logged out | Required | 6-8 h |
| P1-7 | **Open-weight path** (Spark Open on Baseten for the orchestrator behind `ORCHESTRATOR_BACKEND`), run 3 scenarios on it, 10 s in the video, README section. Go/no-go decision at 15:00 today; drop if tool-call JSON is unreliable | A $10K bonus prize and extra Meta-tooling credit | 3 h |
| P1-8 | **Replay site** on GitHub Pages: exported sessions of Heroes A-C and one failure-and-recovery, same UI, read-only, no keys | Judges cannot run our phones | 3-4 h |
| P1-9 | Paired-phone check: install the APK on the owner's Android phone, pair, run one task ("open Settings, tell me the battery") and, if it works, show it for 10 s in Hero A | "Your own phone" is the strongest Impact moment; unverified today | 1 h owner + 1 h me |
| P2 | PWA install, Docker deploy, BlueStacks wrapper, more adb phones, the `push_file` op | Cut candidates | - |

## 4. Schedule (IST)
| When | Plan |
|---|---|
| **Mon 2 Nov** 10:00-13:00 | P0-2, P0-1; owner: rotate the key, decide the hero set, charge the phone |
| 13:00-15:00 | P1-7 open-weight spike (go/no-go at 15:00) |
| 15:00-19:00 | P0-3 rehearsal round 1, fix the top failures. **Feature freeze 19:00**: bug fixes only after that |
| evening | P0-4 README + LICENSE, P1-8 replay recorder |
| **Tue 3 Nov** 09:00 | Reset, rehearsal round 2 (full run with a stopwatch), reboot the Mac, close all other apps |
| 11:00-17:00 | **Record** (3 takes per hero, then the voice-over), P1-9 paired phone if it works |
| 17:00-22:00 | Edit, captions, export, upload as unlisted, rough-cut review |
| **Wed 4 Nov** 09:00-12:00 | Final edit, replay site live, README from a clean clone |
| 12:00-15:00 | Security sweep again, key rotated, make the repo public, test every link logged out |
| **by 18:00** | **Submit** (form filled from section 7). Thu 5 Nov before 07:30 IST is emergency buffer only |

## 5. Risks and answers
| Risk | Answer |
|---|---|
| Live phone misbehaves on camera | Record each hero as its own take; keep the first clean take; the replay site and a screen-recorded backup of each hero exist |
| Mac memory (swap was 4.8 of 5.1 GB) | Reboot before recording, close every other app, record the Phone 2 segment last, stop its background helper and BlueStacks afterwards |
| Model rate limits or latency (429s) | Retries are built in (about 1 minute); record at a quiet hour; keep the speed-up cut ready |
| The Clash of Clans story is misread as "bot for a game" | Show opening and the refusal only, say the account-protection rule out loud |
| Privacy | Throwaway Google account; fake contacts; the Library and chat checked for the owner's address before every take |
| Judges cannot run it | Replay site, a clear README, `scripts/demo_reset.py`, the honest limits section (local emulator today, a phone is the deployment target) |
| Brand use | Name the models as Meta writes them, no logos, no endorsement claims; check the official brand rules |

## 6. Cut list (in this order)
P2 items, PWA, the replay failure-and-recovery session, the real-phone moment, Hero C's image-injection beat, the open-weight segment (keep the README paragraph if it ran).

## 7. Submission checklist (fill the real field names from the official form)
- [ ] Track: Indie Developer; project name, one-line pitch, long description (use sections 1 and 2)
- [ ] Video: 3:00 or less, public or unlisted link that plays logged out, captions
- [ ] Repo: public, README with the video and replay links, LICENSE, no secrets (tree and history scanned), tests green, new-work statement
- [ ] Meta tooling list: Muse Spark 1.3, Muse Wonder 1.0, Muse Voice Transcribe 1.0, (Spark Open on Baseten if done) and what each does
- [ ] Open-weight bonus: provider, model id, where in the code, evidence (a run log), only if P1-7 passes
- [ ] Testing instructions for judges: the replay link, and "run it yourself" steps with the keys they bring
- [ ] Team members, country, eligibility confirmations, the new-work declaration
- [ ] Submitted at least 12 h before the deadline; confirmation email saved

## 8. Decisions needed from the owner
1. Hero set: A (it finds a way) + B (see it, say it) + C (proactive and safe), or drop one?
2. Open-weight spike: go or no-go?
3. Paired-phone test tomorrow morning: yes or no?
4. Feature freeze at Mon 19:00 IST: ok?
5. LICENSE: MIT ok?
6. Who narrates and where is it hosted (YouTube unlisted)?
7. Paste the official rubric and submission requirements, or the page link, so the checklist uses the real wording.


## 9. Decided use cases and proposed cut (3 Nov, late)
Decided as video material: heroes A, B, C (section 2) and the use cases below. The order and the exact cut are still the owner's call.
| Beat | Use case | Built and verified |
|---|---|---|
| A | It finds a way (an app that will not run moves to the phone that works) | yes (QA run) |
| 1 | "Is this link safe?" (#1), APK check (#2), "Show me" (#11) | yes; the link check has a first raw take |
| 10 | A verification code from the owner's own phone, approved per code (#10) | server and card yes; the phone-side capture is unfinished |
| C | Approval pinned above the chat, the vault secret, nothing risky without a yes | yes |
| 12 | Another AI agent hires this one over A2A (#12): command in a terminal on the left, the owner's page on the right, verdict and report come back; Master recalls it | yes, run live (35 s) |
| - | Connectors (Fetch) and the Agents page, as a 10 s montage | yes |
Proposed timing for 3:00: A 0:40, link/APK/show-me 0:40, code relay 0:30, C 0:30, A2A 0:30, montage and close 0:10. Cut for time: #3, #4, #7, #8, #9 (not built).
Recording note for the A2A beat: the recorder is a headless Chrome, so the terminal is a styled panel fed by the real command's output (unbuffered, with its own timestamps), placed beside the app in one 1920x1080 frame.
