"""Make fresh copies of a demo channel through a pool of worker accounts.

This is main.py's mode 3. The signed-in (premium) account is the operator.

    .set [link]               choose the demo channel that .gen copies
    .login [phone]            add a worker account, by phone, code and 2FA
    .session [string]         add a worker from a session string, no code
    .gen [count]              make that many copies of the demo channel
    .delete [link]            delete a copy and free its worker, never the demo
    .accounts                 list the workers and which ones are spent
    .reset [name|all]         mark workers unused again
    .remove <name|cuties>     forget workers; "cuties" drops the frozen ones
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

from main import (
    COMMAND_PATTERN,
    GATE_HELP,
    PLAIN,
    ClonePlan,
    CommandGate,
    channel_from_updates,
    clone_into,
    collect_clone_posts,
    failure,
    info,
    is_supported_chat,
    looks_like_chat_reference,
    message_text,
    report_gate,
    resolve_clone_source,
    set_status,
    success,
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
CODE_ATTEMPTS = 3
PASSWORD_ATTEMPTS = 3

PHONE_RE = re.compile(r"^\+?\d[\d\s\-().]{5,22}$")
INVITE_HASH_RE = re.compile(r"t\.me/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE)
# A fresh channel takes a moment to propagate; retry the join and member
# lookup this many times, waiting JOIN_RETRY_DELAY * attempt seconds between.
JOIN_RETRIES = 4
JOIN_RETRY_DELAY = 3


# Premium custom emoji shown on .gen progress: an hourglass while working and
# a check mark when done. The text after the emoji is bold.
WORKING_EMOJI = ("\u23f3", 6113844439292054570)
DONE_EMOJI = ("\u2705", 5411197345968701560)


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


async def set_fancy(event: Any, text: str, done: bool = False) -> None:
    """Like set_status, but with a custom emoji in front and the text in bold."""
    emoji, emoji_id = DONE_EMOJI if done else WORKING_EMOJI
    full = f"{emoji} {text}"
    entities = [
        types.MessageEntityCustomEmoji(offset=0, length=_utf16(emoji), document_id=emoji_id),
        types.MessageEntityBold(offset=_utf16(emoji) + 1, length=_utf16(text)),
    ]
    kwargs = {"link_preview": False, "formatting_entities": entities}
    with contextlib.suppress(Exception):
        if getattr(event, "out", True):
            await event.edit(full, **kwargs)
            return
        reply = getattr(event, "status_reply", None)
        if reply is None:
            event.status_reply = await event.reply(full, **kwargs)
            return
        with contextlib.suppress(errors.MessageNotModifiedError):
            await reply.edit(full, **kwargs)
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


def operator_rights(megagroup: bool) -> types.ChatAdminRights:
    """Return the rights the operator account is promoted with.

    Channels and groups take different sets, the way the official apps offer
    them: posting and editing exist only in broadcast channels, banning and
    pinning only in groups, and Telegram can refuse a promotion that mixes the
    two. The copy needs change_info for the photo, description and content
    protection, post and edit rights for the posts and their links, and delete
    rights to remove service messages. add_admins is never given, so the
    operator cannot promote anyone else.
    """
    if megagroup:
        return supported_rights(
            change_info=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            manage_call=True,
            other=True,
        )
    return supported_rights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        invite_users=True,
        manage_call=True,
        post_stories=True,
        edit_stories=True,
        delete_stories=True,
        other=True,
    )


def full_rights(megagroup: bool) -> types.ChatAdminRights:
    """Return every admin right the owner can hand out, for the admin bot.

    Like operator_rights, channels and groups get their own set, since Telegram
    can refuse a promotion that mixes the two. Unlike the operator, the bot is
    given add_admins too: it gets full rights. anonymous is left off, so the
    bot's actions stay visible as its own.
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
    """Make the operator an admin with every right the copy needs.

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
                admin_rights=operator_rights(bool(channel.megagroup)),
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

    The worker does this because it owns the channel: only the owner can grant
    add_admins, which the operator deliberately does not have. Promoting a bot
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
) -> MadeChat:
    """Have one worker create the channel, then copy the demo channel into it.

    Raises WorkerUnusable when the account itself is the problem, which is the
    signal for .gen to set it aside and pick another one, and OperatorBlocked
    when it is not. Either way, a channel that was created but never got its
    copy is deleted again.
    """
    title = utils.get_display_name(source) or "Copy"
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

    # From here on the operator does every write, and the worker is done. The
    # channel exists and the worker's quota is spent, so whatever happens next
    # is reported with the link rather than thrown away.
    try:
        outcome = await clone_into(operator, source, target, plan, status)
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
    await set_status(event, "\n".join(lines))


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
FROZEN_WORDS = {"cuties", "cutie", "frozen", "dead"}


async def worker_health(
    worker: Worker, api_id: int, api_hash: str
) -> tuple[str, str]:
    """Say whether a worker is frozen, dead or still fine.

    Returns one of "frozen", "dead", "ok" or "unknown", with a reason.

    A frozen account answers normally to reads, which is why it is only caught
    halfway through a .gen, sometimes as a flood wait that is not a real rate
    limit. Telegram refuses writes on it, so the account's own "about" text is
    written back unchanged: nothing on the profile moves, and a frozen account
    raises FROZEN_METHOD_INVALID instead.
    """
    if "frozen" in worker.disabled.lower():
        return "frozen", worker.disabled
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
        return "ok", "writes are allowed"
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
    """Check every worker and forget the ones Telegram has frozen or killed."""
    if not state.workers:
        await set_status(event, "There are no workers yet. Add one with .login.")
        return
    doomed: list[tuple[Worker, str]] = []
    kept: list[str] = []
    total = len(state.workers)
    for number, worker in enumerate(list(state.workers), start=1):
        await set_fancy(event, f"Checking {worker.name}... {number}/{total}")
        verdict, reason = await worker_health(worker, api_id, api_hash)
        if verdict in {"frozen", "dead"}:
            doomed.append((worker, f"{verdict}: {reason}"))
        else:
            kept.append(f"{worker.name} ({reason})" if verdict == "unknown" else worker.name)

    for worker, _reason in doomed:
        state.workers.remove(worker)
    if doomed:
        state.save()
        success(f"Removed {len(doomed)} frozen or dead worker(s).")

    lines: list[str] = []
    if doomed:
        lines.append(f"Removed {len(doomed)} of {total}:")
        lines += [f"  {worker.name} ({worker.label}) - {reason}" for worker, reason in doomed]
        lines.append(
            "Their sessions are gone from the state file; log them out of "
            "Telegram itself to be sure."
        )
    else:
        lines.append(f"All {total} worker(s) answered; none is frozen or dead.")
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
            "       .remove cuties     check them all, drop the frozen ones\n"
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
    await set_status(event, "\n".join(lines))


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
    await set_status(event, "\n".join(lines))


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
    made: list[MadeChat] = []
    refused: list[tuple[str, str]] = []  # Workers set aside during this run.
    stopped = ""
    for number in range(1, count + 1):
        prefix = f"Copy {number}/{count} of {name}: "

        async def status(text: str, prefix: str = prefix) -> None:
            await set_fancy(event, prefix + text)

        try:
            result = await _gen_once(
                client, me.id, state, api_id, api_hash, source, plan, status, refused
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
                client, operator_id, worker, api_id, api_hash, source, plan, status
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
        return await self._status.edit(text, **{**PLAIN, **kwargs})

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


COMMAND_HELP = (
    "Auto maker\n"
    ".set [link]            choose the demo channel (send it in the channel)\n"
    ".gen [count]           make copies of the demo channel\n"
    ".delete [link]         delete a copy, free its worker (not the demo)\n"
    ".login [phone]         add a worker account (phone, code, 2FA)\n"
    ".session [string]      add a worker from a session string (no code)\n"
    ".accounts              list the workers and the demo channel\n"
    ".reset [name|all]      free spent and set-aside workers\n"
    ".remove <name|aside|cuties|all>  forget workers (cuties = frozen)\n"
    ".terminate <name|all>  log every other device out of the workers\n"
    ".cancel                stop a login that is waiting\n"
    + GATE_HELP + "\n"
    ".help                  show this list"
)

# Commands that need the API credentials to sign in to a worker account.
WITH_CREDENTIALS = {"login", "session", "gen", "terminate", "delete", "remove"}
COMMANDS: dict[str, Callable[..., Awaitable[None]]] = {
    "set": command_set,
    "login": command_login,
    "session": command_session,
    "gen": command_gen,
    "delete": command_delete,
    "accounts": command_accounts,
    "reset": command_reset,
    "remove": command_remove,
    "terminate": command_terminate,
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
        handler = COMMANDS.get(name)
        if handler is None:
            return
        if busy.locked():
            await set_status(event, "Still working on the previous command.")
            return

        args = raw_args.split() if raw_args else []
        async with busy:
            if name in {"login", "session"}:
                awaiting.add(event.chat_id)
            try:
                if name in {"set", "gen", "delete"}:
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
    info("Send the commands from this account, or an admin's, in any chat. Ctrl+C to stop.")
    await client.run_until_disconnected()
