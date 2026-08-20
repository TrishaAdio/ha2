# Telethon Chat Tools

Three Telethon userbot scripts:

- **`main.py`** copies every photo from one chat to another and posts a linked index, and also runs a [command mode](#commands-mainpy-command-mode) with `.change` and `.clone`. Channels, supergroups, forum groups, and legacy basic groups all work on both sides.
- **`oho.py`** clones a whole source chat into many freshly created groups, using several backup accounts that share the work. See [oho.py](#ohopy-multi-account-group-cloner).
- **`reindex.py`** adds the styled caption index to groups that already hold the copied posts, which is what `oho.py` leaves behind. See [reindex.py](#reindexpy-add-the-index-to-finished-groups).

# main.py: Chat Photo Copier

A Telethon userbot that copies every photo from a selected source chat to a selected destination chat. Broadcast channels, supergroups, forum groups, and legacy basic groups are all supported on both sides.

## Features

- Interactive numbered source and destination selection covering channels, supergroups, broadcast groups, and basic groups
- Labels every choice with its chat type and public username so channels and groups are easy to tell apart
- Lists only destinations the account can actually post photos in, using per-chat-type permission rules
- Optional forum topic selection: read from one source topic and publish into one destination topic
- Colored Colorama terminal UI with an ASCII startup banner and status display
- Copies photos from oldest to newest
- Preserves Telegram photo albums/batches
- Preserves the exact caption text, emoji, and Telegram formatting entities
- Preserves spoiler state on each photo
- Downloads media to temporary storage and removes it after each batch
- Creates a final index using the first line of every non-empty caption
- Removes ordinary caption emoji from generated index titles only
- Appends ` | Demo` to every generated index title
- Sends the supplied sticker immediately before the final index
- Formats the index as a blockquote with the supplied custom emoji
- Makes every index title bold, underlined, and linked to its copied post
- Copies direct text replies to source photos with their exact text and formatting
- Keeps copied text messages attached as replies to the matching destination photos
- Supports public, private, and forum-topic post URLs
- Waits and retries automatically on flood limits and on group slow mode
- Falls back to individual posts when slow mode forbids albums
- Keeps going when the index sticker or Premium custom emoji is unavailable

## Group support notes

- Supergroups, broadcast groups, forum groups, and legacy basic groups appear alongside channels in both menus.
- Posting rights are resolved per chat type: broadcast channels and broadcast groups need the post-messages admin right, while groups honour your personal restrictions, your admin rights, and the group's default permissions.
- In a forum group you are asked which topic to use. Choosing a source topic copies only that topic's photos and replies; choosing a destination topic publishes the photos, the sticker, and the index inside it.
- Legacy basic groups have no per-message permalinks, so index titles stay bold and underlined but are not clickable. The run reports this at the end.
- Slow mode is handled: the script waits out each interval, and because Telegram rejects albums under slow mode, those batches are published one photo at a time.

## Modes

`main.py` starts by asking what to do:

1. **Copy photos between two chats** — the interactive copier described above.
2. **Command mode** — stays connected and listens for `.change` and `.clone`.

Set `MAIN_MODE=copy` or `MAIN_MODE=commands` in `.env` to skip the question.

# Commands (`main.py` command mode)

In command mode the script listens for dot commands typed **from your own account** in any chat. Nobody else can trigger them. The command message itself is replaced with live progress, so chats stay clean.

```text
.change @newname            replace every username in every post
.change @oldname @newname   replace only that username
.clone [title]              clone this chat into a private channel
.help                       show the list
```

## `.change`

Run it in a channel to rewrite usernames across the whole history. With one argument every `@mention` becomes the new name; with two, only the named username is touched, which is the safer form.

What it rewrites:

- `@mentions` in message text and in media captions.
- Bare `t.me/username` links, keeping any `https://` scheme and trailing slash.
- Hyperlink targets that point at a bare `t.me/username`, even when the visible text needs no change.

What it deliberately leaves alone:

- Invite links (`t.me/+hash`, `t.me/joinchat/...`) and private post links (`t.me/c/...`).
- Public **post** links like `t.me/oldchan/45`, because rewriting the name would repoint a specific post at a different channel.
- Email addresses, and reserved `t.me` paths such as `addstickers` or `boost`.

**Formatting is preserved.** Replacing a username changes the text length, which shifts every entity after it. Offsets are recomputed in UTF-16 units, which is what Telegram counts — with emoji in a message those differ from Python string indices, so getting this wrong silently moves bold and links onto the wrong words. Telegram re-detects mentions, URLs and hashtags itself, so stale copies of those are dropped rather than remapped; real styling (bold, italic, underline, strike, spoiler, code, hyperlinks, custom emoji, blockquote) is remapped and kept.

Editing is rate limited, so the run paces itself and waits out flood limits. Posts it cannot edit are counted and reported rather than aborting the run: in channels an admin with edit rights can edit any post at any time, but in ordinary groups Telegram only allows editing your own messages for 48 hours.

## `.clone`

Run it in a channel or group to copy the whole chat into a **fresh private channel** owned by you. An optional title overrides the default, which reuses the source title.

- Every post is copied oldest first: text, photos, albums, documents, captions, formatting entities and spoilers.
- Replies are remapped, so a reply in the clone points at the copy of the message it originally answered.
- Media is reused by file reference, so nothing is downloaded in the normal case. Expired references fall back to a download and re-upload.
- Media Telegram cannot resend, such as stories, is skipped and counted.
- The invite link is sent to your **Saved Messages** together with the copy counts, and also printed in the terminal.

## Code size

`main.py` uses **1532 total lines**, of which **1291 are non-empty, non-comment lines**.

## Requirements

- Python 3.10 or newer
- Telethon 1.44 or newer
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- Permission to post in the destination chat
- A Telegram account allowed to send the supplied custom emoji (Telegram Premium may be required; without it the index is posted with the plain emoji instead)

## Installation

```bash
git clone https://github.com/TrishaAdio/ha2.git
cd ha2
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set your credentials in `.env`:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=channel_copier
```

## Usage

```bash
python main.py
```

On the first run, Telethon asks for your Telegram phone number, login code, and two-step verification password if enabled. The script then:

1. Shows available channels and groups and asks for the source.
2. Asks which source topic to read when the source is a forum group.
3. Shows chats where your account can post photos and asks for the destination.
4. Asks which destination topic to write into when the destination is a forum group.
5. Copies all source photos and albums with their captions, formatting, emoji, and spoilers.
6. Copies direct source text replies and replies them to the matching copied photos.
7. Sends the supplied sticker.
8. Posts one or more styled index messages containing clickable first-caption lines.

Example index appearance:

```text
🟢 real choclate | Demo
🟢 real dustbin | Demo
🟢 brush for sell | Demo
🟢 nft selling | Demo
```

The entire list is a blockquote. Ordinary emoji found in source caption titles are excluded from this index, while the original copied captions remain unchanged. The green marker uses custom emoji ID `6298751564592973547`; each title and its ` | Demo` suffix are bold, underlined, and link directly to the copied destination post.

## Known limitations

- The index sticker is referenced by a fixed file reference. Telegram expires those over time, so the sticker may be skipped with a warning while the index itself is still posted.
- The forum topic pickers show the 100 most recent topics.
- Only plain-text replies are copied. Replies carrying their own media are skipped.

> The index described above is a `main.py` feature. `oho.py` copies content but does not post an index, so groups it creates start without one. Use [reindex.py](#reindexpy-add-the-index-to-finished-groups) to add it afterwards.

# oho.py: Multi-Account Group Cloner

Creates many groups across several accounts and clones an entire source chat into each one.

## What it does per group

The requested group total is divided between the backup accounts, so 6 groups over 3 accounts means 2 groups each. Every group then goes through the same nine stages:

1. The owner account creates a supergroup. With no public link it starts **private**.
2. The main account joins through a single-use invite link.
3. The owner promotes the main account to **anonymous admin**, so its posts are signed by the group instead of the account.
4. The main account copies **every message** from the source: text, photos, albums, videos, documents, stickers, captions, formatting entities, spoilers, and reply threading.
5. The main account leaves the group.
6. The owner deletes every service message, so the create, join, and leave notices leave no trace.
7. The owner promotes itself to **anonymous admin**.
8. The owner claims a public link, which makes the group **public**, then enables **join approval**.
9. The finished link is sent to the managing admin, `@siyorou` by default.

Every stage is logged with timing to the terminal in color and to `oho.log` in full. A per-group summary is printed at the end, and a group that fails records the stage it stopped at without stopping the rest of the run.

## Ordering that Telegram forces

- `channels.toggleJoinRequest` fails with `CHAT_PUBLIC_REQUIRED` unless the group already has a public link, so the username is always claimed first and join approval second.
- The link is shared after the group is public, so `@siyorou` receives the final public link rather than a private invite link.
- Telegram [clears every privilege when an owner edits its own admin entry](https://bugs.telegram.org/c/2599), so the owner's full rights set is always resent together with the anonymous flag.
- Anonymous admins are hidden from the member list, so the main account is located and promoted before it becomes anonymous.
- Per Telegram's [rights documentation](https://core.telegram.org/api/rights), enabling `anonymous` automatically sets the admin's `send_as` to the group, so no explicit sender needs to be passed when posting.

## Accounts

`oho.py` uses one main account and one to three backup accounts:

- `MAIN_SESSION` reads the source and posts into every group.
- `OWNER_SESSIONS` lists the backup accounts that own the groups. Leave it unset to be asked how many to use.

Each session logs in interactively on first use, asking for the phone number, login code, and two-step password. Sessions are reused afterwards. The script refuses to run if two sessions turn out to be the same account.

## Usage

```bash
python oho.py
```

You are asked for the group total, a title base, and a public-link prefix, then shown the full plan for confirmation before anything is created. Nothing is created until you confirm.

Titles become `Archive 1`, `Archive 2`, and so on; links become `t.me/archive1`, `t.me/archive2`, and so on. Taken links automatically fall back to a suffixed candidate.

## Reliability

- Flood waits, premium flood waits, and group slow-mode waits are waited out; transient server errors are retried with a growing delay.
- Media is resent by file reference, so nothing is downloaded in the normal case. Expired references are refreshed from the source, then re-uploaded as a last resort.
- Albums rejected under slow mode are sent one photo at a time.
- Media Telegram cannot resend, such as stories and giveaways, is skipped and counted rather than failing the group.
- If the owner cannot message the sharing admin, the main account tries instead, and the link is always logged either way.

## Pacing

`MESSAGE_DELAY` (default `1.0`) and `GROUP_DELAY` (default `15.0`) throttle the run. Raise them for large sources or fresh accounts. `COPY_LIMIT` caps the messages per group and is useful for a trial run.

## Code size

`oho.py` uses **1256 total lines**, of which **1042 are non-empty, non-comment lines**.

## Limitations

- Creating many groups or claiming many public links from one account can hit Telegram's own limits, such as `CHANNELS_ADMIN_PUBLIC_TOO_MUCH`. The failing group reports the error and the run continues.
- A source with content protection enabled may refuse to hand over its media. The run warns up front and reports whatever it has to skip.
- Fresh or spam-limited accounts may be unable to create groups or message the sharing admin at all.

# reindex.py: Add the Index to Finished Groups

`oho.py` copies posts but never posts an index, so groups it built have all the content and no index. `reindex.py` fixes that after the fact. It reads the posts already sitting in each group, builds the index from their captions, and posts it with links that point at those copies.

## Preview first

The default run posts nothing. It scans every matching group and prints what it would do, including the resolved link for each entry:

```text
  Demos 1 (t.me/XCRYPTO1)
    5 media post(s), 4 index entr(y/ies), 1 message(s) to post
    🟢 real choclate | Demo  ->  https://t.me/XCRYPTO1/10
    🟢 real dustbin | Demo   ->  https://t.me/XCRYPTO1/12
```

Open one of those links to confirm it lands on the right post, then answer `y` to post. Answering anything else exits without touching the groups.

## Links

Links use the group's public link when it has one, so `https://t.me/XCRYPTO1/10` rather than the internal `https://t.me/c/<numeric id>/10` form that only resolves for members. Groups still private fall back to the `t.me/c/` form.

For an album, the entry links to the item that actually carries the caption, not the first item.

## Which groups

Groups are selected by prefix, and only groups the signed-in account created are considered:

- **Group title starts with** — matches `Demos 1`, `Demos 2`, and so on.
- **Public link starts with** — matches `t.me/XCRYPTO1`, `t.me/XCRYPTO2`, and so on.

Either match is enough. Anything else the account owns is left alone.

## Re-running

Re-running replaces the previous index rather than stacking another copy underneath. The old index sticker and index messages are detected (no media, wrapped in a blockquote, carrying the marker emoji and the title suffix), deleted, then the fresh index is posted. Old index messages are never mistaken for content.

## What gets indexed

Only posts carrying media with a non-empty caption. Plain text posts are not captions, so they are skipped, as are captions that reduce to nothing once emoji are removed. The title is the first caption line that still has text after emoji are stripped.

## Usage

```bash
python reindex.py
```

Credentials and `OWNER_SESSIONS` are read from `.env`, the same values `oho.py` uses. The accounts must be the ones that own the groups, since the main account has already left them.

# Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`.
