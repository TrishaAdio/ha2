"""Create group chats through a pool of worker accounts, on command.

This is main.py's mode 3. The signed-in account is the operator: it listens for
dot commands, holds the worker sessions, joins every chat the workers create,
and publishes the invite link where the owner or staff can see it.

    .set                      publish the links in this chat
    .login [phone]            add a worker account, by phone, code and 2FA
    .gen [count] [title]      make a chat with an unused worker, hand over a link
    .accounts                 list the workers and which ones are spent
    .kind group|channel       what .gen creates
    .title <template>         title for new chats, {n} is a counter
    .reset [name|all]         mark workers unused again
    .remove <name>            forget a worker and its session
    .cancel                   abort a login that is waiting for an answer
    .help                     show the list

The split of work is deliberate. A worker account creates the chat, so the
operator's own account never owns anything and never spends its own creation
quota; the operator is promoted to admin in every chat and is the account that
exports the delivered link, because it is the one that stays alive.

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
from telethon.sessions import StringSession

from main import (
    COMMAND_PATTERN,
    GATE_HELP,
    CommandGate,
    channel_from_updates,
    failure,
    info,
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
# Pause between two chats in one .gen run, so a batch does not look automated.
GEN_DELAY = float(os.getenv("AUTOMAKER_GEN_DELAY") or 8)
DEFAULT_TITLE = (os.getenv("AUTOMAKER_TITLE") or "Demo {n}").strip() or "Demo {n}"
CODE_ATTEMPTS = 3
PASSWORD_ATTEMPTS = 3

PHONE_RE = re.compile(r"^\+?\d[\d\s\-().]{5,22}$")
INVITE_HASH_RE = re.compile(r"t\.me/(?:joinchat/|\+)([\w-]+)", re.IGNORECASE)
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


class LoginAborted(Exception):
    """The operator cancelled a login, or stopped answering."""


class WorkerUnusable(Exception):
    """This worker cannot make a chat now, so .gen should try another one."""


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


def operator_rights() -> types.ChatAdminRights:
    """Return the rights the operator account is promoted with.

    Everything except add_admins: the operator has to be able to run the other
    commands in the chat, but a promoted account that can promote further
    accounts is a wider hole than this needs to open.
    """
    return supported_rights(
        change_info=True,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        manage_call=True,
        manage_topics=True,
        post_stories=True,
        edit_stories=True,
        delete_stories=True,
        other=True,
        add_admins=False,
        anonymous=False,
    )


@dataclass
class MadeChat:
    """One chat a worker created, and the link handed to the staff."""

    chat_id: int
    title: str
    link: str
    worker: str
    kind: str
    promoted: bool
    made_at: str = field(default_factory=now_stamp)

    def to_json(self) -> dict[str, Any]:
        """Return the plain dictionary stored in the state file."""
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "link": self.link,
            "worker": self.worker,
            "kind": self.kind,
            "promoted": self.promoted,
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
    delivery_id: int | None = None
    delivery_title: str = ""
    kind: str = "group"
    title_template: str = DEFAULT_TITLE
    counter: int = 0

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
        delivery = raw.get("delivery") or {}
        chat_id = delivery.get("chat_id")
        state.delivery_id = int(chat_id) if chat_id is not None else None
        state.delivery_title = str(delivery.get("title") or "")
        state.kind = "channel" if raw.get("kind") == "channel" else "group"
        state.title_template = str(raw.get("title_template") or DEFAULT_TITLE)
        state.counter = int(raw.get("counter") or 0)
        return state

    def save(self) -> None:
        """Write the state file atomically, readable only by its owner.

        The temporary file is created in the same directory and replaced over
        the real one, so a crash halfway through cannot leave a half-written
        file where the worker sessions used to be.
        """
        payload = {
            "workers": [worker.to_json() for worker in self.workers],
            "delivery": {"chat_id": self.delivery_id, "title": self.delivery_title},
            "kind": self.kind,
            "title_template": self.title_template,
            "counter": self.counter,
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
        """Return a worker by name, case-insensitively."""
        wanted = name.strip().lower()
        for worker in self.workers:
            if worker.name.lower() == wanted:
                return worker
        return None

    def next_name(self) -> str:
        """Return the next free worker name: w1, w2, and so on."""
        taken = {worker.name.lower() for worker in self.workers}
        number = 1
        while f"w{number}" in taken:
            number += 1
        return f"w{number}"

    def next_title(self) -> str:
        """Render the title template, advancing its counter."""
        self.counter += 1
        try:
            return self.title_template.format(n=self.counter) or f"Chat {self.counter}"
        except (IndexError, KeyError, ValueError):
            # A template with a stray brace must not break a whole .gen run.
            return f"{self.title_template} {self.counter}".strip()

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


async def create_chat(
    client: TelegramClient, title: str, kind: str
) -> types.Channel:
    """Create the supergroup, or broadcast channel, a .gen hands over."""
    request = functions.channels.CreateChannelRequest(
        title=title,
        about="",
        megagroup=kind == "group",
        broadcast=kind == "channel",
    )
    try:
        created = await bounded(client, request, label="create a chat")
    except errors.ChannelsTooMuchError as exc:
        raise WorkerUnusable("it has created as many chats as Telegram allows") from exc
    except DEAD_SESSION_ERRORS as exc:
        raise WorkerUnusable(f"its session died ({type(exc).__name__})") from exc
    return channel_from_updates(created)


async def operator_joins(
    worker: TelegramClient, operator: TelegramClient, channel: types.Channel
) -> str:
    """Let the operator into the new chat, and return the link it used.

    The operator joins by invite link rather than being added by the worker:
    adding needs the two accounts to pass each other's privacy settings, while
    a link the worker just made always works.
    """
    exported = await bounded(
        worker,
        functions.messages.ExportChatInviteRequest(peer=channel, title="operator"),
        label="export an invite link",
    )
    link = getattr(exported, "link", None)
    if not link:
        raise RuntimeError("Telegram returned no invite link for the new chat")

    try:
        await bounded(
            operator,
            functions.messages.ImportChatInviteRequest(hash=invite_hash_of(link)),
            label="join the new chat",
        )
    except errors.UserAlreadyParticipantError:
        pass  # Already in: nothing to do, and not a failure.
    return link


async def promote_operator(
    worker: TelegramClient, channel: types.Channel, operator_id: int
) -> bool:
    """Make the operator an admin, reporting whether it worked.

    The worker has to learn the operator's access hash before it can name it in
    a request, and reading the member list is what teaches it: the operator has
    just joined, so it is in there.
    """
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
    if found is None:
        warning("The operator is not in the member list yet, so it stays a member.")
        return False

    try:
        await bounded(
            worker,
            functions.channels.EditAdminRequest(
                channel=channel,
                user_id=types.InputUser(
                    user_id=found.id, access_hash=found.access_hash or 0
                ),
                admin_rights=operator_rights(),
                rank="staff",
            ),
            label="promote the operator",
        )
        return True
    except Exception as exc:  # noqa: BLE001  A member-only operator still works.
        warning(f"The operator could not be promoted: {exc}")
        return False


async def staff_link(
    operator: TelegramClient,
    worker: TelegramClient,
    channel: types.Channel,
    promoted: bool,
    fallback: str,
) -> str:
    """Export the link the staff are given.

    Preferably the operator's own link: an invite link dies with the account
    that made it, and the operator is the account meant to outlive the workers.
    """
    if promoted:
        try:
            exported = await bounded(
                operator,
                functions.messages.ExportChatInviteRequest(peer=channel, title="staff"),
                label="export the staff link",
            )
            link = getattr(exported, "link", None)
            if link:
                return link
        except Exception as exc:  # noqa: BLE001
            warning(f"The operator could not export a link ({exc}); using the worker's.")
    return fallback


async def make_one_chat(
    operator: TelegramClient,
    operator_id: int,
    worker: Worker,
    api_id: int,
    api_hash: str,
    title: str,
    kind: str,
) -> MadeChat:
    """Run one worker through the whole job, start to finish.

    Raises WorkerUnusable when the account itself is the problem, which is the
    signal for .gen to set it aside and pick another one.
    """
    client = await worker_client(worker, api_id, api_hash)
    try:
        channel = await create_chat(client, title, kind)
        info(f"{worker.name} created {title!r} (id {channel.id}).")
        join_link = await operator_joins(client, operator, channel)
        promoted = await promote_operator(client, channel, operator_id)
        link = await staff_link(operator, client, channel, promoted, join_link)
    finally:
        await client.disconnect()

    return MadeChat(
        chat_id=channel.id,
        title=title,
        link=link,
        worker=worker.name,
        kind=kind,
        promoted=promoted,
    )


async def deliver(
    client: TelegramClient, state: AutoState, made: MadeChat
) -> str:
    """Post the link where the staff will see it, falling back to Saved Messages."""
    label = "group" if made.kind == "group" else "channel"
    text = (
        f"New {label}: {made.title}\n"
        f"{made.link}\n"
        f"Made by {made.worker}"
        + ("" if made.promoted else " (operator is a member, not an admin)")
    )
    target = state.delivery_id if state.delivery_id is not None else "me"
    where = state.delivery_title or "Saved Messages"
    try:
        await client.send_message(target, text, link_preview=False)
        return where
    except Exception as exc:  # noqa: BLE001  The link must not be lost.
        failure(f"Could not post the link in {where}: {exc}")
        if target != "me":
            with contextlib.suppress(Exception):
                await client.send_message("me", text, link_preview=False)
                return "Saved Messages (the set chat refused it)"
        return "nowhere; the link is in the terminal only"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def command_set(
    client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Choose the chat that receives every invite link."""
    if args and looks_like_chat_reference(args[0]):
        try:
            chat = await resolve_clone_source(client, args[0])
        except Exception as exc:  # noqa: BLE001
            await set_status(event, f"Cannot use {args[0]}: {exc}")
            return
        state.delivery_id = utils.get_peer_id(chat)
        state.delivery_title = utils.get_display_name(chat) or args[0]
    else:
        chat = await event.get_chat()
        state.delivery_id = event.chat_id
        state.delivery_title = utils.get_display_name(chat) or str(event.chat_id)
    state.save()
    success(f"Links will be posted in {state.delivery_title}.")
    await set_status(event, f"Links will be posted here: {state.delivery_title}.")


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
            mark = f"aside: {worker.disabled}"
        elif worker.used:
            mark = f"spent, made {len(worker.made)}"
        else:
            mark = "ready"
        lines.append(f"{worker.name}  {worker.label}  {mask_phone(worker.phone)}  {mark}")
    lines.append(f"Creates: {state.kind}   Titles: {state.title_template}")
    lines.append(f"Links go to: {state.delivery_title or 'Saved Messages (no .set yet)'}")
    await set_status(event, "\n".join(lines))


async def command_kind(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Choose whether .gen makes a group or a broadcast channel."""
    wanted = (args[0].lower() if args else "")
    if wanted in {"group", "gc", "supergroup"}:
        state.kind = "group"
    elif wanted in {"channel", "broadcast"}:
        state.kind = "channel"
    else:
        await set_status(event, "Usage: .kind group   or   .kind channel")
        return
    state.save()
    await set_status(event, f".gen will create a {state.kind} from now on.")


async def command_title(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Set the title template new chats are named with."""
    if not args:
        await set_status(
            event,
            f"Titles: {state.title_template} (next number {state.counter + 1})\n"
            "Usage: .title Demo {n}",
        )
        return
    state.title_template = " ".join(args).strip()
    state.save()
    await set_status(event, f"New chats will be titled: {state.title_template}")


async def command_reset(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Put spent workers back in the pool."""
    target = args[0] if args else "all"
    if target.lower() == "all":
        for worker in state.workers:
            worker.used = False
        state.save()
        free, _used, _aside = state.counts
        await set_status(event, f"Every worker is unused again: {free} ready.")
        return
    worker = state.find(target)
    if worker is None:
        await set_status(event, f"No worker called {target}.")
        return
    worker.used = False
    worker.disabled = ""
    state.save()
    await set_status(event, f"{worker.name} is ready again.")


async def command_remove(
    _client: TelegramClient, state: AutoState, event: Any, args: list[str]
) -> None:
    """Forget a worker, dropping its stored session."""
    if not args:
        await set_status(event, "Usage: .remove <name>")
        return
    worker = state.find(args[0])
    if worker is None:
        await set_status(event, f"No worker called {args[0]}.")
        return
    state.workers.remove(worker)
    state.save()
    success(f"Removed worker {worker.name}.")
    await set_status(
        event,
        f"Removed {worker.name} ({worker.label}). Its session is gone from the "
        "state file; log it out of Telegram itself to be sure.",
    )


async def command_gen(
    client: TelegramClient,
    state: AutoState,
    event: Any,
    args: list[str],
    api_id: int,
    api_hash: str,
) -> None:
    """Make chats with unused workers and hand over the links."""
    count = 1
    words = list(args)
    if words and words[0].isdigit():
        count = max(1, min(int(words[0]), 50))
        words = words[1:]
    override = " ".join(words).strip()

    free, _used, _aside = state.counts
    if not free:
        await set_status(
            event,
            "No unused worker left. Add one with .login, or free the spent ones "
            "with .reset all.",
        )
        return
    if count > free:
        warning(f"{count} asked for, but only {free} worker(s) are ready.")
        count = free

    me = await client.get_me()
    made: list[MadeChat] = []
    for number in range(1, count + 1):
        await set_status(event, f"Making chat {number}/{count}...")
        result = await _gen_once(client, me.id, state, api_id, api_hash, override)
        if result is None:
            break
        made.append(result)
        where = await deliver(client, state, result)
        success(f"{result.title}: {result.link} (posted in {where})")
        if number < count:
            await asyncio.sleep(GEN_DELAY)

    free, used, aside = state.counts
    if not made:
        await set_status(event, "No chat was made; every worker was refused.")
        return
    lines = [f"Made {len(made)} {'chat' if len(made) == 1 else 'chats'}:"]
    lines += [f"{item.title}\n{item.link}" for item in made]
    lines.append(f"Workers: {free} ready, {used} spent, {aside} aside")
    if state.delivery_title:
        lines.append(f"Links posted in {state.delivery_title}")
    else:
        lines.append("Links went to Saved Messages; use .set to choose a chat")
    await set_status(event, "\n".join(lines))


async def _gen_once(
    client: TelegramClient,
    operator_id: int,
    state: AutoState,
    api_id: int,
    api_hash: str,
    override: str,
) -> MadeChat | None:
    """Make one chat, moving on to another worker while any are left.

    The title is settled before the first attempt, so a worker that has to be
    set aside does not burn a number and leave a gap in the titles.
    """
    title = override or state.next_title()
    while True:
        worker = state.pick()
        if worker is None:
            return None
        try:
            made = await make_one_chat(
                client, operator_id, worker, api_id, api_hash, title, state.kind
            )
        except WorkerUnusable as exc:
            worker.disabled = str(exc)
            state.save()
            warning(f"{worker.name} set aside: {exc}. Trying another worker.")
            continue
        except Exception as exc:  # noqa: BLE001  Keep the worker, report the failure.
            failure(f"{worker.name} could not make {title!r}: {exc}")
            state.save()  # The title counter moved; keep it.
            return None

        worker.used = True
        worker.made.append(made.to_json())
        state.save()
        return made


COMMAND_HELP = (
    "Auto maker\n"
    ".set [link]            post the invite links in this chat\n"
    ".login [phone]         add a worker account (phone, code, 2FA)\n"
    ".gen [count] [title]   make chats with unused workers\n"
    ".accounts              list the workers and the settings\n"
    ".kind group|channel    what .gen creates\n"
    ".title Demo {n}        title template for new chats\n"
    ".reset [name|all]      mark workers unused again\n"
    ".remove <name>         forget a worker and its session\n"
    ".cancel                stop a login that is waiting\n"
    + GATE_HELP + "\n"
    ".help                  show this list"
)

# Commands that need the API credentials to sign a worker in.
WITH_CREDENTIALS = {"login", "gen"}
COMMANDS: dict[str, Callable[..., Awaitable[None]]] = {
    "set": command_set,
    "login": command_login,
    "gen": command_gen,
    "accounts": command_accounts,
    "kind": command_kind,
    "title": command_title,
    "reset": command_reset,
    "remove": command_remove,
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
            if name == "login":
                awaiting.add(event.chat_id)
            try:
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
    info(f"Creates: {state.kind}. Links go to: {state.delivery_title or 'Saved Messages'}")
    if not state.workers:
        warning("No workers yet. Send .login to add one.")
    report_gate(gate)
    info("Send the commands from this account, or an admin's, in any chat. Ctrl+C to stop.")
    await client.run_until_disconnected()
