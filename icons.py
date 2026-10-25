"""Premium custom emoji for every status message, from the tgmacicons pack.

Pack: https://t.me/addemoji/tgmacicons ("TG iOS & macOS Icons", 138 emoji).
Every icon is monochrome and repaints to the text colour, so they read as one
consistent icon set. The plain emoji next to each ID is the fallback Telegram
shows wherever custom emoji cannot render; it is the pack's own and must stay.

Custom emoji only render when the sending account has Telegram Premium.

A status message comes out as:  <icon> <bold first line>
                                  <the rest, as plain text>
Links stay clickable: URLs get their own entities, because a message sent with
explicit entities is not auto-linked by Telegram.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from telethon import errors, types

# name: (fallback emoji, custom_emoji_id, pack #)
ICONS: dict[str, tuple[str, int]] = {
    "working": ("\u23f2", 5258258882022612173),   # 76  stopwatch
    "busy": ("\U0001f554", 5258419835922030550),  # 75  clock with arrow
    "done": ("\u2705", 5260416304224936047),      # 78  check in circle
    "fail": ("\u274c", 5260342697075416641),      # 28  cross in circle
    "warn": ("\u2757", 5258474669769497337),      # 69  exclamation
    "info": ("\u2139", 5258503720928288433),      # 86  info
    "help": ("\U0001f4d6", 5258328383183396223),  # 18  open book
    "search": ("\U0001f50e", 5429571366384842791),  # 126 magnifier
    "delete": ("\U0001f5d1", 5258130763148172425),  # 34  trash
    "remove": ("\u26d4", 5275969776668134187),    # 128 minus in circle
    "reset": ("\U0001f504", 5258420634785947640),  # 64  refresh
    "logout": ("\U0001f6aa", 5258084656674250503),  # 45  exit arrow
    "add_user": ("\U0001f464", 5258362837411045098),  # 4   person with plus
    "user": ("\U0001f464", 5258011929993026890),  # 54  person
    "users": ("\U0001f465", 5258513401784573443),  # 33  two people
    "robot": ("\U0001f916", 5258093637450866522),  # 19  robot
    "lock": ("\U0001f512", 5258476306152038031),  # 48  padlock
    "pin": ("\U0001f4cc", 5258461531464539536),   # 58  pushpin
    "link": ("\u26d3", 5260730055880876557),      # 29  chain link
    "copy": ("\U0001f4c4", 5258477770735885832),  # 31  two documents
    "channel": ("\U0001f4e3", 5260268501515377807),  # 26  megaphone
    "photo": ("\U0001f5bc", 5258254475386167466),  # 83  picture
    "edit": ("\u270d", 5258331647358540449),      # 35  pencil on square
    "settings": ("\u2699", 5258096772776991776),  # 107 gear
    "stop": ("\U0001f91a", 5260249440450520061),  # 72  raised hand
    "send": ("\u2708", 5258073068852485953),      # 1   paper plane
    "star": ("\u2b50", 5258165702707125574),      # 108 filled star
    "premium": ("\U0001f48e", 5298988357538328017),  # 138 gem
}

URL_RE = re.compile(r"(?:https?://|tg://)\S+|\bt\.me/\S+|(?<![\w@])@[A-Za-z]\w{3,31}")

# First matching rule wins; each is tested against the first line, lowercased.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("busy", ("still working",)),
    ("help", ("usage:", "auto maker", "commands")),
    ("fail", ("cannot", "could not", "couldn't", "failed", "no copy", "stopped",
              "is not a", "not a valid", "refused", "error", "expired")),
    ("warn", ("no worker", "no unused", "nothing", "there are no", "no demo",
              "no posts", "has no", "every worker is", "only the account")),
    ("delete", ("deleting", "deleted")),
    ("remove", ("removed", "removing")),
    ("reset", ("ready again", "reset")),
    ("search", ("reading", "scanning", "resolving", "checking", "looking")),
    ("working", ("...", "\u2026", "working", "creating", "copying", "joining")),
]


def pick_icon(text: str) -> str:
    """Choose an icon name from what a status message says."""
    head = (text.strip().splitlines() or [""])[0].lower()
    for name, needles in _RULES:
        if any(needle in head for needle in needles):
            return name
    return "done"


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def styled(text: str, icon: str | None = None) -> tuple[str, list[Any]]:
    """Return the text with its icon in front, and the entities to send with it."""
    emoji, emoji_id = ICONS.get(icon or pick_icon(text), ICONS["info"])
    full = f"{emoji} {text}"
    start = _utf16(emoji) + 1
    entities: list[Any] = [
        types.MessageEntityCustomEmoji(offset=0, length=_utf16(emoji), document_id=emoji_id)
    ]
    head = text.split("\n", 1)[0]
    if head.strip():
        entities.append(types.MessageEntityBold(offset=start, length=_utf16(head)))
    for found in URL_RE.finditer(text):
        token = found.group(0).rstrip(".,;:)!?'\"")
        offset = start + _utf16(text[: found.start()])
        if token.startswith("@"):
            entities.append(types.MessageEntityMention(offset=offset, length=_utf16(token)))
        else:
            entities.append(types.MessageEntityUrl(offset=offset, length=_utf16(token)))
    entities.sort(key=lambda e: (e.offset, -e.length))
    return full, entities


def styled_kwargs(text: str, icon: str | None = None) -> tuple[str, dict[str, Any]]:
    """styled(), packed as the arguments send_message and edit take."""
    full, entities = styled(text, icon)
    return full, {"formatting_entities": entities, "link_preview": False}


async def set_status(event: object, text: str, icon: str | None = None) -> None:
    """Show a command's progress with an icon and a bold headline, never raising.

    A command this account sent is edited in place. A command an admin sent
    from another account cannot be edited, since nobody may edit someone else's
    message, so the first status becomes a reply and later ones edit that reply.
    """
    full, kwargs = styled_kwargs(text, icon)
    with contextlib.suppress(Exception):  # Status must never break the command.
        if getattr(event, "out", True):
            with contextlib.suppress(errors.MessageNotModifiedError):
                await event.edit(full, **kwargs)
            return
        reply = getattr(event, "status_reply", None)
        if reply is None:
            event.status_reply = await event.reply(full, **kwargs)
            return
        with contextlib.suppress(errors.MessageNotModifiedError):
            await reply.edit(full, **kwargs)
