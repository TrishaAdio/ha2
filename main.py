"""Interactively copy every photo from one Telegram chat to another.

Broadcast channels, supergroups, forum groups, and legacy basic groups all work
as sources and destinations. Captions, Telegram formatting entities, emoji,
albums, and media spoilers are preserved. After copying, the script posts a
linked index made from the first line of every non-empty caption.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, Union

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils
from telethon.tl.custom import Dialog, Message

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


async def send_index_sticker(client: TelegramClient, destination: CopyTarget) -> bool:
    """Send the index sticker, reporting failures without stopping the run."""
    try:
        await retry_on_wait(
            client.send_file,
            destination.entity,
            INDEX_STICKER,
            reply_to=destination.reply_to_topic,
        )
        return True
    except Exception as exc:  # A stale sticker reference must not lose the index.
        warning(f"Index sticker skipped: {exc}")
        return False


async def send_index_chunk(
    client: TelegramClient,
    destination: CopyTarget,
    text: str,
    entities: Sequence[types.TypeMessageEntity],
) -> None:
    """Send one styled index message into the destination chat or topic."""
    await retry_on_wait(
        client.send_message,
        destination.entity,
        text,
        formatting_entities=list(entities),
        reply_to=destination.reply_to_topic,
        link_preview=False,
    )


async def post_index(
    client: TelegramClient,
    destination: CopyTarget,
    entries: Sequence[IndexEntry],
) -> int:
    """Send the index sticker, then all styled linked-index chunks."""
    if not entries:
        return 0

    await send_index_sticker(client, destination)

    count = 0
    for text, entities in make_index_messages(entries):
        try:
            await send_index_chunk(client, destination, text, entities)
        except Exception as exc:
            # Custom emoji need Telegram Premium; retry with the plain emoji.
            warning(f"Styled index chunk failed ({exc}); retrying without custom emoji.")
            try:
                await send_index_chunk(
                    client, destination, text, without_custom_emoji(entities)
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
    index_messages = await post_index(client, destination, index_entries)
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


async def main() -> None:
    """Sign in to Telegram and start the interactive copier."""
    show_banner()
    api_id, api_hash, session = read_credentials()
    info("Connecting to Telegram...")
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    success("Telegram account connected.")
    try:
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
