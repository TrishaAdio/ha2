"""Interactively copy every photo from one Telegram chat to another.

Broadcast channels, supergroups, forum groups, and legacy basic groups all work
as sources and destinations. Captions, Telegram formatting entities, emoji,
albums, and media spoilers are preserved. After copying, the script posts a
linked index made from the first line of every non-empty caption.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import getpass
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, Union

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.tl.custom import Dialog, Message

if sys.version_info < (3, 10):
    # zip(..., strict=...) is used throughout; on 3.9 it raises TypeError deep
    # inside a run, which looks like a Telegram problem rather than a version one.
    raise SystemExit(
        "This tool needs Python 3.10 or newer "
        f"(running {sys.version_info.major}.{sys.version_info.minor})."
    )

ChatEntity = Union[types.Channel, types.Chat]
Result = TypeVar("Result")

INDEX_CUSTOM_EMOJI = "🟢"
INDEX_CUSTOM_EMOJI_ID = 6298751564592973547
INDEX_TITLE_SUFFIX = " | Demo"
GENERAL_TOPIC_ID = 1
TOPIC_LIST_LIMIT = 100
ASCII_BANNER = r"""
   ________                     __   ______            _
  / ____/ /_  ____ _____  ____  / /  / ____/___  ____  (_)__  _____
 / /   / __ \/ __ `/ __ \/ __ \/ /  / /   / __ \/ __ \/ / _ \/ ___/
/ /___/ / / / /_/ / / / / / / / /  / /___/ /_/ / /_/ / /  __/ /
\____/_/ /_/\__,_/_/ /_/_/ /_/_/   \____/\____/ .___/_/\___/_/
                                               /_/
"""
INDEX_STICKER = types.InputDocument(
    id=6016935932551241313,
    access_hash=-7366320401303529044,
    file_reference=bytes.fromhex(
        "01004f3c996a58123cb397c2c4097adce4b99e40692afdf4f3"
    ),
)
WAIT_ERRORS = (
    errors.FloodWaitError,
    errors.FloodPremiumWaitError,
    errors.SlowModeWaitError,
)
STALE_MEDIA_ERRORS = (
    errors.FileReferenceExpiredError,
    errors.FileReferenceInvalidError,
    errors.FileReferenceEmptyError,
)
WRITE_DENIED_ERRORS = (
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ChatGuestSendForbiddenError,
    errors.ChatRestrictedError,
    errors.ChatSendMediaForbiddenError,
    errors.ChatSendPhotosForbiddenError,
    errors.UserBannedInChannelError,
)


@dataclass(frozen=True)
class IndexEntry:
    """One caption title and the URL of its copied destination post."""

    title: str
    url: str | None


@dataclass(frozen=True)
class CopyTarget:
    """A chosen chat plus the forum topic to read from or write into."""

    dialog: Dialog
    topic_id: int | None = None
    topic_title: str | None = None

    @property
    def entity(self) -> ChatEntity:
        """Return the underlying channel, supergroup, or basic group."""
        return self.dialog.entity

    @property
    def name(self) -> str:
        """Return the chat title, including the topic when one is chosen."""
        if self.topic_title:
            return f"{self.dialog.name} / {self.topic_title}"
        return self.dialog.name

    @property
    def reply_to_topic(self) -> int | None:
        """Return the message id that threads a new post into the topic.

        The General topic needs no header because it is the default placement,
        and its root id is not a real message that can be replied to.
        """
        if not self.topic_id or self.topic_id == GENERAL_TOPIC_ID:
            return None
        return self.topic_id


def show_banner() -> None:
    """Initialize terminal colors and display the startup banner."""
    colorama_init(autoreset=True)
    print(Fore.GREEN + Style.BRIGHT + ASCII_BANNER)
    print(Fore.CYAN + Style.BRIGHT + "  TELETHON PHOTO MIGRATION CONSOLE")
    print(Fore.BLUE + "  Channels | Groups | Topics | Albums | Spoilers | Index\n")


def info(message: str) -> None:
    """Print an informational terminal message."""
    print(Fore.CYAN + f"[i] {message}")


def success(message: str) -> None:
    """Print a successful terminal message."""
    print(Fore.GREEN + f"[+] {message}")


def warning(message: str) -> None:
    """Print a warning terminal message."""
    print(Fore.YELLOW + f"[!] {message}")


def failure(message: str) -> None:
    """Print a failed terminal message."""
    print(Fore.RED + f"[-] {message}")


def read_credentials() -> tuple[int, str, str]:
    """Read Telegram API credentials from .env or prompt for missing values."""
    load_dotenv()

    raw_api_id = (os.getenv("TELEGRAM_API_ID") or "").strip() or input(
        Fore.CYAN + "Telegram API ID: "
    ).strip()
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip() or getpass.getpass(
        Fore.CYAN + "Telegram API hash: "
    ).strip()
    session = (os.getenv("TELEGRAM_SESSION") or "").strip() or "channel_copier"

    if not raw_api_id:
        raise ValueError("TELEGRAM_API_ID cannot be empty.")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise ValueError("TELEGRAM_API_ID must be a positive integer.")
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")

    return api_id, api_hash, session


async def retry_on_wait(
    action: Callable[..., Awaitable[Result]],
    *args: object,
    **kwargs: object,
) -> Result:
    """Run a Telegram call, sleeping through flood limits and slow mode."""
    while True:
        try:
            return await action(*args, **kwargs)
        except WAIT_ERRORS as exc:
            wait_seconds = exc.seconds + 1
            warning(f"Telegram asked to wait {wait_seconds} seconds...")
            await asyncio.sleep(wait_seconds)


def is_supported_chat(entity: object) -> bool:
    """Return whether an entity is a channel or group this script can use."""
    if isinstance(entity, types.Channel):
        if getattr(entity, "monoforum", False):
            return False
        return bool(entity.broadcast or entity.megagroup or entity.gigagroup)
    if isinstance(entity, types.Chat):
        return not (entity.deactivated or entity.migrated_to)
    return False


def chat_type_label(entity: ChatEntity) -> str:
    """Describe a chat so channels and groups are distinguishable in menus."""
    if isinstance(entity, types.Chat):
        return "group"
    if entity.gigagroup:
        return "broadcast group"
    if entity.broadcast:
        return "channel"
    if entity.forum:
        return "forum group"
    return "supergroup"


def can_send_photos(entity: ChatEntity) -> bool:
    """Return whether the current account may publish photos in a chat."""
    if getattr(entity, "left", False):
        return False
    if isinstance(entity, types.Chat) and (entity.deactivated or entity.migrated_to):
        return False
    if entity.creator:
        return True

    admin_rights = entity.admin_rights
    if isinstance(entity, types.Channel) and (entity.broadcast or entity.gigagroup):
        return bool(admin_rights and admin_rights.post_messages)

    banned_rights = getattr(entity, "banned_rights", None)
    if banned_rights and (
        banned_rights.view_messages
        or banned_rights.send_messages
        or banned_rights.send_media
        or banned_rights.send_photos
    ):
        return False
    if admin_rights:
        return True

    default_rights = entity.default_banned_rights
    return not (
        default_rights
        and (
            default_rights.send_messages
            or default_rights.send_media
            or default_rights.send_photos
        )
    )


def entity_username(entity: ChatEntity) -> str | None:
    """Return an active public username, or None for private chats."""
    username = getattr(entity, "username", None)
    if username:
        return username
    for extra in getattr(entity, "usernames", None) or []:
        if extra.active:
            return extra.username
    return None


def chat_label(dialog: Dialog) -> str:
    """Build a readable label for an interactive chat choice."""
    entity = dialog.entity
    username = entity_username(entity)
    visibility = f"@{username}" if username else "private"
    return f"{dialog.name} [{chat_type_label(entity)}, {visibility}]"


def choose_chat(prompt: str, dialogs: Sequence[Dialog]) -> Dialog:
    """Show numbered chats and return the selected dialog."""
    if not dialogs:
        raise RuntimeError("No eligible channels or groups were found for this account.")

    print(Fore.YELLOW + Style.BRIGHT + f"\n== {prompt.upper()} ==")
    for number, dialog in enumerate(dialogs, start=1):
        print(
            Fore.GREEN
            + Style.BRIGHT
            + f"  [{number:>3}] "
            + Fore.WHITE
            + chat_label(dialog)
        )

    while True:
        raw_choice = input(Fore.CYAN + "Select number > ").strip()
        try:
            choice = int(raw_choice)
        except ValueError:
            warning("Enter one of the numbers shown above.")
            continue

        if 1 <= choice <= len(dialogs):
            selected = dialogs[choice - 1]
            success(f"Selected: {chat_label(selected)}")
            return selected
        warning("Enter one of the numbers shown above.")


async def get_forum_topics(
    client: TelegramClient, channel: types.Channel
) -> list[types.ForumTopic]:
    """Return the most recent forum topics of a group with topics enabled."""
    peer = await client.get_input_entity(channel)
    result = await retry_on_wait(
        client,
        functions.messages.GetForumTopicsRequest(
            peer=peer,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=TOPIC_LIST_LIMIT,
        ),
    )
    return [topic for topic in result.topics if isinstance(topic, types.ForumTopic)]


def choose_topic(
    prompt: str, topics: Sequence[types.ForumTopic], default_label: str
) -> types.ForumTopic | None:
    """Show numbered topics and return the selection, or None for the default."""
    print(Fore.YELLOW + Style.BRIGHT + f"\n== {prompt.upper()} ==")
    print(Fore.GREEN + Style.BRIGHT + "  [  0] " + Fore.WHITE + default_label)
    for number, topic in enumerate(topics, start=1):
        state = " (closed)" if topic.closed else ""
        print(
            Fore.GREEN
            + Style.BRIGHT
            + f"  [{number:>3}] "
            + Fore.WHITE
            + f"{topic.title}{state}"
        )

    while True:
        raw_choice = input(Fore.CYAN + "Select number > ").strip()
        try:
            choice = int(raw_choice)
        except ValueError:
            warning("Enter one of the numbers shown above.")
            continue

        if choice == 0:
            success(f"Selected: {default_label}")
            return None
        if 1 <= choice <= len(topics):
            selected = topics[choice - 1]
            success(f"Selected topic: {selected.title}")
            return selected
        warning("Enter one of the numbers shown above.")


async def build_target(
    client: TelegramClient, dialog: Dialog, *, reading: bool
) -> CopyTarget:
    """Wrap a dialog, asking which forum topic to use when the chat has topics."""
    entity = dialog.entity
    if not isinstance(entity, types.Channel) or not entity.forum:
        return CopyTarget(dialog)

    topics = await get_forum_topics(client, entity)
    if not reading:
        topics = [topic for topic in topics if not topic.closed or entity.creator]
    if not topics:
        return CopyTarget(dialog)

    default_label = "All topics" if reading else "General topic"
    topic = choose_topic(f"{dialog.name} topic", topics, default_label)
    if topic is None:
        return CopyTarget(dialog)
    return CopyTarget(dialog, topic.id, topic.title)


def topic_reply_to(destination: CopyTarget) -> types.InputReplyToMessage | None:
    """Build the reply header that places a new message inside a topic."""
    topic_id = destination.reply_to_topic
    if topic_id is None:
        return None
    return types.InputReplyToMessage(reply_to_msg_id=topic_id, top_msg_id=topic_id)


def message_topic_id(message: Message) -> int:
    """Return the forum topic a message belongs to, defaulting to General."""
    reply_to = message.reply_to
    if not isinstance(reply_to, types.MessageReplyHeader) or not reply_to.forum_topic:
        return GENERAL_TOPIC_ID
    return reply_to.reply_to_top_id or reply_to.reply_to_msg_id or GENERAL_TOPIC_ID


def replied_message_id(message: Message) -> int | None:
    """Return the message this one really replies to, ignoring topic headers."""
    reply_to = message.reply_to
    if not isinstance(reply_to, types.MessageReplyHeader):
        return None
    if reply_to.reply_to_peer_id is not None:
        return None
    if reply_to.forum_topic and reply_to.reply_to_top_id is None:
        # In forums a plain topic message carries the topic id here, not a reply.
        return None
    return reply_to.reply_to_msg_id


async def iter_source_photos(
    client: TelegramClient, source: CopyTarget
) -> AsyncIterator[Message]:
    """Yield source photo messages oldest first, honoring a chosen topic."""
    if source.topic_id is None:
        iterator = client.iter_messages(
            source.entity,
            filter=types.InputMessagesFilterPhotos,
            reverse=True,
        )
    else:
        # A server-side media filter cannot be combined with a topic, so the
        # whole history is scanned and photos are selected locally instead.
        iterator = client.iter_messages(source.entity, reverse=True)

    async for message in iterator:
        if not message.photo:
            continue
        if source.topic_id is not None and message_topic_id(message) != source.topic_id:
            continue
        yield message


async def iter_photo_groups(
    client: TelegramClient, source: CopyTarget
) -> AsyncIterator[list[Message]]:
    """Yield source photos chronologically, retaining Telegram album groups."""
    pending_album: list[Message] = []
    pending_group_id: int | None = None

    async for message in iter_source_photos(client, source):
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
        downloaded = await retry_on_wait(
            client.download_media, message, file=str(requested_path)
        )
        if not downloaded:
            raise RuntimeError(f"Telegram did not return photo {message.id}.")
        paths.append(Path(downloaded))
    return paths


async def send_single_photo(
    client: TelegramClient,
    destination: CopyTarget,
    path: Path,
    source_message: Message,
) -> list[Message]:
    """Upload and publish one photo while preserving its caption and spoiler."""
    peer = await client.get_input_entity(destination.entity)
    uploaded = await retry_on_wait(client.upload_file, str(path))
    media = types.InputMediaUploadedPhoto(
        file=uploaded,
        spoiler=has_media_spoiler(source_message),
    )
    request = functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=source_message.message or "",
        entities=list(source_message.entities or []),
        reply_to=topic_reply_to(destination),
    )
    result = await retry_on_wait(client, request)
    sent = client._get_response_message(request, result, peer)
    return [sent] if sent else []


async def send_photos_separately(
    client: TelegramClient,
    destination: CopyTarget,
    paths: Sequence[Path],
    source_messages: Sequence[Message],
) -> list[Message]:
    """Publish album photos as individual posts, keeping every caption."""
    sent_messages: list[Message] = []
    for path, source_message in zip(paths, source_messages, strict=True):
        sent_messages.extend(
            await send_single_photo(client, destination, path, source_message)
        )
    return sent_messages


async def send_photo_album(
    client: TelegramClient,
    destination: CopyTarget,
    paths: Sequence[Path],
    source_messages: Sequence[Message],
) -> list[Message]:
    """Upload and publish an album with per-photo captions and spoilers."""
    peer = await client.get_input_entity(destination.entity)
    input_media: list[types.InputSingleMedia] = []

    for path, source_message in zip(paths, source_messages, strict=True):
        uploaded = await retry_on_wait(client.upload_file, str(path))
        uploaded_photo = types.InputMediaUploadedPhoto(file=uploaded)
        uploaded_result = await retry_on_wait(
            client,
            functions.messages.UploadMediaRequest(peer=peer, media=uploaded_photo),
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
        reply_to=topic_reply_to(destination),
    )
    try:
        result = await retry_on_wait(client, request)
    except errors.SlowModeMultiMsgsDisabledError:
        # Slow mode forbids albums, so the batch is published one photo at a time.
        warning("Slow mode blocks albums here: sending these photos separately.")
        return await send_photos_separately(
            client, destination, paths, source_messages
        )

    random_ids = [item.random_id for item in input_media]
    sent = client._get_response_message(random_ids, result, peer)
    if not sent:
        return []
    return list(sent) if isinstance(sent, list) else [sent]


async def copy_photo_group(
    client: TelegramClient,
    destination: CopyTarget,
    messages: Sequence[Message],
) -> list[Message]:
    """Download a source group temporarily and publish it at the destination."""
    with tempfile.TemporaryDirectory(prefix="telethon-photo-copy-") as temp_dir:
        paths = await download_group(client, messages, Path(temp_dir))
        if len(messages) == 1:
            return await send_single_photo(client, destination, paths[0], messages[0])
        return await send_photo_album(client, destination, paths, messages)


def is_plain_text_message(message: Message) -> bool:
    """Return whether a message is text only, ignoring link-preview media."""
    if message.action or not message.message:
        return False
    return message.media is None or isinstance(
        message.media, types.MessageMediaWebPage
    )


async def copy_text_replies(
    client: TelegramClient,
    source: CopyTarget,
    destination: CopyTarget,
    copied_photo_ids: dict[int, int],
) -> tuple[int, int]:
    """Copy plain-text messages that directly reply to copied photos."""
    copied = 0
    failed = 0

    async for message in client.iter_messages(source.entity, reverse=True):
        if source.topic_id is not None and message_topic_id(message) != source.topic_id:
            continue
        if not is_plain_text_message(message):
            continue
        destination_photo_id = copied_photo_ids.get(replied_message_id(message))
        if destination_photo_id is None:
            continue

        try:
            await retry_on_wait(
                client.send_message,
                destination.entity,
                message.message,
                formatting_entities=list(message.entities or []),
                reply_to=destination_photo_id,
                link_preview=bool(message.web_preview),
            )
            copied += 1
        except WRITE_DENIED_ERRORS as exc:
            failure(f"Cannot post in {destination.name}: {exc}")
            break
        except Exception as exc:  # Keep copying after one malformed reply.
            failed += 1
            failure(f"Text reply {message.id} failed: {exc}")

    return copied, failed


def caption_position(messages: Sequence[Message]) -> int | None:
    """Return the position of the first photo containing a non-empty caption."""
    for position, message in enumerate(messages):
        if message.message and message.message.strip():
            return position
    return None


def is_emoji_character(character: str) -> bool:
    """Return whether a character belongs to a common Unicode emoji range."""
    codepoint = ord(character)
    return (
        codepoint in {0x00A9, 0x00AE, 0x200D, 0x203C, 0x2049, 0x20E3, 0x2122}
        or 0x2190 <= codepoint <= 0x21FF
        or 0x2300 <= codepoint <= 0x23FF
        or 0x2460 <= codepoint <= 0x24FF
        or 0x25A0 <= codepoint <= 0x27BF
        or 0x2B00 <= codepoint <= 0x2BFF
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0xE0020 <= codepoint <= 0xE007F
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def remove_caption_emoji(value: str) -> str:
    """Remove ordinary emoji and normalize whitespace for an index title."""
    characters: list[str] = []
    for character in value:
        if ord(character) == 0x20E3 and characters:
            if characters[-1] in "#*0123456789":
                characters.pop()
            continue
        if not is_emoji_character(character):
            characters.append(character)
    return " ".join("".join(characters).split())


def index_title(caption: str) -> str | None:
    """Extract the first emoji-free caption line usable as an index title."""
    for line in caption.splitlines():
        title = remove_caption_emoji(line)
        if title:
            return title
    return None


def message_link(
    entity: ChatEntity, message_id: int, topic_id: int | None = None
) -> str | None:
    """Create a public or private post URL, or None when links are impossible."""
    if isinstance(entity, types.Chat):
        # Legacy basic groups have no per-message permalinks at all.
        return None

    thread = f"{topic_id}/" if topic_id and topic_id != GENERAL_TOPIC_ID else ""
    username = entity_username(entity)
    if username:
        return f"https://t.me/{username}/{thread}{message_id}"
    return f"https://t.me/c/{entity.id}/{thread}{message_id}"


def utf16_length(value: str) -> int:
    """Measure text in Telegram's UTF-16 entity-offset units."""
    return len(value.encode("utf-16-le")) // 2


def make_index_messages(
    entries: Iterable[IndexEntry],
    max_units: int = 3900,
    max_entries: int = 20,
) -> Iterable[tuple[str, list[types.TypeMessageEntity]]]:
    """Build blockquoted index messages with custom-emoji linked titles."""
    text = ""
    line_entities: list[types.TypeMessageEntity] = []
    entry_count = 0

    def finish_chunk() -> tuple[str, list[types.TypeMessageEntity]]:
        finished_text = text.rstrip("\n")
        blockquote = types.MessageEntityBlockquote(
            offset=0,
            length=utf16_length(finished_text),
        )
        return finished_text, [blockquote, *line_entities]

    for entry in entries:
        display_title = f"{entry.title}{INDEX_TITLE_SUFFIX}"
        addition = f"{INDEX_CUSTOM_EMOJI} {display_title}\n"
        if entry_count and (
            utf16_length(text + addition) > max_units
            or entry_count >= max_entries
        ):
            yield finish_chunk()
            text = ""
            line_entities = []
            entry_count = 0

        line_offset = utf16_length(text)
        title_offset = line_offset + utf16_length(f"{INDEX_CUSTOM_EMOJI} ")
        title_length = utf16_length(display_title)
        line_entities.append(
            types.MessageEntityCustomEmoji(
                offset=line_offset,
                length=utf16_length(INDEX_CUSTOM_EMOJI),
                document_id=INDEX_CUSTOM_EMOJI_ID,
            )
        )
        if entry.url:
            line_entities.append(
                types.MessageEntityTextUrl(
                    offset=title_offset,
                    length=title_length,
                    url=entry.url,
                )
            )
        line_entities.extend(
            [
                types.MessageEntityBold(offset=title_offset, length=title_length),
                types.MessageEntityUnderline(offset=title_offset, length=title_length),
            ]
        )
        text += addition
        entry_count += 1

    if entry_count:
        yield finish_chunk()


def without_custom_emoji(
    entities: Sequence[types.TypeMessageEntity],
) -> list[types.TypeMessageEntity]:
    """Drop custom-emoji entities so non-Premium accounts can still post."""
    return [
        entity
        for entity in entities
        if not isinstance(entity, types.MessageEntityCustomEmoji)
    ]


async def send_index_sticker(
    client: TelegramClient, entity: ChatEntity, reply_to: int | None
) -> bool:
    """Send the index sticker, reporting failures without stopping the run."""
    try:
        await retry_on_wait(
            client.send_file, entity, INDEX_STICKER, reply_to=reply_to
        )
        return True
    except Exception as exc:  # A stale sticker reference must not lose the index.
        warning(f"Index sticker skipped: {exc}")
        return False


async def send_index_chunk(
    client: TelegramClient,
    entity: ChatEntity,
    reply_to: int | None,
    text: str,
    entities: Sequence[types.TypeMessageEntity],
) -> None:
    """Send one styled index message into the destination chat or topic."""
    await retry_on_wait(
        client.send_message,
        entity,
        text,
        formatting_entities=list(entities),
        reply_to=reply_to,
        link_preview=False,
    )


async def post_index(
    client: TelegramClient,
    entity: ChatEntity,
    reply_to: int | None,
    entries: Sequence[IndexEntry],
) -> int:
    """Send the index sticker, then all styled linked-index chunks.

    Takes a bare chat rather than a CopyTarget so that both the interactive
    copier and .clone, which creates its destination and has no Dialog for it,
    can post the same index.
    """
    if not entries:
        return 0

    await send_index_sticker(client, entity, reply_to)

    count = 0
    for text, entities in make_index_messages(entries):
        try:
            await send_index_chunk(client, entity, reply_to, text, entities)
        except Exception as exc:
            # Custom emoji need Telegram Premium; retry with the plain emoji.
            warning(f"Styled index chunk failed ({exc}); retrying without custom emoji.")
            try:
                await send_index_chunk(
                    client, entity, reply_to, text, without_custom_emoji(entities)
                )
            except Exception as retry_exc:
                failure(f"Index chunk failed: {retry_exc}")
                continue
        count += 1
    return count


async def get_supported_dialogs(client: TelegramClient) -> list[Dialog]:
    """Return every channel and group dialog visible to the signed-in account."""
    dialogs: list[Dialog] = []
    async for dialog in client.iter_dialogs():
        if is_supported_chat(dialog.entity):
            dialogs.append(dialog)
    return dialogs


async def choose_targets(client: TelegramClient) -> tuple[CopyTarget, CopyTarget]:
    """Ask for the source and destination chats, including forum topics."""
    dialogs = await get_supported_dialogs(client)
    source_dialog = choose_chat("Source channel or group", dialogs)
    source = await build_target(client, source_dialog, reading=True)

    destination_dialogs = [
        dialog
        for dialog in dialogs
        if dialog.id != source_dialog.id and can_send_photos(dialog.entity)
    ]
    if not destination_dialogs:
        raise RuntimeError("No channel or group where this account can post photos.")
    destination_dialog = choose_chat("Destination channel or group", destination_dialogs)
    destination = await build_target(client, destination_dialog, reading=False)
    return source, destination


async def run_copy(client: TelegramClient) -> None:
    """Run chat selection, photo copying, and index generation."""
    source, destination = await choose_targets(client)
    info(f"Copying photos from {source.name} to {destination.name}...")
    if getattr(source.entity, "noforwards", False):
        warning("Source has content protection enabled; downloads may be refused.")

    index_entries: list[IndexEntry] = []
    copied_photo_ids: dict[int, int] = {}
    copied_photos = 0
    copied_posts = 0
    failed_groups = 0
    group_number = 0

    async for source_messages in iter_photo_groups(client, source):
        group_number += 1
        source_ids = ", ".join(str(message.id) for message in source_messages)
        try:
            sent_messages = await copy_photo_group(
                client, destination, source_messages
            )
        except WRITE_DENIED_ERRORS as exc:
            raise RuntimeError(f"Cannot post in {destination.name}: {exc}") from exc
        except Exception as exc:  # Keep a long copy moving after one failure.
            failed_groups += 1
            failure(f"Source message(s) {source_ids} failed: {exc}")
            continue

        if not sent_messages:
            failed_groups += 1
            failure(f"Source message(s) {source_ids} returned no destination post.")
            continue

        copied_photos += len(source_messages)
        copied_posts += 1
        for source_message, destination_message in zip(
            source_messages, sent_messages, strict=False
        ):
            copied_photo_ids[source_message.id] = destination_message.id
        success(
            f"Batch {group_number}: copied {len(source_messages)} photo(s) "
            f"from source message(s) {source_ids}"
        )

        position = caption_position(source_messages)
        if position is None:
            continue

        title = index_title(source_messages[position].message)
        destination_message = sent_messages[min(position, len(sent_messages) - 1)]
        if title:
            index_entries.append(
                IndexEntry(
                    title=title,
                    url=message_link(
                        destination.entity,
                        destination_message.id,
                        destination.topic_id,
                    ),
                )
            )

    info("Copying text replies to copied photos...")
    copied_replies, failed_replies = await copy_text_replies(
        client,
        source,
        destination,
        copied_photo_ids,
    )
    info("Sending final sticker and styled index...")
    index_messages = await post_index(
        client, destination.entity, destination.reply_to_topic, index_entries
    )
    print(Fore.GREEN + Style.BRIGHT + "\n========== COPY COMPLETE ==========")
    success(f"Photos copied: {copied_photos}")
    success(f"Photo posts/albums copied: {copied_posts}")
    success(f"Text replies copied: {copied_replies}")
    success(f"Index entries: {len(index_entries)}")
    success(f"Index messages posted: {index_messages}")
    if isinstance(destination.entity, types.Chat) and index_entries:
        warning("Basic groups have no post links, so index titles are not clickable.")
    if failed_groups:
        failure(f"Failed photo batches: {failed_groups}")
    if failed_replies:
        failure(f"Failed text replies: {failed_replies}")


# ---------------------------------------------------------------------------
# live commands: .change and .clone
# ---------------------------------------------------------------------------
# Telegram re-detects these itself, so stale copies are dropped rather than
# remapped. Everything else carries real styling and must survive the edit.
AUTO_ENTITIES = (
    types.MessageEntityMention,
    types.MessageEntityUrl,
    types.MessageEntityEmail,
    types.MessageEntityHashtag,
    types.MessageEntityBotCommand,
    types.MessageEntityCashtag,
    types.MessageEntityPhone,
    types.MessageEntityBankCard,
)
# t.me paths that are not usernames and must never be rewritten.
RESERVED_TME_PATHS = frozenset(
    {
        "addemoji", "addlist", "addstickers", "addtheme", "bg", "boost", "c",
        "confirmphone", "contact", "giftcode", "invoice", "iv", "joinchat",
        "login", "m", "proxy", "s", "setlanguage", "share", "socks",
    }
)
MENTION_RE = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(
    r"(?<![\w/])(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?(?![\w/])",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
EDIT_DELAY = 0.4
CLONE_DELAY = 0.6
STATUS_EVERY = 20


@dataclass(frozen=True)
class Rewrite:
    """One username occurrence to replace, measured in Python indices."""

    start: int
    end: int
    replacement: str


@dataclass
class ChangeStats:
    """Counters for a .change run."""

    edited: int = 0
    unchanged: int = 0
    failed: int = 0
    locked: int = 0


def utf16_offset(text: str, index: int) -> int:
    """Convert a Python string index into a Telegram UTF-16 entity offset."""
    return utf16_length(text[:index])


def clean_username(raw: str) -> str | None:
    """Normalize a supplied @username, returning None when it is unusable."""
    candidate = raw.strip().lstrip("@")
    if candidate.lower().startswith("t.me/"):
        candidate = candidate[5:]
    candidate = candidate.strip("/")
    return candidate if USERNAME_RE.fullmatch(candidate) else None


def find_username_rewrites(
    text: str, old: str | None, new: str
) -> list[Rewrite]:
    """Locate every username occurrence that should become the new one.

    Only the username characters are replaced, so a leading @, an http scheme
    and a trailing slash all survive untouched.
    """
    rewrites: list[Rewrite] = []

    for match in MENTION_RE.finditer(text):
        name = match.group(1)
        if old and name.lower() != old.lower():
            continue
        if name.lower() != new.lower():
            rewrites.append(Rewrite(match.start(1), match.end(1), new))

    for match in TME_RE.finditer(text):
        name = match.group(1)
        if name.lower() in RESERVED_TME_PATHS:
            continue
        if old and name.lower() != old.lower():
            continue
        if name.lower() != new.lower():
            rewrites.append(Rewrite(match.start(1), match.end(1), new))

    rewrites.sort(key=lambda item: item.start)
    deduped: list[Rewrite] = []
    for rewrite in rewrites:
        if deduped and rewrite.start < deduped[-1].end:
            continue  # Overlapping matches would corrupt the offsets.
        deduped.append(rewrite)
    return deduped


def offset_mapper(spans: Sequence[tuple[int, int, int]]) -> Callable[[int], int]:
    """Build a function moving old UTF-16 offsets onto the rewritten text."""

    def mapped(offset: int) -> int:
        delta = 0
        for start, end, new_length in spans:
            if offset >= end:
                delta += new_length - (end - start)
            elif offset > start:
                # The offset sat inside replaced text; clamp to its new end.
                return start + delta + new_length
            else:
                break
        return offset + delta

    return mapped


def splice_text(text: str, rewrites: Sequence[Rewrite]) -> str:
    """Apply replacement spans to a string from left to right."""
    pieces: list[str] = []
    cursor = 0
    for rewrite in rewrites:
        pieces.append(text[cursor : rewrite.start])
        pieces.append(rewrite.replacement)
        cursor = rewrite.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _with_url(
    entity: types.TypeMessageEntity, relink: Callable[[str], str]
) -> types.TypeMessageEntity:
    """Rewrite a hyperlink target, returning the original when unchanged."""
    if not isinstance(entity, types.MessageEntityTextUrl):
        return entity
    updated = relink(entity.url)
    if updated == entity.url:
        return entity
    changed = copy.copy(entity)
    changed.url = updated
    return changed


def apply_text_rewrites(
    text: str,
    entities: Sequence[types.TypeMessageEntity] | None,
    rewrites: Sequence[Rewrite],
    relink: Callable[[str], str],
) -> tuple[str, list[types.TypeMessageEntity]] | None:
    """Rewrite text spans and hyperlinks, keeping every entity aligned.

    Returns None when nothing needs to change. Offsets are recomputed in
    UTF-16 units because that is what Telegram entity offsets count, and any
    emoji in the text makes them differ from Python indices.
    """
    kept = [
        entity for entity in entities or [] if not isinstance(entity, AUTO_ENTITIES)
    ]

    if not rewrites:
        # The visible text is fine, but a hyperlink may still be stale.
        relinked = [_with_url(entity, relink) for entity in kept]
        if any(new is not old for new, old in zip(relinked, kept, strict=True)):
            return text, relinked
        return None

    new_text = splice_text(text, rewrites)
    spans = [
        (
            utf16_offset(text, rewrite.start),
            utf16_offset(text, rewrite.end),
            utf16_length(rewrite.replacement),
        )
        for rewrite in rewrites
    ]
    mapped = offset_mapper(spans)

    moved: list[types.TypeMessageEntity] = []
    for entity in kept:
        start = mapped(entity.offset)
        end = mapped(entity.offset + entity.length)
        if end <= start:
            continue  # The styled text was replaced entirely.
        shifted = copy.copy(entity)
        shifted.offset = start
        shifted.length = end - start
        moved.append(_with_url(shifted, relink))
    return new_text, moved


def username_relinker(old: str | None, new: str) -> Callable[[str], str]:
    """Build a hyperlink rewriter that swaps usernames inside t.me targets."""

    def relink(url: str) -> str:
        return splice_text(url, find_username_rewrites(url, old, new))

    return relink


def rewrite_message_text(
    text: str,
    entities: Sequence[types.TypeMessageEntity] | None,
    old: str | None,
    new: str,
) -> tuple[str, list[types.TypeMessageEntity]] | None:
    """Swap usernames in a message, keeping every other entity aligned."""
    return apply_text_rewrites(
        text,
        entities,
        find_username_rewrites(text, old, new),
        username_relinker(old, new),
    )


async def set_status(event: object, text: str) -> None:
    """Replace the command message with a progress line, ignoring failures."""
    with contextlib.suppress(Exception):  # Status must never break the command.
        await event.edit(text)


async def command_change(client: TelegramClient, event: object, args: list[str]) -> None:
    """Rewrite usernames across every post in the current chat."""
    if not args or len(args) > 2:
        await set_status(
            event,
            "Usage: .change @newname   or   .change @oldname @newname",
        )
        return

    if len(args) == 2:
        old = clean_username(args[0])
        new = clean_username(args[1])
        if not old:
            await set_status(event, f"{args[0]} is not a valid username.")
            return
    else:
        old = None
        new = clean_username(args[0])
    if not new:
        await set_status(event, f"{args[-1]} is not a valid username.")
        return

    scope = f"@{old}" if old else "every username"
    info(f"Rewriting {scope} to @{new} in chat {event.chat_id}...")
    await set_status(event, f"Rewriting {scope} to @{new}...")

    stats = ChangeStats()
    async for message in client.iter_messages(event.chat_id):
        if message.action is not None or message.id == event.id:
            continue
        if not message.message:
            continue

        result = rewrite_message_text(message.message, message.entities, old, new)
        if result is None:
            stats.unchanged += 1
            continue

        new_text, new_entities = result
        try:
            await retry_on_wait(
                client.edit_message,
                event.chat_id,
                message.id,
                new_text,
                formatting_entities=new_entities,
                link_preview=bool(message.web_preview),
            )
            stats.edited += 1
        except errors.MessageNotModifiedError:
            stats.unchanged += 1
        except (
            errors.MessageAuthorRequiredError,
            errors.MessageEditTimeExpiredError,
            errors.ChatAdminRequiredError,
        ):
            stats.locked += 1
        except Exception as exc:  # noqa: BLE001
            stats.failed += 1
            failure(f"Message {message.id} could not be edited: {exc}")

        if stats.edited and stats.edited % STATUS_EVERY == 0:
            await set_status(event, f"Rewriting to @{new}... {stats.edited} edited")
        await asyncio.sleep(EDIT_DELAY)

    summary = f"Rewrote {scope} to @{new}: {stats.edited} edited"
    if stats.unchanged:
        summary += f", {stats.unchanged} already fine"
    if stats.locked:
        summary += f", {stats.locked} not editable"
    if stats.failed:
        summary += f", {stats.failed} failed"
    success(summary)
    await set_status(event, summary)


# A link to a post in the very chat being cloned, public or private form,
# optionally with a topic segment. The message id is always the last number.
SELF_POST_RE = re.compile(
    r"(?<![\w/])(?:https?://)?t\.me/"
    r"(?:c/(?P<cid>\d+)|(?P<name>[A-Za-z][A-Za-z0-9_]{3,31}))"
    r"/(?P<first>\d+)(?:/(?P<second>\d+))?",
    re.IGNORECASE,
)


def find_self_post_links(
    text: str,
    username: str | None,
    channel_id: int,
    new_url_for: Callable[[int], str | None],
) -> list[Rewrite]:
    """Locate links pointing at posts of the chat being cloned."""
    rewrites: list[Rewrite] = []
    for match in SELF_POST_RE.finditer(text):
        raw_channel = match.group("cid")
        if raw_channel is not None:
            if raw_channel != str(channel_id):
                continue
        else:
            name = match.group("name")
            if not username or not name or name.lower() != username.lower():
                continue

        message_id = int(match.group("second") or match.group("first"))
        replacement = new_url_for(message_id)
        if replacement is None:
            continue  # That post was never cloned, so the old link stays.
        if not match.group(0).lower().startswith("http"):
            replacement = replacement.removeprefix("https://")
        rewrites.append(Rewrite(match.start(), match.end(), replacement))
    return rewrites


@dataclass(frozen=True)
class CloneLinks:
    """Maps post links of the source chat onto their clones."""

    username: str | None
    channel_id: int
    target: types.Channel
    id_map: dict[int, int]

    def new_url(self, message_id: int) -> str | None:
        """Return the cloned post's link, or None when it was not cloned."""
        cloned = self.id_map.get(message_id)
        return None if cloned is None else message_link(self.target, cloned)

    def rewrites(self, text: str) -> list[Rewrite]:
        """Find self links in a piece of text that can be repointed."""
        return find_self_post_links(text, self.username, self.channel_id, self.new_url)

    def relink(self, url: str) -> str:
        """Repoint a hyperlink target at the clone."""
        return splice_text(url, self.rewrites(url))

    def present_in(self, message: Message) -> bool:
        """Return whether a post links to the source chat at all.

        Detection ignores the id map, because during copying the later posts
        have not been created yet.
        """
        def any_target(_message_id: int) -> str:
            return "pending"

        if message.message and find_self_post_links(
            message.message, self.username, self.channel_id, any_target
        ):
            return True
        return any(
            isinstance(entity, types.MessageEntityTextUrl)
            and find_self_post_links(
                entity.url, self.username, self.channel_id, any_target
            )
            for entity in message.entities or []
        )


async def repoint_clone_links(
    client: TelegramClient,
    links: CloneLinks,
    pending: Sequence[tuple[int, Message]],
) -> tuple[int, int, int]:
    """Edit cloned posts so their links point inside the clone.

    This runs after copying finishes, because a post may link forward to one
    that had not been cloned yet while the copy was still in progress.

    Returns (repointed, left alone, failed). A post is left alone when every
    link in it points at something that was never cloned, which is a normal
    outcome and not a failure.
    """
    repointed = 0
    untouched = 0
    failed = 0
    for new_id, source_message in pending:
        text = source_message.message or ""
        result = apply_text_rewrites(
            text, source_message.entities, links.rewrites(text), links.relink
        )
        if result is None:
            untouched += 1
            continue

        new_text, new_entities = result
        try:
            await retry_on_wait(
                client.edit_message,
                links.target,
                new_id,
                new_text,
                formatting_entities=new_entities,
                link_preview=bool(source_message.web_preview),
            )
            repointed += 1
        except errors.MessageNotModifiedError:
            untouched += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failure(f"Could not repoint links in cloned post {new_id}: {exc}")
        await asyncio.sleep(EDIT_DELAY)
    return repointed, untouched, failed


# A private invite link, either the modern +hash form or the old joinchat one.
INVITE_RE = re.compile(
    r"^(?:https?://)?t\.me/(?:joinchat/|\+)(?P<hash>[\w-]+)/?$", re.IGNORECASE
)
# A public link or @handle naming a chat.
PUBLIC_RE = re.compile(
    r"^(?:(?:https?://)?t\.me/|@)(?P<name>[A-Za-z][A-Za-z0-9_]{3,31})/?$", re.IGNORECASE
)
# A private post link, which identifies the chat by its internal id.
PRIVATE_ID_RE = re.compile(
    r"^(?:https?://)?t\.me/c/(?P<id>\d+)(?:/\d+)*/?$", re.IGNORECASE
)


def looks_like_chat_reference(token: str) -> bool:
    """Return whether a token names a chat rather than being title text."""
    return bool(
        INVITE_RE.match(token) or PUBLIC_RE.match(token) or PRIVATE_ID_RE.match(token)
    )


async def resolve_clone_source(client: TelegramClient, token: str) -> ChatEntity:
    """Resolve a link, invite or @handle into a chat that can be cloned.

    An invite link is inspected before joining, so a chat the account is
    already in is used as it stands instead of failing on a second join.
    """
    invite = INVITE_RE.match(token)
    if invite:
        return await resolve_invite(client, invite.group("hash"))

    private_id = PRIVATE_ID_RE.match(token)
    if private_id:
        return await retry_on_wait(
            client.get_entity, types.PeerChannel(int(private_id.group("id")))
        )

    public = PUBLIC_RE.match(token)
    if public:
        return await retry_on_wait(client.get_entity, public.group("name"))

    raise RuntimeError(f"{token} is not a chat link, invite or @handle")


async def resolve_invite(client: TelegramClient, invite_hash: str) -> ChatEntity:
    """Turn an invite hash into a chat, joining only when necessary."""
    try:
        checked = await retry_on_wait(
            client, functions.messages.CheckChatInviteRequest(hash=invite_hash)
        )
    except (errors.InviteHashInvalidError, errors.InviteHashEmptyError) as exc:
        raise RuntimeError("that invite link is not valid") from exc
    except errors.InviteHashExpiredError as exc:
        raise RuntimeError("that invite link has expired") from exc

    # Already a member, or holding a temporary peek: use the chat directly.
    existing = getattr(checked, "chat", None)
    if existing is not None:
        info(f"Already have access to {utils.get_display_name(existing)}.")
        return existing

    if getattr(checked, "request_needed", False):
        raise RuntimeError(
            "that invite needs admin approval, so its history cannot be read yet"
        )

    try:
        joined = await retry_on_wait(
            client, functions.messages.ImportChatInviteRequest(hash=invite_hash)
        )
    except errors.UserAlreadyParticipantError as exc:
        # Raced with an existing membership; re-check to get the chat object.
        rechecked = await retry_on_wait(
            client, functions.messages.CheckChatInviteRequest(hash=invite_hash)
        )
        already = getattr(rechecked, "chat", None)
        if already is None:
            raise RuntimeError(
                "already a member, but the chat could not be read"
            ) from exc
        return already
    except errors.InviteRequestSentError as exc:
        raise RuntimeError(
            "a join request was sent; approve it first, then clone"
        ) from exc

    entity = channel_from_updates(joined)
    info(f"Joined {utils.get_display_name(entity)} to read its history.")
    return entity


def channel_from_updates(updates: object) -> types.Channel:
    """Pull a channel out of an Updates envelope, or its nested result."""
    chats = getattr(updates, "chats", None)
    if not chats:
        inner = getattr(updates, "updates", None)
        if inner is not None:
            chats = getattr(inner, "chats", None)
    for chat in chats or []:
        if isinstance(chat, types.Channel):
            return chat
    raise RuntimeError("Telegram did not return the new channel.")


def clone_input_media(message: Message) -> object | None:
    """Reuse the existing file for a clone, or None when the post is text."""
    media = message.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return None
    input_media = utils.get_input_media(media)
    if isinstance(input_media, types.InputMediaEmpty):
        raise TypeError(f"{type(media).__name__} cannot be resent")
    if hasattr(input_media, "spoiler"):
        input_media.spoiler = has_media_spoiler(message)
    return input_media


async def iter_clone_batches(
    client: TelegramClient, source: ChatEntity
) -> AsyncIterator[list[Message]]:
    """Yield every source post oldest first, keeping albums together."""
    album: list[Message] = []
    album_id: int | None = None

    async for message in client.iter_messages(source, reverse=True):
        if message.action is not None:
            continue
        if not message.message and message.media is None:
            continue

        if message.grouped_id is None:
            if album:
                yield album
                album, album_id = [], None
            yield [message]
            continue

        if album and message.grouped_id != album_id:
            yield album
            album = []
        album_id = message.grouped_id
        album.append(message)

    if album:
        yield album


async def send_clone_batch(
    client: TelegramClient,
    target: types.Channel,
    batch: Sequence[Message],
    medias: Sequence[object],
    id_map: dict[int, int],
) -> list[Message]:
    """Publish one cloned post or album, keeping replies pointing correctly."""
    first = batch[0]
    reply_to = None
    replied = replied_message_id(first)
    if replied is not None and replied in id_map:
        reply_to = types.InputReplyToMessage(reply_to_msg_id=id_map[replied])

    if len(batch) > 1:
        items = [
            types.InputSingleMedia(
                media=media,
                message=message.message or "",
                entities=list(message.entities or []),
            )
            for message, media in zip(batch, medias, strict=True)
        ]
        query = functions.messages.SendMultiMediaRequest(
            peer=target, multi_media=items, reply_to=reply_to
        )
        result = await retry_on_wait(client, query)
        produced = client._get_response_message(
            [item.random_id for item in items], result, target
        )
        if not produced:
            return []
        return list(produced) if isinstance(produced, list) else [produced]

    if medias[0] is None:
        query = functions.messages.SendMessageRequest(
            peer=target,
            message=first.message,
            entities=list(first.entities or []),
            no_webpage=not first.web_preview,
            reply_to=reply_to,
        )
    else:
        query = functions.messages.SendMediaRequest(
            peer=target,
            media=medias[0],
            message=first.message or "",
            entities=list(first.entities or []),
            reply_to=reply_to,
        )
    result = await retry_on_wait(client, query)
    sent = client._get_response_message(query, result, target)
    return [sent] if sent else []


async def reuploaded_media(
    client: TelegramClient, message: Message, directory: Path
) -> object | None:
    """Download and upload a post's media when its reference cannot be reused."""
    if message.media is None or isinstance(message.media, types.MessageMediaWebPage):
        return None
    path = await retry_on_wait(
        client.download_media, message, file=str(directory / str(message.id))
    )
    if not path:
        raise RuntimeError(f"could not download media of message {message.id}")
    handle = await retry_on_wait(client.upload_file, path)
    if message.photo:
        return types.InputMediaUploadedPhoto(
            file=handle, spoiler=has_media_spoiler(message)
        )
    return types.InputMediaUploadedDocument(
        file=handle,
        mime_type=message.file.mime_type if message.file else "application/octet-stream",
        attributes=list(getattr(message.document, "attributes", None) or []),
        spoiler=has_media_spoiler(message),
    )


def clone_index_entry(
    batch: Sequence[Message],
    sent: Sequence[Message],
    target: types.Channel,
) -> IndexEntry | None:
    """Build the index entry for one cloned post, or None when it needs none.

    Only media posts are indexed, matching reindex.py: a plain text post is a
    post, not a caption, so it never becomes an index title.
    """
    position = caption_position(batch)
    if position is None:
        return None
    source_message = batch[position]
    if source_message.media is None or isinstance(
        source_message.media, types.MessageMediaWebPage
    ):
        return None
    title = index_title(source_message.message)
    if not title:
        return None
    cloned = sent[min(position, len(sent) - 1)]
    return IndexEntry(title=title, url=message_link(target, cloned.id))


async def command_clone(client: TelegramClient, event: object, args: list[str]) -> None:
    """Clone a chat into a fresh private channel.

    With no link the current chat is cloned. A first argument that names a
    chat, by invite link, public link or @handle, clones that chat instead
    and any remaining words become the new title.

    Once every post is copied, links between posts are repointed at the clone
    and the styled linked index is posted, so the clone needs no reindex.py run.
    """
    title_words = list(args)
    if args and looks_like_chat_reference(args[0]):
        await set_status(event, f"Resolving {args[0]}...")
        try:
            source = await resolve_clone_source(client, args[0])
        except RuntimeError as exc:
            await set_status(event, f"Cannot clone {args[0]}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            await set_status(event, f"Cannot resolve {args[0]}: {exc}")
            return
        title_words = list(args[1:])
    else:
        source = await event.get_chat()

    if not is_supported_chat(source):
        await set_status(event, "That chat cannot be cloned.")
        return

    title = " ".join(title_words).strip() or utils.get_display_name(source) or "Clone"
    info(f"Cloning {utils.get_display_name(source)} into a private channel...")
    await set_status(event, f"Creating private channel {title!r}...")

    created = await retry_on_wait(
        client,
        functions.channels.CreateChannelRequest(title=title, about="", broadcast=True),
    )
    target = channel_from_updates(created)
    success(f"Created private channel {title!r} (id {target.id}).")

    if getattr(source, "noforwards", False):
        warning("Source has content protection enabled; some media may be refused.")

    id_map: dict[int, int] = {}
    links = CloneLinks(
        username=entity_username(source) if isinstance(source, types.Channel) else None,
        channel_id=source.id,
        target=target,
        id_map=id_map,
    )
    pending_links: list[tuple[int, Message]] = []
    index_entries: list[IndexEntry] = []
    copied = skipped = failed = 0
    await set_status(event, f"Cloning into {title!r}...")

    async for batch in iter_clone_batches(client, source):
        ids = ", ".join(str(message.id) for message in batch)
        try:
            medias = [clone_input_media(message) for message in batch]
        except (TypeError, ValueError, AttributeError) as exc:
            skipped += len(batch)
            warning(f"Skipping {ids} ({exc}).")
            continue

        try:
            sent = await send_clone_batch(client, target, batch, medias, id_map)
        except STALE_MEDIA_ERRORS:
            warning(f"File references for {ids} expired; re-uploading.")
            try:
                with tempfile.TemporaryDirectory(prefix="clone-") as temp_dir:
                    fresh = [
                        await reuploaded_media(client, message, Path(temp_dir))
                        for message in batch
                    ]
                    sent = await send_clone_batch(client, target, batch, fresh, id_map)
            except Exception as exc:  # noqa: BLE001
                failed += len(batch)
                failure(f"Gave up on {ids} ({exc}).")
                continue
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            failure(f"Could not clone {ids} ({exc}).")
            continue

        if not sent:
            failed += len(batch)
            continue
        for source_message, new_message in zip(batch, sent, strict=False):
            id_map[source_message.id] = new_message.id
            if links.present_in(source_message):
                pending_links.append((new_message.id, source_message))
        entry = clone_index_entry(batch, sent, target)
        if entry is not None:
            index_entries.append(entry)
        copied += len(batch)
        if copied % STATUS_EVERY == 0:
            await set_status(event, f"Cloning into {title!r}... {copied} posts")
        await asyncio.sleep(CLONE_DELAY)

    repointed = untouched = link_failures = 0
    if pending_links:
        info(f"Repointing links in {len(pending_links)} cloned post(s)...")
        await set_status(event, f"Repointing links in {len(pending_links)} post(s)...")
        repointed, untouched, link_failures = await repoint_clone_links(
            client, links, pending_links
        )
        success(f"Repointed links in {repointed} post(s).")
    else:
        info("No post carried a link to the source, so none needed repointing.")

    index_messages = 0
    if index_entries:
        info(f"Posting the linked index for {len(index_entries)} post(s)...")
        await set_status(event, f"Posting the index for {len(index_entries)} post(s)...")
        index_messages = await post_index(client, target, None, index_entries)
        success(f"Index posted in {index_messages} message(s).")
    else:
        info("No caption produced an index title, so no index was posted.")

    exported = await retry_on_wait(
        client,
        functions.messages.ExportChatInviteRequest(peer=target, title="clone"),
    )
    link = getattr(exported, "link", None) or "no link returned"

    report = f"Cloned {copied} post(s) into {title!r}\n{link}"
    if index_entries:
        report += f"\nIndex: {len(index_entries)} entries in {index_messages} message(s)"
    if repointed:
        report += f"\nLinks repointed at the clone: {repointed}"
    if untouched:
        report += f"\nLinks left pointing at the source: {untouched}"
    if link_failures:
        report += f"\nLinks that could not be edited: {link_failures}"
    if skipped:
        report += f"\nSkipped: {skipped}"
    if failed:
        report += f"\nFailed: {failed}"

    saved = True
    try:
        await retry_on_wait(client.send_message, "me", report, link_preview=False)
    except Exception as exc:  # noqa: BLE001
        saved = False
        failure(f"Could not save the invite link to Saved Messages: {exc}")

    success(f"Clone finished: {copied} copied, {skipped} skipped, {failed} failed.")
    success(f"Invite link: {link}")
    await set_status(
        event,
        report + ("\nSaved to your Saved Messages." if saved else "\nNot saved; see log."),
    )


COMMANDS: dict[str, Callable[..., Awaitable[None]]] = {
    "change": command_change,
    "clone": command_clone,
}
COMMAND_HELP = (
    "Commands\n"
    ".change @newname          replace every username in every post\n"
    ".change @oldname @newname replace only that username\n"
    ".clone [title]            clone this chat, with its linked index\n"
    ".clone <link> [title]     clone that chat, by invite link or @handle\n"
    ".help                     show this list"
)


async def run_commands(client: TelegramClient) -> None:
    """Listen for the dot commands until interrupted."""
    busy = asyncio.Lock()

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]*))?$"))
    async def dispatch(event: object) -> None:
        name = (event.pattern_match.group(1) or "").lower()
        raw_args = (event.pattern_match.group(2) or "").strip()
        if name == "help":
            await set_status(event, COMMAND_HELP)
            return
        handler = COMMANDS.get(name)
        if handler is None:
            return
        if busy.locked():
            await set_status(event, "Still working on the previous command.")
            return
        async with busy:
            try:
                await handler(client, event, raw_args.split() if raw_args else [])
            except Exception as exc:  # noqa: BLE001
                failure(f".{name} failed: {exc}")
                await set_status(event, f".{name} failed: {exc}")

    print(Fore.YELLOW + Style.BRIGHT + "\n== COMMAND MODE ==")
    print(Fore.WHITE + COMMAND_HELP)
    info("Send the commands from this account in any chat. Ctrl+C to stop.")
    await client.run_until_disconnected()


def choose_mode() -> str:
    """Pick between the interactive copier and the live command listener."""
    configured = (os.getenv("MAIN_MODE") or "").strip().lower()
    if configured in {"copy", "commands"}:
        return configured

    print(Fore.YELLOW + Style.BRIGHT + "\n== MODE ==")
    print(Fore.GREEN + Style.BRIGHT + "  [  1] " + Fore.WHITE + "Copy photos between two chats")
    print(
        Fore.GREEN + Style.BRIGHT + "  [  2] "
        + Fore.WHITE + "Command mode (.change, .clone)"
    )
    while True:
        choice = input(Fore.CYAN + "Select number > ").strip()
        if choice == "1":
            return "copy"
        if choice == "2":
            return "commands"
        warning("Enter 1 or 2.")


async def main() -> None:
    """Sign in to Telegram and start the chosen mode."""
    show_banner()
    api_id, api_hash, session = read_credentials()
    info("Connecting to Telegram...")
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    success("Telegram account connected.")
    try:
        if choose_mode() == "commands":
            await run_commands(client)
        else:
            await run_copy(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        warning("Stopped by user.")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
