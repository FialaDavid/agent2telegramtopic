"""Atomic local control requests for the bridge."""
import json
import os
from pathlib import Path
import time
import uuid


def control_dir(bot_name, home=None):
    from .config import state_dir
    return state_dir(home) / bot_name / "control"


def write_close(bot_name, chat, thread, home=None):
    root = control_dir(bot_name, home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"action": "close", "chat": chat, "thread": thread,
                                     "requested_at": time.time()}), encoding="utf-8")
    os.replace(temporary, path)
    return path


def close_main(argv):
    import argparse
    from .config import build_config, load_config, state_db_path
    from .state import LaneStore
    from .routing import LaneKey
    parser = argparse.ArgumentParser(prog="agent2telegramtopic close")
    parser.add_argument("--thread", type=int, required=True)
    parser.add_argument("--chat", type=int)
    args = parser.parse_args(argv)
    try:
        raw = load_config()
        cfg = build_config(raw)
        store = LaneStore(state_db_path(cfg.bot_name))
        try:
            rows = [row for row in store.list_all() if row.thread_id == args.thread]
            if args.chat is not None:
                rows = [row for row in rows if row.chat_id == args.chat]
            if len(rows) != 1:
                print("lane is missing or thread does not identify exactly one non-archived lane")
                return 2
            row = rows[0]
            if row.status == "archived":
                print("lane is already archived")
                return 2
            write_close(cfg.bot_name, row.chat_id, row.thread_id)
        finally:
            store._db.close()
        print(f"close requested for thread {row.thread_id}")
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 2
