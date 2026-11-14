"""Make fresh copies of a demo channel through a pool of worker accounts.

This is main.py's mode 3. The signed-in (premium) account is the operator.

    .set [link]               choose the demo channel that .gen copies
    .login [phone]            add a worker account, by phone, code and 2FA
    .session [string]         add a worker from a session string, no code
    .gen [count]              make that many copies of the demo channel
    .delete [link]            delete a copy and free its worker, never the demo
    .accounts                 list the workers and which ones are spent
    .ids [all]                IDs of the copies that finished and are alive
    .reset [name|all]         mark workers unused again
    .remove <name|cuties>     forget workers; "cuties" drops frozen/spam ones
    .terminate [name|all]     log every other device out of the workers
    .cancel                   abort a login that is waiting for an answer
    .help                     show the list

One .gen is one copy. A random unused worker creates an empty channel of the
same kind and name as the demo channel, the operator joins it and is promoted
to admin, and from then on the operator does every write: profile photo,
description, every post in order with its formatting, the pinned post and
content protection, exactly as main.py's .clone does. Nothing is ever posted in
the demo channel itself. The invite link goes back to whoever typed .gen.

The split of work is deliberate. The worker spends its own channel-creation
quota, so the operator's account never runs out; the operator posts, so
premium emoji and formatting come through; and the delivered invite link is
the operator's own, because a link dies with the account that made it.

Codes and passwords typed into a chat are deleted the moment they are read, and
no credential is ever written to the terminal or the log. The sessions
themselves are stored in the state file, which is as sensitive as a password:
it is written with owner-only permissions and must never be committed.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from colorama import Fore, Style
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from autodelete import command_autodel, start_autodelete
from main import (
    COMMAND_PATTERN,
    GATE_HELP,
    PLAIN,
    STATUS_EVERY,
    ClonePlan,
    CommandGate,
    GenEffects,
    channel_from_updates,
    clone_into,
    collect_clone_posts,
    delete_recent_service,
    failure,
    info,
    is_supported_chat,
    looks_like_chat_reference,
    message_text,
    report_gate,
    resolve_clone_source,
    retry_on_wait,
    set_status,
    success,
    transform_plan,
    warning,
)

STATE_FILE = (os.getenv("AUTOMAKER_STATE") or "automaker_state.json").strip()
# How long a login step waits for the operator to type an answer.
ASK_TIMEOUT = float(os.getenv("AUTOMAKER_ASK_TIMEOUT") or 300)
# A flood wait longer than this is not waited out: the worker is set aside and
# the next one is tried instead, which is the whole point of having a pool.
MAX_WAIT = float(os.getenv("AUTOMAKER_MAX_WAIT") or 300)
# Pause between two copies in one .gen run, so a batch does not look automated.
GEN_DELAY = float(os.getenv("AUTOMAKER_GEN_DELAY") or 8)
GEN_LIMIT = 20  # Most copies one .gen may make.
# Bot added as a full admin to every channel .gen creates. AUTOMAKER_ADMIN_BOT
# in .env picks another one; unset or empty falls back to this default.
DEFAULT_ADMIN_BOT = "DemoSaviourBot"
ADMIN_BOT = (
    (os.getenv("AUTOMAKER_ADMIN_BOT") or "").strip().lstrip("@") or DEFAULT_ADMIN_BOT
)
# Every copy's title ends in a tag of its own, so "Demos" becomes
# "Demos - #k7f2q". AUTOMAKER_TITLE_TAG=off keeps the demo channel's own title.
TITLE_TAG_LENGTH = 5
TITLE_TAG_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
TITLE_TAG_ON = (os.getenv("AUTOMAKER_TITLE_TAG") or "on").strip().lower() not in {
    "off", "no", "0", "false", "none",
}
TITLE_LIMIT = 128  # Telegram refuses a longer chat title.
DEFAULT_WATERMARK = "Your Pleasure Is Not A Sin"
WATERMARK_TEXT = (
    os.getenv("AUTOMAKER_WATERMARK")
    if os.getenv("AUTOMAKER_WATERMARK") is not None
    else DEFAULT_WATERMARK
).strip()
DP_DIR = Path((os.getenv("AUTOMAKER_DP_DIR") or "automaker_dps").strip())
DP_COUNT = 6
CODE_ATTEMPTS = 3
PASSWORD_ATTEMPTS = 3

PHONE_RE = re.compile(r"^\+?\d[\d\s\-().]{5,22}$")
INVITE_HASH_RE = re.compile(r"t\.me/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE)
# A fresh channel takes a moment to propagate; retry the join and member
# lookup this many times, waiting JOIN_RETRY_DELAY * attempt seconds between.
JOIN_RETRIES = 4
JOIN_RETRY_DELAY = 3


async def set_fancy(event: Any, text: str, done: bool = False, icon: str | None = None) -> None:
    """Status with a tgmacicons icon: the stopwatch while working, a check when done."""
    await set_status(event, text, icon or ("done" if done else "working"))
CANCEL_WORDS = frozenset({".cancel", "cancel", ".stop", "stop", ".abort"})
# Telegram answers a dead or revoked session with one of these, and no amount
# of retrying brings it back; the worker has to be re-added with .login.
DEAD_SESSION_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyInvalidError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

# A Telegram auth key is always 256 bytes, so 512 hex characters.
AUTH_KEY_BYTES = 256
# Production Telegram data-centre addresses, used to complete an
# <auth_key_hex>:<dc_id> session, which carries the key and the data-centre
# number but no server to reach. A full Telethon StringSession already records
# its own address, so it never needs this table. These addresses have been
# stable for years; Telethon migrates automatically if Telegram ever moves one.
DC_ADDRESSES: dict[int, tuple[str, int]] = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}
# <auth_key_hex>:<dc_id>, the bare form some tools export.
AUTH_KEY_DC_RE = re.compile(r"\s*([0-9A-Fa-f]+)\s*:\s*(\d+)\s*\Z")


class LoginAborted(Exception):
    """The operator cancelled a login, or stopped answering."""


def build_string_session(text: str) -> str:
    """Turn a pasted session into a Telethon StringSession string.

    Two shapes are accepted, so a session exported by almost anything can be
    pasted straight in on a server that never receives a login code:

      * a normal Telethon StringSession, which already carries its data centre
        and server address, kept as it is; and
      * <auth_key_hex>:<dc_id>, the bare 256-byte key and data-centre number
        some tools hand out, completed here with that data centre's address.

    The returned value is itself a Telethon StringSession string, which is what
    every worker stores and what worker_client() reconnects with.
    """
    candidate = text.strip()
    if not candidate:
        raise LoginAborted("no session string was given")

    match = AUTH_KEY_DC_RE.fullmatch(candidate)
    if match:
        hex_key, dc_text = match.group(1), match.group(2)
        try:
            key = bytes.fromhex(hex_key)
        except ValueError as exc:
            raise LoginAborted("the auth key is not valid hex") from exc
        if len(key) != AUTH_KEY_BYTES:
            raise LoginAborted(
                f"an auth key is {AUTH_KEY_BYTES} bytes ({AUTH_KEY_BYTES * 2} hex "
                f"characters); this one is {len(key)}"
            )
        dc_id = int(dc_text)
        address = DC_ADDRESSES.get(dc_id)
        if address is None:
            raise LoginAborted(
                f"data centre {dc_id} is not a known Telegram one (expected 1-5)"
            )
        session = StringSession()
        session.set_dc(dc_id, address[0], address[1])
        session.auth_key = AuthKey(key)
        return session.save()

    # Anything else is treated as an ordinary Telethon StringSession.
    try:
        return StringSession(candidate).save()
    except Exception as exc:  # noqa: BLE001  A malformed string has to say so.
        raise LoginAborted(
            "that is neither a Telethon session string nor the "
            "<auth_key_hex>:<dc_id> form"
        ) from exc


class WorkerUnusable(Exception):
    """This worker cannot make a chat now, so .gen should try another one."""


class OperatorBlocked(Exception):
    """The operator itself cannot join or run the copy.

    Not the worker's fault, and trying the next worker would fail the same way
    while burning its channel quota too, so .gen stops instead.
    """


def short_error(exc: BaseException) -> str:
    """Return an error as one short line fit for a chat message.

    Telethon's messages run to several sentences and end with "(caused by
    NoneType)", which is noise in a report listing six accounts.
    """
    text = str(exc).split("(caused by")[0].strip()
    text = " ".join(text.split())
    if len(text) > 70:
        text = text[:69].rstrip(" ,.;") + "…"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def why_unusable(exc: BaseException) -> str:
    """Say, in plain words, why an account Telegram refused cannot be used."""
    if isinstance(exc, WorkerUnusable):
        return str(exc)
    if isinstance(exc, errors.FrozenMethodInvalidError):
        return "the account is frozen by Telegram"
    if isinstance(exc, errors.UserRestrictedError):
        return "Telegram restricts this account (spam limit)"
    if isinstance(exc, errors.ChannelsTooMuchError):
        return "it has created as many channels as Telegram allows"
    if isinstance(exc, errors.ChatAdminRequiredError):
        return "it could not hand over admin rights in its own channel"
    if isinstance(exc, DEAD_SESSION_ERRORS):
        return f"its session is dead ({type(exc).__name__})"
    return short_error(exc)


def now_stamp() -> str:
    """Return a UTC timestamp for the state file."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_phone(raw: str) -> str:
    """Return a phone number as +digits, or raise when it is not one."""
    candidate = raw.strip().replace("\u2212", "-")
    if not PHONE_RE.match(candidate):
        raise LoginAborted(f"{raw.strip()!r} is not a phone number")
    digits = re.sub(r"\D", "", candidate)
    if not 7 <= len(digits) <= 15:  # E.164 allows at most 15 digits.
        raise LoginAborted("a phone number has between 7 and 15 digits")
    return f"+{digits}"


def mask_phone(phone: str) -> str:
    """Hide the middle of a phone number, for logs and listings."""
    if len(phone) < 7:
        return "+" + "*" * max(len(phone) - 1, 1)
    return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"


def supported_rights(**wanted: bool) -> types.ChatAdminRights:
    """Build admin rights, keeping only the flags this Telethon knows.

    Telegram keeps adding admin flags and Telethon follows; passing one an
    older install has never heard of is a TypeError, which would make .gen fail
    on exactly the machines that are a version behind.
    """
    allowed = set(inspect.signature(types.ChatAdminRights.__init__).parameters)
    return types.ChatAdminRights(
        **{name: value for name, value in wanted.items() if name in allowed}
    )


def full_rights(megagroup: bool) -> types.ChatAdminRights:
    """Return every admin right the owner can hand out.

    Both the operator (your premium account) and the admin bot get this set,
    add_admins included, so either can manage the channel and promote others
    without the worker. Channels and groups get their own set, the way the
    official apps offer them: posting and editing exist only in broadcast
    channels, banning and pinning only in groups, and Telegram can refuse a
    promotion that mixes the two. anonymous is left off, so actions stay
    visible as their own.
    """
    if megagroup:
        return supported_rights(
            change_info=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=True,
            manage_call=True,
            manage_topics=True,
            post_stories=True,
            edit_stories=True,
            delete_stories=True,
            other=True,
        )
    return supported_rights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        invite_users=True,
        add_admins=True,
        manage_call=True,
        post_stories=True,
        edit_stories=True,
        delete_stories=True,
        other=True,
    )


def tagged_title(title: str) -> str:
    """Return the demo channel's title with a random tag of its own.

    "Demos" becomes "Demos - #k7f2q", a different tag for every copy, so the
    copies are told apart at a glance and no two share a name.
    """
    if not TITLE_TAG_ON:
        return title[:TITLE_LIMIT]
    code = "".join(random.choice(TITLE_TAG_ALPHABET) for _ in range(TITLE_TAG_LENGTH))
    suffix = f" - #{code}"
    return f"{title[: TITLE_LIMIT - len(suffix)].rstrip()}{suffix}"


def permit_rights(megagroup: bool) -> types.ChatAdminRights:
    rights = full_rights(megagroup)
    rights.add_admins = False
    return rights


@dataclass
class MadeChat:
    """One copy of the demo channel, and the link handed over."""

    chat_id: int
    title: str
    link: str
    worker: str
    copied: int
    total: int
    report: list[str] = field(default_factory=list)
    made_at: str = field(default_factory=now_stamp)

    def to_json(self) -> dict[str, Any]:
        """Return the plain dictionary stored in the state file."""
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "link": self.link,
            "worker": self.worker,
            "copied": self.copied,
            "total": self.total,
            "made_at": self.made_at,
        }


@dataclass
class Worker:
    """One signed-in account that exists to create chats."""

    name: str
    phone: str
    user_id: int
    label: str
    session: str  # A Telethon StringSession: full access to the account.
    used: bool = False
    disabled: str = ""  # Why it was set aside; empty means it is fine.
    added_at: str = field(default_factory=now_stamp)
    made: list[dict[str, Any]] = field(default_factory=list)

    @property
    def available(self) -> bool:
        """Return whether .gen may still pick this worker."""
        return not self.used and not self.disabled

    def to_json(self) -> dict[str, Any]:
        """Return the plain dictionary stored in the state file."""
        return {
            "name": self.name,
            "phone": self.phone,
            "user_id": self.user_id,
            "label": self.label,
            "session": self.session,
            "used": self.used,
            "disabled": self.disabled,
            "added_at": self.added_at,
            "made": self.made,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Worker:
        """Rebuild a worker from the state file, tolerating missing keys."""
        return cls(
            name=str(raw.get("name") or "?"),
            phone=str(raw.get("phone") or ""),
            user_id=int(raw.get("user_id") or 0),
            label=str(raw.get("label") or ""),
            session=str(raw.get("session") or ""),
            used=bool(raw.get("used")),
            disabled=str(raw.get("disabled") or ""),
            added_at=str(raw.get("added_at") or now_stamp()),
            made=list(raw.get("made") or []),
        )


@dataclass
class AutoState:
    """Everything mode 3 remembers between runs."""

    path: Path
    workers: list[Worker] = field(default_factory=list)
    # The demo channel .gen copies, by its marked peer id, and its name.
    source_id: int | None = None
    source_title: str = ""
    permitted: list[dict[str, Any]] = field(default_factory=list)
    dp_photos: list[str] = field(default_factory=list)
    fun: bool = False
    spoiler: bool = True
    reactions: bool = True

    @classmethod
    def load(cls, path: str | Path = STATE_FILE) -> AutoState:
        """Read the state file, starting empty when it does not exist yet."""
        state = cls(path=Path(path))
        try:
            raw = json.loads(state.path.read_text("utf-8"))
        except FileNotFoundError:
            return state
        except (OSError, ValueError) as exc:
            # A corrupt file is kept, not overwritten: it holds the sessions.
            raise RuntimeError(f"{state.path} could not be read ({exc})") from exc

        state.workers = [Worker.from_json(item) for item in raw.get("workers") or []]
        # Keys an older version wrote (delivery, kind, title_template) are
        # ignored; the workers and their sessions carry over untouched.
        source = raw.get("source") or {}
        chat_id = source.get("chat_id")
        state.source_id = int(chat_id) if chat_id is not None else None
        state.source_title = str(source.get("title") or "")
        state.permitted = [
            dict(item)
            for item in raw.get("permitted") or []
            if isinstance(item, dict) and item.get("user_id")
        ]
        options = raw.get("options") or {}
        state.dp_photos = [str(path) for path in options.get("dp_photos") or []]
        state.fun = bool(options.get("fun", False))
        state.spoiler = bool(options.get("spoiler", True))
        state.reactions = bool(options.get("reactions", True))
        return state

    def save(self) -> None:
        """Write the state file atomically, readable only by its owner.

        The temporary file is created in the same directory and replaced over
        the real one, so a crash halfway through cannot leave a half-written
        file where the worker sessions used to be.
        """
        payload = {
            "workers": [worker.to_json() for worker in self.workers],
            "source": {"chat_id": self.source_id, "title": self.source_title},
            "permitted": self.permitted,
            "options": {
                "dp_photos": self.dp_photos,
                "fun": self.fun,
                "spoiler": self.spoiler,
                "reactions": self.reactions,
            },
        }
        directory = self.path.parent if str(self.path.parent) else Path(".")
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(directory), prefix=".automaker-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
            os.chmod(temporary, 0o600)  # Session strings are credentials.
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def gen_effects(self) -> GenEffects:
        """Build the per-copy effects from the current options."""
        return GenEffects(
            spoiler=self.spoiler,
            watermark=WATERMARK_TEXT or None,
            fun=self.fun,
            scramble=True,
            reactions=self.reactions,
        )

    def pick_dp(self) -> str | None:
        """Return a random saved demo photo that still exists on disk."""
        existing = [path for path in self.dp_photos if os.path.isfile(path)]
        return random.choice(existing) if existing else None

    def find(self, name: str) -> Worker | None:
        """Return a worker by its short name, or by the account's own name.

        .accounts lists both, so either is a reasonable thing to type; only the
        short name used to work, which made ".remove Rizwan Raza" answer "no
        worker called Rizwan".
        """
        wanted = name.strip().lower().lstrip("@")
        if not wanted:
            return None
        for worker in self.workers:
            if worker.name.lower() == wanted:
                return worker
        for worker in self.workers:
            if worker.label.lower() == wanted or worker.phone == wanted:
                return worker
        matches = [worker for worker in self.workers if wanted in worker.label.lower()]
        return matches[0] if len(matches) == 1 else None

    def next_name(self) -> str:
        """Return the next free worker name: w1, w2, and so on."""
        taken = {worker.name.lower() for worker in self.workers}
        number = 1
        while f"w{number}" in taken:
            number += 1
        return f"w{number}"

    def pick(self) -> Worker | None:
        """Return a random worker that has not made a chat yet.

        Random, not in order: picking the oldest unused account every time
        makes the accounts age at different rates and the pattern of a batch
        obvious. A worker that is used, or was set aside, is never returned, so
        .gen simply moves on to another one.
        """
        available = [worker for worker in self.workers if worker.available]
        return random.choice(available) if available else None

    def permit_for(self, user_id: int) -> dict[str, Any] | None:
        return next(
            (item for item in self.permitted if int(item["user_id"]) == user_id), None
        )

    def watched_channels(self) -> dict[int, Worker | None]:
        watched: dict[int, Worker | None] = {}
        if self.source_id is not None:
            bare, _kind = utils.resolve_id(self.source_id)
            watched[bare] = None
        for worker in self.workers:
            for made in worker.made:
                if made.get("chat_id"):
                    watched[int(made["chat_id"])] = worker
        return watched

    @property
    def counts(self) -> tuple[int, int, int]:
        """Return how many workers are free, spent and set aside."""
        free = sum(1 for worker in self.workers if worker.available)
        used = sum(1 for worker in self.workers if worker.used)
        aside = sum(1 for worker in self.workers if worker.disabled)
        return free, used, aside


class Conversation:
    """Reads the answers the operator types after a command, in one chat.

    The command message itself is edited into the live prompt, which keeps the
    chat clean, and every answer that carries a secret is deleted as soon as it
    has been read, so a login code never sits in the history.
    """

    def __init__(self, client: TelegramClient, event: Any) -> None:
        self.client = client
        self.event = event
        self.chat_id = event.chat_id
        # Answers are read from whoever started the command: this account, or
        # an admin messaging it from another account.
        self.by_self = bool(getattr(event, "out", False))
        self.sender_id = getattr(event, "sender_id", None)
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self._handler: Callable[..., Awaitable[None]] | None = None

    def is_answer(self, event: Any) -> bool:
        """Return whether a new message is an answer from the right person.

        For an admin's login the status is a reply this account sends into the
        same chat, so matching on the sender keeps those replies from being
        read back as answers.
        """
        if event.id == self.event.id:
            return False  # The command message is the prompt, not an answer.
        if self.by_self:
            return bool(getattr(event, "out", False))
        return not getattr(event, "out", False) and event.sender_id == self.sender_id

    async def __aenter__(self) -> Conversation:
        async def collect(event: Any) -> None:
            await self.queue.put(event)

        self._handler = collect
        self.client.add_event_handler(
            collect, events.NewMessage(chats=self.chat_id, func=self.is_answer)
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._handler is not None:
            self.client.remove_event_handler(self._handler)
            self._handler = None

    async def say(self, text: str) -> None:
        """Show the operator where the login has got to."""
        await set_status(self.event, text)

    async def ask(self, prompt: str, *, secret: bool = False) -> str:
        """Ask for one answer, deleting it afterwards when it is a secret."""
        await self.say(f"{prompt}\n\nSend it here, or .cancel to stop.")
        try:
            answer = await asyncio.wait_for(self.queue.get(), ASK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise LoginAborted(
                f"nothing was sent for {int(ASK_TIMEOUT)}s, so the login stopped"
            ) from exc

        text = message_text(answer).strip()
        if secret:
            # Read once, then gone: a code or password must not stay readable.
            # In a private chat this removes it for both sides; in a group it
            # needs the right to delete other people's messages.
            try:
                await self.client.delete_messages(self.chat_id, [answer.id], revoke=True)
            except Exception as exc:  # noqa: BLE001
                warning(f"Could not delete a login answer ({exc}); delete it by hand.")
        if text.lower() in CANCEL_WORDS:
            raise LoginAborted("cancelled")
        if not text:
            raise LoginAborted("that message carried no text")
        return text


async def bounded(
    client: TelegramClient, request: object, *, label: str, max_wait: float = MAX_WAIT
) -> Any:
    """Send a request, waiting out only the flood limits worth waiting for.

    main.py's retry_on_wait sleeps through any wait, which is right for a copy
    that has to finish. Here a long wait means this account is rate limited for
    hours, and the pool exists so that another account can be used instead.
    """
    while True:
        try:
            return await client(request)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as exc:
            if exc.seconds > max_wait:
                raise WorkerUnusable(
                    f"Telegram wants {exc.seconds}s before it will {label}"
                ) from exc
            warning(f"Telegram asked to wait {exc.seconds + 1}s before {label}...")
            await asyncio.sleep(exc.seconds + 1)


def invite_hash_of(link: str) -> str:
    """Return the hash inside a private invite link."""
    found = INVITE_HASH_RE.search(link or "")
    if not found:
        raise RuntimeError(f"{link!r} is not a private invite link")
    return found.group(1)


async def worker_client(
    worker: Worker, api_id: int, api_hash: str
) -> TelegramClient:
    """Connect a stored worker session, or say why it cannot be used."""
    client = TelegramClient(StringSession(worker.session), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise WorkerUnusable("its session is no longer authorized")
    except WorkerUnusable:
        await client.disconnect()
        raise
    except DEAD_SESSION_ERRORS as exc:
        await client.disconnect()
        raise WorkerUnusable(f"its session is dead ({type(exc).__name__})") from exc
    except Exception as exc:  # noqa: BLE001  Any connect failure is the worker's.
        await client.disconnect()
        raise WorkerUnusable(f"it could not connect ({exc})") from exc
    return client


async def terminate_other_logins(
    worker: Worker, api_id: int, api_hash: str
) -> None:
    """Log every other device out of one worker account.

    Connects the worker's stored session and asks Telegram to reset all other
    authorizations, which terminates every login on the account except this
    one. Any unauthorized session signed in to the worker is kicked out, so the
    pool is left holding the only key. The worker's own session is untouched.
    """
    client = await worker_client(worker, api_id, api_hash)
    try:
        await bounded(
            client,
            functions.auth.ResetAuthorizationsRequest(),
            label="terminate the other logins",
        )
    finally:
        await client.disconnect()


async def create_chat(
    client: TelegramClient, title: str, megagroup: bool
) -> types.Channel:
    """Create the empty channel, or group, that becomes a copy."""
    request = functions.channels.CreateChannelRequest(
        title=title,
        about="",
        megagroup=megagroup,
        broadcast=not megagroup,
    )
    try:
        created = await bounded(client, request, label="create a channel")
    except WorkerUnusable:
        raise
    except Exception as exc:  # noqa: BLE001
        # Whatever Telegram says here, this account cannot make the channel, so
        # it is set aside and the next worker is tried. Letting the error
        # through instead was what stopped a whole .gen on the first frozen
        # account while six usable workers were never asked.
        raise WorkerUnusable(why_unusable(exc)) from exc
    return channel_from_updates(created)


async def delete_chat(client: TelegramClient, channel: types.Channel) -> None:
    """Remove a half-made copy, so a failed .gen leaves nothing behind."""
    try:
        await client(functions.channels.DeleteChannelRequest(channel=channel))
        info(f"Deleted the unfinished channel {channel.title!r}.")
    except Exception as exc:  # noqa: BLE001
        warning(f"Could not delete the unfinished channel {channel.title!r}: {exc}")


async def operator_joins(
    worker: TelegramClient, operator: TelegramClient, channel: types.Channel
) -> types.Channel:
    """Let the operator into the new channel, and return it as the operator sees it.

    The operator joins by invite link rather than being added by the worker:
    adding depends on both accounts' privacy settings, a fresh link does not.

    The channel the worker holds cannot simply be handed over. Telegram gives
    every account its own access hash for the same channel, so a request the
    operator makes with the worker's copy is refused; the operator's own copy
    comes back from the join itself.
    """
    try:
        exported = await bounded(
            worker,
            functions.messages.ExportChatInviteRequest(peer=channel, title="operator"),
            label="export an invite link",
        )
        link = getattr(exported, "link", None)
        if not link:
            raise RuntimeError("Telegram returned no invite link")
    except WorkerUnusable:
        raise
    except Exception as exc:  # noqa: BLE001  Still the worker's own channel.
        raise WorkerUnusable(f"it cannot invite anyone in ({why_unusable(exc)})") from exc
    invite = invite_hash_of(link)

    try:
        # A brand-new channel and its link can take a few seconds to reach
        # every Telegram server, and until then the link reads as expired.
        for attempt in range(JOIN_RETRIES):
            try:
                joined = await bounded(
                    operator,
                    functions.messages.ImportChatInviteRequest(hash=invite),
                    label="join the new channel",
                )
                break
            except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
                if attempt == JOIN_RETRIES - 1:
                    raise
                warning(f"The new invite link is not live yet; retrying ({attempt + 1})...")
                await asyncio.sleep(JOIN_RETRY_DELAY * (attempt + 1))
        return channel_from_updates(joined)
    except errors.UserAlreadyParticipantError:
        checked = await operator(functions.messages.CheckChatInviteRequest(hash=invite))
        existing = getattr(checked, "chat", None)
        if isinstance(existing, types.Channel):
            return existing
        raise OperatorBlocked("joined, but the channel could not be read back") from None
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError) as exc:
        # The link was exported a moment ago, so it cannot really have
        # expired: Telegram says this when the worker's new channel is no
        # longer valid, usually because the worker is spam-limited and its
        # channel was restricted or removed. That is the worker's problem,
        # so set it aside and let .gen try the next one.
        raise WorkerUnusable(
            f"Telegram rejected the fresh invite link to its channel ({why_unusable(exc)})"
        ) from exc
    except errors.ChannelsTooMuchError as exc:
        raise OperatorBlocked(
            "your account is in as many channels as Telegram allows; leave some"
        ) from exc
    except errors.FrozenMethodInvalidError as exc:
        raise OperatorBlocked(
            "your own account is frozen by Telegram, so it cannot join or post"
        ) from exc
    except WorkerUnusable as exc:
        # A long flood wait on the operator is the operator's problem, not
        # the worker's, and every other worker would hit it too.
        raise OperatorBlocked(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  The operator could not get in.
        raise OperatorBlocked(f"your account could not join ({why_unusable(exc)})") from exc


async def promote_operator(
    worker: TelegramClient, channel: types.Channel, operator_id: int
) -> None:
    """Make the operator an admin with full rights, add_admins included.

    The worker has to learn the operator's access hash before it can name it in
    a request, and reading the member list is what teaches it: the operator has
    just joined, so it is in there. Without admin rights the operator could not
    post a single message, so a failure here sets the worker aside.
    """
    # The member list can lag a few seconds behind the join, so look again
    # before giving up on the worker.
    found = None
    for attempt in range(JOIN_RETRIES):
        participants = await bounded(
            worker,
            functions.channels.GetParticipantsRequest(
                channel=channel,
                filter=types.ChannelParticipantsRecent(),
                offset=0,
                limit=100,
                hash=0,
            ),
            label="read the member list",
        )
        found = next(
            (
                user
                for user in getattr(participants, "users", None) or []
                if getattr(user, "id", None) == operator_id
            ),
            None,
        )
        if found is not None:
            break
        if attempt < JOIN_RETRIES - 1:
            warning(f"Your account is not in the member list yet; retrying ({attempt + 1})...")
            await asyncio.sleep(JOIN_RETRY_DELAY * (attempt + 1))
    if found is None:
        raise WorkerUnusable("it cannot see your account among the new members")

    try:
        await bounded(
            worker,
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=types.InputUser(
                    user_id=found.id, access_hash=found.access_hash or 0
                ),
                admin_rights=full_rights(bool(channel.megagroup)),
                rank="admin" if channel.megagroup else "",
            ),
            label="promote your account",
        )
    except WorkerUnusable:
        raise
    except Exception as exc:  # noqa: BLE001
        # The worker owns this channel, so if it cannot hand over admin rights
        # the worker is the problem and another one is worth trying.
        raise WorkerUnusable(
            "it could not make your account admin"
            if isinstance(exc, errors.ChatAdminRequiredError)
            else f"it could not make your account admin ({why_unusable(exc)})"
        ) from exc


async def add_admin_bot(worker: TelegramClient, channel: types.Channel) -> str:
    """Add ADMIN_BOT to the new channel as an admin with full rights.

    The worker does this because it owns the channel and the bot is added
    alongside the operator, both with full rights. Promoting a bot
    that is not a member adds it at the same time, so no separate invite is
    needed.

    A failure here does not throw the copy away: the channel and the worker's
    quota are already spent, so it comes back as a line for the report instead.
    """
    if not ADMIN_BOT:
        return ""
    try:
        bot = await worker.get_input_entity(ADMIN_BOT)
        await bounded(
            worker,
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=bot,
                admin_rights=full_rights(bool(channel.megagroup)),
                rank="bot" if channel.megagroup else "",
            ),
            label=f"add @{ADMIN_BOT} as admin",
        )
    except Exception as exc:  # noqa: BLE001  Report it, keep the copy.
        warning(f"Could not add @{ADMIN_BOT} as admin: {why_unusable(exc)}")
        return f"@{ADMIN_BOT} was NOT added as admin: {why_unusable(exc)}"
    success(f"Added @{ADMIN_BOT} as admin with full rights.")
    return f"@{ADMIN_BOT} added as admin with full rights."


JOIN_ACTIONS = (
    types.MessageActionChatJoinedByLink,
    types.MessageActionChatJoinedByRequest,
    types.MessageActionChatAddUser,
)


async def drop_join_notice(operator: TelegramClient, channel: types.Channel) -> None:
    """Delete the "joined via invite link" line a group shows for the operator."""
    if not channel.megagroup:
        return  # Broadcast channels announce no joins.
    found: list[int] = []
    with contextlib.suppress(Exception):
        async for message in operator.iter_messages(channel, limit=10):
            if isinstance(message.action, JOIN_ACTIONS):
                found.append(message.id)
    if found:
        with contextlib.suppress(Exception):
            await operator.delete_messages(channel, found)


async def operator_link(operator: TelegramClient, channel: types.Channel) -> str:
    """Export the invite link that is handed over, made by the operator."""
    exported = await bounded(
        operator,
        functions.messages.ExportChatInviteRequest(peer=channel, title="copy"),
        label="export the invite link",
    )
    link = getattr(exported, "link", None)
    if not link:
        raise RuntimeError("Telegram returned no invite link")
    return link


async def make_one_copy(
    operator: TelegramClient,
    operator_id: int,
    worker: Worker,
    api_id: int,
    api_hash: str,
    source: types.Channel,
    plan: ClonePlan,
    status: Callable[[str], Awaitable[None]],
    effects: GenEffects | None = None,
    dp_path: str | None = None,
) -> MadeChat:
    """Have one worker create the channel, then copy the demo channel into it.

    Raises WorkerUnusable when the account itself is the problem, which is the
    signal for .gen to set it aside and pick another one, and OperatorBlocked
    when it is not. Either way, a channel that was created but never got its
    copy is deleted again.
    """
    title = tagged_title(utils.get_display_name(source) or "Copy")
    client = await worker_client(worker, api_id, api_hash)
    channel: types.Channel | None = None
    try:
        await status(f"{worker.name} is creating {title!r}...")
        channel = await create_chat(client, title, megagroup=bool(source.megagroup))
        info(f"{worker.name} created {title!r} (id {channel.id}).")
        await status("Joining it and becoming admin...")
        target = await operator_joins(client, operator, channel)
        await promote_operator(client, channel, operator_id)
        if ADMIN_BOT:
            await status(f"Adding @{ADMIN_BOT} as admin...")
        bot_note = await add_admin_bot(client, channel)
    except BaseException:
        if channel is not None:
            await delete_chat(client, channel)
        raise
    finally:
        await client.disconnect()
    await drop_join_notice(operator, target)
    reactions_note = ""
    if effects is not None and not effects.reactions:
        reactions_note = await reactions_off_in_new(operator, target)

    # From here on the operator does every write, and the worker is done. The
    # channel exists and the worker's quota is spent, so whatever happens next
    # is reported with the link rather than thrown away.
    try:
        use_plan = transform_plan(plan, effects, random.Random()) if effects else plan
        outcome = await clone_into(
            operator, source, target, use_plan, status,
            effects=effects, profile_photo=dp_path,
        )
        copied, total, report = outcome.copied, outcome.total, outcome.lines()
    except Exception as exc:  # noqa: BLE001
        failure(f"The copy into {title!r} stopped early: {exc}")
        copied, total = 0, len(plan.posts)
        report = [f"Copy stopped early: {exc}", "The channel is kept; check it by hand."]
    try:
        link = await operator_link(operator, target)
    except Exception as exc:  # noqa: BLE001
        failure(f"Could not export an invite link for {title!r}: {exc}")
        link = f"(no invite link: {exc}; export one in the channel's settings)"
    if reactions_note:
        report = [reactions_note, *report]
    if bot_note:
        report = [bot_note, *report]
    return MadeChat(
        chat_id=target.id,
        title=title,
        link=link,
        worker=worker.name,
        copied=copied,
        total=total,
        report=report,
    )


async def resolve_source(client: TelegramClient, state: AutoState) -> types.Channel:
    """Return the demo channel set with .set, or explain why there is none."""
    if state.source_id is None:
        raise RuntimeError("no demo channel yet: send .set in it, or .set <link>")
    try:
        source = await client.get_entity(state.source_id)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"cannot open {state.source_title or 'the demo channel'} any more ({exc}); "
            "send .set again"
        ) from exc
    if not isinstance(source, types.Channel) or not is_supported_chat(source):
        raise RuntimeError("the demo channel is not a channel or group that can be copied")
    return source


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def command_set(
    client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Choose the demo channel that every .gen copies.

    Sent inside the demo channel it picks that channel; with a link, invite or
    @handle it picks that one, which is how an admin sets it from a DM.
    """
    if args and looks_like_chat_reference(args[0]):
        try:
            chat = await resolve_clone_source(client, args[0])
        except Exception as exc:  # noqa: BLE001
            await set_status(event, f"Cannot use {args[0]}: {exc}")
            return
    elif args:
        await set_status(event, f"{args[0]} is not a channel link, invite or @handle.")
        return
    else:
        chat = await event.get_chat()

    if not isinstance(chat, types.Channel) or not is_supported_chat(chat):
        await set_status(
            event,
            "Send .set inside the demo channel, or .set <link to it>. "
            "This chat is not a channel or group that can be copied.",
        )
        return

    state.source_id = utils.get_peer_id(chat)
    state.source_title = utils.get_display_name(chat) or str(chat.id)
    state.save()
    plan = await collect_clone_posts(client, chat)
    free, _used, _aside = state.counts
    success(f"Demo channel set: {state.source_title} ({len(plan.posts)} posts).")
    await set_status(
        event,
        f"Demo channel set: {state.source_title}\n"
        f"{len(plan.posts)} post(s) will be copied by each .gen.\n"
        f"Workers ready: {free}",
        "pin",
    )


async def command_delete(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Delete a channel made by .gen and give its worker back to the pool.

    Sent inside the copy it deletes that one; with a link, invite or @handle it
    deletes that one. The demo channel set with .set is never touched. The
    worker that created the copy owns it, so the worker deletes it, and is then
    marked unused again so .gen can pick it once more.
    """
    if args and looks_like_chat_reference(args[0]):
        try:
            chat = await resolve_clone_source(client, args[0])
        except Exception as exc:  # noqa: BLE001
            await set_status(event, f"Cannot use {args[0]}: {exc}")
            return
    elif args:
        await set_status(event, f"{args[0]} is not a channel link, invite or @handle.")
        return
    elif getattr(event, "is_private", False):
        await set_status(event, "Send .delete inside the copy, or .delete <link to it>.")
        return
    else:
        chat = await event.get_chat()

    if not isinstance(chat, types.Channel):
        await set_status(event, "That is not a channel or group.")
        return
    if state.source_id is not None and utils.get_peer_id(chat) == state.source_id:
        await set_status(event, "That is the demo channel set with .set; it is never deleted.")
        return

    title = utils.get_display_name(chat) or str(chat.id)
    owner = next(
        (w for w in state.workers if any(m.get("chat_id") == chat.id for m in w.made)),
        None,
    )
    # The command sits in the chat about to vanish, so report from elsewhere.
    if not args:
        with contextlib.suppress(Exception):
            await event.delete()
        event = await client.send_message("me", f".delete: {title}", **PLAIN)

    await set_fancy(event, f"Deleting {title}...")
    try:
        if owner is None:
            # Not made by a known worker: only works if this account owns it.
            await client(functions.channels.DeleteChannelRequest(channel=chat))
        else:
            worker = await worker_client(owner, api_id, api_hash)
            try:
                await worker.get_dialogs()  # Teaches it the channel's access hash.
                own = await worker.get_input_entity(types.PeerChannel(chat.id))
                await worker(functions.channels.DeleteChannelRequest(channel=own))
            finally:
                await worker.disconnect()
    except Exception as exc:  # noqa: BLE001
        failure(f"Could not delete {title}: {exc}")
        await set_status(event, f"Could not delete {title}: {short_error(exc)}")
        return

    lines = [f"Deleted {title}."]
    if owner is not None:
        owner.made = [m for m in owner.made if m.get("chat_id") != chat.id]
        owner.used = False
        state.save()
        lines.append(f"{owner.name} is ready again.")
    else:
        lines.append("No worker made it, so no worker was freed.")
    success(" ".join(lines))
    await set_fancy(event, "\n".join(lines), done=True)


REACTIONS_PAUSE = 0.5
GONE_ERRORS = (errors.ChannelPrivateError, errors.ChannelInvalidError)


async def set_reactions(client: TelegramClient, channel: Any, enabled: bool) -> bool:
    """Turn every reaction on or off in a channel. Returns whether it changed.

    Off is "no reactions at all". On allows every reaction, custom emoji
    reactions included where Telegram lets the channel have them, and plain
    ones otherwise.
    """
    if enabled:
        choices: list[Any] = [
            types.ChatReactionsAll(allow_custom=True),
            types.ChatReactionsAll(allow_custom=False),
        ]
    else:
        choices = [types.ChatReactionsNone()]
    refused: Exception | None = None
    for available in choices:
        try:
            await retry_on_wait(
                client,
                functions.messages.SetChatAvailableReactionsRequest(
                    peer=channel, available_reactions=available
                ),
            )
            return True
        except errors.ChatNotModifiedError:
            return False
        except errors.ReactionInvalidError as exc:
            refused = exc
    raise refused or RuntimeError("Telegram refused every reaction setting")


async def reactions_off_in_new(client: TelegramClient, channel: types.Channel) -> str:
    """Switch reactions off in a copy being made, as a line for its report."""
    try:
        await set_reactions(client, channel, False)
    except Exception as exc:  # noqa: BLE001  Report it, keep the copy.
        warning(f"Could not turn reactions off: {short_error(exc)}")
        return f"Reactions could NOT be turned off: {short_error(exc)}"
    return "Reactions: off"


async def set_reactions_by_worker(
    owner: Worker, channel_id: int, enabled: bool, api_id: int, api_hash: str
) -> bool:
    """Have the worker that owns a copy change its reactions."""
    worker = await worker_client(owner, api_id, api_hash)
    try:
        await worker.get_dialogs()
        channel = await worker.get_input_entity(types.PeerChannel(channel_id))
        return await set_reactions(worker, channel, enabled)
    finally:
        await worker.disconnect()


async def command_reactions(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """.reactions on|off: in every copy now, and in every copy made from now on."""
    choice = args[0].lower() if args else ""
    if choice not in {"on", "off"}:
        await set_status(
            event,
            f"Reactions are {'on' if state.reactions else 'off'} for new copies.\n"
            "Usage: .reactions off   or   .reactions on",
        )
        return
    state.reactions = choice == "on"
    state.save()

    copies: dict[int, tuple[Worker, str]] = {}
    for worker in state.workers:
        for item in worker.made:
            if item.get("chat_id"):
                copies[int(item["chat_id"])] = (worker, str(item.get("title") or item["chat_id"]))
    future = (
        "New copies keep reactions on."
        if state.reactions
        else "Every new copy from .gen or .upgrade has reactions off."
    )
    if not copies:
        await set_fancy(event, f"Reactions {choice}. No copies exist yet. {future}", done=True, icon="settings")
        return

    await set_fancy(event, f"Turning reactions {choice} in {len(copies)} cop{'y' if len(copies) == 1 else 'ies'}...")
    alive: dict[int, types.Channel] = {}
    async for dialog in client.iter_dialogs():
        if isinstance(dialog.entity, types.Channel):
            alive[dialog.entity.id] = dialog.entity

    changed = already = gone = 0
    failed: list[str] = []
    for done, (channel_id, (owner, title)) in enumerate(copies.items(), start=1):
        channel = alive.get(channel_id)
        if channel is None:
            gone += 1
            continue
        try:
            did = await set_reactions(client, channel, state.reactions)
        except GONE_ERRORS:
            gone += 1
            continue
        except Exception as exc:  # noqa: BLE001  The owning worker may still can.
            warning(f"Your account could not change reactions in {title}: {short_error(exc)}; asking {owner.name}.")
            try:
                did = await set_reactions_by_worker(
                    owner, channel_id, state.reactions, api_id, api_hash
                )
            except Exception as worker_exc:  # noqa: BLE001
                failed.append(f"{title}: {why_unusable(worker_exc)}")
                continue
        changed += did
        already += not did
        if done % STATUS_EVERY == 0:
            await set_fancy(event, f"Turning reactions {choice}... {done}/{len(copies)}")
        await asyncio.sleep(REACTIONS_PAUSE)

    lines = [f"Reactions {choice}: {changed} changed, {already} already {choice}"]
    if gone:
        lines.append(f"{gone} cop{'y is' if gone == 1 else 'ies are'} gone (deleted, or you were removed), skipped")
    if failed:
        lines.append(f"Could not change {len(failed)}:")
        lines += [f"  {line}" for line in failed[:10]]
    lines.append(future)
    success(lines[0])
    await set_fancy(event, "\n".join(lines), icon="warn" if failed else "done")


UPGRADE_ID_RE = re.compile(r"^-100(?P<id>\d+)$")
DELETE_CHUNK = 100
DELETE_PAUSE = 0.5
TITLE_ACTIONS = (types.MessageActionChatEditTitle,)


async def upgrade_target(
    client: TelegramClient, event: Any, args: list[str]
) -> types.Channel:
    """Return the copy .upgrade works on, as this account sees it."""
    if args:
        bare = UPGRADE_ID_RE.match(args[0])
        if bare:
            chat = await client.get_entity(types.PeerChannel(int(bare.group("id"))))
        elif looks_like_chat_reference(args[0]):
            chat = await resolve_clone_source(client, args[0])
        else:
            raise RuntimeError(f"{args[0]} is not a channel link, invite, @handle or -100 id")
    elif getattr(event, "is_private", False):
        raise RuntimeError("send .upgrade inside the copy, or .upgrade <link to it>")
    else:
        chat = await event.get_chat()
    if not isinstance(chat, types.Channel):
        raise RuntimeError("that is not a channel or group")
    fresh = await client.get_entity(types.PeerChannel(chat.id))
    return fresh if isinstance(fresh, types.Channel) else chat


def upgrade_refusal(channel: types.Channel) -> str:
    """Say why this account cannot rebuild the channel, or "" when it can."""
    if channel.creator:
        return ""
    rights = channel.admin_rights
    if rights is None:
        return "your account is not an admin there"
    if not rights.delete_messages:
        return "your account cannot delete messages there"
    if not channel.megagroup and not rights.post_messages:
        return "your account cannot post there"
    if not rights.change_info:
        return "your account cannot change its name and photo there"
    return ""


async def clear_channel(client: TelegramClient, channel: types.Channel) -> tuple[int, int]:
    """Delete every message in a channel. Returns (deleted, posts still left).

    Service lines such as "channel created" are removed too where Telegram
    allows it; the first one usually cannot be deleted, which is harmless, so
    only real posts count as left over.
    """
    posts: list[int] = []
    service: list[int] = []
    async for message in client.iter_messages(channel):
        (service if message.action is not None else posts).append(message.id)
    for start in range(0, len(posts), DELETE_CHUNK):
        await retry_on_wait(client.delete_messages, channel, posts[start : start + DELETE_CHUNK])
        await asyncio.sleep(DELETE_PAUSE)
    for start in range(0, len(service), DELETE_CHUNK):
        with contextlib.suppress(Exception):
            await retry_on_wait(
                client.delete_messages, channel, service[start : start + DELETE_CHUNK]
            )
    left = 0
    async for message in client.iter_messages(channel):
        if message.action is None:
            left += 1
    return len(posts), left


async def retitle(client: TelegramClient, channel: types.Channel, title: str) -> bool:
    """Rename a channel, leaving no "title changed" line behind."""
    try:
        await retry_on_wait(
            client, functions.channels.EditTitleRequest(channel=channel, title=title)
        )
    except errors.ChatNotModifiedError:
        return False
    await delete_recent_service(client, channel, TITLE_ACTIONS)
    return True


async def command_upgrade(
    client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Rebuild an existing copy with everything .gen does today.

    Every message in the copy is deleted, then the demo channel is copied in
    again with the current options: a tagged title, a saved demo photo, the
    watermark, spoilers, scrambled photo posts and .fun. The channel, its
    members, its admins and its invite links all stay; only the content is
    replaced. The demo is read and the rights checked before anything is
    deleted, so a refusal never leaves the copy empty.
    """
    try:
        target = await upgrade_target(client, event, args)
    except Exception as exc:  # noqa: BLE001
        reason = str(exc) if isinstance(exc, RuntimeError) else short_error(exc)
        await set_status(event, f"Cannot .upgrade: {reason}")
        return
    demo_refusal = "That is the demo channel set with .set; .upgrade only rebuilds copies."
    if state.source_id is not None and target.id == utils.resolve_id(state.source_id)[0]:
        await set_status(event, demo_refusal)
        return

    old_title = utils.get_display_name(target) or str(target.id)
    if event.chat_id == utils.get_peer_id(target):
        with contextlib.suppress(Exception):
            await event.delete()
        where = "me" if getattr(event, "out", False) else event.sender_id
        status_message = await client.send_message(where, f".upgrade: {old_title}", **PLAIN)
        event = StatusElsewhere(event, status_message)

    refusal = upgrade_refusal(target)
    if refusal:
        await set_status(event, f"Cannot upgrade {old_title}: {refusal}. Nothing was deleted.")
        return
    try:
        source = await resolve_source(client, state)
    except RuntimeError as exc:
        await set_status(event, f"Cannot upgrade {old_title}: {exc}. Nothing was deleted.")
        return
    if source.id == target.id:
        await set_status(event, demo_refusal)
        return

    async def status(text: str) -> None:
        await set_fancy(event, f"Upgrading {old_title}: {text}")

    await status(f"reading {utils.get_display_name(source)}...")
    plan = await collect_clone_posts(client, source, command_id=event.id)
    if not plan.posts:
        await set_status(event, f"The demo channel has no posts, so {old_title} was left as it is.")
        return

    await status("deleting every message...")
    try:
        deleted, left = await clear_channel(client, target)
    except Exception as exc:  # noqa: BLE001
        failure(f".upgrade could not empty {old_title}: {exc}")
        await set_status(event, f"Could not empty {old_title}: {short_error(exc)}. Check it by hand.")
        return
    if left:
        await set_status(
            event,
            f"{left} post(s) in {old_title} could not be deleted, so nothing new was "
            "posted. Delete them by hand and run .upgrade again.",
        )
        return
    info(f".upgrade: deleted {deleted} message(s) from {old_title}.")

    title = tagged_title(utils.get_display_name(source) or old_title)
    try:
        await retitle(client, target, title)
    except Exception as exc:  # noqa: BLE001
        warning(f".upgrade could not rename {old_title}: {exc}")
        title = old_title
    reactions_note = "" if state.reactions else await reactions_off_in_new(client, target)

    effects = state.gen_effects()
    use_plan = transform_plan(plan, effects, random.Random())
    outcome = await clone_into(
        client, source, target, use_plan, status,
        effects=effects, profile_photo=state.pick_dp(),
    )

    record = next(
        (item for worker in state.workers for item in worker.made if item.get("chat_id") == target.id),
        None,
    )
    if record is not None:
        record.update(
            title=title, copied=outcome.copied, total=outcome.total, upgraded_at=now_stamp()
        )
        state.save()

    lines = [
        f"Upgraded {old_title}" + (f" -> {title}" if title != old_title else ""),
        f"Deleted {deleted} old message(s), then copied the demo again.",
        *([reactions_note] if reactions_note else []),
        *outcome.lines(),
    ]
    if record is not None and record.get("link"):
        lines.append(f"Invite link unchanged: {record['link']}")
    success(lines[0])
    await set_fancy(event, "\n".join(lines), done=outcome.ok)


async def command_login(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Sign a worker account in, asking for its phone, code and 2FA password."""
    async with Conversation(client, event) as convo:
        try:
            raw_phone = args[0] if args else await convo.ask("Send the phone number.")
            phone = normalize_phone(raw_phone)
            worker = TelegramClient(StringSession(), api_id, api_hash)
            await convo.say(f"Asking Telegram for a code for {mask_phone(phone)}...")
            await worker.connect()
            try:
                await _sign_worker_in(convo, worker, state, phone)
            finally:
                await worker.disconnect()
        except LoginAborted as exc:
            warning(f"Login stopped: {exc}")
            await set_status(event, f"Login stopped: {exc}")


async def command_session(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Add a worker from a pasted session string, with no code or 2FA.

    This is how a worker is added on a server that never receives the login
    code: the account is signed in somewhere that can get the code, its session
    is pasted here, and it joins the pool at once. A normal Telethon
    StringSession works, and so does the bare <auth_key_hex>:<dc_id> form.

    The string is full access to the account, exactly as sensitive as the
    password, so it is deleted the moment it has been read.
    """
    async with Conversation(client, event) as convo:
        try:
            raw = (
                " ".join(args).strip()
                if args
                else await convo.ask(
                    "Send the session string.\n\n"
                    "A Telethon StringSession, or the <auth_key_hex>:<dc_id> form. "
                    "It is full access to the account, so it is deleted as soon "
                    "as it is read.",
                    secret=True,
                )
            )
            # Overwrite the command message straight away: when the string is
            # typed inline as .session <string>, this is what takes it out of
            # the chat history.
            await convo.say("Checking the session with Telegram...")
            serialized = build_string_session(raw)
            worker = TelegramClient(StringSession(serialized), api_id, api_hash)
            await worker.connect()
            try:
                if not await worker.is_user_authorized():
                    raise LoginAborted(
                        "Telegram rejected that session; it is dead, revoked, or "
                        "never authorized"
                    )
                await _store_worker(convo, worker, state, phone="")
            finally:
                await worker.disconnect()
        except LoginAborted as exc:
            warning(f"Could not add the session: {exc}")
            await set_status(event, f"Could not add the session: {exc}")
        except Exception as exc:  # noqa: BLE001  A bad session must not stop the mode.
            reason = short_error(exc)
            warning(f"Could not add the session: {reason}")
            await set_status(event, f"Could not add the session: {reason}")


async def _sign_worker_in(
    convo: Conversation, worker: TelegramClient, state: AutoState, phone: str
) -> None:
    """Walk one account through code and 2FA, then store its session."""
    try:
        await worker.send_code_request(phone)
    except errors.AuthRestartError:
        await worker.send_code_request(phone)  # Telegram asks for exactly one retry.
    except errors.PhoneNumberBannedError as exc:
        raise LoginAborted("that number is banned from Telegram") from exc
    except errors.PhoneNumberInvalidError as exc:
        raise LoginAborted("Telegram does not recognise that number") from exc
    except (errors.PhoneNumberFloodError, errors.FloodWaitError) as exc:
        seconds = getattr(exc, "seconds", None)
        raise LoginAborted(
            f"too many code requests for that number; wait {seconds}s"
            if seconds
            else "too many code requests for that number"
        ) from exc

    for attempt in range(1, CODE_ATTEMPTS + 1):
        code = await convo.ask(
            f"Code sent to {mask_phone(phone)}. Send the login code."
            + ("" if attempt == 1 else f" (attempt {attempt}/{CODE_ATTEMPTS})")
            + "\n\nTelegram invalidates a code it sees forwarded in a chat, so"
            " type it by hand; it is deleted as soon as it is read.",
            secret=True,
        )
        try:
            await worker.sign_in(phone=phone, code=re.sub(r"\D", "", code))
            break
        except errors.SessionPasswordNeededError:
            await _enter_password(convo, worker)
            break
        except errors.PhoneCodeInvalidError:
            if attempt == CODE_ATTEMPTS:
                raise LoginAborted("that code was wrong three times") from None
            warning("Wrong login code.")
        except errors.PhoneCodeExpiredError as exc:
            raise LoginAborted("that code expired; run .login again") from exc
        except errors.PhoneNumberUnoccupiedError as exc:
            raise LoginAborted(
                "no account exists on that number; sign it up in a Telegram app first"
            ) from exc
        except errors.FloodWaitError as exc:
            raise LoginAborted(f"Telegram asked to wait {exc.seconds}s") from exc

    await _store_worker(convo, worker, state, phone)


async def _enter_password(convo: Conversation, worker: TelegramClient) -> None:
    """Answer the 2FA prompt, allowing for a mistyped password."""
    for attempt in range(1, PASSWORD_ATTEMPTS + 1):
        password = await convo.ask(
            "That account has two-step verification. Send its password."
            + ("" if attempt == 1 else f" (attempt {attempt}/{PASSWORD_ATTEMPTS})"),
            secret=True,
        )
        try:
            await worker.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            if attempt == PASSWORD_ATTEMPTS:
                raise LoginAborted("that password was wrong three times") from None
            warning("Wrong 2FA password.")
        except errors.FloodWaitError as exc:
            raise LoginAborted(f"Telegram asked to wait {exc.seconds}s") from exc


async def _store_worker(
    convo: Conversation, worker: TelegramClient, state: AutoState, phone: str
) -> None:
    """Record a freshly signed-in account, refusing a duplicate."""
    me = await worker.get_me()
    if me is None:
        raise LoginAborted("Telegram accepted the login but returned no account")
    if getattr(me, "bot", False):
        raise LoginAborted("that is a bot account, which cannot create chats")

    # A session added with .session carries no phone of its own, so the
    # account's own number is used when Telegram still exposes it.
    if not phone and getattr(me, "phone", None):
        phone = f"+{me.phone}"

    existing = next(
        (item for item in state.workers if item.user_id == me.id), None
    )
    if existing is not None:
        # Re-adding an account would hand the same one out twice, so the stored
        # session is refreshed instead and the account goes back in the pool.
        existing.session = worker.session.save()
        existing.phone = phone
        existing.label = utils.get_display_name(me) or existing.label
        existing.disabled = ""
        state.save()
        success(f"Refreshed the session of {existing.name}.")
        await convo.say(
            f"{existing.name} was already added; its session is refreshed"
            f" and it is {'spent' if existing.used else 'ready'}."
        )
        return

    added = Worker(
        name=state.next_name(),
        phone=phone,
        user_id=me.id,
        label=utils.get_display_name(me) or str(me.id),
        session=worker.session.save(),
    )
    state.workers.append(added)
    state.save()
    free, _used, _aside = state.counts
    success(f"Added worker {added.name} ({added.label}).")
    await convo.say(
        f"Added {added.name}: {added.label}, {mask_phone(phone)}.\n"
        f"{free} worker(s) ready for .gen."
    )


async def collect_demo_photos(convo: Conversation) -> list[str]:
    """Read DP_COUNT photos the operator sends, and save them to DP_DIR.

    Only photos count; a text message that is not .cancel is answered with a
    reminder and does not use up a slot.
    """
    DP_DIR.mkdir(parents=True, exist_ok=True)
    for stale in DP_DIR.glob("dp*.jpg"):
        with contextlib.suppress(OSError):
            stale.unlink()

    saved: list[str] = []
    while len(saved) < DP_COUNT:
        await convo.say(
            f"Send demo photo {len(saved) + 1} of {DP_COUNT} as a photo.\n\n"
            "One at a time, or .cancel to stop."
        )
        try:
            answer = await asyncio.wait_for(convo.queue.get(), ASK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise LoginAborted(
                f"nothing was sent for {int(ASK_TIMEOUT)}s, so .save stopped"
            ) from exc
        if message_text(answer).strip().lower() in CANCEL_WORDS:
            raise LoginAborted("cancelled")
        if not getattr(answer, "photo", None):
            await convo.say("That was not a photo. Send a photo, or .cancel to stop.")
            continue
        path = str(DP_DIR / f"dp{len(saved) + 1}.jpg")
        await answer.download_media(file=path)
        saved.append(path)
    return saved


async def command_save(
    client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """.save dps: collect demo profile photos .gen picks from at random."""
    what = args[0].lower() if args else "dps"
    if what not in {"dps", "dp", "pfp"}:
        await set_status(event, "Usage: .save dps   then send 6 photos one by one.")
        return
    try:
        async with Conversation(client, event) as convo:
            saved = await collect_demo_photos(convo)
    except LoginAborted as exc:
        await set_status(event, f".save stopped: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        failure(f".save failed: {exc}")
        await set_status(event, f".save failed: {short_error(exc)}")
        return
    state.dp_photos = saved
    state.save()
    success(f"Saved {len(saved)} demo photo(s) in {DP_DIR}.")
    await set_fancy(
        event,
        f"Saved {len(saved)} demo photos. Each .gen copy now gets one at random "
        "as its profile photo.",
        done=True,
    )


async def command_fun(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """.fun on|off: readable letter swaps in captions and text posts."""
    choice = args[0].lower() if args else ("off" if state.fun else "on")
    if choice not in {"on", "off"}:
        await set_status(event, "Usage: .fun on   or   .fun off")
        return
    state.fun = choice == "on"
    state.save()
    await set_fancy(
        event,
        "Fun is ON: each copy swaps some letters (a->@, i->!) in captions and "
        "text posts, differently every time."
        if state.fun
        else "Fun is OFF: captions and text are copied as they are.",
        done=True,
        icon="settings",
    )


async def command_spoiler(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """.spoiler on|off: send every photo hidden behind a tap (default on)."""
    choice = args[0].lower() if args else ("off" if state.spoiler else "on")
    if choice not in {"on", "off"}:
        await set_status(event, "Usage: .spoiler on   or   .spoiler off")
        return
    state.spoiler = choice == "on"
    state.save()
    await set_fancy(
        event,
        "Spoiler is ON: every photo is sent hidden behind a tap; captions stay "
        "visible."
        if state.spoiler
        else "Spoiler is OFF: photos are sent shown, unless the source hid them.",
        done=True,
        icon="settings",
    )


async def command_accounts(
    _client: TelegramClient, state: AutoState, event: Any, _args: list[str]
) -> None:
    """List the workers, with what each one has done."""
    if not state.workers:
        await set_status(event, "No workers yet. Add one with .login.")
        return
    free, used, aside = state.counts
    lines = [f"Workers: {len(state.workers)} ({free} ready, {used} spent, {aside} aside)"]
    for worker in state.workers:
        if worker.disabled:
            mark = f"ASIDE - {worker.disabled}"
        elif worker.used:
            mark = f"spent, made {len(worker.made)}"
        else:
            mark = "ready"
        lines.append(f"{worker.name}  {worker.label}  {mask_phone(worker.phone)}  {mark}")
    if aside:
        lines.append(
            f"{aside} set aside. .reset all retries them; .remove aside forgets them."
        )
    lines.append(f"Demo channel: {state.source_title or 'none yet, send .set in it'}")
    dp_count = len([p for p in state.dp_photos if os.path.isfile(p)])
    lines.append(
        f"Demo photos: {dp_count or 'none (.save dps)'}  |  "
        f"fun: {'on' if state.fun else 'off'}  |  "
        f"spoiler: {'on' if state.spoiler else 'off'}  |  "
        f"reactions: {'on' if state.reactions else 'off'}  |  "
        f"watermark: {WATERMARK_TEXT or 'off'}"
    )
    await set_status(event, "\n".join(lines), "users")


IDS_CHUNK = 3500  # Telegram caps a message at 4096 characters.


async def command_ids(
    client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """List the IDs of every copy that finished and is still alive.

    A copy counts when every post went across (copied == total) and the
    channel is still in this account's chat list, not deleted, banned or
    restricted by Telegram. ".ids all" also lists the half-done and dead ones,
    each with the reason, so nothing is hidden.
    """
    show_all = bool(args) and args[0].lower() == "all"
    made = [(w, m) for w in state.workers for m in w.made if m.get("chat_id")]
    if not made:
        await set_status(event, "No copies yet. Make one with .gen.")
        return

    await set_status(event, f"Checking {len(made)} cop{'y' if len(made) == 1 else 'ies'}...")
    # One pass over the chat list: a copy this account no longer sees in it
    # was deleted, or this account was removed from it.
    alive: dict[int, types.Channel] = {}
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, types.Channel):
            alive[entity.id] = entity

    good: list[str] = []
    bad: list[str] = []
    seen: set[int] = set()
    for worker, item in made:
        chat_id = int(item["chat_id"])
        if chat_id in seen:
            continue
        seen.add(chat_id)
        full_id = f"-100{chat_id}"
        title = item.get("title") or "?"
        copied, total = int(item.get("copied") or 0), int(item.get("total") or 0)
        channel = alive.get(chat_id)
        if channel is None:
            why = "gone (deleted, or you were removed)"
        elif getattr(channel, "restricted", False) or getattr(channel, "scam", False) or getattr(channel, "fake", False):
            why = "restricted by Telegram"
        elif total == 0 or copied < total:
            why = f"incomplete ({copied}/{total} posts)"
        else:
            good.append(full_id)
            continue
        bad.append(f"{full_id}  {title}  [{worker.name}] - {why}")

    lines = [f"Active copies: {len(good)} of {len(seen)}"]
    lines += good or ["(none)"]
    if show_all and bad:
        lines += ["", f"Not counted ({len(bad)}):", *bad]
    elif bad:
        lines += ["", f"{len(bad)} more are incomplete or dead; .ids all lists them."]

    text = "\n".join(lines)
    chunks: list[str] = []
    while len(text) > IDS_CHUNK:
        cut = text.rfind("\n", 0, IDS_CHUNK)
        cut = cut if cut > 0 else IDS_CHUNK
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    await set_status(event, chunks[0], "copy" if good else "warn")
    for extra in chunks[1:]:
        with contextlib.suppress(Exception):
            await client.send_message("me", extra, **PLAIN)
    if len(chunks) > 1:
        info(f".ids: the rest of the list ({len(chunks) - 1} more message(s)) is in Saved Messages.")


async def command_reset(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Put spent and set-aside workers back in the pool.

    "all" clears the set-aside reason as well as the spent mark. Clearing only
    the spent mark meant .reset all could answer "0 ready" while every worker
    sat aside, and the next .gen then said there was no worker left.
    """
    target = " ".join(args).strip() or "all"
    if target.lower() in {"all", "aside", "disabled"}:
        freed = 0
        for worker in state.workers:
            if worker.used or worker.disabled:
                freed += 1
            worker.used = False
            worker.disabled = ""
        state.save()
        free, _used, _aside = state.counts
        if not state.workers:
            await set_status(event, "There are no workers yet. Add one with .login.")
            return
        await set_status(
            event,
            f"Freed {freed} worker(s); {free} ready for .gen.\n"
            "A worker Telegram still refuses is set aside again on the next .gen.",
        )
        return
    worker = state.find(target)
    if worker is None:
        await set_status(event, f"No worker matches {target!r}. Check .accounts.")
        return
    worker.used = False
    worker.disabled = ""
    state.save()
    await set_status(event, f"{worker.name} ({worker.label}) is ready again.")


# Words that mean "remove the frozen accounts" to .remove.
FROZEN_WORDS = {"cuties", "cutie", "frozen", "dead", "spam"}


# What a set-aside reason says when the account is spam-limited. A fresh
# invite link that reads as expired is a limited worker's channel being pulled.
SPAM_SIGNS = (
    "spam limit",
    "restricts this account",
    "rejected the fresh invite link",
    "invitehashexpired",
    "userrestricted",
)
SPAMBOT = "SpamBot"
SPAMBOT_WAIT = 20.0


async def ask_spambot(client: TelegramClient) -> tuple[str, str]:
    """Ask @SpamBot whether this account is limited, then clear the chat.

    Only the bot's English replies are read. Anything it says that is not
    clearly "no limits" or clearly "limited" comes back as unknown, which
    keeps the worker: a wrong guess must never remove a good account.
    """
    try:
        async with client.conversation(SPAMBOT, timeout=SPAMBOT_WAIT) as convo:
            await convo.send_message("/start")
            reply = await convo.get_response()
        text = (reply.raw_text or "").lower()
    except Exception as exc:  # noqa: BLE001
        return "unknown", f"@SpamBot did not answer ({short_error(exc)})"
    finally:
        with contextlib.suppress(Exception):
            await client.delete_dialog(SPAMBOT)
    if "frozen" in text or "blocked" in text:
        return "frozen", "@SpamBot says the account is frozen"
    if "no limits" in text or "good news" in text or "free as a bird" in text:
        return "ok", "@SpamBot: no limits"
    if "limited" in text or "restrict" in text or "annoying" in text:
        return "spam", "@SpamBot says the account is spam-limited"
    return "unknown", "@SpamBot gave an answer this did not recognise"


async def worker_health(
    worker: Worker, api_id: int, api_hash: str
) -> tuple[str, str]:
    """Say whether a worker is frozen, dead or still fine.

    Returns one of "frozen", "spam", "dead", "ok" or "unknown", with a reason.

    A worker .gen already set aside for a spam limit, or for a fresh invite
    link Telegram rejected (what a limited account's new channel does), counts
    as spam without connecting. Every other worker that passes the write test
    is then asked about by @SpamBot.

    A frozen account answers normally to reads, which is why it is only caught
    halfway through a .gen, sometimes as a flood wait that is not a real rate
    limit. Telegram refuses writes on it, so the account's own "about" text is
    written back unchanged: nothing on the profile moves, and a frozen account
    raises FROZEN_METHOD_INVALID instead.
    """
    recorded = worker.disabled.lower()
    if "frozen" in recorded:
        return "frozen", worker.disabled
    if any(sign in recorded for sign in SPAM_SIGNS):
        return "spam", worker.disabled
    try:
        client = await worker_client(worker, api_id, api_hash)
    except WorkerUnusable as exc:
        reason = str(exc)
        return ("dead" if "session" in reason else "unknown"), reason
    try:
        full = await client(
            functions.users.GetFullUserRequest(types.InputUserSelf())
        )
        about = getattr(getattr(full, "full_user", None), "about", None) or ""
        await client(functions.account.UpdateProfileRequest(about=about))
        return await ask_spambot(client)
    except errors.FrozenMethodInvalidError:
        return "frozen", "the account is frozen by Telegram"
    except errors.UserDeactivatedBanError:
        return "dead", "the account is banned by Telegram"
    except DEAD_SESSION_ERRORS as exc:
        return "dead", f"its session is dead ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001  Not proof of anything; keep it.
        return "unknown", why_unusable(exc)
    finally:
        await client.disconnect()


async def remove_frozen(
    state: AutoState,
    event: Any,
    api_id: int,
    api_hash: str,
) -> None:
    """Check every worker and forget the frozen, spam-limited and dead ones."""
    if not state.workers:
        await set_status(event, "There are no workers yet. Add one with .login.")
        return
    doomed: list[tuple[Worker, str]] = []
    kept: list[str] = []
    total = len(state.workers)
    for number, worker in enumerate(list(state.workers), start=1):
        await set_fancy(event, f"Checking {worker.name}... {number}/{total}")
        verdict, reason = await worker_health(worker, api_id, api_hash)
        if verdict in {"frozen", "spam", "dead"}:
            doomed.append((worker, f"{verdict}: {reason}"))
        else:
            kept.append(f"{worker.name} ({reason})" if verdict == "unknown" else worker.name)

    for worker, _reason in doomed:
        state.workers.remove(worker)
    if doomed:
        state.save()
        success(f"Removed {len(doomed)} frozen, spam-limited or dead worker(s).")

    lines: list[str] = []
    if doomed:
        lines.append(f"Removed {len(doomed)} of {total}:")
        lines += [f"  {worker.name} ({worker.label}) - {reason}" for worker, reason in doomed]
        lines.append(
            "Their sessions are gone from the state file; log them out of "
            "Telegram itself to be sure."
        )
    else:
        lines.append(f"All {total} worker(s) answered; none is frozen, spam-limited or dead.")
    if kept:
        lines.append("Kept: " + ", ".join(kept[:12]))
    free, used, aside = state.counts
    lines += ["", f"Workers: {free} ready, {used} spent, {aside} aside"]
    await set_fancy(event, "\n".join(lines), done=True)


async def command_remove(
    _client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Forget workers, dropping their stored sessions.

    Takes a short name, an account name, or several of either: one per line, or
    separated by commas. "aside" removes every set-aside worker and "all"
    removes the lot, which beats typing .remove thirteen times.
    """
    raw = " ".join(args).strip()
    if not raw:
        await set_status(
            event,
            "Usage: .remove <name>     (w3, or the account's name)\n"
            "       .remove aside      every worker Telegram refused\n"
            "       .remove cuties     drop frozen + spam-limited workers\n"
            "       .remove all        every worker",
        )
        return

    if raw.lower() in FROZEN_WORDS:
        await remove_frozen(state, event, api_id, api_hash)
        return

    if raw.lower() in {"all", "aside", "disabled"}:
        doomed = (
            list(state.workers)
            if raw.lower() == "all"
            else [worker for worker in state.workers if worker.disabled]
        )
        if not doomed:
            await set_status(event, "Nothing to remove.")
            return
        for worker in doomed:
            state.workers.remove(worker)
        state.save()
        success(f"Removed {len(doomed)} worker(s).")
        await set_status(
            event,
            f"Removed {len(doomed)} worker(s): "
            + ", ".join(f"{worker.name} ({worker.label})" for worker in doomed[:12])
            + ".\nTheir sessions are gone from the state file; log them out of "
            "Telegram itself to be sure.",
        )
        return

    # One target per line, or comma separated. A line may repeat the command,
    # which is what a pasted list of ".remove w1" lines looks like.
    targets: list[str] = []
    for piece in re.split(r"[\n,]+", raw):
        piece = re.sub(r"^\s*\.remove\b", "", piece, flags=re.IGNORECASE).strip()
        if piece:
            targets.append(piece)

    removed: list[str] = []
    unknown: list[str] = []
    for target in targets:
        worker = state.find(target)
        if worker is None:
            unknown.append(target)
            continue
        state.workers.remove(worker)
        removed.append(f"{worker.name} ({worker.label})")
    if removed:
        state.save()
        success(f"Removed {len(removed)} worker(s).")

    lines: list[str] = []
    if removed:
        lines.append(f"Removed {len(removed)}: " + ", ".join(removed))
        lines.append(
            "Their sessions are gone from the state file; log them out of "
            "Telegram itself to be sure."
        )
    if unknown:
        lines.append("No worker matches: " + ", ".join(unknown[:12]))
        lines.append("Check .accounts for the names.")
    await set_status(event, "\n".join(lines), "remove")


async def command_terminate(
    _client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Terminate every other login on the worker accounts.

    ".terminate all" walks every worker; ".terminate <name>" does one. For each
    it connects the stored session and asks Telegram to reset all other
    authorizations, so any login into that account other than the pool's own is
    terminated. Each worker keeps only the session the pool controls; the
    stored session is not touched, so .gen can still use it afterwards.
    """
    if not state.workers:
        await set_status(event, "No workers yet. Add one with .login.")
        return

    target = " ".join(args).strip() or "all"
    if target.lower() == "all":
        workers = list(state.workers)
    else:
        one = state.find(target)
        if one is None:
            await set_status(event, f"No worker matches {target!r}. Check .accounts.")
            return
        workers = [one]

    await set_status(
        event, f"Terminating other logins on {len(workers)} worker(s)..."
    )
    cleared: list[str] = []
    failed: list[tuple[str, str]] = []
    for worker in workers:
        try:
            await terminate_other_logins(worker, api_id, api_hash)
        except Exception as exc:  # noqa: BLE001  One account must not stop the rest.
            reason = why_unusable(exc)
            failed.append((worker.name, reason))
            warning(f"{worker.name}: could not terminate other logins ({reason}).")
            continue
        cleared.append(f"{worker.name} ({worker.label})")
        success(f"{worker.name}: other logins terminated.")

    lines: list[str] = []
    if cleared:
        lines.append(f"Terminated other logins on {len(cleared)} worker(s):")
        lines += [f"  {entry}" for entry in cleared[:12]]
        lines.append("Only the pool's own session is left signed in on each.")
    if failed:
        lines.append(f"Could not do {len(failed)} worker(s):")
        lines += [f"  {name}: {reason}" for name, reason in failed[:12]]
    if not lines:
        lines.append("Nothing to do.")
    await set_status(event, "\n".join(lines), "logout")


async def command_gen(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Make copies of the demo channel, one unused worker per copy."""
    count = 1
    if args:
        if not args[0].isdigit() or int(args[0]) < 1:
            await set_status(event, "Usage: .gen   or   .gen <how many copies>")
            return
        count = min(int(args[0]), GEN_LIMIT)

    try:
        source = await resolve_source(client, state)
    except RuntimeError as exc:
        await set_status(event, f"Cannot .gen: {exc}")
        return

    free, _used, _aside = state.counts
    if not free:
        await set_status(
            event,
            "No unused worker left. Add one with .login, or free the spent ones "
            "with .reset all.",
        )
        return
    if count > free:
        warning(f"{count} copies asked for, but only {free} worker(s) are ready.")
        count = free

    name = utils.get_display_name(source)
    await set_fancy(event, f"Reading {name}...")
    # Read once for the whole run: every copy gets the same posts in the same
    # order, and an expired file reference is re-uploaded post by post.
    plan = await collect_clone_posts(client, source, command_id=event.id)
    if not plan.posts:
        await set_status(event, f"{name} has no posts to copy.")
        return
    info(f"{name}: {len(plan.posts)} post(s) per copy, {count} copy(ies) to make.")

    me = await client.get_me()
    effects = state.gen_effects()
    made: list[MadeChat] = []
    refused: list[tuple[str, str]] = []  # Workers set aside during this run.
    stopped = ""
    for number in range(1, count + 1):
        prefix = f"Copy {number}/{count} of {name}: "

        async def status(text: str, prefix: str = prefix) -> None:
            await set_fancy(event, prefix + text)

        try:
            result = await _gen_once(
                client, me.id, state, api_id, api_hash, source, plan, status, refused,
                effects,
            )
        except OperatorBlocked as exc:
            stopped = f"your own account: {exc}"
            failure(f".gen stopped: {exc}")
            break
        if result is None:
            stopped = "every ready worker was refused by Telegram"
            break
        made.append(result)
        report = "\n".join([f"{result.title}", result.link, *result.report])
        success(f"{result.title}: {result.link}")
        with contextlib.suppress(Exception):
            await client.send_message("me", report, **PLAIN)
        if number < count:
            await asyncio.sleep(GEN_DELAY)

    free, used, aside = state.counts
    lines: list[str] = []
    if made:
        lines.append(f"Made {len(made)} cop{'y' if len(made) == 1 else 'ies'} of {name}:")
        for item in made:
            lines += ["", item.title, item.link, *item.report[:2]]
    else:
        lines.append(f"No copy of {name} was made.")
    if stopped:
        lines += ["", f"Stopped: {stopped}"]
    if refused:
        lines += ["", f"Set aside during this run ({len(refused)}):"]
        lines += [f"  {worker}: {reason}" for worker, reason in refused[:10]]
    lines += ["", f"Workers: {free} ready, {used} spent, {aside} aside"]
    if not free and aside:
        lines.append(
            "Every worker is set aside. Add fresh accounts with .login, or "
            "retry the set-aside ones with .reset all."
        )
    if made:
        lines.append("Full reports are in Saved Messages.")
    if made:
        await set_fancy(event, "\n".join(lines), done=True)
    else:
        await set_status(event, "\n".join(lines))


async def _gen_once(
    client: TelegramClient,
    operator_id: int,
    state: AutoState,
    api_id: int,
    api_hash: str,
    source: types.Channel,
    plan: ClonePlan,
    status: Callable[[str], Awaitable[None]],
    refused: list[tuple[str, str]],
    effects: GenEffects | None = None,
) -> MadeChat | None:
    """Make one copy, moving on to another worker while any are left.

    A worker Telegram refuses is set aside with the reason and the next one is
    tried, which is the whole point of a pool. Only OperatorBlocked is passed
    up, because that would stop every worker the same way. A worker is marked
    used as soon as its channel exists, since the channel quota is spent
    whether or not every post copies.
    """
    while True:
        worker = state.pick()
        if worker is None:
            return None
        try:
            made = await make_one_copy(
                client, operator_id, worker, api_id, api_hash, source, plan, status,
                effects=effects or state.gen_effects(), dp_path=state.pick_dp(),
            )
        except OperatorBlocked:
            raise
        except Exception as exc:  # noqa: BLE001  Any refusal is this worker's.
            reason = why_unusable(exc)
            worker.disabled = reason
            state.save()
            refused.append((worker.name, reason))
            warning(f"{worker.name} set aside: {reason}. Trying another worker.")
            await status(f"{worker.name} refused ({reason}); trying another worker...")
            continue

        worker.used = True
        worker.made.append(made.to_json())
        state.save()
        return made


class StatusElsewhere:
    """A command event whose progress is shown in another message.

    A command typed inside the demo channel must not stay there: the channel
    is what every copy is made from. The command message is deleted and the
    progress goes to a fresh message elsewhere, while everything else, such as
    which chat the command came from, still reads from the real event.
    """

    out = True  # set_status edits this rather than replying to it.

    def __init__(self, event: Any, status_message: Any) -> None:
        self._event = event
        self._status = status_message

    async def edit(self, text: str, **kwargs: Any) -> Any:
        """Edit the stand-in status message."""
        return await self._status.edit(text, **({**PLAIN, **kwargs} if not kwargs else kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)


async def keep_out_of_source(client: TelegramClient, state: AutoState, event: Any, name: str, args: list[str]) -> Any:
    """Move a command out of the demo channel, returning the event to use.

    Applies to anything sent in the demo channel, and to a bare .set sent in
    any channel or group, since that one is about to become the demo channel.
    """
    in_source = state.source_id is not None and event.chat_id == state.source_id
    setting_here = name == "set" and not args and not getattr(event, "is_private", False)
    if not (in_source or setting_here):
        return event
    with contextlib.suppress(Exception):
        await event.delete()
    # The account itself reads its Saved Messages; an admin gets a DM.
    where = "me" if getattr(event, "out", False) else event.sender_id
    status_message = await client.send_message(where, f".{name}: working...", **PLAIN)
    return StatusElsewhere(event, status_message)


async def command_autodel_auto(
    client: TelegramClient, _state: AutoState, event: Any, args: list[str]
) -> None:
    """.autodel inside the auto maker; see autodelete.py."""
    await command_autodel(client, event, args)


PERMIT_POLL = float(os.getenv("AUTOMAKER_PERMIT_POLL") or 3)
PERMIT_GAP = 0.5
PERMIT_SCAN_LIMIT = 200
MEMBER_TYPES = (types.ChannelParticipant, types.ChannelParticipantSelf)
JOIN_LOG_ACTIONS = (
    types.ChannelAdminLogEventActionParticipantJoin,
    types.ChannelAdminLogEventActionParticipantJoinByInvite,
    types.ChannelAdminLogEventActionParticipantJoinByRequest,
)


class PermitWatch:
    def __init__(
        self, client: TelegramClient, state: AutoState, api_id: int, api_hash: str
    ) -> None:
        self.client = client
        self.state = state
        self.api_id = api_id
        self.api_hash = api_hash
        self.in_flight: set[tuple[int, int]] = set()
        self.dialogs_read = False
        self.log_seen: dict[int, int] = {}
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.client.add_event_handler(
            self.on_participant, events.Raw(types.UpdateChannelParticipant)
        )
        self.client.add_event_handler(self.on_action, events.ChatAction())
        if PERMIT_POLL > 0:
            self.task = asyncio.create_task(self.poll())

    async def channel(self, channel_id: int) -> types.Channel | None:
        for attempt in range(2):
            try:
                entity = await self.client.get_entity(types.PeerChannel(channel_id))
                return entity if isinstance(entity, types.Channel) else None
            except Exception:  # noqa: BLE001
                if attempt or self.dialogs_read:
                    return None
                self.dialogs_read = True
                with contextlib.suppress(Exception):
                    await self.client.get_dialogs()
        return None

    async def participant(
        self, channel: types.Channel, user: types.InputUser
    ) -> Any | None:
        try:
            result = await bounded(
                self.client,
                functions.channels.GetParticipantRequest(channel=channel, participant=user),
                label="check a member",
            )
        except errors.UserNotParticipantError:
            return None
        return result.participant

    async def promote(
        self, channel_id: int, user_id: int, participant: Any | None = None
    ) -> bool:
        item = self.state.permit_for(user_id)
        watched = self.state.watched_channels()
        key = (channel_id, user_id)
        if item is None or channel_id not in watched or key in self.in_flight:
            return False
        self.in_flight.add(key)
        try:
            channel = await self.channel(channel_id)
            if channel is None:
                return False
            user = types.InputUser(
                user_id=user_id, access_hash=int(item.get("access_hash") or 0)
            )
            if participant is None:
                participant = await self.participant(channel, user)
            if not isinstance(participant, MEMBER_TYPES):
                return False
            name = item.get("name") or str(user_id)
            title = utils.get_display_name(channel) or str(channel_id)
            megagroup = bool(channel.megagroup)
            try:
                await bounded(
                    self.client,
                    functions.channels.EditAdminRequest(
                        channel=channel,
                        user_id=user,
                        admin_rights=permit_rights(megagroup),
                        rank="admin" if megagroup else "",
                    ),
                    label=f"promote {name}",
                )
                by = "your account"
            except Exception as exc:  # noqa: BLE001
                owner = watched[channel_id]
                if owner is None:
                    return await self.report_failure(name, title, exc)
                try:
                    await self.promote_by_worker(owner, channel_id, user_id, megagroup)
                except Exception as worker_exc:  # noqa: BLE001
                    return await self.report_failure(name, title, worker_exc)
                by = owner.name
            success(f"{name} joined {title} and was made admin by {by}.")
            with contextlib.suppress(Exception):
                await self.client.send_message(
                    "me", f".permit: {name} joined {title} and is now admin ({by}).", **PLAIN
                )
            return True
        finally:
            self.in_flight.discard(key)

    async def report_failure(self, name: str, title: str, exc: BaseException) -> bool:
        if isinstance(exc, (errors.ChatAdminRequiredError, errors.RightForbiddenError)):
            why = "the account doing it is not an admin there with the right to add admins"
        else:
            why = short_error(exc)
        failure(f"Could not make {name} admin in {title}: {why}")
        with contextlib.suppress(Exception):
            await self.client.send_message(
                "me", f".permit: {name} joined {title} but could not be made admin: {why}", **PLAIN
            )
        return False

    async def promote_by_worker(
        self, owner: Worker, channel_id: int, user_id: int, megagroup: bool
    ) -> None:
        worker = await worker_client(owner, self.api_id, self.api_hash)
        try:
            await worker.get_dialogs()
            channel = await worker.get_input_entity(types.PeerChannel(channel_id))
            members = await bounded(
                worker,
                functions.channels.GetParticipantsRequest(
                    channel=channel,
                    filter=types.ChannelParticipantsRecent(),
                    offset=0,
                    limit=PERMIT_SCAN_LIMIT,
                    hash=0,
                ),
                label="read the member list",
            )
            found = next(
                (u for u in getattr(members, "users", None) or [] if u.id == user_id),
                None,
            )
            if found is None:
                raise RuntimeError(f"{owner.name} cannot see them among the members")
            await bounded(
                worker,
                functions.channels.EditAdminRequest(
                    channel=channel,
                    user_id=types.InputUser(
                        user_id=found.id, access_hash=found.access_hash or 0
                    ),
                    admin_rights=permit_rights(megagroup),
                    rank="admin" if megagroup else "",
                ),
                label="promote a permitted user",
            )
        finally:
            await worker.disconnect()

    async def on_participant(self, update: types.UpdateChannelParticipant) -> None:
        if isinstance(update.new_participant, MEMBER_TYPES):
            await self.promote(update.channel_id, update.user_id, update.new_participant)

    async def on_action(self, event: Any) -> None:
        if not (event.user_joined or event.user_added) or not event.is_channel:
            return
        channel_id, _kind = utils.resolve_id(event.chat_id)
        for user_id in event.user_ids or []:
            await self.promote(channel_id, user_id)

    async def joined_since(self, channel_id: int, channel: types.Channel) -> set[int] | None:
        seen = self.log_seen.get(channel_id, 0)
        try:
            log = await bounded(
                self.client,
                functions.channels.GetAdminLogRequest(
                    channel=channel,
                    q="",
                    max_id=0,
                    min_id=seen,
                    limit=50,
                    events_filter=types.ChannelAdminLogEventsFilter(join=True, invite=True),
                ),
                label="read recent actions",
            )
        except (errors.ChatAdminRequiredError, errors.ChannelPrivateError):
            return None
        fresh = [entry for entry in getattr(log, "events", None) or [] if entry.id > seen]
        if fresh:
            self.log_seen[channel_id] = max(entry.id for entry in fresh)
        joined: set[int] = set()
        for entry in fresh:
            if isinstance(entry.action, JOIN_LOG_ACTIONS):
                joined.add(entry.user_id)
            elif isinstance(entry.action, types.ChannelAdminLogEventActionParticipantInvite):
                invited = getattr(entry.action.participant, "user_id", None)
                if invited:
                    joined.add(invited)
        return joined

    async def sweep(self) -> int:
        wanted = {int(item["user_id"]) for item in self.state.permitted}
        if not wanted:
            return 0
        promoted = 0
        for channel_id in list(self.state.watched_channels()):
            channel = await self.channel(channel_id)
            if channel is None:
                continue
            joined = await self.joined_since(channel_id, channel)
            if joined is not None:
                for user_id in joined & wanted:
                    promoted += await self.promote(channel_id, user_id)
                await asyncio.sleep(PERMIT_GAP)
                continue
            try:
                members = await bounded(
                    self.client,
                    functions.channels.GetParticipantsRequest(
                        channel=channel,
                        filter=types.ChannelParticipantsRecent(),
                        offset=0,
                        limit=PERMIT_SCAN_LIMIT,
                        hash=0,
                    ),
                    label="read the member list",
                )
            except Exception as exc:  # noqa: BLE001
                warning(f".permit could not read the members of {channel.title!r}: {short_error(exc)}")
                continue
            for member in getattr(members, "participants", None) or []:
                user_id = getattr(member, "user_id", None)
                if user_id in wanted and isinstance(member, MEMBER_TYPES):
                    promoted += await self.promote(channel_id, user_id, member)
            await asyncio.sleep(1)
        return promoted

    async def poll(self) -> None:
        while True:
            await asyncio.sleep(PERMIT_POLL)
            try:
                await self.sweep()
            except Exception as exc:  # noqa: BLE001
                warning(f".permit check failed: {short_error(exc)}")

    async def catch_up(self, user_id: int) -> int:
        promoted = 0
        for channel_id in list(self.state.watched_channels()):
            try:
                promoted += await self.promote(channel_id, user_id)
            except Exception as exc:  # noqa: BLE001
                warning(f".permit could not check channel {channel_id}: {short_error(exc)}")
            await asyncio.sleep(1)
        return promoted


PERMIT_USAGE = (
    "Send .permit in their DM, reply to one of their messages with .permit, "
    "or use .permit @user / .permit <id>. .permit list shows everyone permitted."
)


async def permit_target(
    client: TelegramClient, event: Any, args: list[str]
) -> types.User | None:
    if args:
        raw = args[0]
        entity = await client.get_entity(int(raw) if raw.lstrip("-").isdigit() else raw)
    elif getattr(event, "is_reply", False):
        reply = await event.get_reply_message()
        entity = await reply.get_sender()
    elif getattr(event, "is_private", False):
        entity = await event.get_chat()
    else:
        return None
    if not isinstance(entity, types.User) or entity.is_self:
        return None
    return entity


async def report_privately(client: TelegramClient, event: Any, name: str) -> Any:
    chat = await event.get_chat()
    if not getattr(event, "is_private", False) or getattr(chat, "is_self", False):
        return event
    with contextlib.suppress(Exception):
        await event.delete()
    where = "me" if getattr(event, "out", False) else event.sender_id
    status_message = await client.send_message(where, f".{name}: working...", **PLAIN)
    return StatusElsewhere(event, status_message)


def render_permits(state: AutoState) -> str:
    if not state.permitted:
        return "Nobody is permitted yet. Send .permit in someone's DM."
    lines = [f"Permitted ({len(state.permitted)}): made admin when they join"]
    for item in state.permitted:
        handle = f" @{item['username']}" if item.get("username") else ""
        lines.append(f"{item.get('name') or '?'}{handle}  {item['user_id']}")
    lines.append(f"Watching {len(state.watched_channels())} channel(s): the demo and its copies.")
    return "\n".join(lines)


async def command_permit(
    client: TelegramClient, state: AutoState, event: Any, args: list[str], watch: PermitWatch
) -> None:
    if args and args[0].lower() == "list":
        await set_status(event, render_permits(state), "users")
        return
    event = await report_privately(client, event, "permit")
    try:
        user = await permit_target(client, event, args)
    except Exception as exc:  # noqa: BLE001
        await set_status(event, f"Cannot find {args[0]}: {short_error(exc)}")
        return
    if user is None:
        await set_status(event, PERMIT_USAGE)
        return

    name = utils.get_display_name(user) or str(user.id)
    state.permitted = [item for item in state.permitted if int(item["user_id"]) != user.id]
    state.permitted.append(
        {
            "user_id": user.id,
            "access_hash": user.access_hash or 0,
            "name": name,
            "username": user.username or "",
            "added_at": now_stamp(),
        }
    )
    state.save()
    watched = len(state.watched_channels())
    info(f"Permitted {name} ({user.id}); checking {watched} channel(s).")
    await set_fancy(event, f"Permitted {name}. Checking {watched} channel(s)...")
    promoted = await watch.catch_up(user.id)
    lines = [
        f"Permitted {name}.",
        f"They are made admin as soon as they join the demo channel or any copy "
        f"({watched} watched).",
    ]
    if promoted:
        lines.append(f"Already a member of {promoted}: made admin there now.")
    await set_fancy(event, "\n".join(lines), done=True)


async def command_unpermit(
    client: TelegramClient, state: AutoState, event: Any, args: list[str], _watch: PermitWatch
) -> None:
    event = await report_privately(client, event, "unpermit")
    if args and args[0].lstrip("-").isdigit():
        user_id: int | None = int(args[0])
    else:
        try:
            user = await permit_target(client, event, args)
        except Exception as exc:  # noqa: BLE001
            await set_status(event, f"Cannot find {args[0]}: {short_error(exc)}")
            return
        user_id = user.id if user is not None else None
    if user_id is None:
        await set_status(event, PERMIT_USAGE.replace(".permit", ".unpermit"))
        return
    item = state.permit_for(user_id)
    if item is None:
        await set_status(event, "They were not permitted.")
        return
    state.permitted = [entry for entry in state.permitted if int(entry["user_id"]) != user_id]
    state.save()
    name = item.get("name") or str(user_id)
    await set_fancy(
        event,
        f"{name} is no longer permitted. Admin rights already given stay; "
        "remove them in the channel if needed.",
        done=True,
    )


COMMAND_HELP = (
    "Auto maker\n"
    ".set [link]            choose the demo channel (send it in the channel)\n"
    ".gen [count]           make copies of the demo channel\n"
    ".delete [link]         delete a copy, free its worker (not the demo)\n"
    ".upgrade [link]        wipe a copy and rebuild it with the current options\n"
    ".login [phone]         add a worker account (phone, code, 2FA)\n"
    ".session [string]      add a worker from a session string (no code)\n"
    ".accounts              list the workers and the demo channel\n"
    ".ids [all]             IDs of finished, live copies (all = with dead)\n"
    ".save dps              send 6 photos; each copy gets one as its PFP\n"
    ".fun [on|off]          readable letter swaps in captions/text per copy\n"
    ".spoiler [on|off]      send photos hidden behind a tap (default on)\n"
    ".reactions [on|off]    reactions in every copy, now and in new ones\n"
    ".permit [@user|list]   make them admin when they join (send in their DM)\n"
    ".unpermit [@user|id]   stop doing that for them\n"
    ".autodel [on|off|...]  delete quick replies 1-2 h after sending\n"
    ".reset [name|all]      free spent and set-aside workers\n"
    ".remove <name|aside|cuties|all>  forget workers (cuties = frozen/spam)\n"
    ".terminate <name|all>  log every other device out of the workers\n"
    ".cancel                stop a login that is waiting\n"
    + GATE_HELP + "\n"
    ".help                  show this list"
)

# Commands that need the API credentials to sign in to a worker account.
WITH_CREDENTIALS = {"login", "session", "gen", "terminate", "delete", "remove", "reactions"}
COMMANDS: dict[str, Callable[..., Awaitable[None]]] = {
    "set": command_set,
    "login": command_login,
    "session": command_session,
    "gen": command_gen,
    "delete": command_delete,
    "upgrade": command_upgrade,
    "accounts": command_accounts,
    "ids": command_ids,
    "save": command_save,
    "fun": command_fun,
    "spoiler": command_spoiler,
    "reactions": command_reactions,
    "autodel": command_autodel_auto,
    "reset": command_reset,
    "remove": command_remove,
    "terminate": command_terminate,
}
PERMIT_COMMANDS: dict[str, Callable[..., Awaitable[None]]] = {
    "permit": command_permit,
    "unpermit": command_unpermit,
}


async def run_auto_maker(client: TelegramClient, api_id: int, api_hash: str) -> None:
    """Listen for the auto-maker commands until interrupted."""
    state = AutoState.load()
    busy = asyncio.Lock()
    gate = CommandGate()
    await gate.load(client)
    # While a login is waiting for a phone code, every other command is ignored
    # in that chat: the answers are plain messages and a stray command in the
    # middle of the flow would be read as one of them.
    awaiting: set[int] = set()
    watch = PermitWatch(client, state, api_id, api_hash)

    @client.on(events.NewMessage(pattern=COMMAND_PATTERN, func=gate.allows))
    async def dispatch(event: Any) -> None:
        name = (event.pattern_match.group(1) or "").lower()
        raw_args = (event.pattern_match.group(2) or "").strip()
        if event.chat_id in awaiting:
            return  # The Conversation handler is reading this chat.
        if name == "help":
            await set_status(event, COMMAND_HELP)
            return
        if name == "cancel":
            await set_status(event, "Nothing is waiting for an answer.")
            return
        try:
            if await gate.handle(client, event, name, raw_args.split()):
                return
        except Exception as exc:  # noqa: BLE001
            failure(f".{name} failed: {exc}")
            await set_status(event, f".{name} failed: {exc}")
            return
        if name in PERMIT_COMMANDS:
            try:
                await PERMIT_COMMANDS[name](
                    client, state, event, raw_args.split() if raw_args else [], watch
                )
            except Exception as exc:  # noqa: BLE001
                failure(f".{name} failed: {exc}")
                await set_status(event, f".{name} failed: {exc}")
            return
        handler = COMMANDS.get(name)
        if handler is None:
            return
        if busy.locked():
            await set_status(event, "Still working on the previous command.")
            return

        args = raw_args.split() if raw_args else []
        async with busy:
            if name in {"login", "session", "save"}:
                awaiting.add(event.chat_id)
            try:
                if name in {"set", "gen", "delete", "upgrade"}:
                    event = await keep_out_of_source(client, state, event, name, args)
                if name in WITH_CREDENTIALS:
                    await handler(client, state, event, args, api_id, api_hash)
                else:
                    await handler(client, state, event, args)
            except Exception as exc:  # noqa: BLE001  One command must not stop the mode.
                failure(f".{name} failed: {exc}")
                await set_status(event, f".{name} failed: {exc}")
            finally:
                awaiting.discard(event.chat_id)

    free, used, aside = state.counts
    print(Fore.YELLOW + Style.BRIGHT + "\n== AUTO MAKER ==")
    print(Fore.WHITE + COMMAND_HELP)
    info(f"State file: {state.path} (holds the worker sessions; keep it private)")
    info(f"Workers: {free} ready, {used} spent, {aside} set aside")
    info(f"Demo channel: {state.source_title or 'none yet, send .set in it'}")
    if not state.workers:
        warning("No workers yet. Send .login to add one.")
    report_gate(gate)
    start_autodelete(client)
    watch.start()
    if state.permitted:
        info(f"Permitted: {len(state.permitted)} user(s) are made admin when they join.")
    info("Send the commands from this account, or an admin's, in any chat. Ctrl+C to stop.")
    await client.run_until_disconnected()
