# Ported from agent2telegram wizard.py (MIT): the prompts, hidden token entry and owner capture.
"""Interactive setup wizard: ``python3 -m agent2telegramtopic setup``.

  1. Pick the agent (Claude Code or Codex) that is installed on this machine.
  2. Paste the bot token; it is validated live and the bot must have Topics enabled.
  3. Authorize yourself: send the bot a message in its private chat, the wizard reads your id.
  4. Optionally add an ElevenLabs key (voice notes in and out).
  5. Write ~/.agent2telegramtopic/config.json (0600) and start the background service.
"""
from __future__ import annotations

import getpass
import os
import shutil
import sys
import time
from pathlib import Path

from . import service
from .config import config_file, hooks_settings_file, lanes_root, save_config, write_hooks_settings
from .skills import install_skill
from .telegram import TelegramClient, TelegramError

AGENTS = {"claude-code": ("Claude Code", "claude"), "codex": ("Codex", "codex")}
TOPICS_HELP = (
    "  ✗ This bot does not have Topics (Threaded Mode) enabled yet. The bridge gives every topic its own\n"
    "    agent session; until then all messages share one conversation. Open @BotFather → Mini App\n"
    "    (Open) → select the bot → Bot Settings → Threaded Mode → On. This is not visible in the\n"
    "    BotFather chat, and Telegram may report it only after you have opened the chat with the bot."
)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or default


def _ask_secret(prompt: str) -> str:
    """Hidden input (nothing is echoed)."""
    return getpass.getpass(f"{prompt}: ").strip()


def _yes(prompt: str, default_yes: bool = False) -> bool:
    answer = _ask(f"{prompt} ({'Y/n' if default_yes else 'y/N'})").lower()
    return default_yes if not answer else answer in ("y", "yes")


def find_binary(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return os.path.abspath(found)
    for directory in (Path.home() / ".local/bin", Path.home() / ".npm-global/bin", Path("/opt/homebrew/bin"),
                      Path("/usr/local/bin")):
        candidate = directory / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def choose_agent() -> str | None:
    found = {name: find_binary(binary) for name, (_label, binary) in AGENTS.items()}
    print("\n── Step 1/4 · Agent ──\n")
    for name, (label, binary) in AGENTS.items():
        print(f"  {label:<12} {'✓ ' + found[name] if found[name] else '· `' + binary + '` not found on PATH'}")
    available = [name for name, path in found.items() if path]
    if not available:
        print("\n  ✗ Neither `claude` nor `codex` is installed. Install one of them, log in, and re-run setup.")
        return None
    if len(available) == 1:
        print(f"\n  Using {AGENTS[available[0]][0]}.")
        return available[0]
    while True:
        choice = _ask("Which agent should answer? 1 = Claude Code, 2 = Codex", "1")
        if choice in ("1", "2"):
            return available[int(choice) - 1]


def enter_token(client_factory):
    print("\n── Step 2/4 · Telegram bot ──\n")
    print("Create a bot with @BotFather and paste its token below (hidden as you type).")
    while True:
        token = _ask_secret("Telegram bot token")
        if not token:
            continue
        client = client_factory(token)
        try:
            me = client.get_me()
        except TelegramError as exc:
            print(f"  ✗ {str(exc).replace(token, '[REDACTED]')}")
            continue
        print(f"  ✓ token valid, the bot is @{me.get('username')}")
        if me.get("has_topics_enabled") is True:
            print("  ✓ Topics are enabled for this bot")
        else:
            print(TOPICS_HELP)
        return token, me, client


def capture_owner(client, bot_username: str) -> int | None:
    """The first private-chat message sent to the bot names the one authorized user."""
    print("\n── Step 3/4 · Authorize yourself ──\n")
    print(f"Open Telegram, find @{bot_username} and send it any message (e.g. 'hi').")
    print("The bridge works only in the bot's private chat; messages from groups are ignored.")
    input("Press Enter once you have sent it… ")
    offset = None
    warned_group = False
    for _ in range(8):
        try:
            updates = client.get_updates(offset, timeout=5)
        except TelegramError as exc:
            print(f"  (could not read updates: {exc})")
            break
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            sender = message.get("from") or {}
            if (message.get("chat") or {}).get("type") != "private":
                if not warned_group:
                    print("  ! A message from a group arrived. Write to the bot in its PRIVATE chat instead.")
                    warned_group = True
                continue
            if sender.get("id") and not sender.get("is_bot"):
                who = sender.get("username") or sender.get("first_name") or "you"
                print(f"  ✓ authorized {who} (id {sender['id']})")
                return int(sender["id"])
        time.sleep(1)
    manual = _ask("Could not detect it. Enter your numeric Telegram user id (or leave empty to abort)")
    return int(manual) if manual.isdigit() else None


def run(home: Path | None = None, client_factory=TelegramClient, platform: str = sys.platform) -> int:
    print("=== Agent2TelegramTopic setup ===")
    existing = config_file(home)
    if existing.exists() and not _yes(f"A config already exists at {existing}. Overwrite?"):
        print("Aborted, keeping the existing config.")
        return 0
    agent = choose_agent()
    if agent is None:
        return 1
    entered = enter_token(client_factory)
    if entered is None:
        return 1
    token, me, client = entered
    owner = capture_owner(client, me.get("username", "your_bot"))
    if owner is None:
        print("  ✗ No authorized user, nothing written. Re-run setup.")
        return 1
    bot = me.get("username")
    raw = {"agent": agent, "token": token, "allowed_user_ids": [owner], "bot_username": bot}
    print("\n── Step 4/4 · Voice (optional) ──\n")
    if _yes("Enable voice notes via ElevenLabs (transcription and spoken replies)?"):
        key = _ask_secret("ElevenLabs API key (hidden)")
        if key:
            raw["elevenlabs_api_key"] = key
    path = save_config(raw, home)
    print(f"\n✓ Saved config to {path} (permissions 0600).")
    if agent == "claude-code":
        # Claude Code takes its Stop/SessionStart hooks from a settings file passed with --settings
        # at every launch; the bridge rewrites it on start. Nothing global is edited.
        write_hooks_settings(hooks_settings_file(bot, home), sys.executable, bot, lanes_root(bot, home))
        print("  ✓ Claude Code hooks are passed per session with --settings; ~/.claude is not modified.")
    else:
        print("  ✓ Codex needs no hook; turn boundaries come from its rollout log.")
    print(f"  ✓ {install_skill(agent, home)}")
    if _yes("\nInstall and start the background service now?", default_yes=True):
        try:
            where = service.install(platform, home=home, path=_service_path(agent))
            print(f"  ✓ service installed: {where}")
            print(f"  Logs: {service.log_path(home) if platform == 'darwin' else 'journalctl --user -u ' + service.UNIT}")
            if platform != "darwin":
                print("  To keep it running after you log out:  loginctl enable-linger $USER")
        except (OSError, RuntimeError) as exc:
            print(f"  ✗ could not install the service: {exc}")
            print("    Run the bridge by hand instead:  python3 -m agent2telegramtopic run")
            return 1
    else:
        print("Run the bridge by hand:  python3 -m agent2telegramtopic run")
    print(f"\nAll set. Open the private chat with @{bot}, create a topic and write in it.")
    return 0


def _service_path(agent: str) -> str:
    """PATH for the service: the current PATH plus the directories of the agent CLI and tmux."""
    extra = [os.path.dirname(p) for p in (find_binary(AGENTS[agent][1]), shutil.which("tmux")) if p]
    parts = list(dict.fromkeys([*extra, *os.environ.get("PATH", "").split(os.pathsep)]))
    return os.pathsep.join(p for p in parts if p)


def main(argv=None) -> int:
    try:
        return run()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130
