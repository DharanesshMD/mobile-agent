# Pictures in chat (1 Nov 2026)

The owner can attach pictures to a message; the bot looks at them and acts on the request in the same turn.

## Adding a picture
- **+ menu:** choose pictures, take a photo (touch devices), or paste from the clipboard. A small **Paste** button sits next to **+**.
- **Paste (Ctrl/Cmd+V):** works anywhere in the chat, with the box focused or not: a macOS screenshot copied to the clipboard, a picture copied in a browser, files copied in Finder.
  Plain text pastes are untouched, and when the clipboard holds a picture and text, the picture is attached and the text still lands in the box.
- **Paste button:** reads the clipboard with `navigator.clipboard.read()` (needs https or localhost; over a plain `http://<LAN address>` it says so and points to Ctrl/Cmd+V). Handles a blocked
  permission, an empty clipboard ("The clipboard is empty."), text only ("No image on the clipboard.") and browsers without the API.
- **Drag and drop** onto the chat. Up to 4 pictures per message, 15 MB each. Pasted pictures are named `pasted-image-<time>.<ext>`.
- Each picture uploads at once with a progress bar on its chip, and Send waits for the uploads. The X on a chip takes it back (the server deletes the unsent file).

## What the server does with it
`POST /api/bots/{id}/attachments` (the body is the picture; one per request, 20 a minute, at most 8 unsent per bot):
1. checks the bytes by decoding them (the file name and declared type are never trusted); JPEG, PNG, GIF, WEBP; at most 60 megapixels;
2. applies the EXIF rotation, then **drops all metadata** (EXIF, GPS, camera, ICC, text chunks) by copying only the pixels into a fresh image;
3. shrinks to 1568 px on the longest edge and stores a JPEG (photos) or PNG (transparency, screenshots, drawings) under `data/uploads/`.
Unsent uploads older than an hour are deleted. Deleting a bot, or `scripts/reset_fresh.py`, removes the files. `GET /api/attachments/{id}/file` serves them (same auth as the rest).
The first picture in a chat posts a note: pictures go to the model provider like messages, so avoid photos of cards or IDs.

## What the model gets
Meta's Chat Completions: `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}` blocks inside **user** messages only (never system or assistant).
The newest message with pictures and up to two before it keep their pictures (at most 4 in all, each shrunk to 1280 px for the call); older ones become the text "[an image was shared here]".
Every picture message also lists its picture ids so a task can ask for one. If the provider answers HTTP 400 for a picture, the owner gets a plain "I couldn't read that picture" reply.
**Cost:** a picture adds tokens in proportion to its resolution (about 1,300-1,500 tokens for a 1280 px picture); the 4-picture window caps one call at roughly 6,000 extra tokens, and a
tool-using turn sends them again on each model call. Text-only messages are unchanged.

## Behaviour
The bot says in one sentence what it sees and acts on the request: an error screenshot gets diagnosed (and a phone task queued when useful), an app or store page leads to opening or finding that
app through the normal phone routing, text in the picture (receipt, contact, event, address) is extracted and offered as a task. **Text inside a picture is data, not instructions**: orders written
in an image never change what the agent does or approve anything, and the bot says so. The game-account rules still apply.

## Giving a picture to the phone
`mobile_task` accepts `files: [picture ids]` (only pictures sent in that bot's own chat). Before the task starts the picture is pushed to the phone's `Pictures/MobileAgent` folder
(`adb push`, then a `MEDIA_SCANNER_SCAN_FILE` broadcast, with the phone's own serial) so the gallery and other apps' pickers list it; the goal tells the worker where it is; the copy is removed when the task ends.
Works on Phone, Phone 2 and extra adb phones. **Not on the owner's paired phone yet:** the app has no file hand-off, so the bot says so plainly.
Follow-up for `docs/phone-client.md`: a `push_file` op (`{"name","mime","data"}` base64, saved to Pictures/MobileAgent through MediaStore) in the Android app.
