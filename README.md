# Telethon Channel Photo Copier

A Telethon userbot that copies every photo from a selected source channel to a selected destination channel.

## Features

- Interactive numbered source and destination channel selection
- Copies photos from oldest to newest
- Preserves Telegram photo albums/batches
- Preserves the exact caption text, emoji, and Telegram formatting entities
- Preserves spoiler state on each photo
- Downloads media to temporary storage and removes it after each batch
- Creates a final index using the first line of every non-empty caption
- Makes each index title clickable and links it to the copied destination post
- Supports public and private destination post URLs
- Waits and retries automatically when Telegram applies a flood limit

## Code size

`main.py` uses **408 total lines**. Of those, **335 are non-empty, non-comment lines**, measured from the committed file.

## Requirements

- Python 3.10 or newer
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- Permission to post in the destination channel

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
4. Posts one or more index messages containing clickable first-caption lines.

Example index:

```text
real choclate
real dustbin
brush for sell
nft selling
```

Each line links directly to its copied destination post.

## Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`.
