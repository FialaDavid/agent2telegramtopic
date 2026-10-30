"""Lane-scoped Claude Stop hook."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from agent2telegramtopic import config


def main(argv=None) -> int:
    payload = None
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--bot", required=True)
        parser.add_argument("--lanes-root")
        args = parser.parse_args(argv)
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = None
        else:
            try:
                cwd = Path(payload["cwd"]).resolve()
                root = Path(args.lanes_root).resolve() if args.lanes_root else config.lanes_root(args.bot).resolve()
                session_id, transcript_path = payload["session_id"], payload["transcript_path"]
                if (cwd.parent.parent == root and cwd.parent.name.isdigit() and cwd.name.isdigit() and isinstance(session_id, str)
                        and session_id and isinstance(transcript_path, str) and transcript_path):
                    chat_id, lane_id = int(cwd.parent.name), int(cwd.name)
                    # Require the canonical configured path; digit-like aliases such as 007 or
                    # non-ASCII numerals must not write another lane's marker.
                    if (root / str(chat_id) / str(lane_id)).resolve() == cwd:
                        target = config.lane_state_file(args.bot, chat_id, lane_id, "session")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp_name = tempfile.mkstemp(dir=target.parent)
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump({"session_id": session_id, "transcript_path": transcript_path}, f)
                                f.flush()
                                os.fsync(f.fileno())
                            os.replace(tmp_name, target)
                        finally:
                            try:
                                os.unlink(tmp_name)
                            except FileNotFoundError:
                                pass
                        event = payload.get("hook_event_name")
                        source = payload.get("source")
                        if event == "SessionStart":
                            if source == "compact":
                                done = config.lane_state_file(args.bot, chat_id, lane_id, "compact_done")
                                done.parent.mkdir(parents=True, exist_ok=True)
                                done.touch()
                            return 0
                        # Delta (9.12): signal this lane's Stop hook so its bridge can finish
                        # the turn immediately, including on the first turn of a new session.
                        turn_end = config.lane_state_file(args.bot, chat_id, lane_id, "turn_end")
                        turn_end.parent.mkdir(parents=True, exist_ok=True)
                        turn_end.touch()
            except BaseException:
                pass
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
