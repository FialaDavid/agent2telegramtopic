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


def write_send(bot_name, chat, thread, text, home=None):
    root = control_dir(bot_name, home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"action": "send", "chat": chat, "thread": thread,
                                     "text": text, "requested_at": time.time()}), encoding="utf-8")
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


def send_main(argv):
    import argparse
    from .config import build_config, load_config, state_db_path
    from .state import LaneStore
    parser = argparse.ArgumentParser(prog="agent2telegramtopic send")
    parser.add_argument("--thread", type=int, required=True)
    parser.add_argument("--chat", type=int)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file")
    args = parser.parse_args(argv)
    try:
        text = args.text if args.text is not None else Path(args.text_file).read_text(encoding="utf-8")
        if not text or len(text) > 4000:
            print("text must contain 1 to 4000 characters")
            return 2
        raw = load_config()
        cfg = build_config(raw)
        store = LaneStore(state_db_path(cfg.bot_name))
        try:
            rows = [row for row in store.list_all() if row.thread_id == args.thread]
            if args.chat is not None:
                rows = [row for row in rows if row.chat_id == args.chat]
            if len(rows) != 1:
                print("lane is missing or thread does not identify exactly one lane")
                return 2
            row = rows[0]
            if row.status == "archived":
                print("lane is archived")
                return 2
            write_send(cfg.bot_name, row.chat_id, row.thread_id, text)
        finally:
            store._db.close()
        print(f"send requested for thread {row.thread_id}")
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 2
