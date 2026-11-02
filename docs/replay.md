# Replay site

Judges cannot run our phones, so the recorded sessions play back in a browser: the chat on the left, the phone screen on the right, step by step with the
original timing. It is one static page (`web/replay.html`) plus session folders (`web/replay/<name>/`). No server, no phone, no model calls, no keys; the page's
Content-Security-Policy only allows its own files.

- Landing list: `replay.html` (reads `replay/index.json`). One session: `replay.html?s=<name>`.
- Controls: Play/Pause, Restart, a scrub bar, speed 1x/2x/4x. Keys: Space, ←/→ (5 s), 1 2 4, R.
- Works at 390 px (the phone panel moves above the chat) and in light or dark theme.
- A synthetic session `demo` ships in the repo (made-up app and screens, no real data). It is also what the tests export.

## Make a session (after a rehearsal you like)

1. Find the bot and the message range in the app (message ids are in the API: `curl localhost:8000/api/bots/<bot_id>/state`).
2. Export from a **copy** of the database (the exporter copies it itself, the live file is never written):

```bash
.venv/bin/python scripts/export_replay.py <bot_id> --out web/replay/hero-a \
    --title "Hero A: it finds a way" --from-msg 120 --to-msg 168 \
    --mask "<your first name>" --mask "<your Google address>" \
    --redact 0,0,1,0.04            # black out the status bar (time, notification icons) on every screenshot
# add --public-images only for pictures that are safe to show; otherwise they become a grey placeholder
```

3. Check everything under `web/replay/` before committing (the Pages workflow runs the same check and fails on a problem):

```bash
.venv/bin/python scripts/export_replay.py --check web/replay --mask "<your first name>" --mask "<your Google address>"
```

4. Watch it: `python3 -m http.server 8765 --directory web` then open `http://127.0.0.1:8765/replay.html?s=hero-a` (the page needs http, not `file://`).
5. Rebuild the synthetic demo any time with `.venv/bin/python scripts/export_replay.py --demo --out web/replay/demo`.

## What the exporter changes

- Text is masked: email addresses, IPv4/IPv6 addresses, long tokens and `dev_` device tokens, `Bearer` values, pairing codes, file paths (`/Users/...`, `/tmp/...`), phone-like numbers,
  URL query strings, and anything you pass with `--mask`. Cards answered with a secret or a one-time code show "(hidden)".
- Screenshots are re-encoded as JPEG about 360 px wide (quality 60), about 6-25 KB each. A 60-step session is roughly 1 MB.
- Idle gaps longer than 8 s are shortened (`--max-gap`), so a replay never sits still. Event order and the order of steps are kept.
- Captions are plain words ("tap “Wi-Fi”"): element ids are turned into the label the model saw. The step thought is kept (masked).
- The vault is never in the database in clear text, so nothing from it can appear. Saved-secret typing is shown as "(approved by the owner, never shown)".

## Checklist for choosing a session

- [ ] It tells one story from request to result in under about 90 s of replay (use `--from-msg/--to-msg`; the page can be played at 4x).
- [ ] Chat only, no real contacts, no personal photos (`--public-images` off), no banking or account screens.
- [ ] The status bar, notification shade and any account chip are redacted (look at three screenshots in the replay).
- [ ] The `--check` command says OK, and you read the whole `session.json` once for names and places that the masker cannot know (`--mask` them and export again).
- [ ] Clash of Clans or any other game that bans bots: only the open-and-refuse beats are exported, never gameplay (there is none to export; the agent does not play).
- [ ] The page title and the first message make sense to someone who has never seen the app.

## Publish (GitHub Pages)

`.github/workflows/pages.yml` publishes only `web/replay.html` (as the site's `index.html`) and `web/replay/`. The app, the code and `data/` are not published.
**One-time owner step:** repository Settings > Pages > Build and deployment > Source = "GitHub Actions". The site is then at
`https://<owner>.github.io/<repo>/` and a session at `.../?s=hero-a`. Put that link in the README (`REPLAY_URL`) and in the submission form.
The repo can stay private until submission only if your GitHub plan allows Pages for private repositories; otherwise make the repo public first.

## Limits

- Screenshots are small on purpose, so text on the phone is hard to read; the thought and the caption under the phone carry the story.
- A session records what the database and the run folders kept: runs whose `data/runs/<job>/` folder was deleted show the steps without pictures.
- Live control (Take control) and the live view are not part of a replay.
