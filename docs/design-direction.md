# Design direction for the redesign (phase 1: audit and direction, 2 Nov 2026)

> **Status, 2 Nov 2026:** the owner picked Direction A (Switchboard). It is built: see [design-result.md](design-result.md) for what shipped, the screenshots, the checklist result and the known gaps. The light theme and the patch-line loop were dropped on purpose.

Method, steps 1 to 4: look at the real screens, audit against `signs.md`, decide how much to change, write the Direction, stop.
Nothing in `web/` or `agent/` was changed. No image was generated. The skill was cloned to the scratchpad only and not installed.

Screens judged (real app at http://localhost:8000, dark theme; the browser pane scales its screenshots to 800 px wide, so fine detail was read from the CSS as well):
`/private/tmp/claude-501/-Users-dharun-Personal-Projects/5a4fd9ad-aea1-4fb8-8ca6-7e677feb10a0/scratchpad/design/`
- `01-desktop-master-chat.jpg` Master chat, long thread, Details tab (empty routines)
- `02-desktop-games-phone-mobile-landscape.jpg` Phone 2 live view in landscape (taken earlier in the session, before the frame fix)
- `03-desktop-mobile-tab-asleep.jpg`, `10-desktop-games-phone-asleep.jpg` the asleep cards and the paired-phone card
- `04-desktop-mobile-tab-live-emulator.jpg` live emulator view (portrait thumbnail)
- `05-desktop-assistant-report-cards.jpg` Home assistant chat with two report cards and the partial-success reply
- `06-desktop-library.jpg`, `07-desktop-report-opened.jpg` Library and an opened report page
- `08-desktop-vault.jpg` Vault tab
- `09-desktop-games-bot-first-run.jpg` a first-run-like chat (Phone 2's introduction)
- `11-phone390-master-chat.jpg`, `12-phone390-details-panel.jpg`, `13-phone390-mobile-tab-asleep-and-own-phone-card.jpg`, `14-phone390-pairing-card.jpg` at 390x844
Not captured, on purpose: the composer with image chips (it needs a real upload, which changes data) and an approval card (it needs a running job). Both were judged from the CSS and the earlier QA screenshots. `web/replay.html` is not served by the service (only by Pages), so it was judged from its source: it copies the same `:root` tokens.

## 1. Audit

Verdict: **light slop in the craft, heavy template look in the identity.** The CSS is already free of the loud signs (no hue gradients, no glows, no `transition: all`, no emoji, no gradient text). What is wrong is that nobody made a decision: it is a bots-list + chat + side-panel app that could be any chatbot, and the one thing only this product has, several real phones doing work, is the smallest thing on screen. Per the skill's template check (swap the name, does it still fit? yes) this needs a real direction, not just a polish pass.

| # | Sign (signs.md group) | Where it lives | Severity |
|---|---|---|---|
| 1 | Template look: pure chat-app layout, system font, navy-grey ramp and a cobalt accent chosen by nobody | whole app, `:root` tokens, `font: 15px system-ui` | High |
| 2 | The product moment is buried: the phones are a 230 px thumbnail (`.thumb`, max-width 230) at the bottom of a 340 px panel; the centre is walls of text | Mobile tab, 01/04 | High |
| 3 | Violet as a second accent: Master avatar `#8b7cf6`, tag text `#a89bff`, replay banner `#1a1830`/`#cfc9ff`, plus a sparkle glyph (✦) as the Master's icon | `.botrow .tag`, `avatar()`, `replay.html .banner` | Medium |
| 4 | Six random avatar colours (`COLORS` in `agent/db.py`) so nothing leads and the list is confetti | bot list, headers | Medium |
| 5 | Filled cobalt for the user bubble and every primary button: colour spread over chrome | `button.primary`, user messages | Medium |
| 6 | Uppercase tracked labels in 6 places: `dialog .sect`, `.card .k`, `.lib h4`, `.vault h4`, `.botrow .tag`, the report type label "REPORT" | CSS lines 59, 124, 173, 181, 182, 193 | Medium |
| 7 | Off-scale type: 12 sizes including 10, 11, 11.5, 12.5, 13.5; weights 600, 650 and 700 in 15 places | CSS | Medium |
| 8 | Off-grid spacing: 22 paddings and gaps use 3, 5, 6, 7, 9, 10, 13, 14, 15 px | CSS | Medium |
| 9 | Radius mix with no nesting rule: 4, 8, 9, 10, 12, 14, 16, 18, 22, 999, 50% | CSS | Low |
| 10 | Copy: report cards read "Report ready: Report: 1) Open Calculator app and compute..." (title = the raw goal, "Report ready" twice) and "1 pictures"; the Library repeats the same long titles; worker notes are dumped as long grey dashed blocks in the chat | `reportsave.py`, cards, `bot_note` | High for the video |
| 11 | No product mark or name on screen: "Mobile Agent" only exists in the tab title; the bot list header says "Bots" | sidebar header | Medium |
| 12 | Hard-coded dark colours that ignore the light theme (`#2a1519` toast, `#1a1830` banner, `#ffffff14` hovers) | CSS | Low (dark only for the demo) |
| 13 | Tiny status chips: the phone state is a small box ("Asleep", "Ready") under the thumbnail, no lane or queue information | `.chip`, 13 | Medium |
| 14 | Motion is mostly honest (pulse on a working dot, typing dots, a progress shimmer) but the pulse is also used for "waiting" and there is no entrance or move that explains routing | `@keyframes pulse/slide/bounce` | Low |
| 15 | Empty states are explanations, not objects: "No routines yet. Ask the bot in chat, for example..." as grey centred text | Details tab | Low |

What is already good and stays: the quiet hairlines, the honest copy (no hype words, no exclamation marks), the real mock data, the focus rings, the 4.5:1 text, the landscape handling.

## 2. Three candidate directions

All three use the same product line: **Mobile Agent hands tasks to real Android phones, in the background, and tells you what it did.** All are dark, quiet in the chrome, with one bright element, no generated images (an app screen needs none), nothing that needs a build step.

### A. Switchboard (the phones as lit devices on a bench)
- **Product:** a chat where you hand work to your phones (Phone, Phone 2, Phone 3) and watch each one do it.
- **Character:** exact, calm, tactile.
- **World:** a telephone exchange and an operator's bench at night: lines, lamps, patch cords.
- **Moments:** (1) the phone rack: each phone is a rendered device with its live screen, a lane lamp and the current step in one mono line; (2) a patch line that draws from the chat's task card to the phone doing it and re-routes when the agent moves an app to another phone.
- **Signature:** the lane lamp, a small amber dot that is lit while a lane is working, used on bot rows, device frames and task cards and nowhere else, with the lane name in mono ("emulator", "games", "my phone").
- **Palette (ink ramp, from tokens.md):** deepest `#07080d`, page `#0b0d14`, card `#11141c`, raised `#1a1e28`, hairline `#eef0f5` at 8 %, text `#eef0f5` / `#a3a9b8` / `#7a8296`. Accent amber `#f0a43a` (lamp, selection, one key figure; dark text `#1a1206` on it, contrast 9:1). Positive `#4cc38a`, negative `#e5484d`. Primary button: off-white `#eef0f5` with `#0b0d14` text.
- **Type:** Geist (interface) + Geist Mono (lane names, counters, step lines, timestamps). SIL OFL, vendored as two woff2 files in `web/fonts/` with `@font-face` and a system fallback, no external request (the replay page's CSP needs `font-src 'self'`).
- **Values:** spacing 4, 8, 12, 16, 20, 24, 32; radius device 28, card 14, row/input 10, button 8, keycap/tag 6; weights 400 and 500; scale 12 / 13 / 14 / 15 / 18 / 28; one curve `cubic-bezier(0.22, 1, 0.36, 1)`, 150 ms feedback, 200 ms menus, 320 ms entrances.
- **Wireframe (1440x900):**
```
+--------------+------------------------------------+-----------------------------------------+
| [3 phones]   | Master                             |  THE BENCH                               |
| Mobile Agent |                                    |  +------+   +-----------+   +------+      |
|              |  you: open clash of clans          |  | emul |   | games     |   | mine |      |
| Master    *  |  agent: crashes on emulator (3/3)  |  | live |   | live      |   | off  |      |
| Phone 2      |    moving it to games...           |  | 9:20 |   | 16:9      |   |      |      |
| Home assist  |  [ task card ]---- patch line ---->|  +------+   +-----------+   +------+      |
| My phone     |  [ approval slip, amber edge ]     |  * working   * idle   o not connected     |
|              |  [ report: plates 2x3 ]            |  step 6  tap "Enter the Dungeon"          |
| ------------ |                                    |  ---------------------------------------- |
| Owner        | [ composer ]                       |  Details  Library  Vault  (quiet tabs)    |
+--------------+------------------------------------+-----------------------------------------+
```

### B. Black box (every task leaves a contact sheet)
- **Product:** a chat for handing tasks to phones, where every task keeps a frame-by-frame record you can trust.
- **Character:** forensic, honest, quiet.
- **World:** a flight recorder and a darkroom contact sheet.
- **Moments:** the contact sheet: while a task runs, its step screenshots stack into a filmstrip under the task card, with a frame counter in mono ("07 / 23"), and the finished strip becomes the report.
- **Signature:** the frame counter and a thin safelight rule that marks the frame being recorded.
- **Palette (warm ramp):** deepest `#12110f`, page `#161513`, card `#1c1a18`, raised `#262421`, text `#efe9de` / `#aaa398` / `#8a847a`. Accent safelight red-orange `#e8603c` (dark text `#190a05`, about 5.7:1). Positive `#4cc38a`, negative `#e5484d`.
- **Type:** Instrument Sans + IBM Plex Mono (both OFL, vendored).
- **Values:** same grid and radii as A, device frames replaced by 4:5 plates with radius 10.
- **Wireframe:** the same three columns, but the right panel is a vertical filmstrip of the current task and the phones are small tiles inside the task card.

### C. Dispatch ledger (reports as printed slips)
- **Product:** an assistant that works on your phones and writes up what happened, like a dispatcher's ledger.
- **Character:** calm, literate, a little warm.
- **World:** a ledger and carbon-copy dispatch slips on paper.
- **Moments:** reports typeset as slips with screenshot plates and a verdict stamp ("Looks harmful", "Nothing found in these checks").
- **Signature:** the verdict stamp, one quiet outlined label used on every result.
- **Palette (warm ramp, light first option):** page `#f7f4ee`, card `#fdfbf7`, raised `#efe9df`, text `#1f1b16` / `#5e554a` / `#7b7266`; accent moss `#5f7f3b` (white text on it, about 4.6:1).
- **Type:** Newsreader for slip headlines only, Instrument Sans for the interface, Plex Mono for figures.
- **Risk:** a light theme is the opposite of what the video's screens use today, every colour in the replay page and the phone views changes, and it fights the dark device screenshots.

## 3. Recommendation: A, Switchboard

Why it reads in a 3-minute recording at 1080p:
- **The eye lands on the phones.** Today the three phones are the least visible thing on screen. At 1920x1080 the bench takes the right 40 % (about 760 px): three device frames side by side (emulator portrait about 200x440, games phone landscape about 360x204, your phone about 200x430) with their live screens at a size where a viewer can read what the phone is doing, not a 230 px thumbnail.
- **The story is visible without narration.** Hero A (the app crashes on Phone, moves to Phone 2): the Phone lamp goes amber, a mono counter ticks 1/3, 2/3, 3/3, the patch line redraws to the games device, that lamp lights and the game's own screen appears there. Hero B (picture + voice): image chips in the composer, then an approval slip with an amber edge, one primary button. Hero C: a routine message arrives first, the vault slip shows a label and never a value.
- **Approval cards become slips:** the one place the chat uses amber; because amber appears nowhere else in the chrome, a viewer knows instantly "this needs me".
- **Reports become plate grids:** 2x3 screenshot plates with a one-line caption and the verdict label, replacing the raw-goal titles and the grey dashed blocks, so the report reads as a finished thing in 3 seconds.
- **It passes the template check:** swap in another product's name and the bench, lane lamps and patch line no longer fit.
- **It is buildable without a build step:** the devices are CSS (a bezel with a 1 px highlight, an inset screen showing the existing `<img>`), the lamp is a dot, the patch line is one SVG path drawn with `stroke-dashoffset`. No images, no libraries.

Concrete copy and detail fixes inside A: report title becomes a short label ("Calculator, alarm, Android version"), "Report ready: ..." goes, "1 pictures" becomes "1 picture"; worker notes collapse into one muted mono line with a disclosure; bot tags (MASTER, PHONE, GAMES) become quiet lowercase mono text; the Master's ✦ becomes a flat glyph, the logo is the wordmark "Mobile Agent" plus a flat mark of three rounded rectangles (two portrait, one landscape) in one solid colour; user messages become a raised neutral surface (no filled blue); bot messages sit on the page without a bubble; avatars become one neutral disc with the initial, the lane lamp carries the status; the primary button is off-white.

Optional, not needed for the app: a README banner prompt for an image tool (ask before spending): "Overhead photo of three Android phones lying in a row on a dark matte workbench, screens lit and showing different apps, a soft warm lamp from the left, shallow depth of field, background colour #0b0d14, calm empty space across the top third, no text, no cables".

## 4. Passes, effort and what must not change

Estimate for `web/index.html` (1121 lines, about 820 of them script) with vanilla JS and no build step: **about 6 to 7 hours**; cut the patch-line animation and the light theme to stay at 6.

| Pass | Work | Time |
|---|---|---|
| 1 Spacing | everything onto the 4 px grid, one row height, sidebar rows 36 px | 45 min |
| 2 Color | the ink ramp as `:root` tokens, three text roles, amber accent, remove violet and the six avatar colours, neutral primary buttons | 30 min |
| 3 Icons | one inline line-icon set (lucide paths, 16 px, stroke 1.5) replacing the 7 ad-hoc svgs and the ✦ | 30 min |
| 4 Copy | report titles, card text, tags, empty states, no trailing periods on labels | 30 min |
| 5 Type | Geist + Geist Mono vendored, scale and weights, sentence-case labels, tabular numbers | 30 min |
| 6 Layout | the bench column replaces the 230 px thumbnail: three device frames from the `/api/mobile/state` `phones`/`lanes` data and `?phone=` screenshots; chat unchanged in width | 120 min |
| 7 Details | nested radii, quiet tags, plate-grid reports, collapsed worker notes, the Library list | 60 min |
| 8 Visuals | the device frames and the lane lamp in code (no generated images) | in pass 6 |
| 9 Motion | one curve, lamp fade, card entrance, patch line (optional) | 30 min |
| 390 px layout | the bench becomes a horizontal swipe of device cards over the chat | 45 min |

**Must not change:**
- Every element id the script uses: `apkPicker app attMenu bots camera chips composer feed form hAv hName hSub lbImg lightbox menuBtn miApk miCamera miPaste miPick micBtn newBtn ovAsleep ovBar ovClose ovHint ovImg ovScreen overlay pAv pName panelBtn pasteBtn phoneChip picker plusBtn scrim searchBox searchBtn searchInput send sideClose tab text token`, and the ids created in script (`sleepNote stateChip thumbImg`).
- The classes the script toggles: `landscape`, `sel`, `on`, `show-side`, `control`, `off`, and the `.thumb` frame being sized from the image's own shape (the cropping fix from today).
- Every API path and query (`/api/bots...`, `/api/mobile/state|wake|sleep|control?phone=`, `/api/device/screenshot|tap|key|type|swipe|rotate|touch?phone=`, `/api/pair/code`, `/api/transcribe`, `/api/vault...`) and the touch WebSocket.
- Accessibility: focus rings, aria-labels, keyboard paths (paste, Esc to close, Take control), 4.5:1 text, no colour-only state (the lamp always has a text label).
- `web/replay.html` has a copy of the same tokens and a CSP of `default-src 'none'`; either update both files or move the tokens to a shared `web/tokens.css` and add `style-src 'self'` and `font-src 'self'` to the replay CSP.
- Light theme: keep the `prefers-color-scheme` block working, or drop it on purpose (recommended for the demo: dark only; say so in the README).

