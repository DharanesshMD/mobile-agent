# Lessons

Patterns from corrections, so the same mistake is not made twice. Review at the start of a session.

## The chat history must tell the truth
The orchestrator answers from the chat history. Whenever a job ends in a way the chat did not record (a restart, a crash, a failed wake), post a message. A history that ends with "On it, installing X" for a dead job makes the model say "already on it" and do nothing. (Fixed 29 Oct: restart recovery posts a message and the context lists recent job outcomes.)

## Do not restart the dev server while the owner's job is running
Check `/api/mobile/state` first. A restart marks running jobs failed and the owner loses their task. If a restart is needed, say so.

## Reproduce UI problems with real events in the browser pane
Typing bugs came from `on*` handlers returning false and from re-rendering inputs. Check by typing with real key events, not by reading the code.

## Diagnose from the device before giving general advice
For a phone error, read logcat and test the network from the Mac before answering. The Google sign-in failure was a firewall re-signing play.google.com, found in the log (`Trust anchor for certification path not found`).

## Production servers: show, then change, then verify
List what will change and what stays, keep anything that serves a live domain, stop rather than delete, and re-check the sites afterwards. Ask before touching anything the owner may not know is live.

## Every "waiting" state needs a timeout
One job waiting for the owner held the only phone for 17 hours and queued everything behind it. A shared resource must never be held by something that waits on a person: time it out, release the resource, and say so in the chat. Same for owner control of the phone.

## A reply that promises an action must take it
The master said "I'll disconnect the sign-in" and queued nothing. In the same turn either queue the task or say plainly it cannot be done. And never trust a worker's "done": check the fact on the phone (`agent/checks.py`) before reporting success or running a dependent task.

## A step budget must not count waiting
Polling a slow download one short wait at a time used all 25 steps at 9%. Make waiting a single long, cancellable action that is free in the budget, and bound the task by wall-clock time instead. Same for anything that retries: cap automatic retries in code (not only in the prompt), or a failed task loops and burns model calls.

## Chat text is not a card answer
The owner typed "done" in chat while the task waited on a handoff card, and the bot said "continuing" while nothing resumed. Whatever the owner can say in chat that means "go on" must call the same code path as the card, for handoffs only, never for approvals.

## 9. Apps can refuse to run on the emulator: detect the crash, stop, say so
Clash of Clans reads emulator properties at start-up and kills itself ("keeps stopping"). The worker saw only a splash screen and waited for a village that never came, 14+ steps, then the orchestrator retried.
- Read Android's crash log every step (`logcat -b crash`), count crashes per package, tell the model, stop at CRASH_LIMIT with a plain reason.
- A crash-stopped task is never retried automatically, and the reason is remembered in the bot's memory.

## 10. Restarting the server by hand: same PATH, one process
I restarted the service from a shell without `adb` on PATH: the phone looked "booting" forever and jobs failed with "did not wake up". `pgrep` also returned two pids and `kill "a\nb"` killed neither, so two services fought over one emulator.
- Start it with `export PATH=/opt/homebrew/share/android-commandlinetools/platform-tools:$PATH`, and check `which adb` first.
- Kill by pid list (`pkill -f`), then `pgrep` again to confirm exactly one process before starting.

## 11. The project's server is the one in .claude/launch.json (port 8000): never start a second copy
The Claude app starts `mobile-agent` (port 8000, adb on PATH) from `/Users/dharun/Personal/Projects/.claude/launch.json`. I had also started my own copy on 8001, so two services shared one SQLite db and one emulator.
- Before starting a server: `pgrep -fl "agent.service"` and `lsof -iTCP -sTCP:LISTEN | grep python`. If one exists, restart that one (kill its pid, then preview_start `mobile-agent`), do not add another.

## 12. Route by capability; never offer an action the phone cannot do
The Games phone bot asked the owner to approve "install it from the Play Store?" on a phone with no Play Store; the owner approved, it failed, and nothing else was tried. The worker prompt told every phone to offer a Play install, and the bot did not know what its phone could not do.
- Every bot's context carries a phone directory (agent/phones.py): what each phone can and cannot do, its state, its Google account fact, and apps known not to run there (mined from crash-stopped jobs). A phone's bot is told what its own phone cannot do and may not offer or promise it.
- The worker prompt and Device carry `has_play`; a phone without Play refuses Play links and `com.android.vending` with a message the model can act on, and its prompt says to `fail` when an app is not installed.
- When a task Master handed over fails, Master gets a turn to try another phone (at most 2 reroutes per request, never the same goal on a phone that already tried it), then explains honestly. Check where an app is installed with `phone_check` + `on` before routing.
- Share what a phone needs without copying secrets: apps move as APK files (`move_app`, never data); a Google sign-in is never copied, it is a sign-in task on that phone's bot with the vault's `type_secret`. A phone with no Google services is told so, not offered a sign-in.

