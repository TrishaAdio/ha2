# Telethon Chat Tools

Two Telethon userbot scripts:

- **`main.py`** copies every photo from one chat to another and posts a linked index. Channels, supergroups, forum groups, and legacy basic groups all work on both sides.
- **`oho.py`** clones a whole source chat into many freshly created groups, using several backup accounts that share the work. See [oho.py](#ohopy-multi-account-group-cloner).

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

## Code size

`main.py` uses **936 total lines**, of which **785 are non-empty, non-comment lines**.

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

# Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`.
