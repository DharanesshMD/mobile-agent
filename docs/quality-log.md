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
