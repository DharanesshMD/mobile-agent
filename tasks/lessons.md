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
