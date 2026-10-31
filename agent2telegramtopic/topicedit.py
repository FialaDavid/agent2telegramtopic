"""``python3 -m agent2telegramtopic topic-edit "<name>" "<emoji>"``: name and iconify this topic.

Run it from an agent session: the session's working directory is the topic's lane directory,
``<root>/<chat id>/<thread id>``. The bot token comes from config.json and is never printed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .config import bot_token, load_config, state_db_path
from .routing import LaneKey
from .state import LaneStore
from .telegram import TelegramClient, TelegramError

_VS16 = "️"


def topic_from_cwd(cwd: Path) -> tuple[int, int] | None:
    """(chat id, thread id) from the nearest ``<chat>/<thread>`` directory pair at or above cwd."""
    parts = [cwd, *cwd.parents]
    for child in parts:
        parent = child.parent
        if re.fullmatch(r"\d+", child.name) and re.fullmatch(r"-?\d+", parent.name):
            return int(parent.name), int(child.name)
    return None


def main(argv=None, *, home: Path | None = None, cwd: Path | None = None, client=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print('usage: agent2telegramtopic topic-edit "<name>" "<emoji>"', file=sys.stderr)
        return 2
    name, emoji = argv
    topic = topic_from_cwd((cwd or Path.cwd()).resolve())
    if topic is None:
        print("Not inside a topic directory (<root>/<chat id>/<thread id>).", file=sys.stderr)
        return 1
    chat, thread = topic
    try:
        raw_config = load_config(home)
        if client is None:
            client = TelegramClient(bot_token(raw_config))
        stickers = client._call("getForumTopicIconStickers", timeout=15)
        icons = {s["emoji"].replace(_VS16, ""): s["custom_emoji_id"] for s in stickers}
        icon = icons.get(emoji.replace(_VS16, ""))
        if icon is None:
            print(f"Emoji {emoji} is not an allowed topic icon. Allowed: {' '.join(icons)}", file=sys.stderr)
            return 1
        try:
            client._call("editForumTopic", {"chat_id": chat, "message_thread_id": thread, "name": name,
                                            "icon_custom_emoji_id": icon}, timeout=15, retries=0)
        except TelegramError as exc:
            if "TOPIC_NOT_MODIFIED" not in str(exc):
                raise
        store = LaneStore(state_db_path(raw_config["bot_username"].strip(), home))
        store.set_topic_title(LaneKey(chat, thread), name)
    except (TelegramError, ValueError) as exc:
        print(f"topic-edit failed: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0
