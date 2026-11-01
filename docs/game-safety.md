# Game account safety (31 Oct - 1 Nov 2026)

The owner's rule: **users' accounts must not be banned.** Some publishers ban accounts they identify as bots, and an agent that taps through a game is a bot. So the agent
never plays those games, on any phone, however it is asked. Everything that is not gameplay still works.

## What is blocked, and why (public rules)
| Publisher | Apps | Rule it breaks | Source |
|---|---|---|---|
| Supercell | `com.supercell.*`: Clash of Clans, Clash Royale, Brawl Stars, Hay Day, Boom Beach, Squad Busters | Terms of Service 1.1 "Use Limitations" forbids "cheats, exploits, automation software, emulators, bots, hacks, mods"; 1.2 lets Supercell limit, suspend or terminate accounts | https://supercell.com/en/terms-of-service/ |
| Niantic (now run by Scopely) | `com.nianticlabs.*`, `com.nianticproject.*`: Pokemon GO, Monster Hunter Now, Ingress | Pokemon GO Terms 3.1 (unauthorized access, unofficial third-party software), 3.5 (suspension or termination), 6 (no bots or automation) | https://explore.scopely.com/terms/pokemongo/en |

Supercell's own text says accounts "may" be terminated; their fair-play posts say bot accounts get banned permanently. Pokemon GO's terms do not name emulators. We take the
conservative reading.

## What the agent does and does not do
- **Never:** tap, scroll, swipe, type, or press a key other than back/home in one of those apps (code-enforced in the worker, `agent/graph.py`, on every phone: the emulator,
  the games phone, extra adb phones and the real phone's app). It also never types a saved secret there.
- **Still does:** open the app, wait, take screenshots, press back/home, install it, move it between phones, check that it stays up, and tell you what the screen shows (launch,
  age or login screen).
- **How it is enforced:** each screen read also reads the app in front (in parallel, no extra step). Before an input action the worker looks at that verdict; a refused input
  is answered with the reason and the way out ("finish with done and describe the screen"); after three refusals the task ends with "Not played, to protect your account: ...".
  It holds for tasks you ask for, routines, steering notes, and the escalation chain.
- **Asked to play it:** the bot says so plainly in two sentences (the publisher bans bot accounts), lists what it CAN do, and points you to **Take control**: you can drive the
  games phone yourself (Mobile tab > Open > Take control, with tap, scroll, typing and keys), and the agent's lane for that phone pauses while you do.
- **Other games:** the worker warns once before it plays any other game ("Automating a game can get your account banned by its publisher. Continue?", a confirm card) and plays
  only if you approve. Other publishers may ban bots too; the built-in list holds only the ones whose public rules we read.

## Cloud-gaming websites do not get around it
Clash of Clans is also streamed in a browser by third-party sites (CloudMoon, EasyFun.gg, YaksGames and similar). When a browser (Chrome, Firefox, Brave, Edge, Samsung
Internet, Opera, Vivaldi, the WebView browser, ...) is in front, the guard reads the address bar and the page text it already has:
- the address must be a cloud-gaming site AND the page must be about a blocked game (its address, or a play/stream/queue page); then taps, scrolls, typing and other keys are refused
  exactly as in the app. Back, home, opening another page (`open_url`) and closing or opening a tab (the tab buttons) stay allowed, and so does reading the screen;
- a full-screen stream hides the address bar: the last verdict stays until the browser shows another address;
- **not** blocked: a search page that lists those sites, a Wikipedia or news article, the publisher's own site, a cloud-gaming homepage that merely lists many games;
- a request to *play* a blocked game "in the browser" or "on a cloud site" is refused before any task exists, with the same honest answer.

## Third-party sites never get your login
On a third-party cloud/service site (the built-in list, plus every site you opened from an "other ways" card) the worker refuses `type` and `type_secret`: no logins, codes, search
text or saved passwords. A sign-in is always your own handoff. (Official sites keep the normal rule: a saved secret is typed only with your approval each time.)

## Looking for other ways (web research, no key needed)
When the escalation chain ends without a phone that can run the app, or when you ask "find another way to do/play X", the agent researches (`agent/websearch.py`, `agent/options.py`):
1. up to 3 searches: the Brave API when `SEARCH_API_KEY` is set, otherwise the model provider's own web search (the same one the chat uses: no scraping), otherwise DuckDuckGo's plain HTML page (10 s apart, clear User-Agent). From this Mac DuckDuckGo answers with a captcha; that is never worked around: the card then says the search did not work and shows only the official routes. A service the owner names (EasyFun, CloudMoon, YaksGames, now.gg) is also read at its own address, with its terms and privacy pages. A query never carries your data: e-mail
   addresses, long numbers, tokens and anything that looks like a card, ID or password are stripped, and a request that contains one is not searched at all.
2. it reads the top pages (public https only, no cookies, no JavaScript, size-capped, never a private address) and the obvious terms or privacy page of the unofficial candidates;
3. a model picks at most 4 options from the results; the risks come from CODE: a risk tag (third-party login, data shared with marketing partners, no liability for bans, time limits
   or cost, needs Windows, the terms mention bots) appears only with the sentence it was found in, and what was not found is listed as "not found in what I read";
4. official routes come first (the store listing, the publisher's page, Google Play Games on PC for Windows) and every third-party site is marked UNOFFICIAL;
5. a card in the asking bot's chat. **Nothing runs until you pick.** A pick only shows the link, or opens it in the browser of one of your phones and gives you a handoff card
   ("Sign in yourself, then tell me when you're done"). The agent never signs in, plays or pays on a third-party service for you.

## Extend the list
Create `data/policy.json` (not tracked by git): `{"blocked": ["com.example.game"]}`. A package, or a prefix of one, is added to the blocked apps. The built-in entries cannot be removed from
that file. `data/third_party_sites.json` is written by the app (hosts you opened from an options card).

## Honest limits
- A publisher can ban for reasons we cannot see (an emulator, a changed device, an unusual network). Blocking automation removes the main one; it is not a guarantee.
- The guard reads the screen's package and text. A game inside an unknown app, a web game in an unlisted browser, or a stream with no readable address bar and no earlier verdict
  could pass; add it to `data/policy.json` if you know of one.
- The owner can play anything by hand with Take control: that is the owner, not an agent.
