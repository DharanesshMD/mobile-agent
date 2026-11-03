# Log files

Everything the running app does that you may need later is written to `data/logs/` (change with `LOG_DIR`; level with `LOG_LEVEL=DEBUG|INFO|WARNING`, default INFO).
The folder is owner-only (0700), the files 0600, size-rotated at 10 MB with 5 older copies each. `data/` is git-ignored.

| File | What is in it |
|---|---|
| `app.log` | Everything except HTTP requests: start-up, phone power changes, model calls (model, endpoint, seconds, retries), turns, jobs, connectors, A2A, warnings from libraries |
| `errors.log` | WARNING and up with full tracebacks, including crashes of any thread and exceptions nobody caught. Open this first when something broke |
| `access.log` | One line per HTTP request on every door (`via=main`, `via=gateway`, `via=a2a`): method, path, status, ms, bytes, caller address, request id. The page's constant polling is DEBUG, so it is hidden unless `LOG_LEVEL=DEBUG` |
| `jobs.log` | Phone jobs and the orchestrator's turns: queued, started, each worker step (the kind of action only), finished with status and result, what the model decided and how long it took |
| `a2a.log` | The A2A door as JSON lines: `{"ts", "level", "client", "task", "method", "ok", "detail", "msg"}` for every call and refusal |
| `audit.log` | What the owner changed (bots, vault deletions, connectors, A2A callers and tokens, the phone door, stopped jobs) and the door opening or closing |

A line: `2026-11-03 15:39:24.941 INFO  agent.jobs [bot=481efe519d] queued job=47be091821 lane=shared goal=...`. The part in brackets carries the bot, job, task, client or request id of the work that wrote it, so one request or one job can be followed across files (`grep req=8209f667 data/logs/*.log`).

## What is never written
Message text, prompts, model replies, screenshots, file contents, typed text, pairing codes, tokens and keys, vault values. Goals are cut at 120 characters. As the last step before a line reaches a file, a redaction pass hides bearer tokens, `token=`/`key=`/`code=` values, known token shapes (`a2a_`, `dev_`, `sk-`, `ghp_` ...), long random strings, card and ID numbers, "password is ..." phrases, and turns e-mail addresses into `d***@host`. So a call site that forgets still cannot leak. The pairing code is printed to the console only.

## Reading them
- Files: `tail -f data/logs/errors.log`.
- The owner's API (local only): `GET /api/logs` lists the files with sizes; `GET /api/logs/{name}?lines=200&level=warning&q=job` returns the newest records (a traceback stays with its record), already redacted.
- Tests: `tests/test_logs.py` (routing, redaction, rotation, crash capture, request ids, no message text in any file).

## Limits
Logs are local files, not shipped anywhere. A crash of the whole process (power loss, `kill -9`) can lose the last lines. The shared phone's own system log (adb logcat) and the emulator and VM output stay where they were (`data/emulator.log`, `data/virtual-android.log`).
