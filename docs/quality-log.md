# Quality log: real-task testing on the emulator (28 Oct 2026)

Android 14 Play Store emulator, Muse Wonder as the worker (`reasoning_effort: minimal`). One run per task, so treat as first impressions.

| Task | Result | Steps | Notes |
|---|---|---|---|
| Chrome: search "current weather in Chennai", report it | Done | 7 | Handled Chrome welcome and notification prompts itself |
| Chrome: search USD to INR rate, report it | Done | 3 | Used `open_url` straight to the search |
| Contacts: create "Asha Raman" with a number | Done | 13 | Correct and confirmed on screen |
| Messages: send a text "running late" | Stopped and asked for approval | 1 | Correct: sending is consequential. (One earlier run opened Messages first; the model is not fully consistent.) |
| Settings: turn on Dark theme | Ran out of 14 steps (it did turn on) | 14 | 12 steps wasted flip-flopping on scroll direction |
| Calculator: compute 1234 x 56 | Failed: ran out of 14 steps | 14 | The app is not installed on this image; the model kept hunting |

## What the timing showed (per acting step, avg)
First measurement: 11.4 s = observe 2.8 s + model 3.0 s + act 0.9 s + settle 4.7 s. Settle was the biggest cost because it waited for two
pixel-identical screenshots and hit its 4 s timeout. After switching to a tolerant comparison that ignores the status and gesture bars (and
running the screenshot and UI dump in parallel) the average was about 7 s per step. `uiautomator dump` sometimes stalls for 25 to 36 s while
an app loads, so it is now capped on the device (hard kill after 6 s, one retry) and the model falls back to the screenshot alone.

## Fixes made from these findings
- New actions: `scroll` (direction means what you want to see, which models handle better than raw swipes), `find_app` (lists installed apps; tells the
  model when an app is not installed), `open_settings` (26 deep links, one step instead of a menu hunt). The prompt now says: use find_app before
  open_app; if the app is missing, ask about installing it or fail instead of hunting.
- Model client now retries dropped connections (`RemoteDisconnected`, resets, bad bodies), not just HTTP 429/5xx. A dropped connection used to fail the whole job.
- Test waits loosened: a CPU-starved machine made a few background-thread tests flaky.

## Re-verified after the fixes (light profile, 28 Oct 2026)
| Task | Before | After |
|---|---|---|
| Settings: turn on Dark theme | 14 steps, ran out of budget | 3 steps, 20 s, night mode confirmed on the device |
| Calculator (not installed) | 14 steps hunting for it | 2 steps, 16 s: `find_app` reports it is missing, then it asks whether to install one |
| Settings: storage used | not run | 2 steps, 14 s: "4.2 GB of 8.0 GB" |

## Compute: what the emulator costs and what we changed (28 Oct 2026, M-series Mac, same Chrome lookup task)
| Setup | Emulator CPU (avg / peak) | Avg step | Notes |
|---|---|---|---|
| 1080x2400, animations on, software GPU (swiftshader) | 160% / 491% | 8.0 s | Settle 2.2 s |
| 720x1600, animations off, software GPU | 181% / 492% | 7.1 s | Settle 1.2 s, screenshot 443 ms instead of 702 ms |
| 720x1600, animations off, Mac GPU (`-gpu host`) | 83% / 246% | 6.6 s | Screenshot 0.4 s, settle 1.0 s |

Software rendering was the CPU cost; the smaller screen and no animations mainly help latency. A long-running emulator can also run away (250-400% after 7 hours), so restart
it when the machine feels slow. The light profile is now the default: `scripts/emulator.sh` starts the emulator, and the agent applies the 720x1600 screen and no animations on
emulators (`PHONE_PROFILE=off` disables it). `wm size` overrides print an Override line; the size parser now uses it (a bug that would have scaled every tap wrongly).

Where a step still spends time: UI tree dump 2.4 to 2.9 s (starting `uiautomator` each time), model 2.5 s, settle about 1 s. The next big win is a resident accessibility helper
instead of `uiautomator dump`, then sending the screenshot only when needed.

## Resident screen reader (uiautomator2), 28 Oct 2026
`Device` now reads the UI tree and screenshots through uiautomator2's resident on-device service (bundled `u2.jar` and APK, nothing downloaded), and falls back to
plain ADB (`uiautomator dump`, `screencap`) if the service is unavailable or fails. Taps, swipes and typing stay on `adb shell input`, which measured faster
(tap 34 ms vs 150 ms, swipe 350 ms vs 1033 ms). `PHONE_READER=adb` forces the fallback.

| Microbenchmark, idle device | adb | uiautomator2 |
|---|---|---|
| UI tree | 1.04 s | 0.05 s |
| Screenshot | 122 ms | 31 to 49 ms |
| Tree and screenshot together | 140 ms (adb shot, u2 tree) | 68 ms |

| Live task (Chrome: population of Tokyo), same steps | adb reader | uiautomator2 |
|---|---|---|
| Avg step | 7.0 s | 4.1 to 4.2 s (about 40% faster) |
| Observe (tree + screenshot) | 2.88 s | 0.57 to 0.61 s |
| Model | 2.9 s | 2.1 to 2.2 s |
| Settle | 1.2 s | 0.7 s |

Notes: only one accessibility connection can exist at a time, so while the resident service runs the old `uiautomator dump` returns nothing; the fallback stops the service first
(tested). The model call is now the biggest piece of a step (2 to 3 s). The service uses little device memory (the 2.5 GB emulator still had about 1.1 GB free).
Adds Python dependencies: uiautomator2, adbutils, lxml, requests.

## Model cost per step: what we tried and found (28 Oct 2026)
Tool: `scripts/scenarios.py` runs 8 fixed tasks with ground-truth checks on the real phone state (dark theme, Android version, storage, screen timeout, Chrome lookup,
add a contact, missing app, send-a-message safety). Prices used for estimates: Muse Wonder $4.00 per 1M uncached input tokens, $0.20 cached, $20.00 output.

| Config | Pass | Steps | Time | Prompt tokens | Output tokens | Est. cost per set |
|---|---|---|---|---|---|---|
| Image every step, 540 px (start) | 7/8 (the miss was a bug in my checker) | 33 | 127 s | 71.5K | 2.4K | $0.24 |
| Adaptive (text-only most steps), 540 px | 8/8 | 43 | 171 s | 67.5K | 4.5K | $0.23 |
| Adaptive, 360 px | 7/8 | 40 | 136 s | 54.7K | 4.0K | $0.18 |
| Image every step, 360 px | 7/8 | 34 | 134 s | 52.1K | 4.2K | $0.18 |
| **Final defaults: image every step, 360 px, plus the fixes below** | **8/8** | **30** | **114 s** | **45.9K** | **2.2K** | **$0.13** |

Findings:
- A screenshot adds about 800 prompt tokens at 540 px (about 280 at 320 px) but model latency stayed 2 to 3 s per call at every size, including no image. The saving from smaller images is cost, not speed.
- Skipping screenshots (adaptive mode) did not pay off: more steps, no time saved, and a missed task. It stays available (`PHONE_IMAGE=adaptive`; it also re-checks the screen before accepting "done") but is not the default.
- Output tokens are priced at 5x input. A step writes about 70 output tokens; trimming the "thought" saves only 15 to 25 percent, and JSON-only mode did not stop the model adding a sentence before the JSON.
- The big wins came from fixing what made the agent waste steps, not from the model call:
  1. The keyboard's keys filled about 45 of 60 listed elements and pushed real fields off the list. Keyboard and status-bar nodes are now left out, with one line saying the keyboard is open.
  2. My `scroll` swipe started on top of the keyboard, which reads it as glide typing and inserts random words ("By", "VT"). When the keyboard is open, scrolling now stays above it. This turned a task that ran out of steps into 11 steps with a clean "done".
  3. Element state ([on], [off], [scrollable], [text field]) is now in the list, so the model reads switch states without a screenshot.
  4. Typing goes through uiautomator2 (0.19 s vs 0.71 s for 24 characters, handles Tamil and accents, no keyboard autocorrect); adb input text is the fallback.
- A checker bug of mine (wrong database column) made a passing task look like a failure; always check the checker against real data.

Settings: `PHONE_IMAGE` (always, adaptive, never) and `PHONE_IMAGE_WIDTH` (default 360). Run the set with `uv run python scripts/scenarios.py --label name`; results are saved to `runs/scenarios/`.
Cached input tokens grow as the same system prompt is reused (about half of prompt tokens in the final run were cached), which makes the real cost lower than the table suggests.

## Android intent fast paths (28 Oct 2026)
One validated `intent` action does in a single step what used to take many taps. Kinds: set_alarm, set_timer, dial, compose_sms, compose_email, insert_contact,
insert_event (calendar), maps_search, navigate, share_text, camera. Values are validated (hour and minute ranges, phone and email formats, lengths, the event time
is converted using the phone's own time zone) and shell-quoted, so text with quotes, `$()` or non-English characters reaches the app unchanged (tested through a real
shell split). `am start` reports errors in its output with exit code 0, so the output is checked.

Safety design: only `set_alarm` and `set_timer` complete on their own. `compose_sms`, `compose_email`, `insert_contact` and `insert_event` only prefill a screen: the
worker still taps Send or Save and must `ask` the owner before anything is sent. `dial` opens the dialer and never places the call. The result text tells the model which
case it is.

Live results (same phone, defaults 360 px, image every step):
| Scenario | Without intents | With intents |
|---|---|---|
| Add a contact (name + number, save) | 11 steps, 46 s (earlier runs ran out of steps) | 3 to 5 steps, 18 to 19 s |
| Set an alarm (verified from `dumpsys alarm`: 05:17) | not measured | 2 steps, 7 s |
| Open the dialer with a number, no call | not measured | 2 steps, 6 s |
| Directions in Maps (first-run screens needed UI taps) | not measured | 6 steps, 20 to 25 s |
| Send a text (must stop for approval) | stopped and asked | still stops and asks |

Full set of 11 scenarios: 11/11 pass, 36 steps, 141.5 s. On the original 8: 26 steps and 103 s, against 30 steps and 114 s before. The system prompt grew by a few hundred tokens
(cached after the first call), so prompt tokens per step are slightly higher.

Not verified: timers (the Clock app keeps only its nearest timer as a system alarm, so a new timer cannot be told apart), calendar events (the Calendar app shows a first-run
screen before it can save, and no account is signed in), and email (no mail account). The intents open the right forms; the worker handles the rest through the UI.
