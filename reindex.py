"""Add or repair the styled caption index in groups that already hold the posts.

oho.py copies the source content but never posts an index, so groups it built
have every post and no index. This tool works on those finished groups: it reads
the posts that are already there, builds the index from their captions, and posts
it with links that point at the copies.

Nothing is sent until you confirm. The default run is a preview that prints every
title and link so the links can be checked first. Re-running replaces a previously
posted index instead of stacking a second one on top.

Run with `python reindex.py`.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, types, utils
from telethon.tl.custom import Message

# The index styling, link building and entity maths already live in main.py.
from main import (
    INDEX_CUSTOM_EMOJI,
    INDEX_STICKER,
    INDEX_TITLE_SUFFIX,
    IndexEntry,
    caption_position,
    entity_username,
    index_title,
    is_index_sticker,
    looks_like_index,
    make_index_messages,
    message_link,
    without_custom_emoji,
)
from oho import ColorFormatter

Result = TypeVar("Result")

LOG = logging.getLogger("reindex")
LOG_FILE = "reindex.log"

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
TRANSIENT_ATTEMPTS = 4

ASCII_BANNER = r"""
   ___  ______   ___ _   _ ___  _______  __
  / _ \|  ___| |_ _| \ | |   \ | ____\ \/ /
 | |_) | |_     | ||  \| | |\ \|  _|  \  /
 |  _ <|  _|    | || |\  | |/ /| |___ /  \
 |_| \_\_|     |___|_| \_|___/ |_____/_/\_\
"""


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
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
    print(Fore.CYAN + Style.BRIGHT + "  REINDEX | ADD THE CAPTION INDEX TO FINISHED GROUPS")
    print(Fore.BLUE + "  preview the links first, then post\n")


def heading(text: str) -> None:
    """Print a bright section heading."""
    print(Fore.YELLOW + Style.BRIGHT + f"\n== {text.upper()} ==")


def env_text(name: str, default: str = "") -> str:
    """Read a stripped environment value."""
    return (os.getenv(name) or "").strip() or default


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


def ask_yes(prompt: str) -> bool:
    """Ask a yes or no question, defaulting to no."""
    return ask(f"{prompt} (y/n)", "n").lower().startswith("y")


@dataclass(frozen=True)
class Settings:
    """Configuration for a reindex run."""

    api_id: int
    api_hash: str
    owner_sessions: tuple[str, ...]
    title_prefix: str
    username_prefix: str


def read_settings() -> Settings:
    """Collect credentials, sessions and the group filters."""
    load_dotenv()
    raw_api_id = env_text("TELEGRAM_API_ID") or ask("Telegram API ID")
    api_hash = env_text("TELEGRAM_API_HASH") or getpass.getpass(
        Fore.CYAN + "Telegram API hash: "
    ).strip()
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")

    configured = env_text("OWNER_SESSIONS")
    if configured:
        sessions = tuple(name.strip() for name in configured.split(",") if name.strip())
    else:
        raw = ask("Owner session names, comma separated", "oho_owner1,oho_owner2,oho_owner3")
        sessions = tuple(name.strip() for name in raw.split(",") if name.strip())
    if not sessions:
        raise ValueError("At least one owner session is required.")

    heading("which groups")
    title_prefix = ask("Group title starts with", env_text("GROUP_TITLE", "Demos"))
    username_prefix = ask(
        "Public link starts with", env_text("GROUP_USERNAME_PREFIX", "XCRYPTO")
    )
    return Settings(
        api_id=api_id,
        api_hash=api_hash,
        owner_sessions=sessions,
        title_prefix=title_prefix,
        username_prefix=username_prefix,
    )


# ---------------------------------------------------------------------------
# request helper
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


# ---------------------------------------------------------------------------
# accounts and group discovery
# ---------------------------------------------------------------------------
@dataclass
class Account:
    """One signed-in owner account."""

    session: str
    client: TelegramClient
    user: types.User


async def sign_in_all(settings: Settings) -> list[Account]:
    """Sign in every owner account, cleaning up if one fails."""
    heading("accounts")
    signed_in: list[Account] = []
    try:
        for session in settings.owner_sessions:
            LOG.info("Signing in %r...", session)
            client = TelegramClient(session, settings.api_id, settings.api_hash)
            await client.start()
            user = await client.get_me()
            if user is None:
                raise RuntimeError(f"Session {session!r} is not authorized.")
            LOG.info(
                "Ready: %s (id %s)", utils.get_display_name(user) or "unnamed", user.id
            )
            signed_in.append(Account(session, client, user))
    except BaseException:
        for account in signed_in:
            await account.client.disconnect()
        raise
    return signed_in


def matches_filters(entity: types.Channel, settings: Settings) -> bool:
    """Return whether a group looks like one of the groups this run targets."""
    username = entity_username(entity) or ""
    title = entity.title or ""
    if settings.username_prefix and username.lower().startswith(
        settings.username_prefix.lower()
    ):
        return True
    return bool(
        settings.title_prefix
        and title.lower().startswith(settings.title_prefix.lower())
    )


async def discover_groups(
    account: Account, settings: Settings
) -> list[types.Channel]:
    """Find the groups this account owns that match the filters."""
    found: list[types.Channel] = []
    async for dialog in account.client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, types.Channel) or not entity.megagroup:
            continue
        if not entity.creator:
            continue
        if matches_filters(entity, settings):
            found.append(entity)
    found.sort(key=lambda group: (entity_username(group) or "", group.title))
    return found


# ---------------------------------------------------------------------------
# reading a finished group
# ---------------------------------------------------------------------------
@dataclass
class GroupScan:
    """What one group contains and what the index for it should be."""

    group: types.Channel
    entries: list[IndexEntry] = field(default_factory=list)
    old_index_ids: list[int] = field(default_factory=list)
    media_posts: int = 0

    @property
    def title(self) -> str:
        """Return the group title."""
        return self.group.title

    @property
    def link(self) -> str:
        """Return the public link, or a private marker."""
        username = entity_username(self.group)
        return f"t.me/{username}" if username else f"private id {self.group.id}"


async def scan_group(
    account: Account, group: types.Channel
) -> GroupScan:
    """Build the index entries for a group from the posts already inside it."""
    scan = GroupScan(group=group)
    album: list[Message] = []
    album_id: int | None = None

    def flush(batch: Sequence[Message]) -> None:
        if not batch:
            return
        scan.media_posts += 1
        position = caption_position(batch)
        if position is None:
            return
        title = index_title(batch[position].message)
        if not title:
            return
        # Link the entry to the post that actually carries the caption.
        target = batch[position]
        url = message_link(group, target.id)
        scan.entries.append(IndexEntry(title=title, url=url))

    async for message in account.client.iter_messages(group, reverse=True):
        if isinstance(message, types.MessageService) or message.action is not None:
            continue
        if is_index_sticker(message) or looks_like_index(message):
            scan.old_index_ids.append(message.id)
            continue
        if message.media is None:
            # Plain text posts are not captions, so they are never indexed.
            continue

        if message.grouped_id is None:
            flush(album)
            album, album_id = [], None
            flush([message])
            continue

        if album and message.grouped_id != album_id:
            flush(album)
            album = []
        album_id = message.grouped_id
        album.append(message)

    flush(album)
    return scan


# ---------------------------------------------------------------------------
# preview and posting
# ---------------------------------------------------------------------------
def preview_scan(scan: GroupScan) -> None:
    """Print exactly what would be posted for one group."""
    chunks = list(make_index_messages(scan.entries))
    print(
        Fore.GREEN + Style.BRIGHT + f"\n  {scan.title} "
        + Fore.WHITE + f"({scan.link})"
    )
    print(
        Fore.BLUE
        + f"    {scan.media_posts} media post(s), {len(scan.entries)} index entr(y/ies), "
        f"{len(chunks)} message(s) to post"
    )
    if scan.old_index_ids:
        print(
            Fore.YELLOW
            + f"    {len(scan.old_index_ids)} existing index message(s) will be removed first"
        )
    if not scan.entries:
        print(Fore.YELLOW + "    nothing to index: no captions found")
        return

    for entry in scan.entries[:5]:
        target = entry.url or "NO LINK"
        print(
            Fore.WHITE
            + f"    {INDEX_CUSTOM_EMOJI} {entry.title}{INDEX_TITLE_SUFFIX}  ->  {target}"
        )
    if len(scan.entries) > 5:
        print(Fore.BLUE + f"    ... and {len(scan.entries) - 5} more")

    missing = [entry for entry in scan.entries if not entry.url]
    if missing:
        print(Fore.RED + f"    {len(missing)} entr(y/ies) have no link")


async def delete_old_index(
    account: Account, scan: GroupScan
) -> int:
    """Remove a previously posted index so a fresh one replaces it."""
    if not scan.old_index_ids:
        return 0
    await resilient(
        account.client.delete_messages,
        scan.group,
        scan.old_index_ids,
        label=f"{scan.title} delete old index",
    )
    LOG.info("%s: removed %s old index message(s).", scan.title, len(scan.old_index_ids))
    return len(scan.old_index_ids)


async def post_index(account: Account, scan: GroupScan) -> int:
    """Send the sticker and every styled index chunk into the group."""
    try:
        await resilient(
            account.client.send_file,
            scan.group,
            INDEX_STICKER,
            label=f"{scan.title} sticker",
        )
    except Exception as exc:  # A stale sticker must not cost us the index.
        LOG.warning("%s: index sticker skipped (%s).", scan.title, exc)

    posted = 0
    for text, entities in make_index_messages(scan.entries):
        try:
            await resilient(
                account.client.send_message,
                scan.group,
                text,
                formatting_entities=list(entities),
                link_preview=False,
                label=f"{scan.title} index chunk",
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "%s: styled chunk failed (%s); retrying without custom emoji.",
                scan.title,
                exc,
            )
            try:
                await resilient(
                    account.client.send_message,
                    scan.group,
                    text,
                    formatting_entities=without_custom_emoji(entities),
                    link_preview=False,
                    label=f"{scan.title} plain index chunk",
                )
            except Exception as retry_exc:  # noqa: BLE001
                LOG.error("%s: index chunk failed (%s).", scan.title, retry_exc)
                continue
        posted += 1
    LOG.info("%s: posted %s index message(s).", scan.title, posted)
    return posted


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------
@dataclass
class Outcome:
    """Result for one group."""

    title: str
    link: str
    entries: int
    posted: int = 0
    removed: int = 0
    error: str | None = None


async def run(settings: Settings) -> None:
    """Scan every matching group, preview the index, then post on confirmation."""
    accounts = await sign_in_all(settings)
    try:
        scans: list[tuple[Account, GroupScan]] = []
        heading("scanning groups")
        for account in accounts:
            groups = await discover_groups(account, settings)
            LOG.info("%s owns %s matching group(s).", account.session, len(groups))
            for group in groups:
                scan = await scan_group(account, group)
                LOG.info(
                    "%s: %s media post(s), %s index entr(y/ies).",
                    group.title,
                    scan.media_posts,
                    len(scan.entries),
                )
                scans.append((account, scan))

        if not scans:
            LOG.error(
                "No groups matched. Check the title prefix %r and link prefix %r.",
                settings.title_prefix,
                settings.username_prefix,
            )
            return

        heading("preview")
        for _, scan in scans:
            preview_scan(scan)

        total_entries = sum(len(scan.entries) for _, scan in scans)
        total_old = sum(len(scan.old_index_ids) for _, scan in scans)
        print()
        LOG.info("Groups: %s", len(scans))
        LOG.info("Index entries in total: %s", total_entries)
        if total_old:
            LOG.info("Old index messages to replace: %s", total_old)
        no_link = sum(
            1 for _, scan in scans for entry in scan.entries if not entry.url
        )
        if no_link:
            LOG.warning("Entries without a link: %s", no_link)

        print(
            Fore.CYAN
            + "\nOpen one of the links above to confirm it lands on the right post."
        )
        if not ask_yes("Post the index into these groups now?"):
            LOG.info("Preview only, nothing was posted.")
            return

        heading("posting")
        outcomes: list[Outcome] = []
        for account, scan in scans:
            outcome = Outcome(
                title=scan.title, link=scan.link, entries=len(scan.entries)
            )
            try:
                outcome.removed = await delete_old_index(account, scan)
                if scan.entries:
                    outcome.posted = await post_index(account, scan)
                else:
                    LOG.info("%s: nothing to index.", scan.title)
            except Exception as exc:  # noqa: BLE001
                outcome.error = str(exc)
                LOG.error("%s: failed (%s).", scan.title, exc)
            outcomes.append(outcome)

        heading("summary")
        for outcome in outcomes:
            if outcome.error:
                print(
                    Fore.RED + Style.BRIGHT + f"  [fail] {outcome.title}: "
                    + Fore.WHITE + outcome.error
                )
            else:
                print(
                    Fore.GREEN + Style.BRIGHT + f"  [ok]   {outcome.title}: "
                    + Fore.WHITE
                    + f"{outcome.entries} entr(y/ies) in {outcome.posted} message(s)"
                    + (f", replaced {outcome.removed}" if outcome.removed else "")
                )
        print()
        LOG.info(
            "Indexed %s/%s groups.",
            sum(1 for outcome in outcomes if not outcome.error),
            len(outcomes),
        )
        LOG.info("Full log written to %s", LOG_FILE)
    finally:
        for account in accounts:
            await account.client.disconnect()


async def main_entry() -> None:
    """Set up output, read configuration, and start the run."""
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
