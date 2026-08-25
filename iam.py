"""Copy one account's whole profile onto the signed-in account.

Made for recovering your own identity after losing access to an account: sign
the userbot in as the new account, open a chat with the old one, and send
`.this`. Everything readable is copied onto the new account.

    .this            copy the profile of the user in this chat
    .this @handle    copy that user's profile
    .this dry        show what would be copied, change nothing
    .help            show the commands

Copies the name, bio, username (incrementing the trailing number when the old
one is still taken), every profile photo, emoji status, name and profile
colours, birthday, business hours, business location, business intro, and the
channel shown on the profile.

Run with `python iam.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from colorama import Fore, Style, init as colorama_init
from dotenv import load_dotenv
from telethon import TelegramClient, errors, events, functions, types, utils

from oho import ColorFormatter, resilient

LOG = logging.getLogger("iam")
LOG_FILE = "iam.log"

MIN_USERNAME = 5
MAX_USERNAME = 32
USERNAME_TRIES = 30
BIO_LIMIT = 70
PHOTO_LIMIT_DEFAULT = 10

ASCII_BANNER = r"""
   _____   _____  ___
  |_   _| /  _  \|   \
    | |   | |_| || |\ |
   _| |_  |  _  || | \|
  |_____| |_| |_||_|
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
    print(Fore.CYAN + Style.BRIGHT + "  IAM | COPY A PROFILE ONTO THIS ACCOUNT")
    print(Fore.BLUE + "  .this in a chat with the old account\n")


def env_text(name: str, default: str = "") -> str:
    """Read a stripped environment value."""
    return (os.getenv(name) or "").strip() or default


def read_credentials() -> tuple[int, str, str]:
    """Read the API credentials and the session for the new account."""
    load_dotenv()
    raw_api_id = env_text("TELEGRAM_API_ID") or input(Fore.CYAN + "Telegram API ID: ").strip()
    api_hash = env_text("TELEGRAM_API_HASH") or getpass.getpass(
        Fore.CYAN + "Telegram API hash: "
    ).strip()
    session = env_text("IAM_SESSION", "iam_new_account")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer.") from exc
    if not api_hash:
        raise ValueError("TELEGRAM_API_HASH cannot be empty.")
    return api_id, api_hash, session


def photo_limit() -> int:
    """Return how many profile photos to copy at most."""
    raw = env_text("PHOTO_LIMIT")
    return int(raw) if raw.isdigit() and int(raw) > 0 else PHOTO_LIMIT_DEFAULT


# ---------------------------------------------------------------------------
# reading the old profile
# ---------------------------------------------------------------------------
@dataclass
class Snapshot:
    """Everything worth copying from the source account."""

    user: types.User
    about: str | None = None
    birthday: types.Birthday | None = None
    work_hours: types.BusinessWorkHours | None = None
    location: types.BusinessLocation | None = None
    intro: types.BusinessIntro | None = None
    personal_channel_id: int | None = None
    photos: list[types.Photo] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Return the display name of the source account."""
        return utils.get_display_name(self.user) or f"id {self.user.id}"

    @property
    def username(self) -> str | None:
        """Return the primary active username, if any."""
        if self.user.username:
            return self.user.username
        for extra in getattr(self.user, "usernames", None) or []:
            if extra.active:
                return extra.username
        return None

    @property
    def emoji_document_id(self) -> int | None:
        """Return the emoji status document id, whatever its flavour."""
        status = self.user.emoji_status
        if status is None or isinstance(status, types.EmojiStatusEmpty):
            return None
        return getattr(status, "document_id", None)

    @property
    def collectible_status(self) -> bool:
        """Return whether the status is a collectible, which cannot move."""
        return isinstance(self.user.emoji_status, types.EmojiStatusCollectible)


async def read_snapshot(
    client: TelegramClient, user: types.User, limit: int
) -> Snapshot:
    """Read every copyable field from the source account."""
    full = await resilient(
        client, functions.users.GetFullUserRequest(id=user), label="read profile"
    )
    # getFullUser returns the authoritative user object alongside the details.
    fresh = next(
        (u for u in getattr(full, "users", None) or [] if u.id == user.id), user
    )
    details = full.full_user

    snapshot = Snapshot(
        user=fresh,
        about=getattr(details, "about", None),
        birthday=getattr(details, "birthday", None),
        work_hours=getattr(details, "business_work_hours", None),
        location=getattr(details, "business_location", None),
        intro=getattr(details, "business_intro", None),
        personal_channel_id=getattr(details, "personal_channel_id", None),
    )

    try:
        result = await resilient(
            client,
            functions.photos.GetUserPhotosRequest(
                user_id=utils.get_input_user(fresh), offset=0, max_id=0, limit=limit
            ),
            label="read photos",
        )
        snapshot.photos = [
            photo for photo in result.photos if isinstance(photo, types.Photo)
        ]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not list profile photos: %s", exc)

    return snapshot


# ---------------------------------------------------------------------------
# username variants
# ---------------------------------------------------------------------------
def username_variants(original: str) -> Iterator[str]:
    """Yield the original username, then increasing trailing numbers.

    A name already ending in a number carries on from there, so `name2`
    becomes `name3`; one without a number gains a `2`.
    """
    if MIN_USERNAME <= len(original) <= MAX_USERNAME:
        yield original

    match = re.fullmatch(r"(.*?)(\d+)", original)
    if match:
        stem, start = match.group(1), int(match.group(2)) + 1
    else:
        stem, start = original, 2

    for number in range(start, start + USERNAME_TRIES):
        candidate = f"{stem}{number}"
        if MIN_USERNAME <= len(candidate) <= MAX_USERNAME:
            yield candidate


async def claim_username(client: TelegramClient, original: str) -> str:
    """Take the original username, or the next free numbered variant."""
    for candidate in username_variants(original):
        try:
            free = await resilient(
                client,
                functions.account.CheckUsernameRequest(username=candidate),
                label=f"check @{candidate}",
            )
        except errors.UsernameInvalidError:
            continue
        if not free:
            LOG.info("@%s is taken.", candidate)
            continue
        await resilient(
            client,
            functions.account.UpdateUsernameRequest(username=candidate),
            label=f"set @{candidate}",
        )
        return candidate
    raise RuntimeError("no free username variant was found")


# ---------------------------------------------------------------------------
# applying each field
# ---------------------------------------------------------------------------
@dataclass
class Report:
    """Per-field outcome of a copy run."""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def ok(self, field_name: str, detail: str = "") -> None:
        """Record a field that was copied."""
        line = f"{field_name}: {detail}" if detail else field_name
        self.applied.append(line)
        LOG.info("Applied %s", line)

    def skip(self, field_name: str, why: str) -> None:
        """Record a field with nothing to copy."""
        self.skipped.append(f"{field_name}: {why}")
        LOG.debug("Skipped %s (%s)", field_name, why)

    def fail(self, field_name: str, why: str) -> None:
        """Record a field that could not be copied."""
        self.failed.append(f"{field_name}: {why}")
        LOG.error("Failed %s (%s)", field_name, why)

    def render(self, header: str) -> str:
        """Render the outcome for the command message."""
        lines = [header]
        for line in self.applied:
            lines.append(f"+ {line}")
        for line in self.failed:
            lines.append(f"! {line}")
        for line in self.skipped:
            lines.append(f"- {line}")
        return "\n".join(lines)


def describe_reason(exc: Exception) -> str:
    """Turn an exception into a short reason for the report."""
    if isinstance(exc, errors.PremiumAccountRequiredError):
        return "needs Telegram Premium"
    if isinstance(exc, errors.FloodWaitError):
        return f"flood wait {exc.seconds}s"
    return str(exc) or type(exc).__name__


async def apply_names(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the first name, last name and bio."""
    about = (snap.about or "")[:BIO_LIMIT]
    try:
        await resilient(
            client,
            functions.account.UpdateProfileRequest(
                first_name=snap.user.first_name or "",
                last_name=snap.user.last_name or "",
                about=about,
            ),
            label="set name and bio",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("name and bio", describe_reason(exc))
        return
    report.ok("name", utils.get_display_name(snap.user) or "(empty)")
    if snap.about:
        truncated = " (truncated)" if len(snap.about) > BIO_LIMIT else ""
        report.ok("bio", f"{len(about)} chars{truncated}")
    else:
        report.skip("bio", "the old account has none")


async def apply_username(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the username, using a numbered variant when it is taken."""
    original = snap.username
    if not original:
        report.skip("username", "the old account has none")
        return
    try:
        taken = await claim_username(client, original)
    except Exception as exc:  # noqa: BLE001
        report.fail("username", describe_reason(exc))
        return
    detail = f"@{taken}" if taken == original else f"@{taken} (@{original} was taken)"
    report.ok("username", detail)


async def apply_photos(
    client: TelegramClient, snap: Snapshot, report: Report
) -> None:
    """Copy the profile photos, oldest first so the newest ends up current."""
    if not snap.photos:
        report.skip("profile photos", "none found")
        return

    animated = sum(1 for photo in snap.photos if getattr(photo, "video_sizes", None))
    copied = 0
    with tempfile.TemporaryDirectory(prefix="iam-") as temp_dir:
        directory = Path(temp_dir)
        for index, photo in enumerate(reversed(snap.photos)):
            try:
                path = await resilient(
                    client.download_media,
                    photo,
                    file=str(directory / f"dp{index}.jpg"),
                    label=f"download photo {index}",
                )
                if not path:
                    raise RuntimeError("nothing downloaded")
                handle = await resilient(
                    client.upload_file, path, label=f"upload photo {index}"
                )
                await resilient(
                    client,
                    functions.photos.UploadProfilePhotoRequest(file=handle),
                    label=f"set photo {index}",
                )
                copied += 1
            except Exception as exc:  # noqa: BLE001
                report.fail(f"profile photo {index + 1}", describe_reason(exc))

    if copied:
        note = f", {animated} were animated and became stills" if animated else ""
        report.ok("profile photos", f"{copied} of {len(snap.photos)}{note}")


async def apply_emoji_status(
    client: TelegramClient, snap: Snapshot, report: Report
) -> None:
    """Copy the emoji status, which needs Premium."""
    document_id = snap.emoji_document_id
    if document_id is None:
        report.skip("emoji status", "the old account has none")
        return
    try:
        await resilient(
            client,
            functions.account.UpdateEmojiStatusRequest(
                emoji_status=types.EmojiStatus(document_id=document_id)
            ),
            label="set emoji status",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("emoji status", describe_reason(exc))
        return
    if snap.collectible_status:
        report.ok(
            "emoji status",
            "base emoji only, a collectible cannot move between accounts",
        )
    else:
        report.ok("emoji status", str(document_id))


async def apply_colors(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the name colour and the profile colour, both Premium features."""
    for label, for_profile, colour in (
        ("name colour", False, snap.user.color),
        ("profile colour", True, snap.user.profile_color),
    ):
        if colour is None:
            report.skip(label, "the old account has none")
            continue
        try:
            await resilient(
                client,
                functions.account.UpdateColorRequest(
                    for_profile=for_profile,
                    color=types.PeerColor(
                        color=colour.color,
                        background_emoji_id=colour.background_emoji_id,
                    ),
                ),
                label=f"set {label}",
            )
        except Exception as exc:  # noqa: BLE001
            report.fail(label, describe_reason(exc))
            continue
        detail = f"palette {colour.color}"
        if colour.background_emoji_id:
            detail += f", background emoji {colour.background_emoji_id}"
        report.ok(label, detail)


async def apply_birthday(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the birthday."""
    if snap.birthday is None:
        report.skip("birthday", "not visible or not set")
        return
    try:
        await resilient(
            client,
            functions.account.UpdateBirthdayRequest(birthday=snap.birthday),
            label="set birthday",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("birthday", describe_reason(exc))
        return
    year = f"/{snap.birthday.year}" if snap.birthday.year else ""
    report.ok("birthday", f"{snap.birthday.day}/{snap.birthday.month}{year}")


async def apply_work_hours(
    client: TelegramClient, snap: Snapshot, report: Report
) -> None:
    """Copy the business opening hours."""
    if snap.work_hours is None:
        report.skip("business hours", "the old account has none")
        return
    try:
        await resilient(
            client,
            functions.account.UpdateBusinessWorkHoursRequest(
                business_work_hours=snap.work_hours
            ),
            label="set business hours",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("business hours", describe_reason(exc))
        return
    report.ok(
        "business hours",
        f"{len(snap.work_hours.weekly_open)} period(s), {snap.work_hours.timezone_id}",
    )


async def apply_location(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the business location."""
    if snap.location is None:
        report.skip("business location", "the old account has none")
        return
    geo = None
    point = snap.location.geo_point
    if isinstance(point, types.GeoPoint):
        geo = types.InputGeoPoint(
            lat=point.lat, long=point.long, accuracy_radius=point.accuracy_radius
        )
    try:
        await resilient(
            client,
            functions.account.UpdateBusinessLocationRequest(
                geo_point=geo, address=snap.location.address
            ),
            label="set business location",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("business location", describe_reason(exc))
        return
    report.ok("business location", snap.location.address)


async def apply_intro(client: TelegramClient, snap: Snapshot, report: Report) -> None:
    """Copy the business intro, including its sticker when there is one."""
    if snap.intro is None:
        report.skip("business intro", "the old account has none")
        return
    sticker = None
    if getattr(snap.intro, "sticker", None) is not None:
        with contextlib.suppress(Exception):
            sticker = utils.get_input_document(snap.intro.sticker)
    try:
        await resilient(
            client,
            functions.account.UpdateBusinessIntroRequest(
                intro=types.InputBusinessIntro(
                    title=snap.intro.title,
                    description=snap.intro.description,
                    sticker=sticker,
                )
            ),
            label="set business intro",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail("business intro", describe_reason(exc))
        return
    report.ok("business intro", snap.intro.title or "(no title)")


async def apply_personal_channel(
    client: TelegramClient, snap: Snapshot, report: Report
) -> None:
    """Point the profile at the same channel, when this account can see it."""
    channel_id = snap.personal_channel_id
    if channel_id is None:
        report.skip("profile channel", "the old account has none")
        return
    try:
        channel = await resilient(
            client.get_input_entity,
            types.PeerChannel(channel_id),
            label="resolve profile channel",
        )
        await resilient(
            client,
            functions.account.UpdatePersonalChannelRequest(channel=channel),
            label="set profile channel",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail(
            "profile channel",
            f"{describe_reason(exc)}; join it with this account and retry",
        )
        return
    report.ok("profile channel", str(channel_id))


# ---------------------------------------------------------------------------
# command surface
# ---------------------------------------------------------------------------
async def set_status(event: object, text: str) -> None:
    """Replace the command message with a progress line, ignoring failures."""
    with contextlib.suppress(Exception):
        await event.edit(text)


def preview(snap: Snapshot) -> str:
    """Describe what a real run would copy."""
    lines = [f"Would copy from {snap.name}:"]
    lines.append(f"  name: {utils.get_display_name(snap.user) or '(empty)'}")
    lines.append(f"  username: @{snap.username}" if snap.username else "  username: none")
    if snap.username:
        options = list(username_variants(snap.username))[:3]
        lines.append(f"  username tries: {', '.join('@' + o for o in options)}")
    lines.append(f"  bio: {len(snap.about or '')} chars")
    lines.append(f"  profile photos: {len(snap.photos)}")
    status = snap.emoji_document_id
    lines.append(f"  emoji status: {status if status else 'none'}")
    lines.append(f"  name colour: {'yes' if snap.user.color else 'none'}")
    lines.append(f"  profile colour: {'yes' if snap.user.profile_color else 'none'}")
    lines.append(f"  birthday: {'yes' if snap.birthday else 'none'}")
    lines.append(f"  business hours: {'yes' if snap.work_hours else 'none'}")
    lines.append(f"  business location: {'yes' if snap.location else 'none'}")
    lines.append(f"  business intro: {'yes' if snap.intro else 'none'}")
    lines.append(f"  profile channel: {snap.personal_channel_id or 'none'}")
    lines.append("Send .this to apply.")
    return "\n".join(lines)


async def resolve_target(
    client: TelegramClient, event: object, args: list[str]
) -> types.User:
    """Work out whose profile to copy: an argument, a reply, or this chat."""
    if args:
        entity = await client.get_entity(args[0])
    elif event.is_reply:
        replied = await event.get_reply_message()
        entity = await replied.get_sender()
    else:
        entity = await event.get_chat()

    if not isinstance(entity, types.User):
        raise RuntimeError("that is not a user account")
    if entity.is_self:
        raise RuntimeError("that is this account; point it at the old one")
    return entity


async def command_this(client: TelegramClient, event: object, args: list[str]) -> None:
    """Copy a profile onto this account, or preview it."""
    dry = bool(args) and args[0].lower() in {"dry", "preview"}
    if dry:
        args = args[1:]

    try:
        target = await resolve_target(client, event, args)
    except Exception as exc:  # noqa: BLE001
        await set_status(event, f"Cannot use that target: {describe_reason(exc)}")
        return

    await set_status(event, f"Reading {utils.get_display_name(target)}...")
    snap = await read_snapshot(client, target, photo_limit())
    LOG.info("Read profile of %s.", snap.name)

    if dry:
        await set_status(event, preview(snap))
        return

    if not snap.user.premium:
        LOG.warning("The old account is not Premium; colours and status may not apply.")

    await set_status(event, f"Copying {snap.name} onto this account...")
    report = Report()
    await apply_names(client, snap, report)
    await apply_username(client, snap, report)
    await apply_photos(client, snap, report)
    await apply_emoji_status(client, snap, report)
    await apply_colors(client, snap, report)
    await apply_birthday(client, snap, report)
    await apply_work_hours(client, snap, report)
    await apply_location(client, snap, report)
    await apply_intro(client, snap, report)
    await apply_personal_channel(client, snap, report)

    header = (
        f"Copied {snap.name}: {len(report.applied)} applied, "
        f"{len(report.failed)} failed, {len(report.skipped)} skipped"
    )
    LOG.info("%s", header)
    await set_status(event, report.render(header))


COMMAND_HELP = (
    "Commands\n"
    ".this          copy the profile of the user in this chat\n"
    ".this @handle  copy that user's profile\n"
    ".this dry      show what would be copied, change nothing\n"
    ".help          show this list"
)


async def run_commands(client: TelegramClient) -> None:
    """Listen for the dot commands until interrupted."""
    busy = asyncio.Lock()

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(\w+)(?:\s+([\s\S]*))?$"))
    async def dispatch(event: object) -> None:
        name = (event.pattern_match.group(1) or "").lower()
        raw = (event.pattern_match.group(2) or "").strip()
        if name == "help":
            await set_status(event, COMMAND_HELP)
            return
        if name != "this":
            return
        if busy.locked():
            await set_status(event, "Still working on the previous command.")
            return
        async with busy:
            try:
                await command_this(client, event, raw.split() if raw else [])
            except Exception as exc:  # noqa: BLE001
                LOG.error(".this failed: %s", exc)
                await set_status(event, f".this failed: {exc}")

    print(Fore.YELLOW + Style.BRIGHT + "\n== COMMAND MODE ==")
    print(Fore.WHITE + COMMAND_HELP)
    LOG.info("Open a chat with the old account and send .this. Ctrl+C to stop.")
    await client.run_until_disconnected()


async def main() -> None:
    """Sign in as the new account and start the listener."""
    setup_logging()
    show_banner()
    api_id, api_hash, session = read_credentials()
    LOG.info("Connecting as the new account (session %r)...", session)
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    me = await client.get_me()
    LOG.info(
        "Connected as %s%s.",
        utils.get_display_name(me) if me else "unknown",
        " (Premium)" if me and me.premium else " (not Premium)",
    )
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
