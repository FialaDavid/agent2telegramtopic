# Ported from agent2telegram attach.py (MIT)
"""Runnable Telegram bridge for a single bot."""

import argparse
from dataclasses import replace
import logging
import json
import sys
import threading
import time
from pathlib import Path

from agent2telegramtopic import redaction
from agent2telegramtopic.config import (Config, build_config, hooks_settings_file, lane_path,
                                 lane_state_file, load_config, lanes_root, state_db_path,
                                 write_hooks_settings)
from agent2telegramtopic.lane import BOT_COMMANDS, LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.lock import AlreadyRunning, lock_file, single_instance_lock
from agent2telegramtopic.state import Lane, LaneStore
from agent2telegramtopic.telegram import TelegramClient
from agent2telegramtopic.tmux import launch_argv, session_name
from agent2telegramtopic.session import TmuxSession
import uuid
import shutil
import subprocess


def _expected_agent_commands(config: Config) -> list[str]:
    """Return unique executable basenames used to recognize the configured agent TUI."""
    command = config.claude_cmd if config.agent_kind == "claude-code" else config.codex_cmd
    seen = set()
    expected = []
    agent = "claude" if config.agent_kind == "claude-code" else "codex"
    for executable in (*command[:1], agent):
        name = Path(str(executable)).name
        if name and name not in seen:
            seen.add(name)
            expected.append(name)
    return expected


def build_app(config: Config, token: str, root: str) -> Multiplexer:
    client = TelegramClient(token)
    state_dir = state_db_path(config.bot_name).parent / config.bot_name
    lane_store = LaneStore(state_db_path(config.bot_name))
    multiplexer_ref = {}

    def make_bridge(key):
        # Delta (9.9): construct the agent session with this LaneKey's final paths.
        cwd = lane_path(root, key.chat_id, key.thread_id)
        state = lane_state_file(config.bot_name, key.chat_id, key.thread_id, "sent").parent
        cwd.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        # Delta (9.8): topic names are not stored in v0.4; use the specified thread label.
        tmux_name = session_name(
            config.agent_kind, f"topic {key.thread_id}", key.thread_id, key.chat_id,
        )
        marker_path = lane_state_file(config.bot_name, key.chat_id, key.thread_id, "session")
        try:
            marker = json.loads(marker_path.read_text("utf-8"))
            known_session_id = marker.get("session_id", "")
        except (OSError, ValueError, AttributeError):
            marker = {}
            known_session_id = ""
        # Delta (9.8): Codex resumes only an identity recorded in this lane's marker.
        session_id = (known_session_id or str(uuid.uuid4())) if config.agent_kind == "claude-code" else known_session_id
        # Delta (10b): a bridge always has a durable slot record before its worker
        # can report compaction or parking state.
        existing = lane_store.get(key)
        lane_store.upsert(Lane(
            key.chat_id, key.thread_id,
            existing.name if existing is not None else f"topic {key.thread_id}",
            config.agent_kind, session_id,
            existing.status if existing is not None else "parked",
            existing.busy if existing is not None else False,
            existing.last_activity if existing is not None else time.time(),
            existing.last_compact if existing is not None else None,
            existing.ctx_tokens if existing is not None else 0,
        ))
        resume = bool(known_session_id)
        base_cmd = config.claude_cmd if config.agent_kind == "claude-code" else config.codex_cmd
        argv = launch_argv(config.agent_kind, session_id, resume, base_cmd,
                           settings_path=config.hooks_settings_path, cwd=str(cwd))
        # Delta (9.13): tag user turns so LaneBridge can recognize Telegram origins after restart.
        signal_file = lane_state_file(config.bot_name, key.chat_id, key.thread_id, "turn_end")
        bridge_ref = {}
        def park_timed_out_lane(parked_key):
            # Keep the durable runtime state aligned with the killed session. The next
            # routed update will construct a new bridge and resume from the lane marker.
            current = lane_store.get(parked_key)
            bridge = bridge_ref.get("bridge")
            pending = False
            if bridge is not None:
                try:
                    inbox = bridge._ensure_inbox()
                    pending = inbox is not None and bool(inbox.pending())
                except Exception:
                    pending = False
            if current is not None:
                lane_store.upsert(replace(current, status="waiting" if pending else "parked", busy=False))
            else:
                lane_store.upsert(Lane(
                    parked_key.chat_id, parked_key.thread_id, f"topic {parked_key.thread_id}",
                    config.agent_kind, session_id, "waiting" if pending else "parked", False, time.time(), None, 0,
                ))
            mux = multiplexer_ref.get("mux")
            if mux is not None:
                mux._retire_parked_lane(parked_key)
                mux._slot_wakeup.set()

        bridge = LaneBridge(
            client, key, lane_state_file(config.bot_name, key.chat_id, key.thread_id, "sent"),
            lane_state_file(config.bot_name, key.chat_id, key.thread_id, "outbox"),
            agent=config.agent_kind, lane_cwd=cwd,
            session_marker=marker_path,
            # Delta (9.12): provide the chosen Claude identity before the first Stop hook.
            session_id=session_id,
            tmux_session=tmux_name, session=None, lane_store=lane_store,
            on_parked=park_timed_out_lane,
            config=config,
        )
        def session_alive():
            if shutil.which("tmux") is None:
                return False
            return subprocess.run(["tmux", "has-session", "-t", tmux_name],
                                  capture_output=True).returncode == 0
        def activate_session():
            bridge._session = TmuxSession(argv, cwd=cwd,
                name=tmux_name, origin_prefix="[TG] ", signal_file=signal_file,
                expected_agent_commands=_expected_agent_commands(config))
        bridge._session_alive = session_alive
        bridge._activate_session = activate_session
        def close_lane(close_key):
            if close_key != key:
                return
            def park_after_command():
                bridge._closing = True
                # The /close command itself is still owned by the inbound worker.
                # Wait for only that in-flight command; queued updates stay durable
                # for the resumed lane.
                while bridge._inbound_processing.is_set() and not mux._stop.wait(0.01):
                    continue
                while bridge._turn_active.is_set() and not mux._stop.wait(0.1):
                    continue
                session_obj = bridge._session
                if session_obj is not None:
                    if lane_store.get(key).ctx_tokens >= config.compact_min_tokens:
                        event = bridge.compact_internal()
                        if event is None:
                            bridge._closing = False
                            return
                        if not event.wait(300):
                            bridge._closing = False
                            return
                    try:
                        session_obj.close()
                    except Exception:
                        log.exception("could not close lane %s", key)
                    alive = getattr(session_obj, "alive", False)
                    alive = alive() if callable(alive) else alive
                    if alive:
                        bridge._closing = False
                        lane_store.set_status(key, "live")
                        lane_store.set_busy(key, False)
                        bridge.tg.send_message(key.chat_id, "⚠️ Couldn't close this topic's session. Try again.")
                        return
                try:
                    inbox = bridge._ensure_inbox()
                    pending = inbox is not None and bool(inbox.pending())
                except Exception:
                    pending = False
                lane_store.set_status(key, "waiting" if pending else "parked")
                lane_store.set_busy(key, False)
                mux._retire_parked_lane(key, bridge)
                bridge.stop()
                mux._slot_wakeup.set()
            threading.Thread(target=park_after_command, daemon=True).start()
        bridge._on_close = close_lane
        bridge_ref["bridge"] = bridge
        return bridge

    mux = Multiplexer(client, config.allowed_users, state_dir, make_bridge,
                      lane_states_root=state_db_path(config.bot_name).parent / config.bot_name / "lanes",
                      lane_store=lane_store, max_live=config.max_live_lanes,
                      compact_min_tokens=config.compact_min_tokens,
                      idle_minutes=config.idle_compact_minutes)
    multiplexer_ref["mux"] = mux
    return mux


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent2telegramtopic run")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    level = getattr(logging, args.log_level.upper(), None)
    if not isinstance(level, int):
        parser.error("invalid --log-level")
    logging.basicConfig(level=level, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    redaction.install_log_redaction()
    log = logging.getLogger("agent2telegramtopic.app")
    try:
        try:
            raw = load_config()
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        config = build_config(raw)
        token = raw["token"]
        bot = config.bot_name
        with single_instance_lock(lock_file(bot)):
            if raw.get("workdir"):
                selected_cwd = Path(raw["workdir"]).expanduser()
            else:
                selected_cwd = lanes_root(bot)
            selected_cwd.mkdir(parents=True, exist_ok=True)
            settings_path = hooks_settings_file(bot)
            write_hooks_settings(settings_path, sys.executable, bot, selected_cwd.resolve())
            config = replace(config, hooks_settings_path=str(settings_path))
            root = str(selected_cwd.resolve())
            app = build_app(config, token, root)
            app.client.set_my_commands(BOT_COMMANDS)
            app.bot_id = int(token.split(":", 1)[0])
            me = app.client.get_me()
            if me.get("has_topics_enabled") is not True:
                log.error("Bot topics are not enabled; refusing to start")
                return 2
            app.run_forever()
            return 0
    except AlreadyRunning:
        log.error("agent2telegramtopic is already running (lock %s)", lock_file(bot))
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception:
        log.exception("Could not start agent2telegramtopic")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
