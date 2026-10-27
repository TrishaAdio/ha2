# Telethon Chat Tools

Five Telethon userbot scripts:

- **`main.py`** copies every photo from one chat to another and posts a linked index, runs a [command mode](#commands-mainpy-command-mode) with `.change` and `.clone`, and an [auto maker](#auto-maker-mainpy-mode-3) that copies a demo channel into fresh channels made by a pool of worker accounts. Channels, supergroups, forum groups, and legacy basic groups all work on both sides.
- **`automaker.py`** is `main.py` mode 3: the worker pool, the `.login` flow and `.gen`, which copies a demo channel into a fresh channel. It is not run directly.
- **`oho.py`** clones a whole source chat into many freshly created groups, using several backup accounts that share the work. See [oho.py](#ohopy-multi-account-group-cloner).
- **`reindex.py`** adds the styled caption index to groups that already hold the copied posts, which is what `oho.py` leaves behind. See [reindex.py](#reindexpy-add-the-index-to-finished-groups).
- **`lol.py`** deletes posts across your chats: `.delete <text>` removes every post containing a text in groups you moderate, and `.delme` removes every photo you sent, with its caption, everywhere. See [lol.py](#lolpy-delete-posts-by-text).
- **`iam.py`** copies one account's whole profile onto the signed-in account, for rebuilding your own identity after losing access to it. See [iam.py](#iampy-copy-a-profile-onto-this-account).
- **`redirect.py`** keeps named source channels mirrored into throwaway channels that are rebuilt on a timer, dropping the fresh invite links into a chat you assign. See [redirect.py](#redirectpy-rotating-channel-cloner). Run [`setup.py`](#setuppy-installer) once first.

  Related: [what the index lists](#what-the-index-lists), [the link post](#the-link-post), [where the index goes](#where-the-index-goes).

# main.py: Chat Photo Copier

A Telethon userbot that copies every photo from a selected source chat to a selected destination chat. Broadcast channels, supergroups, forum groups, and legacy basic groups are all supported on both sides.

## Features

- Interactive numbered source and destination selection covering channels, supergroups, broadcast groups, and basic groups
- Labels every choice with its chat type and public username so channels and groups are easy to tell apart
- Lists only destinations the account can actually post photos in, using per-chat-type permission rules
- Optional forum topic selection: read from one source topic and publish into one destination topic
- Colored Colorama terminal UI with an ASCII startup banner and status display
- Copies photos from oldest to newest
- Preserves Telegram photo albums/batches
- Preserves the exact caption text, emoji, and Telegram formatting entities
- Preserves spoiler state on each photo
- Downloads media to temporary storage and removes it after each batch
- Creates a final index using the first line of every non-empty caption
- Removes ordinary caption emoji from generated index titles only
- Appends ` | Demo` to every generated index title
- Sends the supplied sticker immediately before the final index
- Formats the index as a blockquote with the supplied custom emoji
- Makes every index title bold, underlined, and linked to its copied post
- Copies direct text replies to source photos with their exact text and formatting
- Keeps copied text messages attached as replies to the matching destination photos
- Supports public, private, and forum-topic post URLs
- Waits and retries automatically on flood limits and on group slow mode
- Falls back to individual posts when slow mode forbids albums
- Keeps going when the index sticker or Premium custom emoji is unavailable

## Group support notes

- Supergroups, broadcast groups, forum groups, and legacy basic groups appear alongside channels in both menus.
- Posting rights are resolved per chat type: broadcast channels and broadcast groups need the post-messages admin right, while groups honour your personal restrictions, your admin rights, and the group's default permissions.
- In a forum group you are asked which topic to use. Choosing a source topic copies only that topic's photos and replies; choosing a destination topic publishes the photos, the sticker, and the index inside it.
- Legacy basic groups have no per-message permalinks, so index titles stay bold and underlined but are not clickable. The run reports this at the end.
- Slow mode is handled: the script waits out each interval, and because Telegram rejects albums under slow mode, those batches are published one photo at a time.

## Modes

`main.py` starts by asking what to do:

1. **Copy photos between two chats** — the interactive copier described above.
2. **Command mode** — stays connected and listens for `.change` and `.clone`.
3. **Auto maker** — `.set` a demo channel, then every `.gen` has a worker account create a fresh channel that your account fills with a full copy of it. See [auto maker](#auto-maker-mainpy-mode-3).

Set `MAIN_MODE=copy`, `MAIN_MODE=commands` or `MAIN_MODE=auto` in `.env` to skip the question.

## Sending commands from another account

Modes 2 and 3 take commands from this account itself, and from **admins**: other accounts allowed to send the same commands to it, typically by private message.

```text
.allow            # sent by this account in an admin's DM: allows that person
.allow @someone   # or by @username / user id, from anywhere
.deny @someone    # takes it away
.admins           # lists them
```

- Only this account can run `.allow` and `.deny`, so an admin can never let more people in.
- Admins can also be listed in `.env` as `COMMAND_ADMINS=123456789,@someone`. Those are read-only; `.allow` adds to `command_admins.json`.
- A command this account sends is edited in place as before. An admin's command gets a reply, which is then edited with the progress, since nobody can edit someone else's message.
- Anyone not on the list is ignored without a reply.
- Run `.login` by **private message**. The code and password are deleted for both sides as soon as they are read. In a group that only works when this account may delete other people's messages, and is reported when it cannot.
- During an admin's `.login`, only that admin's messages are read as answers, so nobody else in the chat can answer it.

# Commands (`main.py` command mode)

In command mode the script listens for dot commands typed **from your own account** in any chat. Nobody else can trigger them. The command message itself is replaced with live progress, so chats stay clean.

```text
.change @newname            replace every username in every post
.change @oldname @newname   replace only that username
.clone [title]              clone this chat into a private channel
.help                       show the list
```

## `.change`

Run it in a channel to rewrite usernames across the whole history. With one argument every `@mention` becomes the new name; with two, only the named username is touched, which is the safer form.

What it rewrites:

- `@mentions` in message text and in media captions.
- Bare `t.me/username` links, keeping any `https://` scheme and trailing slash.
- Hyperlink targets that point at a bare `t.me/username`, even when the visible text needs no change.

What it deliberately leaves alone:

- Invite links (`t.me/+hash`, `t.me/joinchat/...`) and private post links (`t.me/c/...`).
- Public **post** links like `t.me/oldchan/45`, because rewriting the name would repoint a specific post at a different channel.
- Email addresses, and reserved `t.me` paths such as `addstickers` or `boost`.

**Formatting is preserved.** Replacing a username changes the text length, which shifts every entity after it. Offsets are recomputed in UTF-16 units, which is what Telegram counts — with emoji in a message those differ from Python string indices, so getting this wrong silently moves bold and links onto the wrong words. Telegram re-detects mentions, URLs and hashtags itself, so stale copies of those are dropped rather than remapped; real styling (bold, italic, underline, strike, spoiler, code, hyperlinks, custom emoji, blockquote) is remapped and kept.

Editing is rate limited, so the run paces itself and waits out flood limits. Posts it cannot edit are counted and reported rather than aborting the run: in channels an admin with edit rights can edit any post at any time, but in ordinary groups Telegram only allows editing your own messages for 48 hours.

## `.clone`

Copies a whole chat into a **fresh private channel** owned by you.

```text
.clone                        clone the chat you are in
.clone My Backup              clone this chat under a different title
.clone https://t.me/+AbCdEf   clone that chat, from anywhere
.clone @somechannel Archive   clone by handle, with a title
```

With no link it clones the current chat. A first argument that names a chat clones **that** chat instead, so you can run it from any chat — a saved-messages note to yourself, for instance. Anything after the link becomes the title; with no title the source name is reused.

Accepted references: private invite links (`t.me/+hash` and the older `t.me/joinchat/hash`), public links (`t.me/name`), `@handles`, and private post links (`t.me/c/<id>`). A plain word is treated as a title, not a chat, so `.clone My Backup` still works as before.

### Joining

Invite links are inspected before any join attempt:

- **Already a member** — used as it stands, no join, and it still clones. Joining twice would otherwise fail with `USER_ALREADY_PARTICIPANT`.
- **Not a member** — joined first, since the history has to be readable. The account stays in the chat afterwards.
- **Peek link** — some invites grant temporary read access; those clone without joining at all.
- **Approval required** — reported instead of silently producing an empty clone, because a pending join request cannot read history.

Public handles and links need no join, since public history is readable without membership.

Expired and invalid invites are reported and nothing is created.

- The clone gets the source's **profile photo** and **description**. The "channel photo updated" service message Telegram adds is deleted.
- **Every post is copied one by one, in the source's exact order**: text, long markdown posts, photos, albums, documents, stickers, captions, formatting entities and spoilers. Nothing is skipped or moved, including the source's own index post, which stays where it was.
- **Post numbering holds.** Every source post is numbered before anything is sent. A post Telegram refuses is retried without its premium emoji, then re-uploaded, so it keeps its place; one that still fails is reported by number instead of silently shifting every later post up.
- **Nothing is added.** The clone is an exact copy: source post 1 is clone post 1, post 2 is post 2, and so on, stickers included. No extra index or sticker is posted. The source's own index or markdown post is copied in its place, with its links repointed at the clone.
- **Bot messages are left out.** When `.clone` runs inside the source, its own command message ("Reading …") and the reports earlier runs left there are not copied.
- **Order check.** When copying finishes, the clone is read back and compared with the source post by post. The report says `Order check: all N post(s) match the source`, or names the post that differs.
- Replies are remapped, so a reply in the clone points at the copy of the message it originally answered.
- **Links between posts are repointed at the clone.** A post linking to `t.me/srcchan/45` ends up linking to the cloned copy of post 45 instead of back at the source. This covers both plain links in the text and markdown-style hyperlinks, where only the target changes and the visible text is left exactly as it was.
- Media is reused by file reference, so nothing is downloaded in the normal case. Expired references fall back to a download and re-upload.
- Media Telegram cannot resend, such as stories, is reported as a gap by its post number.
- The invite link is sent to your **Saved Messages** together with the copy counts, and also printed in the terminal.

### How the link repointing works

Repointing happens in a second pass, after all posts are copied. It has to: a post can link *forward* to a post further down the channel, which does not exist in the clone yet while copying is still in progress. Once every post has been created, the finished id map is used to rewrite the links.

Recognised and repointed:

- `t.me/srcchan/45` and `https://t.me/srcchan/45` for a public source, keeping the original scheme or lack of one.
- `t.me/c/<source id>/45` for a private source.
- Topic-style `t.me/srcchan/5/45`, where the message id is the last number.
- Query suffixes such as `?single` are preserved.
- Hyperlink targets, leaving the visible text untouched.

Left alone: links to other channels, invite links, and links to posts that were never cloned. Those keep pointing at the source and are counted as unresolved in the report, which is better than producing a dead link.

## Code size

`main.py` uses **1679 total lines**, of which **1412 are non-empty, non-comment lines**.

## Requirements

- Python 3.10 or newer
- Telethon 1.44 or newer
- A Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org)
- Permission to post in the destination chat
- A Telegram account allowed to send the supplied custom emoji (Telegram Premium may be required; without it the index is posted with the plain emoji instead)

## Installation

```bash
git clone https://github.com/TrishaAdio/ha2.git
cd ha2
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set your credentials in `.env`:

```env
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=channel_copier
```

## Usage

```bash
python main.py
```

On the first run the account has to be logged in. The terminal offers two ways:

- **Paste a session string** — press nothing else, just paste an existing session when asked. A normal Telethon `StringSession` works, and so does the bare `auth_key_hex:dc_id` form some tools export. This needs no login code, so it is the way to log in on a **VPS or server where Telegram never delivers the code** (Telegram often refuses to send login codes to datacentre IPs). The session is imported into the local session file, so later runs connect straight away.
- **Phone number and code** — press Enter at that prompt to log in the usual way, with your phone number, the login code, and your two-step verification password if enabled.

All of the scripts here (`main.py`, `oho.py`, `iam.py`, `lol.py`, `redirect.py`, `reindex.py`) share this login step, so a session string can be pasted for any of them.

After login, the script then:

1. Shows available channels and groups and asks for the source.
2. Asks which source topic to read when the source is a forum group.
3. Shows chats where your account can post photos and asks for the destination.
4. Asks which destination topic to write into when the destination is a forum group.
5. Copies all source photos and albums with their captions, formatting, emoji, and spoilers.
6. Copies direct source text replies and replies them to the matching copied photos.
7. Sends the supplied sticker.
8. Posts one or more styled index messages containing clickable first-caption lines.

Example index appearance:

```text
🟢 real choclate | Demo
🟢 real dustbin | Demo
🟢 brush for sell | Demo
🟢 nft selling | Demo
```

The entire list is a blockquote. Ordinary emoji found in source caption titles are excluded from this index, while the original copied captions remain unchanged. The green marker uses custom emoji ID `6298751564592973547`; each title and its ` | Demo` suffix are bold, underlined, and link directly to the copied destination post.

## Known limitations

- The index sticker is referenced by a fixed file reference. Telegram expires those over time, so the sticker may be skipped with a warning while the index itself is still posted.
- The forum topic pickers show the 100 most recent topics.
- Only plain-text replies are copied. Replies carrying their own media are skipped.

> The index described above is a `main.py` feature. `oho.py` copies content but does not post an index, so groups it creates start without one. Use [reindex.py](#reindexpy-add-the-index-to-finished-groups) to add it afterwards.

# Auto maker (`main.py` mode 3)

Mode 3 makes fresh copies of one **demo channel** on demand. Your signed-in (premium) account is the operator; a pool of **worker accounts** creates the channels.

```text
.set [link]            choose the demo channel (send it in the channel)
.gen [count]           make copies of the demo channel
.login [phone]         add a worker account (phone, code, 2FA)
.accounts              list the workers and the demo channel
.reset [name|all]      free spent and set-aside workers
.remove <name|aside|all>  forget workers and their sessions
.cancel                stop a login that is waiting
.allow / .deny / .admins   who else may send these commands
.help                  show the list
```

## Setting it up

```text
.login +15551234567    # add a worker: answer with the code, then the 2FA password
.login                 # repeat for as many workers as you want
.set                   # send this INSIDE the demo channel
.gen                   # one copy
.gen 3                 # three copies, three different workers
```

`.set` sent inside the demo channel is deleted from it straight away, and its reply goes to your Saved Messages, so nothing is ever left in the demo channel. `.set <link>` works from anywhere, which is how an admin sets it from a DM.

## What one `.gen` does

1. Picks a **random worker that has not been used yet**.
2. The worker creates an empty channel with the **same name and the same kind** (channel or group) as the demo channel.
3. Your account joins it by invite link and the worker promotes it to admin with **full rights**, including adding admins, so your premium account can fully manage the copy and promote anyone else without the worker. The worker also adds **@DemoSaviourBot** as an admin with full rights (set `AUTOMAKER_ADMIN_BOT` in `.env` to use another bot; left empty, it uses @DemoSaviourBot). If the bot can't be added, the copy still goes ahead and the report says why. The worker's job ends here.
4. **Your account** then copies the demo channel into it, exactly as `.clone` does:
   - profile photo and description
   - every post one by one in order, with its markdown, links, premium emoji, albums and spoilers
   - links between posts are pointed at the copy
   - the pinned post, and content protection when the demo channel has it
   - an order check that reads the copy back and compares it with the demo channel post by post
5. Your account exports the invite link. It comes back in the reply to `.gen`, and a full report goes to your Saved Messages. **Nothing is posted in the demo channel.**

Your account does the posting, so premium emoji come through. The worker spends its own channel-creation limit, so yours never runs out. The link is yours, so it keeps working whatever happens to the worker.

## When something goes wrong

- **Telegram refuses the worker** — a frozen account, a spam-limited one, a dead session, the channel limit reached, no admin rights handed over, or a flood wait over `AUTOMAKER_MAX_WAIT` (default 300s). The worker is set aside with the reason and **the next worker is tried**, however many it takes. The `.gen` reply lists every worker set aside during the run and why. `.accounts` shows the reasons later, `.reset all` retries them, and `.remove aside` forgets them.
- **Your account can't join or be made admin**: the empty channel is deleted again and `.gen` stops. Another worker would hit the same problem and spend its limit too, so it isn't tried. The worker stays unused.
- **A post fails during the copy**: the copy is kept and handed over. The report names the post by number, as with `.clone`.

A worker is marked used once its channel exists, because its limit is spent at that point whether or not every post copies.

## `.login`

`.login` asks for the phone number, then the login code, then the 2FA password if the account has one. Answer each with an ordinary message in the same chat. `.cancel`, or 5 minutes of silence, stops it.

- The code and password messages are deleted as soon as they are read, and never printed or logged.
- Type the code by hand, for example `1 2 3 4 5`. Telegram invalidates a login code it sees forwarded through a chat.
- A wrong code or password can be retried three times.
- A number with no Telegram account is refused; sign it up in a Telegram app first.
- Re-adding an account that is already stored refreshes its session instead of adding it twice.

## The state file

The workers, their sessions and the demo channel live in `automaker_state.json` (`AUTOMAKER_STATE` moves it). It is written atomically and readable only by you, because **a stored session is full access to that account**. It is in `.gitignore`. `.remove` drops a worker from the file but does not log the account out; do that from its own Telegram app too.

## Limits worth knowing

- Telegram caps how many channels an account may create per day, and new accounts are limited hardest. That is what the pool is for.
- Your account joins every copy, and Telegram caps how many channels one account can be in (500, or 1000 with Premium). `.gen` reports it plainly when you reach that.
- The worker stays the owner of each copy; your account is an admin with full rights (everything except ownership itself). Transferring ownership needs 2FA on the worker and a session several days old, so it is not done.


# oho.py: Multi-Account Group Cloner

Creates many groups across several accounts and clones an entire source chat into each one.

## What it does per group

The requested group total is divided between the backup accounts, so 6 groups over 3 accounts means 2 groups each. Every group then goes through the same nine stages:

1. The owner account creates a supergroup. With no public link it starts **private**.
2. The main account joins through a single-use invite link.
3. The owner promotes the main account to **anonymous admin**, so its posts are signed by the group instead of the account.
4. The main account copies **every message** from the source: text, photos, albums, videos, documents, stickers, captions, formatting entities, spoilers, and reply threading.
5. The main account leaves the group.
6. The owner deletes every service message, so the create, join, and leave notices leave no trace.
7. The owner promotes itself to **anonymous admin**.
8. The owner claims a public link, which makes the group **public**, then enables **join approval**.
9. The finished link is sent to the managing admin, `@siyorou` by default.

Every stage is logged with timing to the terminal in color and to `oho.log` in full. A per-group summary is printed at the end, and a group that fails records the stage it stopped at without stopping the rest of the run.

## Ordering that Telegram forces

- `channels.toggleJoinRequest` fails with `CHAT_PUBLIC_REQUIRED` unless the group already has a public link, so the username is always claimed first and join approval second.
- The link is shared after the group is public, so `@siyorou` receives the final public link rather than a private invite link.
- Telegram [clears every privilege when an owner edits its own admin entry](https://bugs.telegram.org/c/2599), so the owner's full rights set is always resent together with the anonymous flag.
- Anonymous admins are hidden from the member list, so the main account is located and promoted before it becomes anonymous.
- Per Telegram's [rights documentation](https://core.telegram.org/api/rights), enabling `anonymous` automatically sets the admin's `send_as` to the group, so no explicit sender needs to be passed when posting.

## Accounts

`oho.py` uses one main account and one to three backup accounts:

- `MAIN_SESSION` reads the source and posts into every group.
- `OWNER_SESSIONS` lists the backup accounts that own the groups. Leave it unset to be asked how many to use.

Each session logs in interactively on first use, asking for the phone number, login code, and two-step password. Sessions are reused afterwards. The script refuses to run if two sessions turn out to be the same account.

## Usage

```bash
python oho.py
```

You are asked for the group total, a title base, and a public-link prefix, then shown the full plan for confirmation before anything is created. Nothing is created until you confirm.

Titles become `Archive 1`, `Archive 2`, and so on; links become `t.me/archive1`, `t.me/archive2`, and so on. Taken links automatically fall back to a suffixed candidate.

## Reliability

- Flood waits, premium flood waits, and group slow-mode waits are waited out; transient server errors are retried with a growing delay.
- Media is resent by file reference, so nothing is downloaded in the normal case. Expired references are refreshed from the source, then re-uploaded as a last resort.
- Albums rejected under slow mode are sent one photo at a time.
- Media Telegram cannot resend, such as stories and giveaways, is skipped and counted rather than failing the group.
- If the owner cannot message the sharing admin, the main account tries instead, and the link is always logged either way.

## Pacing

`MESSAGE_DELAY` (default `1.0`) and `GROUP_DELAY` (default `15.0`) throttle the run. Raise them for large sources or fresh accounts. `COPY_LIMIT` caps the messages per group and is useful for a trial run.

## Code size

`oho.py` uses **1256 total lines**, of which **1042 are non-empty, non-comment lines**.

## Limitations

- Creating many groups or claiming many public links from one account can hit Telegram's own limits, such as `CHANNELS_ADMIN_PUBLIC_TOO_MUCH`. The failing group reports the error and the run continues.
- A source with content protection enabled may refuse to hand over its media. The run warns up front and reports whatever it has to skip.
- Fresh or spam-limited accounts may be unable to create groups or message the sharing admin at all.

# reindex.py: Add the Index to Finished Groups

`oho.py` copies posts but never posts an index, so groups it built have all the content and no index. `reindex.py` fixes that after the fact. It reads the posts already sitting in each group, builds the index from their captions, and posts it with links that point at those copies.

## Preview first

The default run posts nothing. It scans every matching group and prints what it would do, including the resolved link for each entry:

```text
  Demos 1 (t.me/XCRYPTO1)
    5 media post(s), 4 index entr(y/ies), 1 message(s) to post
    🟢 real choclate | Demo  ->  https://t.me/XCRYPTO1/10
    🟢 real dustbin | Demo   ->  https://t.me/XCRYPTO1/12
```

Open one of those links to confirm it lands on the right post, then answer `y` to post. Answering anything else exits without touching the groups.

## Links

Links use the group's public link when it has one, so `https://t.me/XCRYPTO1/10` rather than the internal `https://t.me/c/<numeric id>/10` form that only resolves for members. Groups still private fall back to the `t.me/c/` form.

For an album, the entry links to the item that actually carries the caption, not the first item.

## Which groups

Groups are selected by prefix, and only groups the signed-in account created are considered:

- **Group title starts with** — matches `Demos 1`, `Demos 2`, and so on.
- **Public link starts with** — matches `t.me/XCRYPTO1`, `t.me/XCRYPTO2`, and so on.

Either match is enough. Anything else the account owns is left alone.

## Re-running

Re-running replaces the previous index rather than stacking another copy underneath. The old index sticker and index messages are detected (no media, wrapped in a blockquote, carrying the marker emoji and the title suffix), deleted, then the fresh index is posted. Old index messages are never mistaken for content.

## What gets indexed

Only posts carrying media with a non-empty caption. Plain text posts are not captions, so they are skipped, as are captions that reduce to nothing once emoji are removed. The title is the first caption line that still has text after emoji are stripped.

## Usage

```bash
python reindex.py
```

Credentials and `OWNER_SESSIONS` are read from `.env`, the same values `oho.py` uses. The accounts must be the ones that own the groups, since the main account has already left them.

# lol.py: Delete Posts by Text

Deletes every post containing a given text across **every group and channel where you can delete other people's messages**. It runs as a live listener and takes dot commands typed from your own account in any chat.

```text
.delete <text>   find every post with that text in chats you moderate
.delme           find every photo you sent, with its caption, everywhere
.confirm         delete what the last .delete or .delme found
.cancel          forget the last scan
.help            show the commands
```

Both `.delete` and `.delme` only find and preview; nothing is removed until you send `.confirm`. The pending result expires after five minutes, so a stale `.confirm` cannot fire an old scan by accident.

## `.delete <text>`

Send `.delete @HJGFDS` and it:

1. Lists every chat you are in where you can delete anyone's messages — that means chats you own or where you hold the delete-messages admin right. Chats where you are only a member are skipped, since a member can only delete their own messages.
2. Searches each one for posts whose text or caption contains `@HJGFDS`. Posts **with media and without** are both matched, because a caption lives in the same place as plain message text.
3. Reports how many posts it found in how many chats, and **deletes nothing yet**.

Matching is a case-insensitive substring, confirmed locally, so `@HJGFDS` and `@hjgfds` both hit. Telegram's own search narrows each chat's history first, then every candidate is re-checked locally so only real matches are deleted.

## `.delme`

Send `.delme` and it finds **every photo this account has sent**, along with its caption, across every chat you are in — groups, channels and private chats alike. A photo and its caption are one message, so deleting it removes both.

- It matches only your own photos. Other people's photos, your text posts and your file/document uploads are left alone.
- No admin right is needed, because you are deleting your own messages. That includes private chats, where the photo is removed for both sides.
- Like `.delete`, it previews first and deletes nothing until `.confirm`.

## Deletion

Both commands delete with revoke, so posts are removed for everyone, not just hidden from you.

## Speed

Chats are scanned several at a time, since each scan is mostly waiting on Telegram. `SCAN_CONCURRENCY` in `.env` sets how many run at once (default 8). Raise it to sweep a large account faster, lower it if you hit flood limits.

## Chats that reject a filtered search

Some chats reject the server-side search that carries a media filter (`INPUT_FILTER_INVALID`). When that happens the scan retries that chat with a plain history scan filtered on the client, so it is still swept rather than skipped. The whole pass is buffered per attempt, so a query that fails part way through never leaves a partial result behind. `.delme` filters photos server-side and checks the sender on the client, which avoids the sender-plus-filter combination that triggers this in the first place.

## Usage

```bash
python lol.py
```

Credentials come from `.env` (`TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`), the same values the other scripts use.

## Caution

`.confirm` deletes for everyone and cannot be undone. The preview names the count and the chats first for exactly this reason — read it before confirming. `.delme` in particular sweeps every chat you are in, so check the preview before you confirm it. A short `.delete` search (one character) is refused so an over-broad sweep is harder to trigger by mistake.

# iam.py: Copy a Profile onto This Account

Rebuilds an account's profile on a different account. The intended use is recovering your own identity after being logged out of an account you can no longer sign into: run the userbot as the **new** account, open a chat with the **old** one, and send `.this`.

```text
.this          copy the profile of the user in this chat
.this @handle  copy that user's profile
.this dry      show what would be copied, change nothing
.help          show the commands
```

The target is the user whose chat you are in, or a reply's sender, or an explicit `@handle`. Run `.this dry` first to see what was found without touching anything.

## What it copies

| Field | Notes |
|---|---|
| First and last name | |
| Bio | Trimmed to whatever limit **this** account has, see below |
| Username | Incremented when the old one is still taken, see below |
| Profile photos | All of them, oldest uploaded first so the newest ends up current |
| Emoji status | Premium |
| Name colour and background emoji | Premium |
| Profile colour and background emoji | Premium |
| Birthday | Only if the old account shows it to you |
| Business hours | Timezone and every weekly period |
| Business location | Address and coordinates |
| Business intro | Title, description and sticker |
| Channel on the profile | Only if the new account can see that channel |

## Usernames

The exact username is tried first, in case the old account has since released it. Otherwise the trailing number is incremented: `name2` becomes `name3`, `karn9` becomes `karn10`, and a name with no number gains a `2`. Candidates outside Telegram's 5 to 32 character range are skipped, and each is checked for availability before being claimed. The report says which one it settled on and why.

## The bio limit

Telegram allows a longer bio on Premium accounts, and the exact numbers are served in its live app config rather than being fixed. So the limit is read at runtime from `about_length_limit_premium` or `about_length_limit_default`, whichever applies to the account being written to — not the account being copied. Whatever Telegram currently says is what gets used.

If the config cannot be read, the documented defaults of 140 for Premium and 70 otherwise are used, and the log says so. If Telegram still rejects the bio as too long, it is shrunk and retried, so a wrong limit cannot cost you the name as well.

A Premium bio copied onto a non-Premium account is therefore trimmed, and the report says exactly that: `bio: 70 of 120 chars, trimmed to this account's 70 limit`.

## Each field is independent

Fields are applied one at a time and a failure never stops the rest. If the new account is not Premium, the emoji status and colours fail with "needs Telegram Premium" while the name, bio, username, photos, birthday and business fields still apply. The final report lists each field as applied, failed with a reason, or skipped because the old account had nothing to copy.

## Limitations

- **Animated profile photos become stills.** The frame is copied, not the video. The report says how many were affected so you can re-upload those manually.
- **A collectible (NFT) emoji status cannot move between accounts.** The underlying base emoji is set instead, and the report says so.
- **The profile channel** must be visible to the new account. Join it first, then re-run.
- Anything the old account hides from you by privacy, such as a birthday, cannot be read and is reported as skipped.

## Usage

```bash
python iam.py
```

`TELEGRAM_API_ID` and `TELEGRAM_API_HASH` come from `.env`. The session is `IAM_SESSION` (default `iam_new_account`), kept separate so signing in as the new account does not disturb the session the other scripts use. `PHOTO_LIMIT` caps how many profile photos are copied (default 10).

This writes over the signed-in account's own profile, which is the point, but there is no undo. `.this dry` shows the whole plan first.

# redirect.py: Rotating Channel Cloner

Registers source channels under short names, clones each one into a brand new private channel, publishes the invite links in a chat you assign, and after a set number of minutes deletes every post, deletes the channel, and builds it all again with fresh links.

## Setup and first run

```bash
python3 setup.py          # creates .venv, installs deps, asks for credentials
source .venv/bin/activate
python redirect.py
```

Then, from the same account, send these as ordinary messages:

```text
.setchannel OK            # in the channel you want cloned
.assign OK                # in the channel where the links should appear
.interval 30
.test
.start
```

`.setchannel` and `.assign` act on the chat you send them in. Either one also takes a link, invite or `@handle` as its last argument, so `.setchannel VIP @somechannel` works from anywhere.

## Commands

| Command | Effect |
| --- | --- |
| `.setchannel NAME [link]` | Register a source channel under `NAME` (also `.setsource`) |
| `.assign NAME [link]` | Publish `NAME`'s links in this chat (`.assing` works too) |
| `.unset NAME` | Forget a source |
| `.unassign NAME` | Forget where `NAME`'s links go |
| `.interval MINUTES` | How long a clone stays up; 10 is the floor |
| `.clones N` | Clone channels per source per cycle, so links per cycle |
| `.links N` | Times each invite link is repeated in the link post (default 5) |
| `.filter WORD` / `.filter off` | Index only media posts with `WORD` in the caption (default `Dm`); everything is still cloned |
| `.start` / `.stop` | Begin rotating / stop and wipe what is published |
| `.rotate` | Wipe and rebuild immediately |
| `.test [NAME] [POSTS]` | Rehearse a whole cycle on a few posts, then undo it |
| `.status` | Configuration, live clones and time until the next rotation |
| `.help` | The list above |

## What one cycle does

1. Creates a fresh private channel per registered source.
2. Copies every post into it: text, media, albums, spoilers, replies, web previews and premium custom emoji are all preserved. Nothing is downloaded, so no "Forwarded from" header appears and the upload is instant.
3. Posts the styled linked index **where the source keeps its own index**, not always at the end.
4. Copies the source's profile photo onto the clone, and deletes the "channel photo changed" service message Telegram adds.
5. Exports an invite link and publishes the links in the assigned chats.
6. Waits out the interval, then deletes every post, deletes the channels, removes the link post, and starts over.

## What the index lists

**Every post is cloned** — media, plain text, and the text replies hanging off a media post. The filter never changes that.

What it changes is the index: by default only posts that carry media **and** mention `Dm` in their caption get a line, because those are the ones the links are selling. Everything else is still copied into the clone, just not listed. A caption is matched on a word boundary, so `Dm`, `dm` and `DM` all count while `admin` does not, and an album is listed when any of its parts carries the word.

`.filter off` lists every post instead, and `.filter <word>` uses a different word.

## The link post

Each clone's invite link is repeated `.links N` times, one per line, all inside a single blockquote, with the marker emoji on the first line:

```text
🟢 https://t.me/+1sj8IghHGBoxNTA1
https://t.me/+1sj8IghHGBoxNTA1
https://t.me/+1sj8IghHGBoxNTA1
https://t.me/+1sj8IghHGBoxNTA1
https://t.me/+1sj8IghHGBoxNTA1
```

The URLs are plain text, so Telegram links them itself and they stay readable. Two registered sources sharing an assigned chat produce one post holding both blocks.

## Where the index goes

The source's own index is not copied, because it links back to the source. Its **position** is copied: if the source keeps its index three posts from the end, the clone posts its index in the same place, then keeps going with the remaining posts.

That needs the index to exist before the posts it links to, so the lines for later posts go out unlinked and are filled in by editing once every post exists. The message layout is measured as though every line were already linked, so the groups computed up front still fit afterwards and no index message has to move. If the source has no index of its own, the clone's goes at the end.

Everything worth keeping lives in `redirect_state.json`, so a restart resumes and a crash cleans up the channels the previous run left behind.

## `.test` first

`.test` runs the real pipeline at small scale and then removes all of it, which is the fastest way to confirm an account can actually do the job before committing to a rotation:

```text
Test: OK
source      My Channel, 3 post(s) sampled
channel     created as My Channel
photo       copied
clone       3/3 post(s), no gaps
order       3/3 in order
index       1 message(s) after post 2, as in the source, 2/3 post(s) listed (caption has 'Dm')
invite      https://t.me/+AbCdEf...
links       published in Drops, repeated 5x
cleanup     test channels and links removed
Everything worked.
```

`order` is the interesting line: it re-reads the finished clone and checks that its Nth post really is the source's Nth post, by caption and album size. `.test OK 10` samples ten posts from one slot. Test channels are tracked separately from the rotation, so a failed test never disturbs a running cycle and its leftovers are cleaned up on the next start.

## Post numbering, and why this is not `main.py`'s `.clone`

`main.py`'s `.clone` now numbers posts up front, retries refused posts, reports gaps by number, and copies every post in place without adding any, the source's own index included. `redirect.py` goes further: it rotates channels on a timer, copies the pin and content protection, handles dividers, and lists every post in the index (with an optional caption filter) rather than only media posts with a caption title.

`redirect.py` treats the post number as a real value:

- Every source post is collected and numbered **before** anything is sent, so a later failure cannot renumber an earlier post.
- A post Telegram refuses is retried without its premium emoji, then re-uploaded, then re-uploaded without premium emoji. A caption costs a post its emoji, not its place.
- A post that still cannot be recreated is recorded as an explicit gap and logged. It never silently pulls the following posts up by one.
- The index emits exactly one line per source post, in post order, so index line N is source post N even when the post is plain text or its caption yields no title. A gapped post keeps its line, without a link.
- Album parts are sorted by message id, and replies are remapped only when Telegram returns as many messages as were sent, instead of zipping two lists of different lengths.
- Index detection requires every line to be an index line, so a genuine post that happens to be a quote is not mistaken for an old index and deleted.

## Notes

- The interval floor is 10 minutes. Creating a channel, uploading a clone and exporting an invite already costs minutes, and Telegram starts answering channel creation with flood waits below that.
- Premium custom emoji need a Premium account to *send*. Without one, posts and the index are published with the fallback emoji and the run reports how many were affected.
- Clone channels are created by the signed-in account, which is what allows the script to delete them again.
- The profile photo is copied as a still image. An animated channel photo becomes its cover frame.
- Telegram limits how many channels an account can create per day. A short interval with several sources reaches that limit quickly; the log shows the flood wait when it does.

# setup.py: Installer

`setup.py` here is an interactive installer, not a packaging script. It creates `.venv`, installs `requirements.txt`, verifies the dependencies import, and writes the settings to `.env`:

```bash
python3 setup.py
```

Re-running it is safe: every prompt offers the current value, so pressing Enter keeps it. Only the keys it owns are touched, so the settings the other scripts read from the same `.env` survive, comments included. It refuses to run when invoked by `pip install .` or a setuptools command.

# Security

Never commit `.env` or Telethon `.session` files. Both are excluded by `.gitignore`, along with `.venv/`, `redirect_state.json` and `*.log`.

## Status icons (`icons.py`)

Every status message from every script starts with an icon from the
[tgmacicons](https://t.me/addemoji/tgmacicons) custom emoji pack, followed by a
bold first line. The icon is chosen from the message (stopwatch while working,
check when done, cross on failure, and so on), or named explicitly with
`set_status(event, text, "icon")`. The table of names and emoji IDs is in
`icons.py`. Custom emoji only render when the account has Telegram Premium;
otherwise the plain fallback emoji shows.

## Auto delete for quick replies (`autodelete.py`)

Runs inside `main.py`'s command mode and auto maker. Every message one of your
Telegram Business quick replies sends (`/category`, `/foreign`, long texts with
links, zips, photos, albums) is deleted for both sides 60 to 120 minutes later.
A typed trigger such as `/add` is deleted 30 minutes later (`.autodel addtime`),
along with everything you send in that chat for the next 3 minutes
(`AUTODELETE_WINDOW`, seconds). Pending
deletions live in `autodelete_state.json` and survive a restart.

`.autodel` shows the status; `.autodel on|off`, `.autodel words category foreign add`
(or `all`), `.autodel time 60 120`, `.autodel now` and `.autodel clear` change it.
