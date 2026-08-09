# Telethon Chat Photo Copier

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

`main.py` uses **936 total lines**. Of those, **785 are non-empty, non-comment lines**, measured from the committed file.

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

## Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`.
