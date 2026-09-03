#!/usr/bin/env python3
"""Rotating channel cloner: exact copies published on a timer, then replaced.

The idea
--------
You register one or more *source* channels under short names, and one or more
*assigned* channels where the invite links are dropped:

    .setchannel OK          register the current chat as source "OK"
    .setchannel VIP @some   register that chat as source "VIP"
    .assign OK              drop OK's links into the current chat
    .interval 30            rotate every 30 minutes (10 is the floor)
    .start                  begin rotating

From then on every cycle does the same four things:

    1. create a brand new private channel per registered source
    2. clone the source into it byte for byte: text, media, albums, spoilers,
       replies, web previews and premium (custom) emoji all survive
    3. post the styled linked index at the end of the clone
    4. edit the assigned channel's post so its blockquote holds one invite
       link per line, one line per clone

When the interval expires every post in every clone is deleted, the clone
channels themselves are deleted, and the whole thing is built again from
scratch with fresh links.

Why this is not main.py's .clone
--------------------------------
main.py loses the ordering: it drops posts silently (service messages, its own
index, anything whose media cannot be resent, anything whose caption Telegram
refuses) and never accounts for the hole, so the source's 3rd post lands where
the 2nd should be and the index points at the wrong post from there on.

This script fixes that by making the post number a first class value:

    * every source post is collected and numbered *before* anything is sent,
      so a later failure can never renumber an earlier post
    * a post that cannot be recreated is recorded as an explicit gap; it never
      silently pulls the following posts up by one
    * the index always emits exactly one line per source post, in post order,
      so index line N is always source post N even when a post is a plain
      text post or its caption has no usable title
    * album replies are paired only when Telegram returns as many messages as
      were sent, and returned messages are sorted by id first, instead of
      zipping two lists of different lengths and hoping

Run setup.py once to create the virtualenv and the .env, then run this.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from colorama import Fore, Style
from colorama import init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, functions, types, utils

Result = TypeVar("Result")
Item = TypeVar("Item")
Message = Any  # telethon.tl.custom.Message, or types.MessageService

LOG = logging.getLogger("redirect")
LOG_FILE = "redirect.log"
STATE_FILE = "redirect_state.json"

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
# The index marker is a premium emoji: the plain 🟢 is the fallback Telegram
# shows to anyone who cannot render the custom one.
MARKER_EMOJI = "🟢"
MARKER_EMOJI_ID = 6298751564592973547
DEFAULT_INDEX_SUFFIX = " | Demo"
INDEX_STICKER = types.InputDocument(
    id=6016935932551241313,
    access_hash=-7366320401303529044,
    file_reference=bytes.fromhex("01004f3c996a58123cb397c2c4097adce4b99e40692afdf4f3"),
)

# Telegram refuses a message over 4096 characters and accepts about a hundred
# entities in one. Both are counted in UTF-16 units, with a margin left over.
MAX_MESSAGE_UNITS = 3900
MAX_MESSAGE_ENTITIES = 95

# A rotation faster than this is pointless: creating a channel, uploading the
# clone and exporting an invite already costs minutes on a busy account, and
# Telegram starts answering channel creation with flood waits.
MIN_INTERVAL_MINUTES = 10
DEFAULT_INTERVAL_MINUTES = 30

SEND_DELAY = 0.6
EDIT_DELAY = 0.4
DELETE_CHUNK = 100
DEFAULT_TEST_POSTS = 3

# How many times the invite link is repeated in the link post, one per line.
DEFAULT_LINK_REPEAT = 5
MAX_LINK_REPEAT = 50
# Only posts whose caption mentions this word are cloned. Empty means all.
DEFAULT_CAPTION_FILTER = "Dm"
TICK_SECONDS = 5.0
SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

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

ASCII_BANNER = r"""
   ____           ___              __
  / __ \___  ____/ (_)_______  ____/ /_
 / /_/ / _ \/ __  / / ___/ _ \/ ___/ __/
/ ____/  __/ /_/ / / /  /  __/ /__/ /_
\/    \___/\__,_/_/_/   \___/\___/\__/
"""


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
class ColorFormatter(logging.Formatter):
    """Format console records with one color and one mark per severity."""

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
        """Return the colored single line rendering of a log record."""
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
    log_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))

    LOG.handlers.clear()
    LOG.addHandler(console)
    LOG.addHandler(log_file)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def show_banner() -> None:
    """Print the startup banner."""
    print(Fore.GREEN + Style.BRIGHT + ASCII_BANNER)
    print(Fore.CYAN + Style.BRIGHT + "  ROTATING CHANNEL CLONER")
    print(Fore.BLUE + "  clone -> publish links -> wait -> wipe -> clone again\n")


def heading(text: str) -> None:
    """Print a bright section heading."""
    print(Fore.YELLOW + Style.BRIGHT + f"\n== {text.upper()} ==")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Everything read from the environment at startup."""

    api_id: int
    api_hash: str
    session: str
    index_suffix: str
    autostart: bool
    # These two only seed a state file that does not exist yet; afterwards
    # .interval and .clones own them, so an edit in Telegram is not undone by
    # the next restart.
    interval_minutes: int
    clones_per_source: int
    link_repeat: int
    caption_filter: str


def env_text(name: str, default: str = "") -> str:
    """Read a stripped environment value, falling back when it is empty."""
    value = (os.getenv(name) or "").strip()
    return value or default


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment value."""
    raw = env_text(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int) -> int:
    """Read a whole number from the environment, never below a floor."""
    raw = env_text(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOG.warning("%s is not a number; using %s.", name, default)
        return default
    if value < minimum:
        LOG.warning("%s is below %s; using %s.", name, minimum, minimum)
        return minimum
    return value


def read_settings() -> Settings:
    """Load the .env and validate the credentials it holds."""
    load_dotenv()
    raw_api_id = env_text("TELEGRAM_API_ID")
    api_hash = env_text("TELEGRAM_API_HASH")
    if not raw_api_id or not api_hash:
        raise ValueError(
            "TELEGRAM_API_ID and TELEGRAM_API_HASH are required; run setup.py first."
        )
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise ValueError("TELEGRAM_API_ID must be a positive integer.")

    # An empty suffix is a deliberate choice, so only an unset variable falls
    # back to the default.
    suffix = os.getenv("INDEX_SUFFIX")
    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        session=env_text("REDIRECT_SESSION", "redirect"),
        index_suffix=DEFAULT_INDEX_SUFFIX if suffix is None else suffix,
        autostart=env_flag("REDIRECT_AUTOSTART", False),
        interval_minutes=env_int(
            "REDIRECT_INTERVAL", DEFAULT_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES
        ),
        clones_per_source=env_int("REDIRECT_CLONES", 1, 1),
        link_repeat=min(
            MAX_LINK_REPEAT, env_int("REDIRECT_LINK_REPEAT", DEFAULT_LINK_REPEAT, 1)
        ),
        # An unset variable takes the default; an empty one means no filter.
        caption_filter=(
            DEFAULT_CAPTION_FILTER
            if os.getenv("REDIRECT_FILTER") is None
            else env_text("REDIRECT_FILTER")
        ),
    )


# ---------------------------------------------------------------------------
# peers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PeerRef:
    """A chat stored well enough to be used again after a restart.

    Telethon can usually resolve a bare id from its session cache, but a cache
    that was rebuilt loses it, so the access hash travels with the reference.
    """

    kind: str  # "channel" or "chat"
    id: int
    access_hash: int
    title: str
    username: str | None = None

    @classmethod
    def of(cls, entity: object) -> PeerRef:
        """Describe a Telethon entity as a storable reference."""
        title = utils.get_display_name(entity) or "Untitled"
        if isinstance(entity, types.Channel):
            return cls(
                kind="channel",
                id=entity.id,
                access_hash=entity.access_hash or 0,
                title=title,
                username=entity_username(entity),
            )
        if isinstance(entity, types.Chat):
            return cls(kind="chat", id=entity.id, access_hash=0, title=title)
        raise TypeError(f"{type(entity).__name__} is not a channel or group")

    @property
    def input_peer(self) -> types.TypeInputPeer:
        """Return the peer to send to and read from."""
        if self.kind == "chat":
            return types.InputPeerChat(chat_id=self.id)
        return types.InputPeerChannel(channel_id=self.id, access_hash=self.access_hash)

    @property
    def input_channel(self) -> types.InputChannel:
        """Return the channel handle needed to delete the channel."""
        if self.kind != "channel":
            raise TypeError(f"{self.title} is not a channel")
        return types.InputChannel(channel_id=self.id, access_hash=self.access_hash)

    def post_link(self, message_id: int) -> str | None:
        """Return a permalink to one post, or None when there can be none."""
        if self.kind != "channel":
            return None  # Legacy basic groups have no per-message permalinks.
        if self.username:
            return f"https://t.me/{self.username}/{message_id}"
        return f"https://t.me/c/{self.id}/{message_id}"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of this reference."""
        return {
            "kind": self.kind,
            "id": self.id,
            "access_hash": self.access_hash,
            "title": self.title,
            "username": self.username,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeerRef:
        """Rebuild a reference from its JSON form."""
        return cls(
            kind=data.get("kind", "channel"),
            id=int(data["id"]),
            access_hash=int(data.get("access_hash") or 0),
            title=data.get("title") or "Untitled",
            username=data.get("username") or None,
        )


def entity_username(entity: object) -> str | None:
    """Return a chat's active username, preferring the main one."""
    username = getattr(entity, "username", None)
    if username:
        return username
    for extra in getattr(entity, "usernames", None) or []:
        if getattr(extra, "active", False) and extra.username:
            return extra.username
    return None


def is_supported_chat(entity: object) -> bool:
    """Return whether an entity is a chat this script can read or write."""
    if isinstance(entity, types.Channel):
        if getattr(entity, "monoforum", False):
            return False
        return bool(entity.broadcast or entity.megagroup or entity.gigagroup)
    if isinstance(entity, types.Chat):
        return not (entity.deactivated or entity.migrated_to)
    return False


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
    """Return whether a token names a chat."""
    return bool(
        INVITE_RE.match(token) or PUBLIC_RE.match(token) or PRIVATE_ID_RE.match(token)
    )


async def resolve_chat(client: TelegramClient, token: str) -> object:
    """Resolve a link, invite or @handle into a usable chat entity."""
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


async def resolve_invite(client: TelegramClient, invite_hash: str) -> object:
    """Turn an invite hash into a chat, joining only when necessary."""
    checked = await retry_on_wait(
        client, functions.messages.CheckChatInviteRequest(hash=invite_hash)
    )
    existing = getattr(checked, "chat", None)
    if existing is not None:
        return existing  # Already a member: joining again would fail.
    joined = await retry_on_wait(
        client, functions.messages.ImportChatInviteRequest(hash=invite_hash)
    )
    chats = getattr(joined, "chats", None) or []
    if not chats:
        raise RuntimeError("the invite link did not return a chat")
    return chats[0]


def channel_from_updates(result: object) -> types.Channel:
    """Pull the created channel out of a CreateChannel result."""
    for chat in getattr(result, "chats", None) or []:
        if isinstance(chat, types.Channel):
            return chat
    raise RuntimeError("channel creation returned no channel")


# ---------------------------------------------------------------------------
# persisted state
# ---------------------------------------------------------------------------
@dataclass
class Slot:
    """One named source channel and the chat its links are published in."""

    name: str
    source: PeerRef
    dest: PeerRef | None = None
    title: str | None = None  # Overrides the clone title; defaults to the source's.

    @property
    def key(self) -> str:
        """Return the case-insensitive lookup key."""
        return self.name.lower()

    def clone_title(self) -> str:
        """Return the title to give a fresh clone of this source."""
        return self.title or self.source.title

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of this slot."""
        return {
            "name": self.name,
            "source": self.source.to_dict(),
            "dest": self.dest.to_dict() if self.dest else None,
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Slot:
        """Rebuild a slot from its JSON form."""
        dest = data.get("dest")
        return cls(
            name=data["name"],
            source=PeerRef.from_dict(data["source"]),
            dest=PeerRef.from_dict(dest) if dest else None,
            title=data.get("title") or None,
        )


@dataclass
class LiveClone:
    """A clone channel that is published right now and must be cleaned up."""

    slot: str
    peer: PeerRef
    link: str
    posts: int
    gaps: int
    published_at: float

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of this clone."""
        return {
            "slot": self.slot,
            "peer": self.peer.to_dict(),
            "link": self.link,
            "posts": self.posts,
            "gaps": self.gaps,
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveClone:
        """Rebuild a clone record from its JSON form."""
        return cls(
            slot=data["slot"],
            peer=PeerRef.from_dict(data["peer"]),
            link=data.get("link", ""),
            posts=int(data.get("posts") or 0),
            gaps=int(data.get("gaps") or 0),
            published_at=float(data.get("published_at") or 0.0),
        )


@dataclass
class LinkPost:
    """The messages holding the current links in one assigned chat."""

    dest: PeerRef
    message_ids: list[int]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of this link post."""
        return {"dest": self.dest.to_dict(), "message_ids": list(self.message_ids)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LinkPost:
        """Rebuild a link post record from its JSON form."""
        return cls(
            dest=PeerRef.from_dict(data["dest"]),
            message_ids=[int(value) for value in data.get("message_ids") or []],
        )


@dataclass
class State:
    """Everything worth surviving a restart."""

    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    clones_per_source: int = 1
    link_repeat: int = DEFAULT_LINK_REPEAT
    caption_filter: str = DEFAULT_CAPTION_FILTER
    running: bool = False
    cycle: int = 0
    published_at: float = 0.0
    slots: dict[str, Slot] = field(default_factory=dict)
    live: list[LiveClone] = field(default_factory=list)
    link_posts: list[LinkPost] = field(default_factory=list)
    # .test builds real channels and real link posts, so they are tracked apart
    # from the rotation and cleaned up separately, including after a crash.
    test_live: list[LiveClone] = field(default_factory=list)
    test_link_posts: list[LinkPost] = field(default_factory=list)
    path: Path = field(default=Path(STATE_FILE), compare=False)

    @property
    def interval_seconds(self) -> float:
        """Return the interval in seconds."""
        return self.interval_minutes * 60.0

    def ready_slots(self) -> list[Slot]:
        """Return the slots that have both a source and a destination."""
        return [slot for slot in self.slots.values() if slot.dest is not None]

    def save(self) -> None:
        """Write the state to disk, leaving the old file if writing fails."""
        payload = {
            "interval_minutes": self.interval_minutes,
            "clones_per_source": self.clones_per_source,
            "link_repeat": self.link_repeat,
            "caption_filter": self.caption_filter,
            "running": self.running,
            "cycle": self.cycle,
            "published_at": self.published_at,
            "slots": [slot.to_dict() for slot in self.slots.values()],
            "live": [clone.to_dict() for clone in self.live],
            "link_posts": [post.to_dict() for post in self.link_posts],
            "test_live": [clone.to_dict() for clone in self.test_live],
            "test_link_posts": [post.to_dict() for post in self.test_link_posts],
        }
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state from disk, starting fresh when it is missing."""
        state = cls(path=path)
        if not path.exists():
            return state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring unreadable %s (%s).", path, exc)
            return state

        state.interval_minutes = max(
            MIN_INTERVAL_MINUTES,
            int(payload.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES),
        )
        state.clones_per_source = max(1, int(payload.get("clones_per_source") or 1))
        state.link_repeat = min(
            MAX_LINK_REPEAT, max(1, int(payload.get("link_repeat") or DEFAULT_LINK_REPEAT))
        )
        # A stored empty string is a real choice: it means clone everything.
        stored_filter = payload.get("caption_filter")
        state.caption_filter = (
            DEFAULT_CAPTION_FILTER if stored_filter is None else str(stored_filter)
        )
        state.running = bool(payload.get("running"))
        state.cycle = int(payload.get("cycle") or 0)
        state.published_at = float(payload.get("published_at") or 0.0)
        for raw in payload.get("slots") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                slot = Slot.from_dict(raw)
                state.slots[slot.key] = slot
        for raw in payload.get("live") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                state.live.append(LiveClone.from_dict(raw))
        for raw in payload.get("link_posts") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                state.link_posts.append(LinkPost.from_dict(raw))
        for raw in payload.get("test_live") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                state.test_live.append(LiveClone.from_dict(raw))
        for raw in payload.get("test_link_posts") or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                state.test_link_posts.append(LinkPost.from_dict(raw))
        return state


# ---------------------------------------------------------------------------
# entity and text helpers
# ---------------------------------------------------------------------------
async def retry_on_wait(
    action: Callable[..., Awaitable[Result]], *args: object, **kwargs: object
) -> Result:
    """Run a Telegram call, sleeping through flood limits and slow mode."""
    while True:
        try:
            return await action(*args, **kwargs)
        except WAIT_ERRORS as exc:
            wait_seconds = exc.seconds + 1
            LOG.warning("Telegram asked to wait %s seconds.", wait_seconds)
            await asyncio.sleep(wait_seconds)


def utf16_length(value: str) -> int:
    """Measure text in Telegram's UTF-16 entity offset units."""
    return len(value.encode("utf-16-le")) // 2


def without_custom_emoji(
    entities: Sequence[types.TypeMessageEntity],
) -> list[types.TypeMessageEntity]:
    """Drop custom emoji entities so a non-Premium account can still post."""
    return [
        entity
        for entity in entities
        if not isinstance(entity, types.MessageEntityCustomEmoji)
    ]


def has_custom_emoji(entities: Sequence[types.TypeMessageEntity]) -> bool:
    """Return whether any entity is a premium custom emoji."""
    return any(
        isinstance(entity, types.MessageEntityCustomEmoji) for entity in entities
    )


def is_emoji_character(character: str) -> bool:
    """Return whether a character belongs to a common Unicode emoji range."""
    codepoint = ord(character)
    return (
        codepoint in {0x00A9, 0x00AE, 0x200D, 0x203C, 0x2049, 0x20E3, 0x2122}
        or 0x2190 <= codepoint <= 0x21FF
        or 0x2300 <= codepoint <= 0x23FF
        # 0x2460-0x24FF (①⑵⒊) and 0x2776-0x2793 (❶➁➌) are numbering, not emoji.
        or (0x25A0 <= codepoint <= 0x27BF and not 0x2776 <= codepoint <= 0x2793)
        or 0x2B00 <= codepoint <= 0x2BFF
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0xE0020 <= codepoint <= 0xE007F
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def remove_caption_emoji(value: str) -> str:
    """Strip decorative emoji from a caption line, keeping its numbering.

    Channels number their posts with keycaps (1️⃣) or circled digits (①, ❶).
    Dropping those left every index entry looking alike, so the keycap
    decoration goes and the digit underneath stays.
    """
    characters: list[str] = []
    for character in value:
        if ord(character) == 0x20E3:
            continue  # Combining keycap: the digit before it is the number.
        if not is_emoji_character(character):
            characters.append(character)
    return " ".join("".join(characters).split())


def index_title(caption: str | None) -> str | None:
    """Extract the first emoji-free caption line usable as an index title."""
    if not caption:
        return None
    for line in caption.splitlines():
        title = remove_caption_emoji(line)
        if title:
            return title
    return None


def caption_position(messages: Sequence[Message]) -> int | None:
    """Return the position of the first message in a post carrying text."""
    for position, message in enumerate(messages):
        text = getattr(message, "message", None)
        if text and text.strip():
            return position
    return None


def has_media_spoiler(message: Message) -> bool:
    """Return whether a post's media is hidden behind a spoiler."""
    return bool(getattr(getattr(message, "media", None), "spoiler", False))


def replied_message_id(message: Message) -> int | None:
    """Return the id this post replies to inside the same chat, if any."""
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return None
    if getattr(reply, "reply_to_peer_id", None) is not None:
        return None  # A reply to another chat cannot be remapped.
    if getattr(reply, "forum_topic", False) and reply.reply_to_top_id is None:
        return None  # The topic header, not a real reply.
    return reply.reply_to_msg_id


def chunked(values: Sequence[Item], size: int) -> Iterator[Sequence[Item]]:
    """Split a sequence into consecutive chunks."""
    for start in range(0, len(values), size):
        yield values[start : start + size]


def format_duration(seconds: float) -> str:
    """Render a second count as a short human duration."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# the styled quote block, shared by the index and the link post
# ---------------------------------------------------------------------------
class QuoteLines:
    """Build one blockquote of marker + bold, optionally linked, lines.

    All of the UTF-16 offset arithmetic Telegram's entities need lives here, so
    the index and the link post cannot drift apart or miscount independently.
    """

    def __init__(self, marker: str = MARKER_EMOJI, marker_id: int = MARKER_EMOJI_ID):
        self._marker = marker
        self._marker_id = marker_id
        self._text = ""
        self._entities: list[types.TypeMessageEntity] = []
        self._lines = 0

    @property
    def lines(self) -> int:
        """Return how many lines are buffered."""
        return self._lines

    def _addition(self, label: str) -> str:
        """Return the raw text one line adds."""
        return f"{self._marker} {label}\n"

    def would_overflow(
        self,
        label: str,
        url: str | None,
        max_units: int = MAX_MESSAGE_UNITS,
        max_entities: int = MAX_MESSAGE_ENTITIES,
    ) -> bool:
        """Return whether adding a line would break Telegram's own limits."""
        if not self._lines:
            return False  # A single line always goes somewhere, even if long.
        needed = 2 + (1 if url else 0)
        return (
            utf16_length(self._text + self._addition(label)) > max_units
            # The +1 leaves room for the blockquote wrapping the whole message.
            or len(self._entities) + needed + 1 > max_entities
        )

    def add(self, label: str, url: str | None) -> None:
        """Append one marked, bold, optionally linked line."""
        line_offset = utf16_length(self._text)
        label_offset = line_offset + utf16_length(f"{self._marker} ")
        label_length = utf16_length(label)
        self._entities.append(
            types.MessageEntityCustomEmoji(
                offset=line_offset,
                length=utf16_length(self._marker),
                document_id=self._marker_id,
            )
        )
        if url:
            self._entities.append(
                types.MessageEntityTextUrl(
                    offset=label_offset, length=label_length, url=url
                )
            )
        # Bold only. Underlining a link as well spent a third of the entity
        # budget on decoration, which is what forced needless extra messages.
        self._entities.append(
            types.MessageEntityBold(offset=label_offset, length=label_length)
        )
        self._text += self._addition(label)
        self._lines += 1

    def build(self) -> tuple[str, list[types.TypeMessageEntity]]:
        """Return the message text and its entities, quote first."""
        text = self._text.rstrip("\n")
        quote = types.MessageEntityBlockquote(offset=0, length=utf16_length(text))
        return text, [quote, *self._entities]


@dataclass(frozen=True)
class IndexEntry:
    """One index line: the source post number, its title and its clone link."""

    ordinal: int
    title: str
    url: str | None


# A stand-in used only while measuring, so grouping assumes the widest case of
# three entities per line and stays valid once the real links are known.
ASSUMED_URL = "https://t.me/c/0000000000/0"


def group_entries(
    entries: Iterable[IndexEntry],
    suffix: str = "",
    max_units: int = MAX_MESSAGE_UNITS,
    max_entities: int = MAX_MESSAGE_ENTITIES,
) -> list[list[IndexEntry]]:
    """Decide which entries share a message, without rendering them.

    Grouping depends only on the titles, because a link lives in an entity and
    not in the text. Measuring every line as though it were linked means the
    layout computed before the links are known still fits afterwards, which is
    what lets the index be posted in the middle of the clone and filled in once
    the posts after it exist.
    """
    groups: list[list[IndexEntry]] = []
    current: list[IndexEntry] = []
    block = QuoteLines()
    for entry in entries:
        label = f"{entry.title}{suffix}"
        if block.would_overflow(label, ASSUMED_URL, max_units, max_entities):
            groups.append(current)
            current = []
            block = QuoteLines()
        block.add(label, ASSUMED_URL)
        current.append(entry)
    if current:
        groups.append(current)
    return groups


def render_entries(
    entries: Sequence[IndexEntry], suffix: str = ""
) -> tuple[str, list[types.TypeMessageEntity]]:
    """Render one group of entries into a blockquoted message."""
    block = QuoteLines()
    for entry in entries:
        block.add(f"{entry.title}{suffix}", entry.url)
    return block.build()


def make_quote_messages(
    entries: Iterable[IndexEntry],
    suffix: str = "",
    max_units: int = MAX_MESSAGE_UNITS,
    max_entities: int = MAX_MESSAGE_ENTITIES,
) -> list[tuple[str, list[types.TypeMessageEntity]]]:
    """Split entries into as few blockquoted messages as Telegram allows."""
    return [
        render_entries(group, suffix)
        for group in group_entries(entries, suffix, max_units, max_entities)
    ]


def render_link_messages(
    links: Sequence[str], repeat: int, max_units: int = MAX_MESSAGE_UNITS
) -> list[tuple[str, list[types.TypeMessageEntity]]]:
    """Render invite links as repeated plain lines inside one blockquote.

    Each link gets `repeat` lines of its own, the first carrying the marker
    emoji. The URLs are left as plain text: Telegram detects them itself, so
    they stay visible and clickable without spending an entity per line.
    """
    messages: list[tuple[str, list[types.TypeMessageEntity]]] = []
    text = ""
    entities: list[types.TypeMessageEntity] = []

    def flush() -> None:
        nonlocal text, entities
        if not text:
            return
        body = text.rstrip("\n")
        quote = types.MessageEntityBlockquote(offset=0, length=utf16_length(body))
        messages.append((body, [quote, *entities]))
        text, entities = "", []

    for link in links:
        block = f"{MARKER_EMOJI} {link}\n" + f"{link}\n" * max(0, repeat - 1)
        # One link's lines are never split across two messages.
        if text and utf16_length(text + block) > max_units:
            flush()
        entities.append(
            types.MessageEntityCustomEmoji(
                offset=utf16_length(text),
                length=utf16_length(MARKER_EMOJI),
                document_id=MARKER_EMOJI_ID,
            )
        )
        text += block
    flush()
    return messages


def looks_like_our_index(message: Message, suffix: str) -> bool:
    """Return whether a post is an index this tool wrote before.

    main.py's version accepted any blockquote containing the marker and the
    suffix anywhere, which also matched genuine posts and silently deleted them
    from the clone. This one insists that *every* line is an index line, which
    a real post practically never is.
    """
    if getattr(message, "media", None) is not None:
        return False
    text = getattr(message, "message", None)
    if not text:
        return False
    entities = getattr(message, "entities", None) or []
    if not any(
        isinstance(entity, types.MessageEntityBlockquote) for entity in entities
    ):
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if not all(line.lstrip().startswith(MARKER_EMOJI) for line in lines):
        return False
    # Our own marker emoji is the strongest signal; the suffix is the fallback
    # for an index that was posted without premium emoji.
    ours = any(
        isinstance(entity, types.MessageEntityCustomEmoji)
        and entity.document_id == MARKER_EMOJI_ID
        for entity in entities
    )
    if ours:
        return True
    return bool(suffix) and all(suffix in line for line in lines)


def is_index_sticker(message: Message) -> bool:
    """Return whether a post is the sticker that introduces an index."""
    document = getattr(message, "document", None)
    return bool(document and document.id == INDEX_STICKER.id)


# ---------------------------------------------------------------------------
# reading the source, with the post numbers fixed up front
# ---------------------------------------------------------------------------
@dataclass
class SourcePost:
    """One numbered source post: a single message, or a whole album."""

    ordinal: int  # 1-based position among the source's content posts.
    messages: list[Message]

    @property
    def ids(self) -> str:
        """Return the source message ids, for logs."""
        return ", ".join(str(message.id) for message in self.messages)


@dataclass
class SourceScan:
    """The numbered posts of a source, and where its own index sat."""

    posts: list[SourcePost] = field(default_factory=list)
    # How many posts came before the source's own index, so the clone can put
    # its index back in the same place. None means the source had none.
    index_after: int | None = None


def caption_filter_pattern(word: str) -> re.Pattern[str] | None:
    """Compile the index filter, or None when every post should be listed."""
    word = word.strip()
    if not word:
        return None
    # A word boundary keeps "Dm" from matching inside "admin".
    return re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)


def has_real_media(message: Message) -> bool:
    """Return whether a message carries media, ignoring link previews."""
    media = getattr(message, "media", None)
    return media is not None and not isinstance(media, types.MessageMediaWebPage)


def matches_caption_filter(
    messages: Sequence[Message], pattern: re.Pattern[str] | None
) -> bool:
    """Return whether a post belongs in the index.

    This decides what the index lists, never what gets cloned: every post is
    copied either way. Both halves matter here: a post with no media is not
    what the filter is for, and the caption is checked across every part of an
    album because Telegram stores it on whichever part the sender typed it on.
    """
    if pattern is None:
        return True
    if not any(has_real_media(message) for message in messages):
        return False
    return any(
        pattern.search(getattr(message, "message", None) or "")
        for message in messages
    )


async def collect_source_posts(
    client: TelegramClient,
    source: PeerRef,
    suffix: str,
    limit: int | None = None,
) -> SourceScan:
    """Read every content post of a source, oldest first, and number them.

    Every post is kept: media, plain text, and the text replies hanging off a
    media post are all part of the channel and all get cloned. What the index
    lists is decided separately, in build_index_entries.

    Numbering happens here, in one pass, before a single message is sent. That
    is the whole point: once a post owns a number, nothing that happens later
    can take it away or hand it to a different post.

    A limit keeps the first few posts only, which is what .test uses; the posts
    it keeps are still numbered from 1, so a limited run is a faithful sample.

    Where the source's own index sat is recorded rather than merely skipped, so
    the clone can put its index back in the same place instead of always at the
    end.
    """
    scan = SourceScan()
    album: list[Message] = []
    album_id: int | None = None

    def keep(messages: list[Message]) -> None:
        """Number and keep a post."""
        scan.posts.append(
            SourcePost(ordinal=len(scan.posts) + 1, messages=messages)
        )

    def flush() -> None:
        nonlocal album, album_id
        if album:
            keep(album)
        album, album_id = [], None

    def full() -> bool:
        return limit is not None and len(scan.posts) >= limit

    async for message in client.iter_messages(source.input_peer, reverse=True):
        if getattr(message, "action", None) is not None:
            continue  # Service messages ("channel created") are not content.
        text = getattr(message, "message", None)
        if not text and getattr(message, "media", None) is None:
            continue
        if is_index_sticker(message) or looks_like_our_index(message, suffix):
            # The source's own index points back at the source, so it is not
            # copied; its position is remembered and a fresh one goes there.
            flush()
            if scan.index_after is None:
                scan.index_after = len(scan.posts)
                LOG.debug(
                    "Source %s: its index sits after post %s.",
                    source.title,
                    scan.index_after,
                )
            continue

        grouped = getattr(message, "grouped_id", None)
        if grouped is None:
            flush()
            if full():
                break
            keep([message])
            if full():
                break
            continue
        if album and grouped != album_id:
            flush()
            if full():
                break
        album_id = grouped
        album.append(message)
    else:
        flush()

    if limit is not None:
        del scan.posts[limit:]
    # An index recorded beyond the posts that were kept is simply the end.
    if scan.index_after is not None and scan.index_after > len(scan.posts):
        scan.index_after = len(scan.posts)
    return scan


# ---------------------------------------------------------------------------
# writing the clone
# ---------------------------------------------------------------------------
@dataclass
class PostResult:
    """What became of one numbered source post in the clone."""

    ordinal: int
    dest_ids: list[int] = field(default_factory=list)
    reason: str | None = None  # Set when the post could not be recreated.
    degraded: bool = False  # Sent without its premium emoji.

    @property
    def ok(self) -> bool:
        """Return whether the post made it into the clone."""
        return bool(self.dest_ids)


def messages_from_updates(
    result: object, random_ids: Sequence[int]
) -> list[Message]:
    """Pull the messages a send request produced out of its updates.

    Telegram answers with an UpdateMessageID per random id and an
    UpdateNewChannelMessage per message; pairing them is how the caller learns
    which id belongs to which item it sent. Results are returned in the order
    the items were sent, and fall back to id order when the pairing is
    incomplete.
    """
    ids_by_random: dict[int, int] = {}
    by_id: dict[int, Message] = {}
    for update in getattr(result, "updates", None) or []:
        if isinstance(update, types.UpdateMessageID):
            ids_by_random[update.random_id] = update.id
        elif isinstance(
            update, (types.UpdateNewChannelMessage, types.UpdateNewMessage)
        ):
            message = update.message
            by_id[message.id] = message

    paired = [
        by_id[ids_by_random[random_id]]
        for random_id in random_ids
        if random_id in ids_by_random and ids_by_random[random_id] in by_id
    ]
    if len(paired) == len(random_ids):
        return paired
    # Sorting by id keeps an album in the order Telegram actually stored it,
    # which is what the post numbers have to agree with.
    return [by_id[key] for key in sorted(by_id)]


def clone_input_media(message: Message) -> object | None:
    """Reuse the source's own file for the clone, or None for a text post.

    Nothing is downloaded: Telegram is told to serve the same file again, which
    is both instant and lossless.
    """
    media = getattr(message, "media", None)
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return None
    input_media = utils.get_input_media(media)
    if isinstance(input_media, types.InputMediaEmpty):
        raise TypeError(f"{type(media).__name__} cannot be resent")
    if hasattr(input_media, "spoiler"):
        input_media.spoiler = has_media_spoiler(message)
    return input_media


async def reuploaded_media(
    client: TelegramClient, message: Message, directory: Path
) -> object | None:
    """Download and re-upload a post's media when its reference went stale."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return None
    path = await retry_on_wait(
        client.download_media, message, file=str(directory / str(message.id))
    )
    if not path:
        raise RuntimeError(f"could not download the media of message {message.id}")
    handle = await retry_on_wait(client.upload_file, path)
    if getattr(message, "photo", None):
        return types.InputMediaUploadedPhoto(
            file=handle, spoiler=has_media_spoiler(message)
        )
    document = getattr(message, "document", None)
    file_info = getattr(message, "file", None)
    return types.InputMediaUploadedDocument(
        file=handle,
        mime_type=getattr(file_info, "mime_type", None) or "application/octet-stream",
        attributes=list(getattr(document, "attributes", None) or []),
        spoiler=has_media_spoiler(message),
    )


async def send_post(
    client: TelegramClient,
    target: PeerRef,
    post: SourcePost,
    medias: Sequence[object | None],
    id_map: dict[int, int],
    keep_custom_emoji: bool,
) -> list[Message]:
    """Publish one post or album into the clone and return what was created."""
    first = post.messages[0]
    peer = target.input_peer

    reply_to = None
    replied = replied_message_id(first)
    if replied is not None and replied in id_map:
        reply_to = types.InputReplyToMessage(reply_to_msg_id=id_map[replied])

    def entities_of(message: Message) -> list[types.TypeMessageEntity]:
        entities = list(getattr(message, "entities", None) or [])
        return entities if keep_custom_emoji else without_custom_emoji(entities)

    if len(post.messages) > 1:
        items = [
            types.InputSingleMedia(
                media=media,
                message=getattr(message, "message", None) or "",
                entities=entities_of(message),
            )
            for message, media in zip(post.messages, medias, strict=True)
        ]
        query = functions.messages.SendMultiMediaRequest(
            peer=peer, multi_media=items, reply_to=reply_to
        )
        result = await retry_on_wait(client, query)
        return messages_from_updates(result, [item.random_id for item in items])

    if medias[0] is None:
        query = functions.messages.SendMessageRequest(
            peer=peer,
            message=first.message,
            entities=entities_of(first),
            no_webpage=not getattr(first, "web_preview", None),
            reply_to=reply_to,
        )
    else:
        query = functions.messages.SendMediaRequest(
            peer=peer,
            media=medias[0],
            message=getattr(first, "message", None) or "",
            entities=entities_of(first),
            reply_to=reply_to,
        )
    result = await retry_on_wait(client, query)
    return messages_from_updates(result, [query.random_id])


async def clone_one_post(
    client: TelegramClient,
    target: PeerRef,
    post: SourcePost,
    id_map: dict[int, int],
) -> PostResult:
    """Recreate one numbered post, trying progressively weaker copies.

    The ladder matters for the numbering. main.py gave up on the first refusal
    and moved on, which pulled every later post up by one; here a caption
    Telegram will not accept costs the post its premium emoji, not its place.
    """
    outcome = PostResult(ordinal=post.ordinal)
    album = len(post.messages) > 1
    premium = any(
        has_custom_emoji(getattr(message, "entities", None) or [])
        for message in post.messages
    )

    # (label, reupload the media, keep the premium emoji)
    attempts: list[tuple[str, bool, bool]] = [("as is", False, True)]
    if premium:
        attempts.append(("without premium emoji", False, False))
    attempts.append(("re-uploaded", True, True))
    if premium:
        attempts.append(("re-uploaded without premium emoji", True, False))

    def check(medias: list[object | None]) -> list[object | None]:
        """Refuse an album whose parts are not all resendable media."""
        if album and any(media is None for media in medias):
            raise TypeError("an album part carries no resendable media")
        return medias

    last_error: str | None = None
    for label, reupload, keep_emoji in attempts:
        try:
            if reupload:
                with tempfile.TemporaryDirectory(prefix="redirect-") as temp_dir:
                    medias = check(
                        [
                            await reuploaded_media(client, message, Path(temp_dir))
                            for message in post.messages
                        ]
                    )
                    sent = await send_post(
                        client, target, post, medias, id_map, keep_emoji
                    )
            else:
                medias = check(
                    [clone_input_media(message) for message in post.messages]
                )
                sent = await send_post(client, target, post, medias, id_map, keep_emoji)
        except STALE_MEDIA_ERRORS as exc:
            last_error = f"{type(exc).__name__}"
            LOG.warning(
                "Post %s (%s): file reference expired, re-uploading.",
                post.ordinal,
                post.ids,
            )
            continue
        except Exception as exc:  # One bad post must not end the clone.
            last_error = str(exc) or type(exc).__name__
            LOG.warning(
                "Post %s (%s) failed %s: %s", post.ordinal, post.ids, label, last_error
            )
            continue

        if not sent:
            last_error = "Telegram returned no message"
            LOG.warning("Post %s (%s) returned no message %s.", post.ordinal, post.ids, label)
            continue

        # Album parts must be numbered in the order Telegram stored them.
        sent = sorted(sent, key=lambda message: message.id)
        outcome.dest_ids = [message.id for message in sent]
        outcome.degraded = not keep_emoji
        if label != "as is":
            LOG.info("Post %s (%s) cloned %s.", post.ordinal, post.ids, label)

        # Replies are remapped only when the counts agree. Zipping a short
        # result against a long album is what made replies land on the wrong
        # post in main.py, so a mismatch maps the first message and says so.
        if len(sent) == len(post.messages):
            for source_message, new_message in zip(post.messages, sent, strict=True):
                id_map[source_message.id] = new_message.id
        else:
            LOG.error(
                "Post %s (%s): sent %s message(s) but Telegram returned %s; "
                "only the first is mapped.",
                post.ordinal,
                post.ids,
                len(post.messages),
                len(sent),
            )
            id_map[post.messages[0].id] = sent[0].id
        return outcome

    outcome.reason = last_error or "unknown error"
    LOG.error(
        "Post %s (%s) could not be cloned (%s); it is recorded as a gap so the "
        "following posts keep their numbers.",
        post.ordinal,
        post.ids,
        outcome.reason,
    )
    return outcome


def build_index_entries(
    posts: Sequence[SourcePost],
    results: Sequence[PostResult],
    target: PeerRef,
    caption_filter: str = "",
) -> list[IndexEntry]:
    """Build the index lines, in source order.

    The filter belongs here and nowhere else: every post is cloned regardless,
    and a post the filter rejects simply gets no line in the index.

    With no filter there is one line per post, always. main.py indexed only
    posts that both carried media and produced a title, so a plain text post or
    a caption made entirely of emoji shifted every later line: the channel's
    3rd post showed up as the index's 2nd. Here a post with no usable caption is
    still a line, and a post that failed to clone is a line without a link.
    """
    pattern = caption_filter_pattern(caption_filter)
    by_ordinal = {result.ordinal: result for result in results}
    entries: list[IndexEntry] = []
    for post in posts:
        if not matches_caption_filter(post.messages, pattern):
            continue
        position = caption_position(post.messages)
        title = (
            index_title(getattr(post.messages[position], "message", None))
            if position is not None
            else None
        )
        result = by_ordinal.get(post.ordinal)

        url = None
        if result is not None and result.ok:
            # Link the entry to the part of the album that carries the caption,
            # and to the post itself when the counts do not line up.
            index = position if position is not None else 0
            if index >= len(result.dest_ids):
                index = 0
            url = target.post_link(result.dest_ids[index])

        entries.append(
            IndexEntry(
                ordinal=post.ordinal,
                title=title or f"Post {post.ordinal}",
                url=url,
            )
        )
    return entries


@dataclass
class IndexPost:
    """The index messages already in the clone, and what belongs in them."""

    groups: list[list[IndexEntry]] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        """Return how many index messages were posted."""
        return len(self.message_ids)


async def post_index(
    client: TelegramClient,
    target: PeerRef,
    entries: Sequence[IndexEntry],
    suffix: str,
) -> IndexPost:
    """Send the index sticker and the styled index where the cursor is now.

    This is called at the position the source kept its own index, which may be
    several posts before the end. The links of posts that come after it do not
    exist yet, so those lines go out unlinked and refresh_index fills them in.
    """
    posted = IndexPost()
    if not entries:
        return posted

    try:
        await retry_on_wait(client.send_file, target.input_peer, INDEX_STICKER)
    except Exception as exc:  # A stale sticker must not cost us the index.
        LOG.warning("Index sticker skipped: %s", exc)

    posted.groups = group_entries(entries, suffix)
    for group in posted.groups:
        text, entities = render_entries(group, suffix)
        try:
            sent = await send_quote(client, target, text, entities)
        except Exception as exc:
            LOG.warning("Styled index failed (%s); retrying without premium emoji.", exc)
            try:
                sent = await send_quote(
                    client, target, text, without_custom_emoji(entities)
                )
            except Exception as retry_exc:
                LOG.error("Index message failed: %s", retry_exc)
                continue
        posted.message_ids.append(sent.id)
        await asyncio.sleep(SEND_DELAY)
    return posted


async def refresh_index(
    client: TelegramClient,
    target: PeerRef,
    posted: IndexPost,
    entries: Sequence[IndexEntry],
    suffix: str,
) -> int:
    """Edit the posted index so every line links to its finished post.

    The grouping was measured as though every line were linked, so the groups
    computed now match the ones already sent and each message keeps its place.
    """
    if not posted.message_ids:
        return 0
    by_ordinal = {entry.ordinal: entry for entry in entries}
    updated = 0
    for message_id, group in zip(posted.message_ids, posted.groups, strict=False):
        final = [by_ordinal.get(entry.ordinal, entry) for entry in group]
        if final == group:
            continue  # Nothing in this message changed.
        text, entities = render_entries(final, suffix)
        try:
            await retry_on_wait(
                client.edit_message,
                target.input_peer,
                message_id,
                text,
                formatting_entities=list(entities),
                link_preview=False,
            )
            updated += 1
        except errors.MessageNotModifiedError:
            pass
        except Exception as exc:
            LOG.warning("Could not refresh index message %s: %s", message_id, exc)
            try:
                await retry_on_wait(
                    client.edit_message,
                    target.input_peer,
                    message_id,
                    text,
                    formatting_entities=without_custom_emoji(entities),
                    link_preview=False,
                )
                updated += 1
            except Exception as retry_exc:
                LOG.error("Index message %s not refreshed: %s", message_id, retry_exc)
        await asyncio.sleep(EDIT_DELAY)
    return updated


async def send_quote(
    client: TelegramClient,
    target: PeerRef,
    text: str,
    entities: Sequence[types.TypeMessageEntity],
) -> Message:
    """Send one blockquoted message with raw entities and no link preview."""
    return await retry_on_wait(
        client.send_message,
        target.input_peer,
        text,
        formatting_entities=list(entities),
        link_preview=False,
    )


@dataclass
class CloneReport:
    """The outcome of cloning one source into one fresh channel."""

    peer: PeerRef
    link: str
    posts: int
    gaps: list[int]
    degraded: int
    index_messages: int
    total: int = 0
    # Kept so .test can read the source back and prove the order survived.
    source_posts: list[SourcePost] = field(default_factory=list)
    results: list[PostResult] = field(default_factory=list)
    indexed: int = 0  # Posts the index lists; the rest are cloned but unlisted.
    index_after: int = 0  # Posts published before the index.
    photo_copied: bool = False


PHOTO_ACTIONS = (
    types.MessageActionChatEditPhoto,
    types.MessageActionChatDeletePhoto,
)


def service_message_ids(result: object, actions: tuple[type, ...]) -> list[int]:
    """Collect the ids of service messages a request's updates announced."""
    ids: list[int] = []
    for update in getattr(result, "updates", None) or []:
        message = getattr(update, "message", None)
        if message is None:
            continue
        if isinstance(getattr(message, "action", None), actions):
            ids.append(message.id)
    return ids


async def sweep_service_messages(
    client: TelegramClient, peer: PeerRef, actions: tuple[type, ...], scan: int = 20
) -> list[int]:
    """Find recent service messages of the given kinds, newest first."""
    found: list[int] = []
    async for message in client.iter_messages(peer.input_peer, limit=scan):
        if isinstance(getattr(message, "action", None), actions):
            found.append(message.id)
    return found


async def copy_profile_photo(
    client: TelegramClient, source: PeerRef, target: PeerRef
) -> bool:
    """Give the clone the source's profile photo, leaving no service message.

    Telegram announces a photo change with a service message in the channel.
    Setting the photo last and deleting that message keeps it out of the middle
    of the cloned posts, which is the only reason the order matters here.
    """
    with tempfile.TemporaryDirectory(prefix="redirect-pfp-") as temp_dir:
        path = await retry_on_wait(
            client.download_profile_photo,
            source.input_peer,
            file=str(Path(temp_dir) / "photo.jpg"),
        )
        if not path:
            LOG.info("%s has no profile photo to copy.", source.title)
            return False
        handle = await retry_on_wait(client.upload_file, path)
        result = await retry_on_wait(
            client,
            functions.channels.EditPhotoRequest(
                channel=target.input_channel,
                photo=types.InputChatUploadedPhoto(file=handle),
            ),
        )

    ids = service_message_ids(result, PHOTO_ACTIONS)
    if not ids:
        # Some layers answer without the service message in the updates, so
        # look for it in the channel instead of leaving it behind.
        ids = await sweep_service_messages(client, target, PHOTO_ACTIONS)
    if ids:
        try:
            await retry_on_wait(client.delete_messages, target.input_peer, ids)
            LOG.info("Copied the profile photo and removed its service message.")
        except Exception as exc:
            LOG.warning("Profile photo set, but its service message stayed: %s", exc)
    else:
        LOG.info("Copied the profile photo.")
    return True


async def create_clone_channel(client: TelegramClient, title: str) -> PeerRef:
    """Create the private channel a clone is published into."""
    created = await retry_on_wait(
        client,
        functions.channels.CreateChannelRequest(title=title, about="", broadcast=True),
    )
    channel = channel_from_updates(created)
    LOG.info("Created channel %r (id %s).", title, channel.id)
    return PeerRef.of(channel)


async def clone_source(
    client: TelegramClient,
    slot: Slot,
    suffix: str,
    limit: int | None = None,
    caption_filter: str = "",
) -> CloneReport:
    """Copy a source into a brand new channel and index it."""
    source = slot.source
    LOG.info("Reading %s...", source.title)
    scan = await collect_source_posts(client, source, suffix, limit)
    posts = scan.posts
    if not posts:
        raise RuntimeError(f"{source.title} has no posts to clone")
    LOG.info("%s: %s post(s) to clone.", source.title, len(posts))

    target = await create_clone_channel(client, slot.clone_title())

    # The source keeps its index after this many posts; None means at the end.
    index_after = len(posts) if scan.index_after is None else scan.index_after
    if scan.index_after is not None and scan.index_after < len(posts):
        LOG.info(
            "%s: the index goes after post %s, as in the source.",
            slot.name,
            index_after,
        )

    id_map: dict[int, int] = {}
    results: list[PostResult] = []
    posted_index = IndexPost()

    async def place_index() -> None:
        """Post the index here, with whatever links already exist."""
        nonlocal posted_index
        entries = build_index_entries(posts, results, target, caption_filter)
        posted_index = await post_index(client, target, entries, suffix)

    for post in posts:
        if len(results) == index_after:
            await place_index()
        results.append(await clone_one_post(client, target, post, id_map))
        done = len(results)
        if done % 20 == 0 or done == len(posts):
            LOG.info("%s: cloned %s/%s post(s).", slot.name, done, len(posts))
        await asyncio.sleep(SEND_DELAY)
    if len(results) == index_after:
        await place_index()  # The index belongs at the very end.

    gaps = [result.ordinal for result in results if not result.ok]
    degraded = sum(1 for result in results if result.degraded)
    if gaps:
        LOG.error(
            "%s: %s post(s) could not be cloned: %s. Their numbers are kept, so "
            "nothing after them shifted.",
            slot.name,
            len(gaps),
            ", ".join(str(ordinal) for ordinal in gaps),
        )
    if degraded:
        LOG.warning("%s: %s post(s) lost their premium emoji.", slot.name, degraded)

    # The photo goes on last so its service message lands after every post,
    # where it can be deleted without leaving a hole among the clones.
    photo_copied = False
    try:
        photo_copied = await copy_profile_photo(client, source, target)
    except Exception as exc:
        LOG.warning("%s: could not copy the profile photo: %s", slot.name, exc)

    # Now that every post exists, the index lines can point at them.
    entries = build_index_entries(posts, results, target, caption_filter)
    refreshed = await refresh_index(client, target, posted_index, entries, suffix)
    if refreshed:
        LOG.info("%s: filled in %s index message(s).", slot.name, refreshed)

    exported = await retry_on_wait(
        client,
        functions.messages.ExportChatInviteRequest(peer=target.input_peer, title="clone"),
    )
    link = getattr(exported, "link", None)
    if not link:
        raise RuntimeError(f"could not export an invite link for {target.title}")

    LOG.info(
        "%s: cloned %s/%s post(s) into %r, index in %s message(s).",
        slot.name,
        len(posts) - len(gaps),
        len(posts),
        target.title,
        posted_index.count,
    )
    return CloneReport(
        peer=target,
        link=link,
        posts=len(posts) - len(gaps),
        gaps=gaps,
        degraded=degraded,
        index_messages=posted_index.count,
        total=len(posts),
        source_posts=posts,
        results=results,
        indexed=len(entries),
        index_after=index_after,
        photo_copied=photo_copied,
    )


@dataclass
class OrderCheck:
    """Whether the clone's posts came out in the source's own order."""

    expected: int
    found: int
    mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the clone matched the source post for post."""
        return self.expected == self.found and not self.mismatches

    def summary(self) -> str:
        """Describe the outcome in one line."""
        if self.ok:
            return f"{self.found}/{self.expected} in order"
        if self.expected != self.found:
            return f"{self.found} post(s) in the clone, expected {self.expected}"
        return f"{len(self.mismatches)} out of order: {'; '.join(self.mismatches[:3])}"


async def verify_clone_order(
    client: TelegramClient, report: CloneReport, suffix: str
) -> OrderCheck:
    """Read the finished clone back and compare it against the source.

    This is the check that would have caught the bug in main.py: it walks the
    clone in the same way the source was walked and asserts that the Nth post
    of the clone is the Nth post of the source, by caption and by album size.
    """
    # The clone is read with no caption filter: whatever the filter let through
    # is already all that was published, and re-filtering would hide a fault.
    cloned = (await collect_source_posts(client, report.peer, suffix)).posts
    expected = [
        post
        for post, result in zip(report.source_posts, report.results, strict=True)
        if result.ok
    ]
    check = OrderCheck(expected=len(expected), found=len(cloned))

    for position, source_post in enumerate(expected):
        if position >= len(cloned):
            break
        clone_post = cloned[position]
        want = index_title(
            getattr(
                source_post.messages[caption_position(source_post.messages) or 0],
                "message",
                None,
            )
        )
        got = index_title(
            getattr(
                clone_post.messages[caption_position(clone_post.messages) or 0],
                "message",
                None,
            )
        )
        if want != got:
            check.mismatches.append(
                f"post {source_post.ordinal} should read {want!r} but reads {got!r}"
            )
        elif len(source_post.messages) != len(clone_post.messages):
            check.mismatches.append(
                f"post {source_post.ordinal} has {len(clone_post.messages)} "
                f"part(s), expected {len(source_post.messages)}"
            )
    return check


# ---------------------------------------------------------------------------
# tearing a cycle down
# ---------------------------------------------------------------------------
async def delete_all_posts(client: TelegramClient, peer: PeerRef) -> int:
    """Delete every message in a chat, oldest first."""
    ids = [message.id async for message in client.iter_messages(peer.input_peer)]
    if not ids:
        return 0
    deleted = 0
    for chunk in chunked(ids, DELETE_CHUNK):
        try:
            await retry_on_wait(client.delete_messages, peer.input_peer, list(chunk))
            deleted += len(chunk)
        except Exception as exc:
            LOG.warning("Could not delete %s post(s) of %s: %s", len(chunk), peer.title, exc)
    return deleted


async def destroy_clone(client: TelegramClient, clone: LiveClone) -> None:
    """Empty a clone channel and then delete the channel itself."""
    deleted = await delete_all_posts(client, clone.peer)
    LOG.info("%s: deleted %s post(s) from %r.", clone.slot, deleted, clone.peer.title)
    try:
        await retry_on_wait(
            client, functions.channels.DeleteChannelRequest(channel=clone.peer.input_channel)
        )
        LOG.info("%s: deleted channel %r.", clone.slot, clone.peer.title)
    except Exception as exc:
        LOG.error("%s: could not delete channel %r: %s", clone.slot, clone.peer.title, exc)


async def remove_link_posts(client: TelegramClient, posts: Sequence[LinkPost]) -> None:
    """Delete the link messages a previous cycle left in the assigned chats."""
    for post in posts:
        if not post.message_ids:
            continue
        try:
            await retry_on_wait(
                client.delete_messages, post.dest.input_peer, list(post.message_ids)
            )
            LOG.info("Removed the old link post from %r.", post.dest.title)
        except Exception as exc:
            LOG.warning("Could not remove the link post in %r: %s", post.dest.title, exc)


async def teardown_cycle(client: TelegramClient, state: State) -> None:
    """Remove the published links, then wipe and delete every live clone."""
    if not state.live and not state.link_posts:
        return
    heading(f"cycle {state.cycle} teardown")
    await remove_link_posts(client, state.link_posts)
    state.link_posts = []
    for clone in list(state.live):
        await destroy_clone(client, clone)
        state.live.remove(clone)
        state.save()
    state.published_at = 0.0
    state.save()


async def teardown_test(client: TelegramClient, state: State) -> None:
    """Undo everything .test created, leaving no channel and no link behind."""
    if not state.test_live and not state.test_link_posts:
        return
    await remove_link_posts(client, state.test_link_posts)
    state.test_link_posts = []
    for clone in list(state.test_live):
        await destroy_clone(client, clone)
        state.test_live.remove(clone)
        state.save()
    state.save()


# ---------------------------------------------------------------------------
# publishing the links
# ---------------------------------------------------------------------------
async def publish_links(
    client: TelegramClient, state: State, clones: Sequence[LiveClone]
) -> list[LinkPost]:
    """Post one blockquote of links per assigned chat, one link per line."""
    grouped: dict[tuple[str, int], tuple[PeerRef, list[LiveClone]]] = {}
    for clone in clones:
        slot = state.slots.get(clone.slot.lower())
        if slot is None or slot.dest is None:
            continue
        key = (slot.dest.kind, slot.dest.id)
        grouped.setdefault(key, (slot.dest, []))[1].append(clone)

    posts: list[LinkPost] = []
    for dest, members in grouped.values():
        links = [clone.link for clone in members]
        message_ids: list[int] = []
        for text, entities in render_link_messages(links, state.link_repeat):
            try:
                sent = await send_quote(client, dest, text, entities)
            except Exception as exc:
                LOG.warning(
                    "Link post in %r failed (%s); retrying without premium emoji.",
                    dest.title,
                    exc,
                )
                try:
                    sent = await send_quote(
                        client, dest, text, without_custom_emoji(entities)
                    )
                except Exception as retry_exc:
                    LOG.error("Link post in %r failed: %s", dest.title, retry_exc)
                    continue
            message_ids.append(sent.id)
            await asyncio.sleep(SEND_DELAY)
        if message_ids:
            LOG.info(
                "Published %s link(s) in %r, each repeated %s time(s).",
                len(members),
                dest.title,
                state.link_repeat,
            )
            posts.append(LinkPost(dest=dest, message_ids=message_ids))
    return posts


# ---------------------------------------------------------------------------
# the rotation
# ---------------------------------------------------------------------------
async def run_cycle(client: TelegramClient, state: State, suffix: str) -> int:
    """Build every clone of one cycle and publish their links."""
    slots = state.ready_slots()
    if not slots:
        LOG.warning("No slot has both a source and an assigned channel.")
        return 0

    state.cycle += 1
    heading(f"cycle {state.cycle}")
    LOG.info(
        "Cloning %s source(s) x %s clone(s) each.", len(slots), state.clones_per_source
    )

    fresh: list[LiveClone] = []
    for slot in slots:
        for copy_number in range(1, state.clones_per_source + 1):
            try:
                report = await clone_source(
                    client, slot, suffix, caption_filter=state.caption_filter
                )
            except Exception as exc:
                LOG.error("%s: clone %s failed: %s", slot.name, copy_number, exc)
                continue
            clone = LiveClone(
                slot=slot.name,
                peer=report.peer,
                link=report.link,
                posts=report.posts,
                gaps=len(report.gaps),
                published_at=time.time(),
            )
            fresh.append(clone)
            state.live.append(clone)
            state.save()

    if not fresh:
        LOG.error("Cycle %s produced no clone.", state.cycle)
        return 0

    state.link_posts = await publish_links(client, state, fresh)
    state.published_at = time.time()
    state.save()
    LOG.info(
        "Cycle %s published: %s clone(s), next rotation in %s.",
        state.cycle,
        len(fresh),
        format_duration(state.interval_seconds),
    )
    return len(fresh)


async def wait_for_rotation(state: State, stop: asyncio.Event) -> None:
    """Sleep until the interval expires or the loop is asked to stop.

    The deadline is recomputed on every tick on purpose: .rotate republishes
    and .interval changes the window, and a deadline captured once would make
    the loop tear down a cycle that had only just gone up.
    """
    while not stop.is_set():
        remaining = state.published_at + state.interval_seconds - time.time()
        if remaining <= 0:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=min(TICK_SECONDS, remaining))


@dataclass
class Runtime:
    """The live objects the commands need to reach."""

    client: TelegramClient
    state: State
    suffix: str
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None

    @property
    def rotating(self) -> bool:
        """Return whether the rotation loop is alive."""
        return self.task is not None and not self.task.done()


async def rotation_loop(runtime: Runtime) -> None:
    """Clone, publish, wait out the interval, wipe, and do it again."""
    state = runtime.state
    try:
        while not runtime.stop.is_set():
            async with runtime.lock:
                published = await run_cycle(runtime.client, state, runtime.suffix)
            if not published:
                LOG.error("Stopping the rotation: nothing could be published.")
                break

            await wait_for_rotation(state, runtime.stop)

            async with runtime.lock:
                await teardown_cycle(runtime.client, state)
    except asyncio.CancelledError:
        LOG.warning("Rotation cancelled.")
        raise
    except Exception as exc:
        LOG.critical("Rotation stopped: %s", exc)
    finally:
        state.running = False
        state.save()
        LOG.info("Rotation loop finished.")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
COMMAND_HELP = (
    "Commands\n"
    ".setchannel NAME [link]   register a source channel under NAME\n"
    ".assign NAME [link]       publish NAME's links in a chat\n"
    ".unset NAME               forget a source\n"
    ".unassign NAME            forget where NAME's links go\n"
    f".interval MINUTES         rotation interval, {MIN_INTERVAL_MINUTES} or more\n"
    ".clones N                 clones to create per source each cycle\n"
    ".links N                  times each invite link is repeated\n"
    ".filter WORD | off        index only posts with WORD in the caption\n"
    ".start                    start rotating\n"
    ".stop                     stop rotating and wipe the live clones\n"
    ".rotate                   wipe and rebuild now\n"
    ".test [NAME] [POSTS]      rehearse a cycle on a few posts, then undo it\n"
    ".status                   current configuration and live clones\n"
    ".help                     this list"
)


async def set_status(event: object, text: str) -> None:
    """Replace the command message with its result."""
    with contextlib.suppress(Exception):
        await event.edit(text)


async def target_from_args(
    client: TelegramClient, event: object, args: Sequence[str]
) -> tuple[PeerRef, list[str]]:
    """Resolve the chat a command applies to and return the leftover words.

    A link, invite or @handle as the last argument names the chat; otherwise
    the command applies to the chat it was sent in.
    """
    words = list(args)
    if words and looks_like_chat_reference(words[-1]):
        entity = await resolve_chat(client, words.pop())
    else:
        entity = await event.get_chat()
    if not is_supported_chat(entity):
        raise RuntimeError("that chat is not a channel or group")
    return PeerRef.of(entity), words


def validate_slot_name(name: str) -> str:
    """Return a slot name, rejecting anything that is not a plain label."""
    if not SLOT_NAME_RE.match(name):
        raise RuntimeError(
            "a name may hold up to 32 letters, digits, dashes or underscores"
        )
    return name


async def command_setchannel(runtime: Runtime, event: object, args: list[str]) -> None:
    """Register the source channel a clone is made from."""
    if not args:
        await set_status(event, "Usage: .setchannel NAME [link]")
        return
    name = validate_slot_name(args[0])
    source, _ = await target_from_args(runtime.client, event, args[1:])

    state = runtime.state
    existing = state.slots.get(name.lower())
    dest = existing.dest if existing else None
    state.slots[name.lower()] = Slot(name=name, source=source, dest=dest)
    state.save()

    LOG.info("Source %s set to %r.", name, source.title)
    lines = [f"{name} source: {source.title}"]
    if dest is not None:
        lines.append(f"{name} links: {dest.title}")
    await set_status(event, "\n".join(lines))


async def command_assign(runtime: Runtime, event: object, args: list[str]) -> None:
    """Choose the chat a source's clone links are published in."""
    if not args:
        await set_status(event, "Usage: .assign NAME [link]")
        return
    name = validate_slot_name(args[0])
    state = runtime.state
    slot = state.slots.get(name.lower())
    if slot is None:
        await set_status(event, f"{name} has no source channel yet.")
        return

    dest, _ = await target_from_args(runtime.client, event, args[1:])
    slot.dest = dest
    state.save()

    LOG.info("Slot %s publishes into %r.", name, dest.title)
    await set_status(
        event, f"{name} source: {slot.source.title}\n{name} links: {dest.title}"
    )


async def command_unset(runtime: Runtime, event: object, args: list[str]) -> None:
    """Forget a source channel entirely."""
    if not args:
        await set_status(event, "Usage: .unset NAME")
        return
    name = args[0]
    if runtime.state.slots.pop(name.lower(), None) is None:
        await set_status(event, f"{name} is not registered.")
        return
    runtime.state.save()
    LOG.info("Slot %s removed.", name)
    await set_status(event, f"{name} removed.")


async def command_unassign(runtime: Runtime, event: object, args: list[str]) -> None:
    """Forget where a source's links are published."""
    if not args:
        await set_status(event, "Usage: .unassign NAME")
        return
    name = args[0]
    slot = runtime.state.slots.get(name.lower())
    if slot is None or slot.dest is None:
        await set_status(event, f"{name} has no assigned chat.")
        return
    slot.dest = None
    runtime.state.save()
    LOG.info("Slot %s has no destination now.", name)
    await set_status(event, f"{name} links: none")


async def command_interval(runtime: Runtime, event: object, args: list[str]) -> None:
    """Set how long a clone stays up before it is replaced."""
    state = runtime.state
    if not args:
        await set_status(event, f"Interval: {state.interval_minutes} minutes")
        return
    try:
        minutes = int(args[0])
    except ValueError:
        await set_status(event, "Usage: .interval MINUTES")
        return
    if minutes < MIN_INTERVAL_MINUTES:
        await set_status(
            event, f"The shortest interval is {MIN_INTERVAL_MINUTES} minutes."
        )
        return
    state.interval_minutes = minutes
    state.save()
    LOG.info("Interval set to %s minutes.", minutes)
    await set_status(event, f"Interval: {minutes} minutes")


async def command_clones(runtime: Runtime, event: object, args: list[str]) -> None:
    """Set how many clones each source produces per cycle."""
    state = runtime.state
    if not args:
        await set_status(event, f"Clones per source: {state.clones_per_source}")
        return
    try:
        count = int(args[0])
    except ValueError:
        await set_status(event, "Usage: .clones N")
        return
    if count < 1:
        await set_status(event, "At least one clone per source.")
        return
    state.clones_per_source = count
    state.save()
    LOG.info("Clones per source set to %s.", count)
    await set_status(event, f"Clones per source: {count}")


async def command_links(runtime: Runtime, event: object, args: list[str]) -> None:
    """Set how many times each invite link is repeated in the link post."""
    state = runtime.state
    if not args:
        await set_status(event, f"Link lines per clone: {state.link_repeat}")
        return
    try:
        count = int(args[0])
    except ValueError:
        await set_status(event, "Usage: .links N")
        return
    if count < 1 or count > MAX_LINK_REPEAT:
        await set_status(event, f"Between 1 and {MAX_LINK_REPEAT}.")
        return
    state.link_repeat = count
    state.save()
    LOG.info("Link lines per clone set to %s.", count)
    await set_status(event, f"Link lines per clone: {count}")


async def command_filter(runtime: Runtime, event: object, args: list[str]) -> None:
    """Set the caption word a post needs to appear in the index.

    This never changes what gets cloned; every post is copied either way.
    """
    state = runtime.state
    if not args:
        current = state.caption_filter or "off"
        await set_status(event, f"Index filter: {current}")
        return
    word = args[0]
    if word.lower() in {"off", "none", "-"}:
        state.caption_filter = ""
        state.save()
        LOG.info("Index filter cleared; every post will be listed.")
        await set_status(event, "Index filter: off")
        return
    if len(word) > 64:
        await set_status(event, "That filter word is too long.")
        return
    state.caption_filter = word
    state.save()
    LOG.info("Index filter set to %r.", word)
    await set_status(event, f"Index filter: {word}")


async def command_start(runtime: Runtime, event: object, args: list[str]) -> None:
    """Start the rotation loop."""
    state = runtime.state
    if runtime.rotating:
        await set_status(event, "Already rotating.")
        return
    ready = state.ready_slots()
    if not ready:
        await set_status(event, "No slot has both a source and an assigned chat.")
        return

    runtime.stop.clear()
    state.running = True
    state.save()
    runtime.task = asyncio.create_task(rotation_loop(runtime))
    LOG.info("Rotation started.")
    names = ", ".join(slot.name for slot in ready)
    await set_status(
        event,
        f"Rotating {names}\nInterval: {state.interval_minutes} minutes\n"
        f"Clones per source: {state.clones_per_source}",
    )


async def command_stop(runtime: Runtime, event: object, args: list[str]) -> None:
    """Stop rotating and remove whatever is published."""
    if not runtime.rotating:
        await set_status(event, "Not rotating.")
        return
    runtime.stop.set()
    await set_status(event, "Stopping after the current step...")
    task, runtime.task = runtime.task, None
    if task is not None:
        with contextlib.suppress(Exception):
            await task
    async with runtime.lock:
        await teardown_cycle(runtime.client, runtime.state)
    runtime.state.running = False
    runtime.state.save()
    LOG.info("Rotation stopped.")
    await set_status(event, "Stopped, clones deleted.")


async def command_rotate(runtime: Runtime, event: object, args: list[str]) -> None:
    """Wipe the live clones and build the next cycle immediately."""
    state = runtime.state
    if not state.ready_slots():
        await set_status(event, "No slot has both a source and an assigned chat.")
        return
    if runtime.lock.locked():
        await set_status(event, "A cycle is already running.")
        return
    await set_status(event, "Rotating now...")
    async with runtime.lock:
        await teardown_cycle(runtime.client, state)
        published = await run_cycle(runtime.client, state, runtime.suffix)
    await set_status(event, f"Cycle {state.cycle}: {published} clone(s) published.")


async def run_slot_test(
    runtime: Runtime, slot: Slot, posts: int
) -> tuple[list[str], bool]:
    """Rehearse the whole pipeline for one slot and describe every step.

    Everything here is real: a real channel, real cloned posts, a real index
    and a real link post in the assigned chat. Only the scale is reduced, and
    the caller removes all of it afterwards.
    """
    state = runtime.state
    lines = [f"Test: {slot.name}"]
    ok = True

    report = await clone_source(
        runtime.client,
        slot,
        runtime.suffix,
        limit=posts,
        caption_filter=state.caption_filter,
    )
    lines.append(f"source      {slot.source.title}, {report.total} post(s) sampled")
    lines.append(f"channel     created as {report.peer.title}")
    lines.append(f"photo       {'copied' if report.photo_copied else 'none to copy'}")

    if report.gaps:
        ok = False
        listed = ", ".join(str(ordinal) for ordinal in report.gaps)
        lines.append(f"clone       {report.posts}/{report.total}, gap(s) at {listed}")
    else:
        lines.append(f"clone       {report.posts}/{report.total} post(s), no gaps")
    if report.degraded:
        lines.append(f"emoji       {report.degraded} post(s) lost premium emoji")

    check = await verify_clone_order(runtime.client, report, runtime.suffix)
    if not check.ok:
        ok = False
    lines.append(f"order       {check.summary()}")

    if report.index_messages:
        placed = (
            "at the end"
            if report.index_after >= report.total
            else f"after post {report.index_after}, as in the source"
        )
        listed = f"{report.indexed}/{report.total} post(s) listed"
        if state.caption_filter:
            listed += f" (caption has {state.caption_filter!r})"
        lines.append(
            f"index       {report.index_messages} message(s) {placed}, {listed}"
        )
    elif report.indexed == 0:
        listed = (
            f"no caption has {state.caption_filter!r}"
            if state.caption_filter
            else "nothing to list"
        )
        lines.append(f"index       not posted, {listed}")
    else:
        ok = False
        lines.append("index       not posted")
    lines.append(f"invite      {report.link}")

    clone = LiveClone(
        slot=slot.name,
        peer=report.peer,
        link=report.link,
        posts=report.posts,
        gaps=len(report.gaps),
        published_at=time.time(),
    )
    state.test_live.append(clone)
    state.save()

    if slot.dest is None:
        lines.append("links       skipped, no assigned chat")
    else:
        published = await publish_links(runtime.client, state, [clone])
        state.test_link_posts.extend(published)
        state.save()
        if published:
            lines.append(
                f"links       published in {slot.dest.title}, "
                f"repeated {state.link_repeat}x"
            )
        else:
            ok = False
            lines.append(f"links       failed in {slot.dest.title}")
    return lines, ok


async def command_test(runtime: Runtime, event: object, args: list[str]) -> None:
    """Rehearse a full cycle on a few posts, then undo all of it.

    Usage: .test [NAME] [POSTS]
    """
    state = runtime.state
    name: str | None = None
    posts = DEFAULT_TEST_POSTS
    for argument in args:
        if argument.isdigit():
            posts = max(1, int(argument))
        elif name is None:
            name = argument

    if name is not None:
        slot = state.slots.get(name.lower())
        if slot is None:
            await set_status(event, f"{name} is not registered.")
            return
        slots = [slot]
    else:
        slots = list(state.slots.values())
        if not slots:
            await set_status(event, "No source registered.")
            return

    if runtime.lock.locked():
        await set_status(event, "A cycle is already running.")
        return

    heading("test run")
    report_lines: list[str] = []
    healthy = True
    async with runtime.lock:
        try:
            for slot in slots:
                await set_status(event, f"Testing {slot.name}...")
                try:
                    lines, ok = await run_slot_test(runtime, slot, posts)
                except Exception as exc:
                    healthy = False
                    LOG.error("Test of %s failed: %s", slot.name, exc)
                    lines = [f"Test: {slot.name}", f"failed      {exc}"]
                else:
                    healthy = healthy and ok
                report_lines.extend(lines)
                report_lines.append("")
        finally:
            # The rehearsal must never leave a channel or a link behind, even
            # when it failed halfway through.
            await set_status(event, "Cleaning up the test...")
            await teardown_test(runtime.client, state)

    report_lines.append("cleanup     test channels and links removed")
    verdict = "Everything worked." if healthy else "Something needs attention."
    report_lines.append(verdict)
    if healthy:
        LOG.info("Test run passed.")
    else:
        LOG.warning("Test run finished with problems.")
    await set_status(event, "\n".join(report_lines).strip())


async def command_status(runtime: Runtime, event: object, args: list[str]) -> None:
    """Show the configuration and what is published right now."""
    state = runtime.state
    lines = [
        f"Rotating: {'yes' if runtime.rotating else 'no'}",
        f"Interval: {state.interval_minutes} minutes",
        f"Clones per source: {state.clones_per_source}",
        f"Link lines per clone: {state.link_repeat}",
        f"Index filter: {state.caption_filter or 'off'}",
        f"Cycle: {state.cycle}",
    ]
    if state.published_at:
        remaining = state.published_at + state.interval_seconds - time.time()
        lines.append(f"Next rotation: {format_duration(remaining)}")

    if state.slots:
        lines.append("")
        for slot in state.slots.values():
            dest = slot.dest.title if slot.dest else "unassigned"
            lines.append(f"{slot.name}: {slot.source.title} -> {dest}")
    else:
        lines.append("")
        lines.append("No source registered.")

    if state.live:
        lines.append("")
        for clone in state.live:
            detail = f"{clone.slot}: {clone.posts} post(s) — {clone.link}"
            if clone.gaps:
                detail += f" ({clone.gaps} gap(s))"
            lines.append(detail)
    await set_status(event, "\n".join(lines))


COMMANDS: dict[str, Callable[[Runtime, object, list[str]], Awaitable[None]]] = {
    "setchannel": command_setchannel,
    "setsource": command_setchannel,
    "assign": command_assign,
    # The command is easy to mistype and the typo is harmless, so both work.
    "assing": command_assign,
    "unset": command_unset,
    "unassign": command_unassign,
    "interval": command_interval,
    "clones": command_clones,
    "links": command_links,
    "filter": command_filter,
    "start": command_start,
    "stop": command_stop,
    "rotate": command_rotate,
    "test": command_test,
    "status": command_status,
}


def register_commands(runtime: Runtime) -> None:
    """Listen for the dot commands sent from this account."""
    busy = asyncio.Lock()

    @runtime.client.on(
        events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]*))?$")
    )
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
                await handler(runtime, event, raw_args.split() if raw_args else [])
            except RuntimeError as exc:
                await set_status(event, f".{name}: {exc}")
            except Exception as exc:
                LOG.error(".%s failed: %s", name, exc)
                await set_status(event, f".{name} failed: {exc}")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    """Sign in, clean up anything a previous run left, and take commands."""
    setup_logging()
    show_banner()
    settings = read_settings()
    state_path = Path(env_text("REDIRECT_STATE", STATE_FILE))
    first_run = not state_path.exists()
    state = State.load(state_path)
    if first_run:
        # Nothing has been configured in Telegram yet, so the .env decides.
        state.interval_minutes = settings.interval_minutes
        state.clones_per_source = settings.clones_per_source
        state.link_repeat = settings.link_repeat
        state.caption_filter = settings.caption_filter
        state.save()
        LOG.info(
            "First run: interval %s minutes, %s clone(s) per source, "
            "%s link line(s), index filter %s.",
            state.interval_minutes,
            state.clones_per_source,
            state.link_repeat,
            state.caption_filter or "off",
        )

    client = TelegramClient(settings.session, settings.api_id, settings.api_hash)
    await client.start()
    me = await client.get_me()
    LOG.info("Signed in as %s.", utils.get_display_name(me))

    runtime = Runtime(client=client, state=state, suffix=settings.index_suffix)
    try:
        # A crash leaves channels behind. They are useless now, and their links
        # are already published, so the first thing to do is clear them out.
        if state.live or state.link_posts:
            LOG.warning("Cleaning up %s clone(s) from the previous run.", len(state.live))
            await teardown_cycle(client, state)
        if state.test_live or state.test_link_posts:
            LOG.warning("Cleaning up %s test clone(s) left behind.", len(state.test_live))
            await teardown_test(client, state)

        register_commands(runtime)
        heading("commands")
        print(Fore.WHITE + COMMAND_HELP)

        if state.slots:
            for slot in state.slots.values():
                dest = slot.dest.title if slot.dest else "unassigned"
                LOG.info("%s: %s -> %s", slot.name, slot.source.title, dest)

        if (state.running or settings.autostart) and state.ready_slots():
            LOG.info("Resuming the rotation.")
            runtime.stop.clear()
            state.running = True
            state.save()
            runtime.task = asyncio.create_task(rotation_loop(runtime))
        else:
            state.running = False
            state.save()

        await client.run_until_disconnected()
    finally:
        runtime.stop.set()
        if runtime.task is not None:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await runtime.task
        state.running = False
        state.save()
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(Fore.YELLOW + "[!] Stopped by user.")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"Error: {exc}") from exc
