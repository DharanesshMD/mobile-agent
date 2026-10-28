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

## Not yet re-verified
The dark-theme, calculator and storage tasks with the new actions. A re-run was blocked by the host: the Mac was swapping about 10.7 of 12.3 GB, the
emulator held 250 to 400 percent CPU, and a UI dump or settle step sometimes took tens of seconds or longer. Free memory before the next run
(for example quit Docker Desktop, which is not needed now) and re-run the scenario set.
