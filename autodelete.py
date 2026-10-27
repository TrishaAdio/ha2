"""Delete quick-reply messages from your chats 1 to 2 hours after they go out.

Runs inside main.py's command mode and auto maker. Your Telegram Business
quick replies (/category, /foreign, ...) go out from this account, and every
message one of them sends is deleted for both sides after a random delay of
60 to 120 minutes: long texts full of links, files, zips, photos, albums.

How a sent message is recognised, since Telegram does not mark a message as
coming from a quick reply once it is in a chat:

* Its content matches a quick reply. The stored quick replies are read from
  Telegram and kept up to date, and an outgoing message counts when it carries
  the same file or photo, or the same text. Texts shorter than MIN_TEXT
  characters never match on their own, so a quick reply that only says "ok"
  does not take every "ok" you type with it. An album counts as a whole once
  one of its items matches.
* It follows a trigger. Sending "/add" (or any other watched word) as a
  message is itself deleted, and so is everything you send in that chat for
  the next WINDOW seconds.

Pending deletions are saved to a file, so a restart loses none of them: one
that fell due while the script was down is deleted as soon as it is back.
Nothing here can stop the command listener; every failure is logged and
retried a few times, then dropped.

    .autodel                    status: on/off, delay, words, what is pending
    .autodel on | off           start or stop scheduling new deletions
    .autodel words <w> [w...]   the watched quick replies / trigger words
                                ("all" = every quick reply you have)
    .autodel time <min> <max>   the delay range in minutes (default 60 120)
    .autodel now                delete everything pending right away
    .autodel clear              forget everything pending, delete nothing
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient, errors, events, functions, types

from icons import set_status
from main import failure, info, success, warning

STATE_FILE = (os.getenv("AUTODELETE_STATE") or "autodelete_state.json").strip()
# After a typed trigger such as /add, everything you send in that chat for this
# many seconds is deleted with it.
WINDOW = float(os.getenv("AUTODELETE_WINDOW") or 180)
REFRESH_EVERY = 300.0  # Re-read the quick replies at least this often.
MIN_TEXT = 12  # Shorter texts never match a quick reply on text alone.
MAX_ATTEMPTS = 6  # A deletion that keeps failing is dropped after this many tries.
BATCH = 100  # Telegram deletes at most 100 messages per request.
DEFAULT_WORDS = ["all", "category", "foreign", "forgein", "add"]
TRIGGER_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@\w+)?$")


@dataclass
class Peer:
    """Enough to reach a chat again after a restart, without any cache."""

    kind: str  # "channel", or "private" for DMs, basic groups and Saved Messages
    id: int
    access_hash: int = 0

    @classmethod
    def of(cls, peer: Any, chat_id: int) -> "Peer":
        """Channels need their access hash; elsewhere message IDs are enough."""
        if isinstance(peer, types.InputPeerChannel):
            return cls("channel", peer.channel_id, peer.access_hash)
        return cls("private", int(chat_id))

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


@dataclass
class Pending:
    """Messages in one chat that are due to be deleted at one moment."""

    peer: Peer
    ids: list[int]
    due: float
    attempts: int = 0

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "peer": asdict(self.peer)}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Pending":
        return cls(
            peer=Peer(**raw["peer"]),
            ids=[int(i) for i in raw.get("ids") or []],
            due=float(raw["due"]),
            attempts=int(raw.get("attempts") or 0),
        )


@dataclass
class Settings:
    enabled: bool = True
    words: list[str] = field(default_factory=lambda: list(DEFAULT_WORDS))
    min_minutes: float = 60
    max_minutes: float = 120


def normalize(text: str) -> str:
    return " ".join((text or "").split())


def media_key(message: Any) -> str | None:
    """Identify the file or photo a message carries; a copy keeps the same ID."""
    media = getattr(message, "media", None)
    document = getattr(media, "document", None)
    if isinstance(document, types.Document):
        return f"d{document.id}"
    photo = getattr(media, "photo", None)
    if isinstance(photo, types.Photo):
        return f"p{photo.id}"
    return None


class AutoDelete:
    """The scheduler, the quick-reply index and the .autodel command."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.path = Path(STATE_FILE)
        self.settings = Settings()
        self.pending: list[Pending] = []
        self.shortcuts: dict[str, int] = {}  # Shortcut name -> its ID.
        self.texts: set[str] = set()
        self.media: set[str] = set()
        self.albums: set[int] = set()  # grouped_ids already being deleted.
        self.windows: dict[str, float] = {}  # Chat key -> window end.
        self.wake = asyncio.Event()
        self.dirty_index = asyncio.Event()
        self.tasks: list[asyncio.Task[Any]] = []
        self.lock = asyncio.Lock()  # .autodel now and the loop never overlap.
        self._load()

    # -- state file ----------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001  A bad file must not stop startup.
            broken = self.path.with_suffix(".broken.json")
            with contextlib.suppress(Exception):
                self.path.replace(broken)
            warning(f"Auto delete: {self.path} was unreadable ({exc}); kept it as {broken}.")
            return
        stored = raw.get("settings") or {}
        self.settings = Settings(
            enabled=bool(stored.get("enabled", True)),
            words=[str(w).lower() for w in stored.get("words") or DEFAULT_WORDS],
            min_minutes=float(stored.get("min_minutes") or 60),
            max_minutes=float(stored.get("max_minutes") or 120),
        )
        for item in raw.get("pending") or []:
            with contextlib.suppress(Exception):
                self.pending.append(Pending.from_json(item))

    def save(self) -> None:
        data = {
            "settings": asdict(self.settings),
            "pending": [p.to_json() for p in self.pending],
        }
        try:
            folder = self.path.resolve().parent
            fd, tmp = tempfile.mkstemp(prefix=".autodelete.", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            failure(f"Auto delete: could not save {self.path}: {exc}")

    # -- what counts ---------------------------------------------------------
    def watched(self, shortcut: str) -> bool:
        words = self.settings.words
        return "all" in words or shortcut.lower() in words

    def is_trigger(self, text: str) -> bool:
        found = TRIGGER_RE.match(text.strip())
        if not found:
            return False
        word = found.group(1).lower()
        return word in self.settings.words or ("all" in self.settings.words and word in self.shortcuts)

    def matches_quick_reply(self, message: Any) -> bool:
        if getattr(message, "grouped_id", None) and message.grouped_id in self.albums:
            return True
        key = media_key(message)
        if key and key in self.media:
            return True
        text = normalize(getattr(message, "raw_text", "") or "")
        return len(text) >= MIN_TEXT and text in self.texts

    async def refresh_index(self) -> None:
        """Read every quick reply and remember what its messages carry."""
        result = await self.client(functions.messages.GetQuickRepliesRequest(hash=0))
        if not isinstance(result, types.messages.QuickReplies):
            return  # Not modified.
        shortcuts: dict[str, int] = {}
        texts: set[str] = set()
        media: set[str] = set()
        for reply in result.quick_replies:
            shortcuts[reply.shortcut.lower()] = reply.shortcut_id
            if not self.watched(reply.shortcut):
                continue
            stored = await self.client(
                functions.messages.GetQuickReplyMessagesRequest(
                    shortcut_id=reply.shortcut_id, hash=0
                )
            )
            for message in getattr(stored, "messages", None) or []:
                key = media_key(message)
                if key:
                    media.add(key)
                text = normalize(getattr(message, "message", "") or "")
                if len(text) >= MIN_TEXT:
                    texts.add(text)
        self.shortcuts, self.texts, self.media = shortcuts, texts, media

    # -- scheduling ----------------------------------------------------------
    def delay(self) -> float:
        low = max(1.0, min(self.settings.min_minutes, self.settings.max_minutes))
        high = max(low, self.settings.max_minutes)
        return random.uniform(low, high) * 60

    def schedule(self, peer: Peer, message_id: int) -> None:
        self.pending.append(Pending(peer, [message_id], time.time() + self.delay()))
        self.save()
        self.wake.set()

    async def on_outgoing(self, event: Any) -> None:
        """Decide whether a message this account just sent is to be deleted."""
        try:
            if not self.settings.enabled:
                return
            message = event.message
            text = getattr(message, "raw_text", "") or ""
            if text.startswith("."):
                return  # The account's own dot commands.
            chat_key = str(event.chat_id)
            now = time.time()
            reason = ""
            if self.is_trigger(text):
                self.windows[chat_key] = now + WINDOW
                reason = f"trigger {text.strip()}"
            elif self.windows.get(chat_key, 0) > now:
                reason = "sent after a trigger"
            elif self.matches_quick_reply(message):
                reason = "quick reply"
            if not reason:
                return
            peer = Peer.of(await event.get_input_chat(), event.chat_id)
            if getattr(message, "grouped_id", None):
                self.albums.add(message.grouped_id)
            self.schedule(peer, message.id)
            info(f"Auto delete: message {message.id} in {peer.key} queued ({reason}).")
        except Exception as exc:  # noqa: BLE001  Must never break the listener.
            failure(f"Auto delete: could not queue a message: {exc}")

    async def delete_due(self, force: bool = False) -> int:
        """Delete every pending item that is due; return how many messages went."""
        async with self.lock:
            return await self._delete_due(force)

    def _drop(self, item: Pending) -> None:
        with contextlib.suppress(ValueError):
            self.pending.remove(item)

    async def _delete_due(self, force: bool) -> int:
        now = time.time()
        due = [p for p in self.pending if force or p.due <= now]
        if not due:
            return 0
        by_chat: dict[str, list[Pending]] = {}
        for item in due:
            by_chat.setdefault(item.peer.key, []).append(item)

        deleted = 0
        for items in by_chat.values():
            peer = items[0].peer
            ids = sorted({i for item in items for i in item.ids})
            try:
                for start in range(0, len(ids), BATCH):
                    await self._delete(peer, ids[start : start + BATCH])
                deleted += len(ids)
                for item in items:
                    self._drop(item)
            except errors.FloodWaitError as exc:
                warning(f"Auto delete: Telegram asks to wait {exc.seconds}s; retrying then.")
                for item in items:
                    item.due = time.time() + exc.seconds + 1
            except (errors.MessageDeleteForbiddenError, errors.PeerIdInvalidError,
                    errors.ChannelPrivateError, errors.UserIsBlockedError) as exc:
                warning(f"Auto delete: cannot delete in {peer.key} ({type(exc).__name__}); dropped.")
                for item in items:
                    self._drop(item)
            except Exception as exc:  # noqa: BLE001
                for item in items:
                    item.attempts += 1
                    if item.attempts >= MAX_ATTEMPTS:
                        failure(f"Auto delete: gave up on {item.ids} in {peer.key}: {exc}")
                        self._drop(item)
                    else:
                        item.due = time.time() + 120 * item.attempts
                        warning(
                            f"Auto delete: deleting in {peer.key} failed ({exc}); "
                            f"retry {item.attempts}/{MAX_ATTEMPTS - 1} in {2 * item.attempts} min."
                        )
        self.save()
        if deleted:
            success(f"Auto delete: deleted {deleted} message(s).")
        return deleted

    async def _delete(self, peer: Peer, ids: list[int]) -> None:
        if peer.kind == "channel":
            channel = types.InputChannel(peer.id, peer.access_hash)
            await self.client(functions.channels.DeleteMessagesRequest(channel, ids))
        else:
            # revoke=True deletes for both sides. IDs already gone are ignored.
            await self.client(functions.messages.DeleteMessagesRequest(ids, revoke=True))

    # -- background loops ----------------------------------------------------
    async def deleter(self) -> None:
        while True:
            try:
                await self.delete_due()
                upcoming = min((p.due for p in self.pending), default=time.time() + 60)
                wait = min(max(upcoming - time.time(), 1.0), 60.0)
                self.wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=wait)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failure(f"Auto delete: the deleter hit {exc}; carrying on.")
                await asyncio.sleep(30)

    async def indexer(self) -> None:
        warned = False
        while True:
            try:
                await self.refresh_index()
                warned = False
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 1)
                continue
            except Exception as exc:  # noqa: BLE001
                if not warned:
                    warning(
                        f"Auto delete: cannot read your quick replies ({exc}); "
                        "typed triggers such as /add still work."
                    )
                    warned = True
            self.dirty_index.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.dirty_index.wait(), timeout=REFRESH_EVERY)

    async def on_raw(self, update: Any) -> None:
        if isinstance(update, QUICK_REPLY_UPDATES):
            self.dirty_index.set()

    def start(self) -> None:
        self.client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        self.client.add_event_handler(self.on_raw, events.Raw())
        loop = asyncio.get_running_loop()
        self.tasks = [loop.create_task(self.deleter()), loop.create_task(self.indexer())]
        state = "on" if self.settings.enabled else "off"
        info(
            f"Auto delete is {state}: quick replies go after "
            f"{self.settings.min_minutes:g}-{self.settings.max_minutes:g} min "
            f"({len(self.pending)} pending). Send .autodel for details."
        )

    # -- .autodel ------------------------------------------------------------
    def status_text(self) -> str:
        s = self.settings
        lines = [
            f"Auto delete is {'on' if s.enabled else 'off'}",
            f"Delay: {s.min_minutes:g}-{s.max_minutes:g} minutes after sending",
            f"Watching: {', '.join('/' + w if w != 'all' else 'every quick reply' for w in s.words)}",
        ]
        if self.shortcuts:
            marked = [
                f"/{name}{'' if self.watched(name) else ' (not watched)'}"
                for name in sorted(self.shortcuts)
            ]
            lines.append("Your quick replies: " + ", ".join(marked))
        else:
            lines.append("Your quick replies: none read yet")
        count = sum(len(p.ids) for p in self.pending)
        lines.append(f"Pending: {count} message(s)")
        if self.pending:
            soonest = min(p.due for p in self.pending) - time.time()
            lines.append(f"Next deletion in {max(0, int(soonest // 60))} min")
        lines.append(f"After a typed trigger, the next {WINDOW:g}s in that chat go too.")
        return "\n".join(lines)

    async def command(self, event: Any, args: list[str]) -> None:
        verb = args[0].lower() if args else ""
        rest = args[1:]
        if verb in {"", "status"}:
            await set_status(event, self.status_text(), "settings")
            return
        if verb in {"on", "off"}:
            self.settings.enabled = verb == "on"
            self.save()
            await set_status(event, f"Auto delete is {verb}.", "settings")
            return
        if verb == "words":
            words = [w.lstrip("/").lower() for w in rest if w.strip("/")]
            if not words:
                await set_status(event, "Usage: .autodel words category foreign add   (or: all)", "help")
                return
            self.settings.words = words
            self.save()
            self.dirty_index.set()
            await set_status(event, f"Watching: {', '.join(words)}", "settings")
            return
        if verb == "time":
            try:
                low, high = sorted(float(x) for x in rest[:2])
                if len(rest) < 2 or low < 1:
                    raise ValueError
            except ValueError:
                await set_status(event, "Usage: .autodel time 60 120   (minutes, at least 1)", "help")
                return
            self.settings.min_minutes, self.settings.max_minutes = low, high
            self.save()
            await set_status(event, f"New quick replies go after {low:g}-{high:g} minutes.", "settings")
            return
        if verb == "now":
            await set_status(event, "Deleting everything pending...")
            count = await self.delete_due(force=True)
            left = sum(len(p.ids) for p in self.pending)
            tail = f"\n{left} could not go yet and will be retried." if left else ""
            await set_status(event, f"Deleted {count} message(s).{tail}")
            return
        if verb == "clear":
            count = sum(len(p.ids) for p in self.pending)
            self.pending.clear()
            self.save()
            await set_status(event, f"Removed {count} message(s) from the queue; nothing was deleted.", "remove")
            return
        await set_status(event, "Usage: .autodel [on|off|words ...|time <min> <max>|now|clear]", "help")


QUICK_REPLY_UPDATES = (
    types.UpdateQuickReplies,
    types.UpdateNewQuickReply,
    types.UpdateDeleteQuickReply,
    types.UpdateQuickReplyMessage,
    types.UpdateDeleteQuickReplyMessages,
)

AUTODEL_HELP = ".autodel [on|off|words|time|now|clear]  delete quick replies after 1-2 h"

_instance: AutoDelete | None = None


def start_autodelete(client: TelegramClient) -> AutoDelete:
    """Start the auto delete once for this client and return it."""
    global _instance
    if _instance is None:
        _instance = AutoDelete(client)
        _instance.start()
    return _instance


async def command_autodel(client: TelegramClient, event: Any, args: list[str]) -> None:
    """The .autodel command, for any listener that has started the feature."""
    await start_autodelete(client).command(event, args)
