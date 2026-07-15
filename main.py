"""Interactively copy every photo from one Telegram channel to another.

Captions, Telegram formatting entities, emoji, albums, and media spoilers are
preserved. After copying, the script posts a linked index made from the first
line of every non-empty caption.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import tempfile
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils
from telethon.tl.custom import Dialog, Message


@dataclass(frozen=True)
class IndexEntry:
    """One caption title and the URL of its copied destination post."""

    title: str
    url: str


def read_credentials() -> tuple[int, str, str]:
    """Read Telegram API credentials from .env or prompt for missing values."""
    load_dotenv()

    raw_api_id = os.getenv("TELEGRAM_API_ID") or input("Telegram API ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or getpass.getpass(
        "Telegram API hash: "
    ).strip()
    session = os.getenv("TELEGRAM_SESSION", "channel_copier").strip()

    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc

    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")
    if not session:
        raise ValueError("TELEGRAM_SESSION cannot be empty.")

    return api_id, api_hash, session


async def get_broadcast_channels(client: TelegramClient) -> list[Dialog]:
    """Return broadcast-channel dialogs visible to the signed-in account."""
    channels: list[Dialog] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, types.Channel) and bool(entity.broadcast):
            channels.append(dialog)
    return channels


def can_post(channel: types.Channel) -> bool:
    """Return whether the current account can publish in a channel."""
    if channel.creator:
        return True
    rights = channel.admin_rights
    return bool(rights and rights.post_messages)


def channel_label(dialog: Dialog) -> str:
    """Build a readable label for an interactive channel choice."""
    username = getattr(dialog.entity, "username", None)
    suffix = f" (@{username})" if username else " (private)"
    return f"{dialog.name}{suffix}"


def choose_channel(prompt: str, dialogs: Sequence[Dialog]) -> Dialog:
    """Show numbered channels and return the selected dialog."""
    if not dialogs:
        raise RuntimeError("No eligible channels were found for this account.")

    print(f"\n{prompt}")
    for number, dialog in enumerate(dialogs, start=1):
        print(f"  {number:>3}. {channel_label(dialog)}")

    while True:
        raw_choice = input("Choose a number: ").strip()
        try:
            choice = int(raw_choice)
        except ValueError:
            print("Enter one of the numbers shown above.")
            continue

        if 1 <= choice <= len(dialogs):
            return dialogs[choice - 1]
        print("Enter one of the numbers shown above.")


async def iter_photo_groups(
    client: TelegramClient, source: types.Channel
) -> AsyncIterator[list[Message]]:
    """Yield source photos chronologically, retaining Telegram album groups."""
    pending_album: list[Message] = []
    pending_group_id: int | None = None

    async for message in client.iter_messages(
        source,
        filter=types.InputMessagesFilterPhotos,
        reverse=True,
    ):
        if not message.photo:
            continue

        if message.grouped_id is None:
            if pending_album:
                yield pending_album
                pending_album = []
                pending_group_id = None
            yield [message]
            continue

        if pending_album and message.grouped_id != pending_group_id:
            yield pending_album
            pending_album = []

        pending_group_id = message.grouped_id
        pending_album.append(message)

    if pending_album:
        yield pending_album


def has_media_spoiler(message: Message) -> bool:
    """Return whether Telegram marks a photo itself as spoilered."""
    return bool(getattr(message.media, "spoiler", False))


async def download_group(
    client: TelegramClient, messages: Sequence[Message], directory: Path
) -> list[Path]:
    """Download a photo or album to uniquely named temporary JPEG files."""
    paths: list[Path] = []
    for position, message in enumerate(messages, start=1):
        requested_path = directory / f"{position:02d}_{message.id}.jpg"
        downloaded = await client.download_media(message, file=str(requested_path))
        if not downloaded:
            raise RuntimeError(f"Telegram did not return photo {message.id}.")
        paths.append(Path(downloaded))
    return paths


async def send_single_photo(
    client: TelegramClient,
    destination: types.Channel,
    path: Path,
    source_message: Message,
) -> list[Message]:
    """Upload and publish one photo while preserving its caption and spoiler."""
    peer = await client.get_input_entity(destination)
    uploaded = await client.upload_file(str(path))
    media = types.InputMediaUploadedPhoto(
        file=uploaded,
        spoiler=has_media_spoiler(source_message),
    )
    request = functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=source_message.message or "",
        entities=list(source_message.entities or []),
    )
    result = await client(request)
    sent = client._get_response_message(request, result, peer)
    return [sent] if sent else []


async def send_photo_album(
    client: TelegramClient,
    destination: types.Channel,
    paths: Sequence[Path],
    source_messages: Sequence[Message],
) -> list[Message]:
    """Upload and publish an album with per-photo captions and spoilers."""
    peer = await client.get_input_entity(destination)
    input_media: list[types.InputSingleMedia] = []

    for path, source_message in zip(paths, source_messages, strict=True):
        uploaded = await client.upload_file(str(path))
        uploaded_photo = types.InputMediaUploadedPhoto(file=uploaded)
        uploaded_result = await client(
            functions.messages.UploadMediaRequest(peer=peer, media=uploaded_photo)
        )
        media = types.InputMediaPhoto(
            id=utils.get_input_photo(uploaded_result.photo),
            spoiler=has_media_spoiler(source_message),
        )
        input_media.append(
            types.InputSingleMedia(
                media=media,
                message=source_message.message or "",
                entities=list(source_message.entities or []),
            )
        )

    request = functions.messages.SendMultiMediaRequest(
        peer=peer,
        multi_media=input_media,
    )
    result = await client(request)
    random_ids = [item.random_id for item in input_media]
    sent = client._get_response_message(random_ids, result, peer)
    if not sent:
        return []
    return list(sent) if isinstance(sent, list) else [sent]


async def copy_photo_group(
    client: TelegramClient,
    destination: types.Channel,
    messages: Sequence[Message],
) -> list[Message]:
    """Download a source group temporarily and publish it at the destination."""
    with tempfile.TemporaryDirectory(prefix="telethon-photo-copy-") as temp_dir:
        paths = await download_group(client, messages, Path(temp_dir))
        if len(messages) == 1:
            return await send_single_photo(client, destination, paths[0], messages[0])
        return await send_photo_album(client, destination, paths, messages)


def caption_position(messages: Sequence[Message]) -> int | None:
    """Return the position of the first photo containing a non-empty caption."""
    for position, message in enumerate(messages):
        if message.message and message.message.strip():
            return position
    return None


def first_caption_line(caption: str) -> str | None:
    """Extract the literal first line of a caption for the final index."""
    lines = caption.splitlines()
    first_line = lines[0].strip() if lines else ""
    return first_line or None


def destination_post_url(channel: types.Channel, message_id: int) -> str:
    """Create a public or private Telegram post URL."""
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    return f"https://t.me/c/{channel.id}/{message_id}"


def utf16_length(value: str) -> int:
    """Measure text in Telegram's UTF-16 entity-offset units."""
    return len(value.encode("utf-16-le")) // 2


def make_index_messages(
    entries: Iterable[IndexEntry],
    max_units: int = 3900,
    max_links: int = 80,
) -> Iterable[tuple[str, list[types.MessageEntityTextUrl]]]:
    """Build safe-size index messages with clickable first-caption lines."""
    text = ""
    entities: list[types.MessageEntityTextUrl] = []

    for entry in entries:
        addition = f"{entry.title}\n"
        if entities and (
            utf16_length(text + addition) > max_units or len(entities) >= max_links
        ):
            yield text.rstrip("\n"), entities
            text = ""
            entities = []

        offset = utf16_length(text)
        entities.append(
            types.MessageEntityTextUrl(
                offset=offset,
                length=utf16_length(entry.title),
                url=entry.url,
            )
        )
        text += addition

    if entities:
        yield text.rstrip("\n"), entities


async def post_index(
    client: TelegramClient,
    destination: types.Channel,
    entries: Sequence[IndexEntry],
) -> int:
    """Post all linked index chunks and return the number of messages sent."""
    count = 0
    for text, entities in make_index_messages(entries):
        while True:
            try:
                await client.send_message(
                    destination,
                    text,
                    formatting_entities=entities,
                    link_preview=False,
                )
                break
            except errors.FloodWaitError as exc:
                wait_seconds = exc.seconds + 1
                print(f"  Telegram rate limit: waiting {wait_seconds} seconds...")
                await asyncio.sleep(wait_seconds)
        count += 1
    return count


async def run_copy(client: TelegramClient) -> None:
    """Run channel selection, photo copying, and index generation."""
    dialogs = await get_broadcast_channels(client)
    source_dialog = choose_channel("Source channel", dialogs)

    destination_dialogs = [
        dialog
        for dialog in dialogs
        if dialog.id != source_dialog.id and can_post(dialog.entity)
    ]
    destination_dialog = choose_channel("Destination channel", destination_dialogs)

    source = source_dialog.entity
    destination = destination_dialog.entity
    print(
        f"\nCopying photos from {source_dialog.name} to {destination_dialog.name}..."
    )

    index_entries: list[IndexEntry] = []
    copied_photos = 0
    copied_posts = 0
    failed_groups = 0
    group_number = 0

    async for source_messages in iter_photo_groups(client, source):
        group_number += 1
        source_ids = ", ".join(str(message.id) for message in source_messages)
        while True:
            try:
                sent_messages = await copy_photo_group(
                    client, destination, source_messages
                )
                break
            except errors.FloodWaitError as exc:
                wait_seconds = exc.seconds + 1
                print(f"  Telegram rate limit: waiting {wait_seconds} seconds...")
                await asyncio.sleep(wait_seconds)
            except Exception as exc:  # Keep a long copy moving after one failure.
                failed_groups += 1
                print(f"  Failed source message(s) {source_ids}: {exc}")
                sent_messages = []
                break

        if not sent_messages:
            continue

        copied_photos += len(source_messages)
        copied_posts += 1
        print(
            f"  Copied batch {group_number}: {len(source_messages)} photo(s) "
            f"from source message(s) {source_ids}"
        )

        position = caption_position(source_messages)
        if position is None or not sent_messages:
            continue

        title = first_caption_line(source_messages[position].message)
        destination_message = sent_messages[min(position, len(sent_messages) - 1)]
        if title and destination_message:
            index_entries.append(
                IndexEntry(
                    title=title,
                    url=destination_post_url(destination, destination_message.id),
                )
            )

    index_messages = await post_index(client, destination, index_entries)
    print("\nFinished.")
    print(f"  Photos copied: {copied_photos}")
    print(f"  Photo posts/albums copied: {copied_posts}")
    print(f"  Index entries: {len(index_entries)}")
    print(f"  Index messages posted: {index_messages}")
    print(f"  Failed batches: {failed_groups}")


async def main() -> None:
    """Sign in to Telegram and start the interactive copier."""
    api_id, api_hash, session = read_credentials()
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    try:
        await run_copy(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
