#!/usr/bin/env python3
"""Interactive installer: build the virtualenv and write the .env.

Run this once with any Python 3.10 or newer, before running redirect.py:

    python3 setup.py

It creates .venv, installs requirements.txt into it, asks for the Telegram
credentials and the rotation settings, and saves them to .env. Running it again
is safe: every prompt offers the current value, so pressing Enter keeps it.

Only the standard library is used here, because nothing is installed yet.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import subprocess
import sys
import venv
from pathlib import Path

MIN_PYTHON = (3, 10)
MIN_INTERVAL_MINUTES = 10
DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_LINK_REPEAT = 5
DEFAULT_CAPTION_FILTER = "Dm"

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
ENV_FILE = ROOT / ".env"
REQUIREMENTS = ROOT / "requirements.txt"

# Keys this installer owns. Anything else already in .env is left untouched,
# because the other scripts in this repo read the same file.
MANAGED_HEADER = "# redirect.py settings, written by setup.py"

SETUPTOOLS_COMMANDS = {
    "bdist_wheel", "build", "develop", "dist_info", "egg_info", "install",
    "sdist", "--version", "--help-commands",
}


# ---------------------------------------------------------------------------
# terminal output
# ---------------------------------------------------------------------------
COLOR = sys.stdout.isatty() and os.name != "nt"


def paint(code: str, text: str) -> str:
    """Wrap text in an ANSI color when the terminal can show one."""
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def info(message: str) -> None:
    """Print an informational line."""
    print(paint("36", f"[i] {message}"))


def success(message: str) -> None:
    """Print a success line."""
    print(paint("32", f"[+] {message}"))


def warning(message: str) -> None:
    """Print a warning line."""
    print(paint("33", f"[!] {message}"))


def failure(message: str) -> None:
    """Print a failure line."""
    print(paint("31", f"[-] {message}"))


def heading(text: str) -> None:
    """Print a section heading."""
    print(paint("1;33", f"\n== {text.upper()} =="))


BANNER = r"""
   ____           ___              __
  / __ \___  ____/ (_)_______  ____/ /_
 / /_/ / _ \/ __  / / ___/ _ \/ ___/ __/
/ ____/  __/ /_/ / / /  /  __/ /__/ /_
\/    \___/\__,_/_/_/   \___/\___/\__/
"""


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
def ask(label: str, current: str = "", secret: bool = False) -> str:
    """Ask for a value, offering the current one as the default.

    A secret is never echoed back; the prompt only says whether one is stored.
    """
    if secret:
        hint = " [kept]" if current else ""
        while True:
            entered = getpass.getpass(paint("36", f"{label}{hint}: ")).strip()
            if entered:
                return entered
            if current:
                return current
            failure("This value cannot be empty.")
    hint = f" [{current}]" if current else ""
    while True:
        entered = input(paint("36", f"{label}{hint}: ")).strip()
        if entered:
            return entered
        if current:
            return current
        failure("This value cannot be empty.")


def ask_int(label: str, current: str, minimum: int, note: str = "") -> str:
    """Ask for a whole number no smaller than a floor."""
    while True:
        raw = ask(label, current)
        try:
            value = int(raw)
        except ValueError:
            failure("Enter a whole number.")
            continue
        if value < minimum:
            failure(note or f"The smallest accepted value is {minimum}.")
            continue
        return str(value)


def ask_yes_no(label: str, current: bool) -> str:
    """Ask a yes or no question and return it as an .env flag."""
    default = "yes" if current else "no"
    while True:
        raw = ask(f"{label} (yes/no)", default).lower()
        if raw in {"y", "yes", "true", "1", "on"}:
            return "true"
        if raw in {"n", "no", "false", "0", "off"}:
            return "false"
        failure("Answer yes or no.")


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def env_value(value: str) -> str:
    """Render a value so python-dotenv reads back exactly what was meant.

    An unquoted value loses its surrounding spaces, which matters: the default
    index suffix is " | Demo" and stripping its leading space would render
    titles as "Title| Demo".
    """
    plain = (
        value
        and value == value.strip()
        and not any(character in value for character in '#\'"\\\n')
    )
    if plain:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_env_value(raw: str) -> str:
    """Undo env_value, so a rewrite offers the real current value."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def read_env(path: Path) -> dict[str, str]:
    """Read the existing .env into a plain mapping, ignoring comments."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if match:
            values[match.group(1)] = parse_env_value(match.group(2))
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Update the managed keys in .env, leaving every other line as it was.

    The other scripts in this repo read the same file, so their settings and
    the comments around them have to survive.
    """
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    for position, line in enumerate(lines):
        match = ENV_LINE_RE.match(line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[position] = f"{key}={env_value(remaining.pop(key))}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        if MANAGED_HEADER not in lines:
            lines.append(MANAGED_HEADER)
        for key, value in remaining.items():
            lines.append(f"{key}={env_value(value)}")

    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# virtualenv
# ---------------------------------------------------------------------------
def venv_python(directory: Path) -> Path:
    """Return the interpreter inside a virtualenv."""
    if os.name == "nt":
        return directory / "Scripts" / "python.exe"
    return directory / "bin" / "python"


def create_venv(directory: Path) -> Path:
    """Create the virtualenv unless a usable one is already there."""
    interpreter = venv_python(directory)
    if interpreter.exists():
        info(f"Reusing the virtualenv in {directory.name}.")
        return interpreter

    info(f"Creating a virtualenv in {directory.name}...")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(directory)
    if not interpreter.exists():
        raise RuntimeError(f"the virtualenv has no interpreter at {interpreter}")
    success(f"Virtualenv ready at {directory}.")
    return interpreter


def run(command: list[str], what: str) -> None:
    """Run a subprocess, reporting a readable error when it fails."""
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{what} failed with exit code {completed.returncode}")


def install_requirements(interpreter: Path) -> None:
    """Install requirements.txt into the virtualenv."""
    if not REQUIREMENTS.exists():
        raise RuntimeError(f"{REQUIREMENTS.name} is missing")
    info("Upgrading pip...")
    run([str(interpreter), "-m", "pip", "install", "--upgrade", "pip"], "pip upgrade")
    info(f"Installing {REQUIREMENTS.name}...")
    run(
        [str(interpreter), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        "dependency installation",
    )
    success("Dependencies installed.")


def verify_install(interpreter: Path) -> None:
    """Import the dependencies once so a broken install is caught here."""
    check = (
        "import telethon, colorama, dotenv; "
        "print('telethon', telethon.__version__)"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", check], check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "the installed dependencies could not be imported:\n"
            f"{completed.stderr.strip()}"
        )
    success(f"Verified: {completed.stdout.strip()}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def refuse_setuptools_use() -> None:
    """Explain the mistake when pip or setuptools invokes this file.

    The name setup.py is loaded with meaning: `pip install .` would otherwise
    run an interactive installer in the middle of a build.
    """
    if any(argument in SETUPTOOLS_COMMANDS for argument in sys.argv[1:]):
        failure("This setup.py is an interactive installer, not a packaging script.")
        info("Run it directly:  python3 setup.py")
        raise SystemExit(2)


def check_python() -> None:
    """Refuse a Python too old for the syntax redirect.py uses."""
    if sys.version_info < MIN_PYTHON:
        needed = ".".join(str(part) for part in MIN_PYTHON)
        current = platform.python_version()
        raise RuntimeError(f"Python {needed} or newer is required; this is {current}")


def collect_settings(existing: dict[str, str]) -> dict[str, str]:
    """Ask for every setting, offering whatever .env already holds."""
    heading("telegram credentials")
    info("Create an API ID and hash at https://my.telegram.org > API development tools.")
    api_id = ask_int("API ID", existing.get("TELEGRAM_API_ID", ""), 1,
                     "The API ID is a positive number.")
    api_hash = ask("API hash", existing.get("TELEGRAM_API_HASH", ""), secret=True)

    heading("rotation")
    session = ask("Session name", existing.get("REDIRECT_SESSION", "redirect"))
    interval = ask_int(
        "Minutes a clone stays up",
        existing.get("REDIRECT_INTERVAL", str(DEFAULT_INTERVAL_MINUTES)),
        MIN_INTERVAL_MINUTES,
        f"The shortest interval is {MIN_INTERVAL_MINUTES} minutes.",
    )
    clones = ask_int(
        "Clones per source each cycle", existing.get("REDIRECT_CLONES", "1"), 1
    )
    info("Each invite link is repeated this many times, one per line, in a quote.")
    repeat = ask_int(
        "Link lines per clone",
        existing.get("REDIRECT_LINK_REPEAT", str(DEFAULT_LINK_REPEAT)),
        1,
    )

    heading("what the index lists")
    info("Every post is cloned. This only decides which ones the index lists.")
    info("Only media posts whose caption mentions this word get a line.")
    info("'-' lists every post instead.")
    caption_filter = ask(
        "Caption must mention", existing.get("REDIRECT_FILTER", DEFAULT_CAPTION_FILTER)
    )
    if caption_filter == "-":
        caption_filter = ""

    current_suffix = existing.get("INDEX_SUFFIX", " | Demo")
    info("The index suffix is appended to every index title. '-' means none.")
    suffix = ask("Index title suffix", current_suffix)
    if suffix == "-":
        suffix = ""

    autostart = ask_yes_no(
        "Start rotating as soon as redirect.py runs",
        (existing.get("REDIRECT_AUTOSTART", "") or "").lower()
        in {"1", "true", "yes", "on"},
    )

    return {
        "TELEGRAM_API_ID": api_id,
        "TELEGRAM_API_HASH": api_hash,
        "REDIRECT_SESSION": session,
        "REDIRECT_INTERVAL": interval,
        "REDIRECT_CLONES": clones,
        "REDIRECT_LINK_REPEAT": repeat,
        "REDIRECT_FILTER": caption_filter,
        "INDEX_SUFFIX": suffix,
        "REDIRECT_AUTOSTART": autostart,
    }


def report_next_steps(interpreter: Path, settings: dict[str, str]) -> None:
    """Print how to run the userbot and what to type once it is up."""
    heading("done")
    success(f"Settings saved to {ENV_FILE.name}.")
    activate = (
        ".venv\\Scripts\\activate" if os.name == "nt" else "source .venv/bin/activate"
    )
    print()
    info("Start the userbot:")
    print(paint("37", f"    {activate}"))
    print(paint("37", "    python redirect.py"))
    print(paint("37", f"    (or without activating: {interpreter} redirect.py)"))
    print()
    info("Telegram will ask for your phone number and login code on the first run.")
    print()
    info("Then, from that same account, in the channel you want to clone:")
    print(paint("37", "    .setchannel OK"))
    info("In the channel where the links should appear:")
    print(paint("37", "    .assign OK"))
    info("And start it:")
    print(paint("37", f"    .interval {settings['REDIRECT_INTERVAL']}"))
    print(paint("37", "    .start"))


def main() -> None:
    """Create the virtualenv, install the dependencies, and write the .env."""
    refuse_setuptools_use()
    print(paint("1;32", BANNER))
    print(paint("1;36", "  ROTATING CHANNEL CLONER | INSTALLER"))
    check_python()
    info(f"Python {platform.python_version()} on {platform.system()}.")

    heading("virtualenv")
    interpreter = create_venv(VENV_DIR)
    install_requirements(interpreter)
    verify_install(interpreter)

    existing = read_env(ENV_FILE)
    if existing:
        info(f"{ENV_FILE.name} already exists; press Enter to keep a value.")
    settings = collect_settings(existing)
    write_env(ENV_FILE, settings)

    if ENV_FILE.name not in (ROOT / ".gitignore").read_text(encoding="utf-8"):
        warning(f"{ENV_FILE.name} is not in .gitignore; do not commit your API hash.")

    report_next_steps(interpreter, settings)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warning("Setup cancelled; nothing was written.")
        raise SystemExit(130) from None
    except (RuntimeError, OSError) as exc:
        failure(str(exc))
        raise SystemExit(1) from exc
