"""``python3 -m agent2telegramtopic uninstall``: stop the service, remove config, state and launcher."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import service
from .config import app_dir


def _stop_tmux(home: Path | None) -> None:
    """Kill the bridge's private tmux server (and so every agent session it started)."""
    socket_dir = app_dir(home) / "tmux"
    if socket_dir.is_dir() and shutil.which("tmux"):
        subprocess.run(["tmux", "kill-server"], env={**os.environ, "TMUX_TMPDIR": str(socket_dir)},
                       capture_output=True)


def _remove_launcher(home: Path) -> None:
    """Delete the `agent2telegramtopic` launcher script install.sh dropped on PATH."""
    # Only directories inside this home: never touch a launcher belonging to another user's HOME.
    candidates = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d.startswith(str(home) + os.sep)]
    for directory in [*candidates, str(home / ".local/bin")]:
        candidate = Path(directory) / "agent2telegramtopic"
        try:
            if candidate.is_file() and "agent2telegramtopic" in candidate.read_text()[:300]:
                candidate.unlink()
                print(f"  removed {candidate}")
        except (OSError, UnicodeDecodeError):
            pass


def main(argv=None, home: Path | None = None, platform: str = sys.platform) -> int:
    parser = argparse.ArgumentParser(prog="agent2telegramtopic uninstall")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    root = app_dir(home)
    if not args.yes:
        print(f"This stops the bridge and deletes {root} (config with your bot token, state and the")
        print("working directories of all topics), the background service and the installed launcher.")
        if input("Continue? (y/N): ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0
    print("Uninstalling Agent2TelegramTopic…")
    try:
        if service.remove(platform, home=home):
            print("  ✓ background service stopped and removed")
    except OSError as exc:
        print(f"  ! could not remove the service: {exc}")
    _stop_tmux(home)
    if root.exists():
        shutil.rmtree(root)
        print(f"  ✓ removed {root}")
    base = home or Path.home()
    _remove_launcher(base)
    source = base / ".agent2telegramtopic-src"
    if source.is_dir() and not source.is_symlink() and (source / "agent2telegramtopic").is_dir():
        shutil.rmtree(source)
        print(f"  ✓ removed {source}")
    print("Done. Telegram side: the bot itself still exists; delete it with @BotFather if you no longer need it.")
    return 0
