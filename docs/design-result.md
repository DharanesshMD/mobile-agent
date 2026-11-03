# Design result: Direction A, "Switchboard" (2 Nov 2026)

Built from [design-direction.md](design-direction.md): the nine passes in order, then the checklist.
Files: `web/index.html` (the app), `web/replay.html` (same tokens and look), `web/fonts/` (Geist and Geist Mono, SIL OFL, with the licence file).

## Before and after
Screenshots are in the scratchpad (the browser pane scales them to 800 px wide); the ones used in the README are copied to `docs/img/`.
Before: `/private/tmp/claude-501/-Users-dharun-Personal-Projects/5a4fd9ad-aea1-4fb8-8ca6-7e677feb10a0/scratchpad/design/` (`01` to `14`).
After: `.../scratchpad/design/after/`.

| Screen | Before | After |
|---|---|---|
| Master chat, first view (1440x900) | `01-desktop-master-chat.jpg`: a bots list, a wall of text, the phone as a 230 px thumbnail under the tabs | `after/01-1440-master-bench-first.jpg`: the bench is the first thing in the right column, the live emulator in a lit device |
| A task running and waiting for approval | `05-desktop-assistant-report-cards.jpg` (no running state on screen) | `after/02-1440-hero-patchline-approval-slip.jpg` and `after/02b-zoom-approval-slip.png`: the patch line from the task to the emulator, the hollow amber lamp, the amber approval slip |
| Two phones working at once (1920x1080) | not possible: one phone per tab | `after/07-1920-hero-two-phones-working.jpg`: the emulator in portrait and Phone 2 in landscape, both lamps lit, the patch line to Phone 2 |
| First run | `09-desktop-games-bot-first-run.jpg`: a grey centred explanation | `after/03-1440-first-run-empty.jpg`: the bot's purpose, one lit lamp, three things to try, one click each |
| Pairing a phone, Vault | `13`, `14`, `08` | `after/04-1440-pair-slot-and-vault.jpg`: a dashed "Not paired" device and the pairing box on the bench |
| Phone width (390x844) | `11`, `12`, `13`, `14` | `after/06-390-chat.jpg` and `after/05-390-bench-drawer.jpg` |
| Replay page | an unstyled copy of the old tokens | `after/08-1440-replay.jpg` (same tokens, device frame follows the picture) |
| Reports | `05`, `07`: "Report ready: Report: 1) Open Calculator…", "1 pictures" | cards with a verdict stamp, a short title and the captured screens as plates (visible in `after/02`, `after/03`) |

## What changed, per pass
1. **Spacing.** Everything on the 4 px scale (4, 8, 12, 16, 20, 24, 32). One bot row height (48), one composer, one card padding (16). The scan found 0 off-grid values after the fixes except borders, the tap ripple centring and the tab underline overlap.
2. **Color.** One ink ramp (`#07080d`, `#0b0d14`, `#11141c`, `#1a1e28`), three text roles (`#eef0f5`, `#a3a9b8`, `#868ea1`), one accent: amber, used for two things only, a lane that is working and something that needs you.
   The violet accent, the cobalt user bubble, the six random avatar colours and the hard-coded dark colours are gone. Primary buttons are plain white on ink; only the approval slip's button is amber.
3. **Icons.** One inline line-icon set (16 px grid, 1.5 stroke, round caps) replaces the seven ad-hoc SVGs, the sparkle glyph and the `&#8981;`/`&#10148;` characters. One calm identity per bot kind: an icon tile for Master, Phone 2 and your phone, a letter for the rest.
4. **Copy.** Report cards read as a kind label (mono), a verdict stamp, a short title, plates and the picture count ("6 pictures", "1 picture"). `agent/report.py` now writes short titles at the source ("Step-by-step guide: Open Google Keep and write a note", "Report: Set an alarm for 6:45 (Nov 3) + 2 more"); the page also shortens old stored titles. Worker notes are one muted line with a disclosure. Lower-case mono tags (`master`, `games`, `phone`). Short strings have no trailing period; no exclamation marks.
5. **Type.** Geist for the interface, Geist Mono for lane names, counters, step lines and timestamps (variable woff2, local). Scale 12, 13, 14, 15, 18, 28; weights 400 and 500; sentence-case labels; no uppercase tracked labels; no negative tracking on small text; tabular figures.
6. **Layout.** The right sidebar follows the earlier Grok-style design: a close button, a big round avatar and the bot's name, pill tabs (Details, Library, Vault, Mobile) and one large rounded card. The Mobile tab shows the selected bot's phone only (a live screen, or its state: asleep, waking, off, connected, not connected); phones that are working elsewhere appear as small lamp chips under the card, and the second phone stays out of sight until it runs. Nothing about phone state sits above the chat. The chat column is a 720 px reading column; below 1100 px the sidebar opens as a drawer.
7. **Details.** Nested radii (device 28 = 8 bezel + 20 screen; card 14; row/input 10; button 8; tag 6), amber approval slips, quiet tabs with an underline, hairline borders only, one focus ring (2 px, text colour).
8. **Visuals.** The devices and the lamps are CSS, the patch line is one SVG path. No generated images, no gradients, no glows, no coloured shadows.
9. **Motion.** One easing curve (`cubic-bezier(0.22, 1, 0.36, 1)`), 150 ms feedback, a slow opacity fade on a working lane's lamp (the only loop), no bounce, no animated layout properties. `prefers-reduced-motion` turns every animation and transition off.

## What was deliberately not changed
- Every id, class hook and data attribute the script uses; every API path and query; the touch WebSocket; owner control (Take control, I'm done, Esc); the picture chips, paste, drag and drop, the clipboard button and the APK check; the pairing flow; routines, Vault, Library; the replay player's CSP (`default-src 'none'`, now with `font-src 'self'`) and its safe-name checks.
- The code between `const MAX_PICS` and `function addPictures`, and the clipboard helpers, which the node tests read by marker.
- The server: only three small changes outside `web/` (below).

## Changes outside `web/`
- `agent/service.py`: a `GET /fonts/{name}` route that serves the two font files (an allow-list of two names) so nothing is loaded from outside.
- `agent/report.py`: `title_for` writes a short human title (`short_subject`, `_strip_filler`). `agent/reportsave.py`: "1 picture" / "N pictures" in the chat text.
- `tests/test_cloudtools.py`: the title test updated for the new titles. `.github/workflows/pages.yml`: the replay site publishes `web/fonts` next to the replay page.
- `README.md` (screenshots), `NOTICE` (Geist), `docs/img/` (six screenshots).

## Performance
- `web/index.html`: 118.6 KB raw, 33.9 KB gzip (was 93.2 KB). Fonts: 140.8 KB once, cached for a day. No other asset, no external request (checked in the page).
- Polling: the bench asks `/api/mobile/state` for each phone that has a lifecycle every 2.5 s and fetches a screenshot every 2.5 s only for a phone that is working (9 s when idle, never for a sleeping or off phone). Polling stops while the tab is hidden.
- No layout thrash: devices are created once and updated in place, the list is re-ordered only when it changes, the patch line is drawn in one `requestAnimationFrame` after the writes, and the report plates are read from the report page once per session and cached.

## Accessibility
- Contrast (WCAG AA is 4.5): text 14.6 to 17.5, secondary text 7.1 to 8.5, tertiary text 5.1 to 6.1, amber 8.0 to 9.6, red 5.5 to 6.6, green 7.5 to 9.0, dark text on amber 8.9.
- Focus ring on every control, every icon-only control has an `aria-label` (checked in the page: 0 unnamed), no state is shown by colour alone (the lamp always has its text: "working", "waiting for you", "idle", "asleep", "off"), keyboard paths unchanged (Esc, Enter to send, the paste shortcut).
- Works at 390, 1440 and 1920 px (screenshots above), no sideways scroll at 390.

## Design checklist (compared with the audit in design-direction.md)
Audit items 1 to 15 are addressed: the template look (the bench, the lamps and the patch line only fit this product: swap the name and they stop making sense), the buried phones (now the hero), violet and avatar colours, cobalt chrome, uppercase tracked labels, off-scale type and spacing, the radius mix, the report copy, the missing brand mark (a three-lamp mark and the name), the hard-coded colours, the tiny state chip, the motion, and the empty states.
Checklist: direction written and followed (yes); 4 px scale (yes); aligned columns (yes); one ramp, three text roles, one accent (yes); no gradients, glows or coloured shadows (the only shadows are black drop shadows under devices and menus; lamps are solid or hollow, not haloed); no emoji (the one remaining camera character, `PIC_ONLY`, is a protocol marker the page hides, not shown); one icon set (yes); type and weights (yes); copy (yes); tabular numbers (yes); nested radii (yes); a real hero (yes); no dead band (yes); motion (one slow lamp fade, nothing else loops); template check (passes).

## Known gaps
- Dark only. The light theme was dropped on purpose for the demo (the README says so); `prefers-color-scheme: light` is not handled.
- A paired phone is a tile, not a screen: the app does not stream the display. The bench shows it as connected or not, with a lamp for its bot's work.
- The bench of lit devices and the patch line were removed in the sidebar restyle; phone state lives in the sidebar only.
- A report card reads the report page once to show up to four plates (the page can be a few hundred KB). Long chats with many reports fetch each report once.
- The old per-bot "Mobile" tab was replaced by the bench; its code path is still in the script but no button opens it.
