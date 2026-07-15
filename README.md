# Telethon Channel Photo Copier

A Telethon userbot that copies every photo from a selected source channel to a selected destination channel.

## Features

- Interactive numbered source and destination channel selection
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
- Supports public and private destination post URLs
- Waits and retries automatically when Telegram applies a flood limit

## Code size

`main.py` uses **597 total lines**. Of those, **501 are non-empty, non-comment lines**, measured from the committed file.

## Requirements

- Python 3.10 or newer
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- Permission to post in the destination channel
- A Telegram account allowed to send the supplied custom emoji (Telegram Premium may be required)

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

1. Shows available broadcast channels and asks for the source channel.
2. Shows channels where your account can post and asks for the destination channel.
3. Copies all source photos and albums with their captions, formatting, emoji, and spoilers.
4. Copies direct source-channel text replies and replies them to the matching copied photos.
5. Sends the supplied sticker.
6. Posts one or more styled index messages containing clickable first-caption lines.

Example index appearance:

```text
🟢 real choclate | Demo
🟢 real dustbin | Demo
🟢 brush for sell | Demo
🟢 nft selling | Demo
```

The entire list is a blockquote. Ordinary emoji found in source caption titles are excluded from this index, while the original copied captions remain unchanged. The green marker uses custom emoji ID `6298751564592973547`; each title and its ` | Demo` suffix are bold, underlined, and link directly to the copied destination post.

## Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`.
