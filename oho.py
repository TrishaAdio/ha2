"""Mass-clone one Telegram source chat into many freshly created groups.

Several backup accounts share the work: the requested group total is divided
between them, and each account owns its share of groups. Every group is created
private, the main account joins it as an anonymous admin and copies the whole
source chat, then leaves. The owner wipes the join/leave service messages, turns
itself into an anonymous admin, makes the group public with join approval
enabled, and sends the link to the managing admin.

Run with `python oho.py`.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import random
import re
import string
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, Union

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils
from telethon.tl.custom import Dialog, Message

ChatEntity = Union[types.Channel, types.Chat]
Result = TypeVar("Result")

LOG = logging.getLogger("oho")
LOG_FILE = "oho.log"

DEFAULT_SHARE_WITH = "siyorou"
DEFAULT_GROUP_ABOUT = ""
DEFAULT_MESSAGE_DELAY = 1.0
DEFAULT_GROUP_DELAY = 15.0
MAX_OWNER_ACCOUNTS = 3
MIN_USERNAME_LENGTH = 5
MAX_USERNAME_LENGTH = 32
USERNAME_ATTEMPTS = 12
TRANSIENT_ATTEMPTS = 4
MEMBER_LOOKUP_ATTEMPTS = 3
ALBUM_LIMIT = 10

ASCII_BANNER = r"""
   ____  __  __ ____
  / __ \/ / / / __ \
 / / / / /_/ / / / /
/ /_/ / __  / /_/ /
\____/_/ /_/\____/
"""

WAIT_ERRORS = (
    errors.FloodWaitError,
    errors.FloodPremiumWaitError,
    errors.SlowModeWaitError,
)
TRANSIENT_ERRORS = (
    errors.ServerError,
    errors.RpcCallFailError,
    errors.TimedOutError,
    ConnectionError,
    asyncio.TimeoutError,
)
STALE_MEDIA_ERRORS = (
    errors.FileReferenceExpiredError,
    errors.FileReferenceInvalidError,
    errors.FileReferenceEmptyError,
)

# Full supergroup owner rights. Telegram resets every privilege when an owner
# edits its own admin entry, so the whole set is always sent back together.
OWNER_ANONYMOUS_RIGHTS = types.ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=True,
    anonymous=True,
    manage_call=True,
    other=True,
    manage_topics=True,
)
# Enough for the copier to publish as the group and clean up after itself.
COPIER_ANONYMOUS_RIGHTS = types.ChatAdminRights(
    delete_messages=True,
    pin_messages=True,
    anonymous=True,
    other=True,
)


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
class ColorFormatter(logging.Formatter):
    """Format console log records with one color per severity."""

    COLORS = {
        logging.DEBUG: Fore.BLUE,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }
    MARKS = {
        logging.DEBUG: "...",
        logging.INFO: "[i]",
        logging.WARNING: "[!]",
        logging.ERROR: "[-]",
        logging.CRITICAL: "[x]",
    }

    def format(self, record: logging.LogRecord) -> str:
        """Return the colored single-line rendering of a log record."""
        color = self.COLORS.get(record.levelno, "")
        mark = self.MARKS.get(record.levelno, "[i]")
        stamp = self.formatTime(record, "%H:%M:%S")
        return f"{color}{stamp} {mark} {record.getMessage()}{Style.RESET_ALL}"


def setup_logging() -> None:
    """Send colored logs to the terminal and plain logs to the log file."""
    colorama_init(autoreset=True)
    LOG.setLevel(logging.DEBUG)
    LOG.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(ColorFormatter())

    log_file = logging.FileHandler(LOG_FILE, encoding="utf-8")
    log_file.setLevel(logging.DEBUG)
    log_file.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    )

    LOG.handlers.clear()
    LOG.addHandler(console)
    LOG.addHandler(log_file)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def show_banner() -> None:
    """Print the startup banner and the pipeline summary."""
    print(Fore.GREEN + Style.BRIGHT + ASCII_BANNER)
    print(Fore.CYAN + Style.BRIGHT + "  OHO | MULTI ACCOUNT GROUP CLONER")
    print(
        Fore.BLUE
        + "  create private -> copy all -> clean up -> public + approval -> share\n"
    )


def heading(text: str) -> None:
    """Print a bright section heading."""
    print(Fore.YELLOW + Style.BRIGHT + f"\n== {text.upper()} ==")


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Everything the run needs, from the environment and the prompts."""

    api_id: int
    api_hash: str
    main_session: str
    owner_sessions: tuple[str, ...]
    share_with: str
    group_title: str
    username_prefix: str
    group_about: str
    total_groups: int
    message_delay: float
    group_delay: float
    copy_limit: int | None


def env_text(name: str, default: str = "") -> str:
    """Read a stripped environment value."""
    return (os.getenv(name) or "").strip() or default


def env_number(name: str, default: float) -> float:
    """Read a numeric environment value, falling back when it is unusable."""
    raw = env_text(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        LOG.warning("%s is not a number (%r); using %s.", name, raw, default)
        return default
    if value < 0:
        LOG.warning("%s cannot be negative; using %s.", name, default)
        return default
    return value


def ask(prompt: str, default: str = "") -> str:
    """Ask for a line of input, offering a default when one exists."""
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(Fore.CYAN + f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default:
            return default
        print(Fore.YELLOW + "  A value is required.")


def ask_int(prompt: str, minimum: int, maximum: int, default: str = "") -> int:
    """Ask for an integer inside an inclusive range."""
    while True:
        raw = ask(prompt, default)
        try:
            value = int(raw)
        except ValueError:
            print(Fore.YELLOW + "  Enter a whole number.")
            continue
        if minimum <= value <= maximum:
            return value
        print(Fore.YELLOW + f"  Enter a number between {minimum} and {maximum}.")


def ask_yes(prompt: str) -> bool:
    """Ask a yes or no question, defaulting to no."""
    return ask(f"{prompt} (y/n)", "n").lower().startswith("y")


def valid_username_prefix(prefix: str) -> str | None:
    """Return the reason a public-link prefix is unusable, or None when fine."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", prefix):
        return "Start with a letter and use only letters, digits and underscore."
    if "__" in prefix:
        return "Double underscores are not allowed."
    if len(prefix) > MAX_USERNAME_LENGTH - 3:
        return f"Keep it under {MAX_USERNAME_LENGTH - 2} characters."
    return None


def read_credentials() -> tuple[int, str]:
    """Read the shared Telegram API credentials."""
    raw_api_id = env_text("TELEGRAM_API_ID") or ask("Telegram API ID")
    api_hash = env_text("TELEGRAM_API_HASH") or getpass.getpass(
        Fore.CYAN + "Telegram API hash: "
    ).strip()
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise ValueError("TELEGRAM_API_ID must be a positive integer.")
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")
    return api_id, api_hash


def read_owner_sessions() -> tuple[str, ...]:
    """Decide which backup-account sessions will own the created groups."""
    configured = env_text("OWNER_SESSIONS")
    if configured:
        sessions = tuple(
            name.strip() for name in configured.split(",") if name.strip()
        )
        if not sessions:
            raise ValueError("OWNER_SESSIONS is set but lists no session names.")
        return sessions

    count = ask_int(
        f"How many backup accounts will own the groups (1-{MAX_OWNER_ACCOUNTS})",
        1,
        MAX_OWNER_ACCOUNTS,
        "3",
    )
    return tuple(f"oho_owner{number}" for number in range(1, count + 1))


def read_settings() -> Settings:
    """Collect configuration from the environment and the operator."""
    load_dotenv()
    api_id, api_hash = read_credentials()
    owner_sessions = read_owner_sessions()

    heading("run setup")
    total_groups = ask_int("How many groups in total", 1, 500, "6")
    group_title = ask("Group title base", env_text("GROUP_TITLE", "Archive"))

    default_prefix = env_text("GROUP_USERNAME_PREFIX")
    while True:
        username_prefix = ask("Public link prefix", default_prefix)
        problem = valid_username_prefix(username_prefix)
        if problem is None:
            break
        print(Fore.YELLOW + f"  {problem}")
        default_prefix = ""

    copy_limit_raw = env_text("COPY_LIMIT")
    copy_limit = int(copy_limit_raw) if copy_limit_raw.isdigit() else None

    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        main_session=env_text("MAIN_SESSION", "oho_main"),
        owner_sessions=owner_sessions,
        share_with=env_text("SHARE_WITH", DEFAULT_SHARE_WITH).lstrip("@"),
        group_title=group_title,
        username_prefix=username_prefix,
        group_about=env_text("GROUP_ABOUT", DEFAULT_GROUP_ABOUT),
        total_groups=total_groups,
        message_delay=env_number("MESSAGE_DELAY", DEFAULT_MESSAGE_DELAY),
        group_delay=env_number("GROUP_DELAY", DEFAULT_GROUP_DELAY),
        copy_limit=copy_limit,
    )


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
async def resilient(
    action: Callable[..., Awaitable[Result]],
    *args: object,
    label: str,
    **kwargs: object,
) -> Result:
    """Run a Telegram call, waiting out limits and retrying transient faults."""
    transient_left = TRANSIENT_ATTEMPTS
    while True:
        try:
            return await action(*args, **kwargs)
        except WAIT_ERRORS as exc:
            delay = exc.seconds + 1
            LOG.warning("%s: Telegram asked to wait %ss.", label, delay)
            await asyncio.sleep(delay)
        except TRANSIENT_ERRORS as exc:
            transient_left -= 1
            if transient_left <= 0:
                LOG.error("%s: giving up after transient failures (%s).", label, exc)
                raise
            delay = (TRANSIENT_ATTEMPTS - transient_left) * 5
            LOG.warning("%s: transient failure (%s); retrying in %ss.", label, exc, delay)
            await asyncio.sleep(delay)


async def request(client: TelegramClient, query: object, *, label: str) -> object:
    """Send one raw MTProto request through the resilient wrapper."""
    return await resilient(client, query, label=label)


def channel_from_updates(updates: object) -> types.Channel:
    """Pull the supergroup out of an Updates envelope."""
    for chat in getattr(updates, "chats", None) or []:
        if isinstance(chat, types.Channel):
            return chat
    raise RuntimeError("Telegram did not return the group in its response.")


def invite_hash(link: str) -> str:
    """Extract the joinable hash from a private invite link."""
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return tail.lstrip("+")


def describe_user(user: types.User) -> str:
    """Render an account as name, username and id for the logs."""
    name = utils.get_display_name(user) or "unnamed"
    handle = f"@{user.username}" if user.username else "no username"
    return f"{name} ({handle}, id {user.id})"


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """One signed-in Telegram account."""

    session: str
    client: TelegramClient
    user: types.User

    @property
    def label(self) -> str:
        """Return a short identifier used in log lines."""
        return f"{self.session}/{describe_user(self.user)}"


async def sign_in(session: str, settings: Settings, role: str) -> Account:
    """Sign a session in, prompting for the login code when needed."""
    LOG.info("Signing in %s account from session %r...", role, session)
    client = TelegramClient(session, settings.api_id, settings.api_hash)
    await client.start()
    user = await client.get_me()
    if user is None:
        raise RuntimeError(f"Session {session!r} is not authorized.")
    LOG.info("%s account ready: %s", role.capitalize(), describe_user(user))
    return Account(session=session, client=client, user=user)


async def sign_in_all(settings: Settings) -> tuple[Account, list[Account]]:
    """Sign in the main copier account and every owner account.

    Any account already connected is disconnected again if a later sign-in
    fails, so a partial start never leaves sessions open.
    """
    heading("accounts")
    signed_in: list[Account] = []
    try:
        main = await sign_in(settings.main_session, settings, "main")
        signed_in.append(main)

        owners: list[Account] = []
        seen = {main.user.id}
        for session in settings.owner_sessions:
            owner = await sign_in(session, settings, "owner")
            signed_in.append(owner)
            if owner.user.id in seen:
                raise RuntimeError(
                    f"Session {session!r} is the same account as one already "
                    "signed in. Every owner account must be distinct from the "
                    "main account."
                )
            seen.add(owner.user.id)
            owners.append(owner)
    except BaseException:
        for account in signed_in:
            await account.client.disconnect()
        raise
    return main, owners


# ---------------------------------------------------------------------------
# source selection
# ---------------------------------------------------------------------------
def is_supported_chat(entity: object) -> bool:
    """Return whether an entity can be used as a copy source."""
    if isinstance(entity, types.Channel):
        if getattr(entity, "monoforum", False):
            return False
        return bool(entity.broadcast or entity.megagroup or entity.gigagroup)
    if isinstance(entity, types.Chat):
        return not (entity.deactivated or entity.migrated_to)
    return False


def chat_type_label(entity: ChatEntity) -> str:
    """Describe a chat so channels and groups are distinguishable."""
    if isinstance(entity, types.Chat):
        return "group"
    if entity.gigagroup:
        return "broadcast group"
    if entity.broadcast:
        return "channel"
    if entity.forum:
        return "forum group"
    return "supergroup"


async def choose_source(main: Account) -> ChatEntity:
    """Resolve the source chat from the environment or an interactive menu."""
    configured = env_text("SOURCE_CHAT")
    if configured:
        LOG.info("Resolving configured source %r...", configured)
        entity = await main.client.get_entity(configured)
        if not is_supported_chat(entity):
            raise RuntimeError(f"{configured!r} is not a channel or group.")
        LOG.info("Source: %s", utils.get_display_name(entity))
        return entity

    dialogs: list[Dialog] = []
    async for dialog in main.client.iter_dialogs():
        if is_supported_chat(dialog.entity):
            dialogs.append(dialog)
    if not dialogs:
        raise RuntimeError("The main account is not in any channel or group.")

    heading("source chat")
    for number, dialog in enumerate(dialogs, start=1):
        kind = chat_type_label(dialog.entity)
        print(
            Fore.GREEN + Style.BRIGHT + f"  [{number:>3}] "
            + Fore.WHITE + f"{dialog.name} [{kind}]"
        )
    choice = ask_int("Select number", 1, len(dialogs))
    entity = dialogs[choice - 1].entity
    LOG.info("Source: %s", utils.get_display_name(entity))
    return entity


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GroupPlan:
    """One group to build, and the account that will own it."""

    index: int
    owner_position: int
    title: str


def distribute(total: int, buckets: int) -> list[int]:
    """Split a total as evenly as possible, giving remainders to the front."""
    base, remainder = divmod(total, buckets)
    return [base + (1 if position < remainder else 0) for position in range(buckets)]


def build_plan(settings: Settings, owners: Sequence[Account]) -> list[GroupPlan]:
    """Assign every group to an owner account in interleaved order."""
    shares = distribute(settings.total_groups, len(owners))
    plans: list[GroupPlan] = []
    index = 0
    for owner_position, share in enumerate(shares):
        for _ in range(share):
            index += 1
            plans.append(
                GroupPlan(
                    index=index,
                    owner_position=owner_position,
                    title=f"{settings.group_title} {index}",
                )
            )
    return plans


def show_plan(
    settings: Settings,
    owners: Sequence[Account],
    plans: Sequence[GroupPlan],
    source: ChatEntity,
) -> None:
    """Print the plan so it can be confirmed before anything is created."""
    shares = distribute(settings.total_groups, len(owners))
    heading("plan")
    print(Fore.WHITE + f"  Source        : {utils.get_display_name(source)}")
    print(Fore.WHITE + f"  Groups        : {settings.total_groups}")
    print(Fore.WHITE + f"  Owner accounts: {len(owners)}")
    for owner, share in zip(owners, shares, strict=True):
        owned = [plan.title for plan in plans if owners[plan.owner_position] is owner]
        print(
            Fore.GREEN + f"    - {owner.session}: {share} group(s) "
            + Fore.WHITE + f"({', '.join(owned)})"
        )
    print(Fore.WHITE + f"  Public links  : t.me/{settings.username_prefix}<n>")
    print(Fore.WHITE + f"  Link goes to  : @{settings.share_with}")
    print(
        Fore.WHITE
        + f"  Pacing        : {settings.message_delay}s per message, "
        f"{settings.group_delay}s between groups"
    )
    if settings.copy_limit:
        print(Fore.YELLOW + f"  COPY_LIMIT    : {settings.copy_limit} messages per group")


# ---------------------------------------------------------------------------
# copying every source message
# ---------------------------------------------------------------------------
@dataclass
class CopyStats:
    """Counters describing one source-to-group copy."""

    copied: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self) -> str:
        """Render the counters for a log line."""
        return f"{self.copied} copied, {self.skipped} skipped, {self.failed} failed"


@dataclass
class CopySession:
    """Mutable state shared by every send in a single group copy."""

    main: Account
    source: ChatEntity
    group: types.Channel
    settings: Settings
    tag: str
    id_map: dict[int, int] = field(default_factory=dict)
    stats: CopyStats = field(default_factory=CopyStats)


def is_copyable(message: Message) -> bool:
    """Return whether a source message carries content worth copying."""
    if isinstance(message, types.MessageService) or message.action is not None:
        return False
    return bool(message.message or message.media)


async def iter_source_messages(
    main: Account, source: ChatEntity, limit: int | None
) -> AsyncIterator[Message]:
    """Yield every copyable source message, oldest first."""
    async for message in main.client.iter_messages(source, reverse=True, limit=limit):
        if is_copyable(message):
            yield message


async def iter_source_batches(
    main: Account, source: ChatEntity, limit: int | None
) -> AsyncIterator[list[Message]]:
    """Yield source messages oldest first, keeping albums together."""
    pending: list[Message] = []
    pending_group: int | None = None

    async for message in iter_source_messages(main, source, limit):
        if message.grouped_id is None:
            if pending:
                yield pending
                pending = []
                pending_group = None
            yield [message]
            continue

        if pending and (
            message.grouped_id != pending_group or len(pending) >= ALBUM_LIMIT
        ):
            yield pending
            pending = []

        pending_group = message.grouped_id
        pending.append(message)

    if pending:
        yield pending


def build_input_media(message: Message) -> object | None:
    """Convert source media into resendable input media, or None for text."""
    media = message.media
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return None
    input_media = utils.get_input_media(media)
    if isinstance(input_media, types.InputMediaEmpty):
        raise TypeError(f"{type(media).__name__} cannot be resent")
    if hasattr(input_media, "spoiler"):
        input_media.spoiler = bool(getattr(media, "spoiler", False))
    return input_media


async def refresh_message(session: CopySession, message: Message) -> Message:
    """Re-fetch a source message so its file references are valid again."""
    fresh = await resilient(
        session.main.client.get_messages,
        session.source,
        ids=message.id,
        label=f"{session.tag} refresh {message.id}",
    )
    if fresh is None:
        raise RuntimeError(f"source message {message.id} is gone")
    return fresh


async def uploaded_media(
    session: CopySession, message: Message, directory: Path
) -> object:
    """Download source media and upload it again as a last resort."""
    path = await resilient(
        session.main.client.download_media,
        message,
        file=str(directory / f"{message.id}"),
        label=f"{session.tag} download {message.id}",
    )
    if not path:
        raise RuntimeError(f"could not download media of message {message.id}")
    handle = await resilient(
        session.main.client.upload_file,
        path,
        label=f"{session.tag} upload {message.id}",
    )
    if message.photo:
        return types.InputMediaUploadedPhoto(
            file=handle, spoiler=bool(getattr(message.media, "spoiler", False))
        )
    return types.InputMediaUploadedDocument(
        file=handle,
        mime_type=message.file.mime_type if message.file else "application/octet-stream",
        attributes=list(getattr(message.document, "attributes", None) or []),
        spoiler=bool(getattr(message.media, "spoiler", False)),
    )


def reply_header(session: CopySession, message: Message) -> object | None:
    """Point a copied message at the copy of whatever it replied to."""
    reply_to = message.reply_to
    if not isinstance(reply_to, types.MessageReplyHeader):
        return None
    if reply_to.reply_to_peer_id is not None:
        return None
    target = session.id_map.get(reply_to.reply_to_msg_id)
    if target is None:
        return None
    return types.InputReplyToMessage(reply_to_msg_id=target)


async def send_text(session: CopySession, message: Message) -> list[Message]:
    """Copy a text-only message, keeping formatting and preview behaviour."""
    query = functions.messages.SendMessageRequest(
        peer=session.group,
        message=message.message,
        entities=list(message.entities or []),
        no_webpage=not message.web_preview,
        reply_to=reply_header(session, message),
    )
    result = await request(
        session.main.client, query, label=f"{session.tag} text {message.id}"
    )
    sent = session.main.client._get_response_message(query, result, session.group)
    return [sent] if sent else []


async def send_media(
    session: CopySession, message: Message, media: object
) -> list[Message]:
    """Copy a single media message with its caption and spoiler intact."""
    query = functions.messages.SendMediaRequest(
        peer=session.group,
        media=media,
        message=message.message or "",
        entities=list(message.entities or []),
        reply_to=reply_header(session, message),
    )
    result = await request(
        session.main.client, query, label=f"{session.tag} media {message.id}"
    )
    sent = session.main.client._get_response_message(query, result, session.group)
    return [sent] if sent else []


async def send_album(
    session: CopySession, messages: Sequence[Message], medias: Sequence[object]
) -> list[Message]:
    """Copy an album, preserving every per-photo caption and spoiler."""
    items = [
        types.InputSingleMedia(
            media=media,
            message=message.message or "",
            entities=list(message.entities or []),
        )
        for message, media in zip(messages, medias, strict=True)
    ]
    query = functions.messages.SendMultiMediaRequest(
        peer=session.group,
        multi_media=items,
        reply_to=reply_header(session, messages[0]),
    )
    ids = ", ".join(str(message.id) for message in messages)
    try:
        result = await request(
            session.main.client, query, label=f"{session.tag} album {ids}"
        )
    except errors.SlowModeMultiMsgsDisabledError:
        LOG.warning("%s: slow mode forbids albums; sending %s one by one.", session.tag, ids)
        sent: list[Message] = []
        for message, media in zip(messages, medias, strict=True):
            sent.extend(await send_media(session, message, media))
        return sent

    random_ids = [item.random_id for item in items]
    produced = session.main.client._get_response_message(
        random_ids, result, session.group
    )
    if not produced:
        return []
    return list(produced) if isinstance(produced, list) else [produced]


async def copy_batch(session: CopySession, batch: Sequence[Message]) -> None:
    """Copy one message or album, remapping reply links and counting results."""
    ids = ", ".join(str(message.id) for message in batch)

    try:
        medias = [build_input_media(message) for message in batch]
    except (TypeError, ValueError, AttributeError) as exc:
        session.stats.skipped += len(batch)
        LOG.warning("%s: skipping %s (%s).", session.tag, ids, exc)
        return

    async def deliver(
        current: Sequence[Message], current_medias: Sequence[object]
    ) -> list[Message]:
        if len(current) > 1:
            return await send_album(session, current, current_medias)
        if current_medias[0] is None:
            return await send_text(session, current[0])
        return await send_media(session, current[0], current_medias[0])

    try:
        sent = await deliver(batch, medias)
    except STALE_MEDIA_ERRORS:
        LOG.warning("%s: file references for %s expired; refreshing.", session.tag, ids)
        try:
            refreshed = [await refresh_message(session, message) for message in batch]
            sent = await deliver(refreshed, [build_input_media(m) for m in refreshed])
        except Exception as exc:  # Re-uploading is the final fallback.
            LOG.warning("%s: resend of %s still failing (%s); re-uploading.", session.tag, ids, exc)
            try:
                with tempfile.TemporaryDirectory(prefix="oho-") as temp_dir:
                    directory = Path(temp_dir)
                    fresh_media = [
                        None
                        if message.media is None
                        or isinstance(message.media, types.MessageMediaWebPage)
                        else await uploaded_media(session, message, directory)
                        for message in batch
                    ]
                    sent = await deliver(batch, fresh_media)
            except Exception as final_exc:  # noqa: BLE001
                session.stats.failed += len(batch)
                LOG.error("%s: gave up on %s (%s).", session.tag, ids, final_exc)
                return
    except Exception as exc:  # noqa: BLE001
        session.stats.failed += len(batch)
        LOG.error("%s: could not copy %s (%s).", session.tag, ids, exc)
        return

    if not sent:
        session.stats.failed += len(batch)
        LOG.error("%s: Telegram returned no message for %s.", session.tag, ids)
        return

    for source_message, new_message in zip(batch, sent, strict=False):
        session.id_map[source_message.id] = new_message.id
    session.stats.copied += len(batch)
    LOG.debug("%s: copied %s.", session.tag, ids)


async def copy_source_into_group(
    main: Account,
    source: ChatEntity,
    group: types.Channel,
    settings: Settings,
    tag: str,
) -> CopyStats:
    """Copy every source message into the group in original order."""
    session = CopySession(
        main=main, source=source, group=group, settings=settings, tag=tag
    )
    batches = 0
    async for batch in iter_source_batches(main, source, settings.copy_limit):
        await copy_batch(session, batch)
        batches += 1
        if batches % 25 == 0:
            LOG.info("%s: %s so far.", tag, session.stats.summary())
        if settings.message_delay:
            await asyncio.sleep(settings.message_delay)
    LOG.info("%s: copy finished, %s.", tag, session.stats.summary())
    return session.stats


# ---------------------------------------------------------------------------
# pipeline stages
# ---------------------------------------------------------------------------
async def create_private_group(
    owner: Account, plan: GroupPlan, settings: Settings, tag: str
) -> types.Channel:
    """Create the supergroup, which starts private because it has no link."""
    result = await request(
        owner.client,
        functions.channels.CreateChannelRequest(
            title=plan.title,
            about=settings.group_about,
            megagroup=True,
        ),
        label=f"{tag} create",
    )
    group = channel_from_updates(result)
    LOG.info("%s: created private group %r (id %s).", tag, plan.title, group.id)
    return group


async def invite_main(
    owner: Account, main: Account, group: types.Channel, tag: str
) -> types.Channel:
    """Let the main account join through a one-off invite link.

    Joining by link avoids the privacy restrictions that block adding a user
    directly, and the reply carries the group with an access hash that is
    valid for the main account.
    """
    exported = await request(
        owner.client,
        functions.messages.ExportChatInviteRequest(peer=group, title="oho setup"),
        label=f"{tag} export invite",
    )
    link = getattr(exported, "link", None)
    if not link:
        raise RuntimeError("Telegram did not return an invite link.")

    joined = await request(
        main.client,
        functions.messages.ImportChatInviteRequest(hash=invite_hash(link)),
        label=f"{tag} join",
    )
    main_group = channel_from_updates(joined)
    LOG.info("%s: main account joined the group.", tag)
    return main_group


async def find_participant(
    owner: Account, group: types.Channel, user_id: int, tag: str
) -> types.InputUser:
    """Locate a member through the owner, which yields a usable access hash.

    A fresh join can take a moment to show up in the member list, so the
    lookup is attempted a few times before giving up.
    """
    for attempt in range(1, MEMBER_LOOKUP_ATTEMPTS + 1):
        participants = await resilient(
            owner.client.get_participants, group, label=f"{tag} participants"
        )
        for participant in participants:
            if participant.id == user_id:
                return utils.get_input_user(participant)
        if attempt < MEMBER_LOOKUP_ATTEMPTS:
            LOG.debug("%s: member %s not listed yet; retrying.", tag, user_id)
            await asyncio.sleep(2)
    raise RuntimeError(f"user {user_id} never appeared in the member list")


async def promote_anonymous(
    owner: Account,
    group: types.Channel,
    who: types.InputUser | types.InputUserSelf,
    rights: types.ChatAdminRights,
    tag: str,
    label: str,
) -> None:
    """Grant anonymous admin rights so posts are signed by the group."""
    await request(
        owner.client,
        functions.channels.EditAdminRequest(
            channel=group,
            user_id=who,
            admin_rights=rights,
            rank="",
        ),
        label=f"{tag} promote {label}",
    )
    LOG.info("%s: %s is now an anonymous admin.", tag, label)


async def leave_group(main: Account, group: types.Channel, tag: str) -> None:
    """Remove the main account from the group once copying is done."""
    await request(
        main.client,
        functions.channels.LeaveChannelRequest(channel=group),
        label=f"{tag} leave",
    )
    LOG.info("%s: main account left the group.", tag)


async def purge_service_messages(
    owner: Account, group: types.Channel, tag: str
) -> int:
    """Delete the join, leave and creation notices so no trace is left."""
    stale: list[int] = []
    async for message in owner.client.iter_messages(group):
        if isinstance(message, types.MessageService) or message.action is not None:
            stale.append(message.id)
    if not stale:
        LOG.info("%s: no service messages to remove.", tag)
        return 0

    await resilient(
        owner.client.delete_messages,
        group,
        stale,
        label=f"{tag} delete service messages",
    )
    LOG.info("%s: removed %s service message(s).", tag, len(stale))
    return len(stale)


def username_candidates(prefix: str, index: int) -> Iterable[str]:
    """Yield public-link candidates, adding random suffixes after the first."""
    base = f"{prefix}{index}"
    yield base.ljust(MIN_USERNAME_LENGTH, "0")
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(USERNAME_ATTEMPTS):
        suffix = "".join(random.choices(alphabet, k=4))
        candidate = f"{base}{suffix}"
        yield candidate[:MAX_USERNAME_LENGTH]


async def make_group_public(
    owner: Account, group: types.Channel, plan: GroupPlan, settings: Settings, tag: str
) -> str:
    """Claim a public username, which is what turns the group public."""
    for candidate in username_candidates(settings.username_prefix, plan.index):
        try:
            available = await request(
                owner.client,
                functions.channels.CheckUsernameRequest(
                    channel=group, username=candidate
                ),
                label=f"{tag} check @{candidate}",
            )
        except errors.UsernameInvalidError:
            LOG.warning("%s: @%s is not a valid link; trying another.", tag, candidate)
            continue
        if not available:
            LOG.warning("%s: @%s is taken; trying another.", tag, candidate)
            continue

        try:
            await request(
                owner.client,
                functions.channels.UpdateUsernameRequest(
                    channel=group, username=candidate
                ),
                label=f"{tag} set @{candidate}",
            )
        except errors.UsernameOccupiedError:
            LOG.warning("%s: @%s was taken meanwhile; trying another.", tag, candidate)
            continue
        LOG.info("%s: group is public at t.me/%s.", tag, candidate)
        return candidate

    raise RuntimeError("could not claim any public link for this group")


async def enable_join_requests(owner: Account, group: types.Channel, tag: str) -> None:
    """Require admin approval to join.

    Telegram rejects this with CHAT_PUBLIC_REQUIRED unless the group already
    has a public link, so this always runs after the username is claimed.
    """
    try:
        await request(
            owner.client,
            functions.channels.ToggleJoinRequestRequest(channel=group, enabled=True),
            label=f"{tag} join requests",
        )
    except errors.ChatNotModifiedError:
        LOG.info("%s: join approval was already enabled.", tag)
        return
    LOG.info("%s: join approval enabled.", tag)


async def share_link(
    owner: Account,
    main: Account,
    settings: Settings,
    plan: GroupPlan,
    username: str,
    tag: str,
) -> bool:
    """Send the finished group link to the managing admin."""
    text = f"{plan.title}\nhttps://t.me/{username}"
    for account in (owner, main):
        try:
            await resilient(
                account.client.send_message,
                settings.share_with,
                text,
                label=f"{tag} share via {account.session}",
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "%s: %s could not message @%s (%s).",
                tag,
                account.session,
                settings.share_with,
                exc,
            )
            continue
        LOG.info("%s: link sent to @%s by %s.", tag, settings.share_with, account.session)
        return True
    LOG.error("%s: link could not be delivered; it is t.me/%s.", tag, username)
    return False


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
@dataclass
class GroupOutcome:
    """What happened to one planned group."""

    plan: GroupPlan
    owner_session: str
    username: str | None = None
    stats: CopyStats | None = None
    service_removed: int = 0
    shared: bool = False
    stage: str = "planned"
    error: str | None = None

    @property
    def link(self) -> str | None:
        """Return the public link once the group has one."""
        return f"https://t.me/{self.username}" if self.username else None

    @property
    def ok(self) -> bool:
        """Return whether the group finished the whole pipeline."""
        return self.error is None and self.stage == "done"


async def build_group(
    main: Account,
    owner: Account,
    source: ChatEntity,
    plan: GroupPlan,
    settings: Settings,
) -> GroupOutcome:
    """Run every stage for one group, reporting where it stopped on failure."""
    tag = f"group {plan.index}/{settings.total_groups} [{owner.session}]"
    outcome = GroupOutcome(plan=plan, owner_session=owner.session)
    LOG.info("%s: starting %r.", tag, plan.title)

    try:
        outcome.stage = "create"
        group = await create_private_group(owner, plan, settings, tag)

        outcome.stage = "join"
        main_group = await invite_main(owner, main, group, tag)

        outcome.stage = "promote main"
        main_as_member = await find_participant(owner, group, main.user.id, tag)
        await promote_anonymous(
            owner, group, main_as_member, COPIER_ANONYMOUS_RIGHTS, tag, "main account"
        )

        outcome.stage = "copy"
        outcome.stats = await copy_source_into_group(
            main, source, main_group, settings, tag
        )

        outcome.stage = "leave"
        await leave_group(main, main_group, tag)

        outcome.stage = "clean up"
        outcome.service_removed = await purge_service_messages(owner, group, tag)

        outcome.stage = "promote owner"
        await promote_anonymous(
            owner,
            group,
            types.InputUserSelf(),
            OWNER_ANONYMOUS_RIGHTS,
            tag,
            "owner",
        )

        outcome.stage = "publish"
        outcome.username = await make_group_public(owner, group, plan, settings, tag)

        outcome.stage = "join approval"
        await enable_join_requests(owner, group, tag)

        outcome.stage = "share"
        outcome.shared = await share_link(
            owner, main, settings, plan, outcome.username, tag
        )

        outcome.stage = "done"
        LOG.info("%s: finished, %s.", tag, outcome.link)
    except Exception as exc:  # noqa: BLE001
        outcome.error = str(exc)
        LOG.error("%s: failed during %s (%s).", tag, outcome.stage, exc)
    return outcome


def show_summary(outcomes: Sequence[GroupOutcome], elapsed: float) -> None:
    """Print the final per-group report."""
    heading("summary")
    for outcome in outcomes:
        if outcome.ok:
            stats = outcome.stats.summary() if outcome.stats else "no messages"
            print(
                Fore.GREEN + Style.BRIGHT + f"  [ok]   {outcome.plan.title}: "
                + Fore.WHITE + f"{outcome.link} | {stats} | "
                f"{outcome.service_removed} service message(s) removed | "
                f"link {'sent' if outcome.shared else 'NOT sent'}"
            )
        else:
            print(
                Fore.RED + Style.BRIGHT + f"  [fail] {outcome.plan.title}: "
                + Fore.WHITE + f"stopped at {outcome.stage} ({outcome.error})"
            )

    done = sum(1 for outcome in outcomes if outcome.ok)
    copied = sum(o.stats.copied for o in outcomes if o.stats)
    failed_messages = sum(o.stats.failed for o in outcomes if o.stats)
    skipped = sum(o.stats.skipped for o in outcomes if o.stats)
    print()
    LOG.info("Groups completed: %s/%s", done, len(outcomes))
    LOG.info("Messages copied: %s", copied)
    if skipped:
        LOG.warning("Messages skipped: %s", skipped)
    if failed_messages:
        LOG.error("Messages failed: %s", failed_messages)
    LOG.info("Total time: %s", f"{elapsed / 60:.1f} min")
    LOG.info("Full log written to %s", LOG_FILE)


async def run(settings: Settings) -> None:
    """Sign in, confirm the plan, then build every group in turn."""
    main, owners = await sign_in_all(settings)
    clients = [main.client, *[owner.client for owner in owners]]
    try:
        source = await choose_source(main)
        if getattr(source, "noforwards", False):
            LOG.warning(
                "The source has content protection enabled, so media may not be "
                "copyable. The run will report anything it has to skip."
            )

        plans = build_plan(settings, owners)
        show_plan(settings, owners, plans, source)
        if not ask_yes("\nStart creating groups now?"):
            LOG.info("Cancelled before anything was created.")
            return

        started = time.monotonic()
        outcomes: list[GroupOutcome] = []
        for position, plan in enumerate(plans):
            owner = owners[plan.owner_position]
            outcomes.append(await build_group(main, owner, source, plan, settings))
            if position + 1 < len(plans) and settings.group_delay:
                LOG.info("Pausing %ss before the next group...", settings.group_delay)
                await asyncio.sleep(settings.group_delay)
        show_summary(outcomes, time.monotonic() - started)
    finally:
        for client in clients:
            await client.disconnect()


async def main_entry() -> None:
    """Set up output, read the configuration, and start the run."""
    setup_logging()
    show_banner()
    settings = read_settings()
    await run(settings)


if __name__ == "__main__":
    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        LOG.warning("Stopped by user.")
    except (RuntimeError, ValueError) as exc:
        LOG.critical("%s", exc)
        raise SystemExit(1) from exc
