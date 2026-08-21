"""Delete every post containing a given text across all groups you moderate.

Runs as a live listener. Send `.delete <text>` from your own account and it
scans every group and channel where you can delete other people's messages,
finds every post whose text or caption contains that text, and reports what it
found. Nothing is deleted until you send `.confirm`.

    .delete @HJGFDS     find posts mentioning @HJGFDS everywhere you moderate
    .confirm            delete the posts from the last .delete
    .cancel             forget the last .delete
    .help               show the commands

Run with `python lol.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Union

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, types, utils
from telethon.tl.custom import Dialog, Message

# Logging setup and the resilient request helper already live in oho.py.
from oho import ColorFormatter, resilient

ChatEntity = Union[types.Channel, types.Chat]

LOG = logging.getLogger("lol")
LOG_FILE = "lol.log"

DELETE_DELAY = 0.5
SCAN_STATUS_EVERY = 25
PENDING_TTL = 300.0
MIN_QUERY_LENGTH = 2

ASCII_BANNER = r"""
   __     ___  __
  / /    / _ \/ /
 / /    / // / /__
/_/____/____/____/
  /_____/
"""


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def setup_logging() -> None:
    """Send colored logs to the terminal and plain logs to the log file."""
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
    colorama_init(autoreset=True)
    print(Fore.GREEN + Style.BRIGHT + ASCII_BANNER)
    print(Fore.CYAN + Style.BRIGHT + "  LOL | DELETE POSTS BY TEXT ACROSS EVERY GROUP")
    print(Fore.BLUE + "  .delete <text>  ->  preview  ->  .confirm\n")


def env_text(name: str, default: str = "") -> str:
    """Read a stripped environment value."""
    return (os.getenv(name) or "").strip() or default


def read_credentials() -> tuple[int, str, str]:
    """Read the API credentials and session name."""
    load_dotenv()
    raw_api_id = env_text("TELEGRAM_API_ID") or input(
        Fore.CYAN + "Telegram API ID: "
    ).strip()
    api_hash = env_text("TELEGRAM_API_HASH") or getpass.getpass(
        Fore.CYAN + "Telegram API hash: "
    ).strip()
    session = env_text("TELEGRAM_SESSION", "channel_copier")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise ValueError("TELEGRAM_API_ID must be a positive integer.")
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")
    return api_id, api_hash, session


# ---------------------------------------------------------------------------
# chat model
# ---------------------------------------------------------------------------
def is_group_or_channel(entity: object) -> bool:
    """Return whether an entity is a group or channel this tool can scan."""
    if isinstance(entity, types.Channel):
        if getattr(entity, "monoforum", False):
            return False
        return bool(entity.broadcast or entity.megagroup or entity.gigagroup)
    if isinstance(entity, types.Chat):
        return not (entity.deactivated or entity.migrated_to)
    return False


def can_delete_others(entity: ChatEntity) -> bool:
    """Return whether the account may delete anyone's messages in a chat.

    That needs ownership or the delete-messages admin right. Being an ordinary
    member is not enough, since a member can only delete their own messages.
    """
    if getattr(entity, "left", False):
        return False
    if isinstance(entity, types.Chat) and (entity.deactivated or entity.migrated_to):
        return False
    if getattr(entity, "creator", False):
        return True
    rights = getattr(entity, "admin_rights", None)
    return bool(rights and rights.delete_messages)


def chat_type_label(entity: ChatEntity) -> str:
    """Describe a chat for the logs."""
    if isinstance(entity, types.Chat):
        return "group"
    if entity.gigagroup:
        return "broadcast group"
    if entity.broadcast:
        return "channel"
    if entity.megagroup:
        return "supergroup"
    return "channel"


def chat_name(entity: ChatEntity) -> str:
    """Return a readable chat name for reports."""
    return utils.get_display_name(entity) or f"id {entity.id}"


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def post_contains(message: Message, needle: str) -> bool:
    """Return whether a post's text or caption contains the needle.

    A media post keeps its caption in the same `message` field as a plain text
    post, so this one check covers posts with media and without.
    """
    text = message.message or ""
    return needle.casefold() in text.casefold()


async def matching_messages(
    client: TelegramClient, entity: ChatEntity, needle: str
) -> AsyncIterator[Message]:
    """Yield posts whose text or caption contains the needle.

    Telegram's own search narrows the history server-side, then each candidate
    is confirmed locally so the match is an exact case-insensitive substring
    rather than whatever the search tokenizer considered close.
    """
    async for message in client.iter_messages(entity, search=needle):
        if message.action is not None:
            continue
        if post_contains(message, needle):
            yield message


# ---------------------------------------------------------------------------
# scanning and the pending plan
# ---------------------------------------------------------------------------
@dataclass
class GroupHits:
    """The matching message ids found in one chat."""

    entity: ChatEntity
    message_ids: list[int] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Return the chat name."""
        return chat_name(self.entity)


@dataclass
class Plan:
    """A scanned-but-not-yet-executed deletion, awaiting confirmation."""

    needle: str
    hits: list[GroupHits]
    created: float = field(default_factory=time.monotonic)

    @property
    def total(self) -> int:
        """Return how many posts would be deleted."""
        return sum(len(hit.message_ids) for hit in self.hits)

    @property
    def expired(self) -> bool:
        """Return whether the plan is too old to confirm."""
        return time.monotonic() - self.created > PENDING_TTL


async def deletable_dialogs(client: TelegramClient) -> list[Dialog]:
    """Return every group or channel where deleting others is allowed."""
    dialogs: list[Dialog] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if is_group_or_channel(entity) and can_delete_others(entity):
            dialogs.append(dialog)
    return dialogs


async def scan(client: TelegramClient, needle: str, event: object) -> Plan:
    """Find every matching post across all moderated chats."""
    dialogs = await deletable_dialogs(client)
    LOG.info("Scanning %s moderated chat(s) for %r...", len(dialogs), needle)

    hits: list[GroupHits] = []
    for index, dialog in enumerate(dialogs, start=1):
        group_hits = GroupHits(entity=dialog.entity)
        try:
            async for message in matching_messages(client, dialog.entity, needle):
                group_hits.message_ids.append(message.id)
        except errors.ChatAdminRequiredError:
            LOG.warning("%s: not allowed to read history; skipping.", group_hits.name)
            continue
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s: could not scan (%s); skipping.", group_hits.name, exc)
            continue

        if group_hits.message_ids:
            hits.append(group_hits)
            LOG.info(
                "%s [%s]: %s match(es).",
                group_hits.name,
                chat_type_label(dialog.entity),
                len(group_hits.message_ids),
            )
        if index % SCAN_STATUS_EVERY == 0:
            await set_status(event, f"Scanning for {needle}... {index}/{len(dialogs)}")

    return Plan(needle=needle, hits=hits)


# ---------------------------------------------------------------------------
# executing the plan
# ---------------------------------------------------------------------------
@dataclass
class DeleteStats:
    """Counters for a confirmed deletion."""

    deleted: int = 0
    failed: int = 0
    groups: int = 0


async def execute(client: TelegramClient, plan: Plan, event: object) -> DeleteStats:
    """Delete every post in a confirmed plan, for everyone."""
    stats = DeleteStats()
    for index, hit in enumerate(plan.hits, start=1):
        try:
            await resilient(
                client.delete_messages,
                hit.entity,
                hit.message_ids,
                revoke=True,
                label=f"delete in {hit.name}",
            )
            stats.deleted += len(hit.message_ids)
            stats.groups += 1
            LOG.info("%s: deleted %s post(s).", hit.name, len(hit.message_ids))
        except Exception as exc:  # noqa: BLE001
            stats.failed += len(hit.message_ids)
            LOG.error("%s: delete failed (%s).", hit.name, exc)
        await set_status(
            event, f"Deleting {plan.needle}... {index}/{len(plan.hits)} chat(s)"
        )
        await asyncio.sleep(DELETE_DELAY)
    return stats


# ---------------------------------------------------------------------------
# command surface
# ---------------------------------------------------------------------------
async def set_status(event: object, text: str) -> None:
    """Replace the command message with a progress line, ignoring failures."""
    with contextlib.suppress(Exception):
        await event.edit(text)


def preview_text(plan: Plan) -> str:
    """Render the scan result for the operator to confirm."""
    if not plan.hits:
        return f"No posts contain {plan.needle}. Nothing to delete."
    lines = [f"Found {plan.total} post(s) with {plan.needle} in {len(plan.hits)} chat(s):"]
    for hit in plan.hits[:15]:
        lines.append(f"  {hit.name}: {len(hit.message_ids)}")
    if len(plan.hits) > 15:
        remaining = sum(len(h.message_ids) for h in plan.hits[15:])
        lines.append(f"  ... {len(plan.hits) - 15} more chat(s), {remaining} post(s)")
    lines.append("Send .confirm within 5 minutes to delete, or .cancel.")
    return "\n".join(lines)


COMMAND_HELP = (
    "Commands\n"
    ".delete <text>   find every post with that text in chats you moderate\n"
    ".confirm         delete the posts found by the last .delete\n"
    ".cancel          forget the last .delete\n"
    ".help            show this list"
)


@dataclass
class CommandState:
    """Holds the single pending plan between .delete and .confirm."""

    pending: Plan | None = None


async def handle_delete(
    client: TelegramClient, event: object, args: str, state: CommandState
) -> None:
    """Scan for a text and store the plan without deleting anything yet."""
    needle = args.strip()
    if len(needle) < MIN_QUERY_LENGTH:
        await set_status(
            event, f"Give at least {MIN_QUERY_LENGTH} characters, e.g. .delete @name"
        )
        return

    await set_status(event, f"Scanning for {needle}...")
    plan = await scan(client, needle, event)
    state.pending = plan if plan.hits else None
    LOG.info("Scan for %r found %s post(s) in %s chat(s).", needle, plan.total, len(plan.hits))
    await set_status(event, preview_text(plan))


async def handle_confirm(
    client: TelegramClient, event: object, state: CommandState
) -> None:
    """Execute the stored plan if one is waiting and still fresh."""
    plan = state.pending
    if plan is None:
        await set_status(event, "Nothing to confirm. Run .delete <text> first.")
        return
    if plan.expired:
        state.pending = None
        await set_status(event, "That .delete expired. Run it again.")
        return

    state.pending = None
    LOG.info("Confirmed: deleting %s post(s) for %r.", plan.total, plan.needle)
    await set_status(event, f"Deleting {plan.total} post(s) with {plan.needle}...")
    stats = await execute(client, plan, event)

    summary = f"Deleted {stats.deleted} post(s) with {plan.needle} from {stats.groups} chat(s)"
    if stats.failed:
        summary += f", {stats.failed} could not be deleted"
    LOG.info("%s", summary)
    await set_status(event, summary)


async def run_commands(client: TelegramClient) -> None:
    """Listen for the dot commands until interrupted."""
    state = CommandState()
    busy = asyncio.Lock()

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]*))?$"))
    async def dispatch(event: object) -> None:
        name = (event.pattern_match.group(1) or "").lower()
        args = event.pattern_match.group(2) or ""
        if name == "help":
            await set_status(event, COMMAND_HELP)
            return
        if name == "cancel":
            state.pending = None
            await set_status(event, "Cleared the pending delete.")
            return
        if name not in {"delete", "confirm"}:
            return
        if busy.locked():
            await set_status(event, "Still working on the previous command.")
            return
        async with busy:
            try:
                if name == "delete":
                    await handle_delete(client, event, args, state)
                else:
                    await handle_confirm(client, event, state)
            except Exception as exc:  # noqa: BLE001
                LOG.error(".%s failed: %s", name, exc)
                await set_status(event, f".{name} failed: {exc}")

    print(Fore.YELLOW + Style.BRIGHT + "\n== COMMAND MODE ==")
    print(Fore.WHITE + COMMAND_HELP)
    LOG.info("Send the commands from this account in any chat. Ctrl+C to stop.")
    await client.run_until_disconnected()


async def main() -> None:
    """Sign in and start the command listener."""
    setup_logging()
    show_banner()
    api_id, api_hash, session = read_credentials()
    LOG.info("Connecting to Telegram...")
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    me = await client.get_me()
    LOG.info("Connected as %s.", chat_name(me) if me else "unknown")
    try:
        await run_commands(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.warning("Stopped by user.")
    except (RuntimeError, ValueError) as exc:
        LOG.critical("%s", exc)
        raise SystemExit(1) from exc
