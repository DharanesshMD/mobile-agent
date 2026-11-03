# OTP relay: a code that was texted to your phone, used on a phone (3 Nov 2026)

Use case: a task on a phone reaches a sign-in or sign-up that sends a one-time code by SMS to **your paired phone**. Instead of reading it out and typing it, the Mobile Agent app on your phone finds the code, and you approve that one code with a click.

## Flow
1. The phone worker reaches a code screen and asks for a code (`request_otp`). The usual card appears in the chat: "Gmail sent a one-time code. Enter it here." You can always type the digits, as before.
2. If one of your paired phones is connected (and it is not the phone the task runs on), the server asks it to **watch** for a code for 2 minutes (`otp_watch`). The card says "Watching Pixel for a code…". If no phone is connected the card says "Connect your phone to fetch codes automatically" and nothing else changes.
3. The phone finds a code in a notification and returns it (`otp_poll`). The card now shows only a **masked hint** (`••••13`), where it came from ("Messages · HDFCBK on Pixel") and how long ago, with **Use this code**. A newer code replaces an older one.
4. You press **Use this code**. The server takes the code from memory, types it on the phone exactly like a typed code, forgets it and tells the phone to wipe its copy (`otp_stop`). The card records "Code from Pixel sent to the phone".
5. The watch ends when the card is answered, the task is stopped or times out, or the window closes (one extension to 3 minutes while you are still waiting). A found code stays usable for 3 minutes, then it is dropped.

## Ops contract with the app (over the existing phone link)
| Op | Arguments | Answer |
|---|---|---|
| `otp_watch` | `seconds` 10..180 | `{enabled, watching, reason: "" \| "off" \| "no_accessibility" \| "notifications_blocked"}` |
| `otp_poll` | none | `{watching, remaining_s, redacted?, codes: [{id, code, source, title, at_ms}]}` newest first, at most 5, not yet acknowledged |
| `otp_ack` | `id` | `{ok}` |
| `otp_stop` | none | `{ok}`, wipes the phone's memory of codes |
The app captures only while a watch is open and only if you switched on "Let tasks fetch verification codes from this phone". The server accepts only a `code` of 4 to 8 digits and never reads anything else from a notification.

## Card data (what the page sees)
`meta.relay = {state: watching | found | off | none | unavailable | redacted | offline | expired, phone, hint, source, title, age_s, at, reason}`. `hint` is bullets plus the last two digits. `source` and `title` come from the phone and have any digit run of 4 or more removed. The card's `relay` entry is removed when the card is answered. Answering with `{approved: true, use_relay: true}` uses the code; the API never returns it (409 "no code from your phone yet" when there is none).

## Privacy and safety rules
- Per-code approval: nothing is ever typed without your click on that code. There is no auto-approve.
- The code lives only in the server's memory (never the database, a message, a job result, a hub event, an API answer, a log line or a model prompt) until used, replaced, expired or the card is gone. Tests assert this.
- At most one watch per card and 3 watches an hour per phone. A watch never starts for a task that runs on that same phone (its own screen already receives the SMS).
- Audit lines (no code): `otp relay started`, `otp code approved`, `otp relay ended: <reason>`.

## Limits (honest)
- The phone must be connected, the app's switch on and Accessibility enabled. Otherwise the card says why and you type the code.
- A phone can hide message text: notifications set to "hide sensitive content" on the lock screen, Xiaomi's "Hide notification content", and Android 15's redaction of one-time codes for apps that read notifications. The card then says "Your phone hid the message text. Open Messages on it, or type the code here".
- It reads notifications, not the SMS inbox (the app holds no SMS permission, which Google Play Protect would block for a sideloaded app).
- Status: the server and page side are built and tested with a scripted phone. The app side was verified on virtual phones only; no real SMS was relayed end to end here.
