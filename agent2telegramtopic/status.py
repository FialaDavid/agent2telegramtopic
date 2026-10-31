"""Status file for external monitors: ``<state dir>/<bot>.status.json`` (schema 1, see README).

One writer thread rewrites it on start, every ``heartbeat_seconds`` and shortly after any lane
change. It holds no secrets and no message text.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import __version__
from .config import lane_path, lane_state_file
from .routing import LaneKey
from .tmux import session_name

log = logging.getLogger(__name__)
SCHEMA = 1
HEARTBEAT_SECONDS = 15
# Lane changes arriving together (a park touches several fields) produce one write.
_COALESCE_SECONDS = 0.5


def tmux_sessions() -> set[str]:
    """Names of all sessions on this instance's tmux server; empty if there is none."""
    try:
        done = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(done.stdout.splitlines()) if done.returncode == 0 else set()


def _seconds(value) -> int | None:
    return int(value) if value is not None else None


def _ctx_windows(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {cwd: value["ctx_window"] for cwd, value in data.items()
            if isinstance(cwd, str) and isinstance(value, dict)
            and isinstance(value.get("ctx_window"), int)
            and not isinstance(value.get("ctx_window"), bool)}


class StatusWriter:
    def __init__(self, path: Path, config, config_path: Path, lane_store, root: str, *,
                 heartbeat_seconds: int = HEARTBEAT_SECONDS, sessions=tmux_sessions, clock=time.time):
        self.path = Path(path)
        self.config = config
        self.config_path = Path(config_path)
        self.lane_store = lane_store
        self.root = root
        self.heartbeat_seconds = heartbeat_seconds
        self._sessions = sessions
        self._clock = clock
        self._started_at = int(clock())
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def poke(self) -> None:
        """A lane changed; cheap and safe to call from any thread."""
        self._changed.set()

    def _last_compact(self, lane) -> int | None:
        marker = lane_state_file(self.config.bot_name, lane.chat_id, lane.thread_id, "compact_done")
        try:
            marked = marker.stat().st_mtime
        except OSError:
            marked = None
        known = [t for t in (lane.last_compact, marked) if t is not None]
        return _seconds(max(known)) if known else None

    def snapshot(self) -> dict:
        titles = self.lane_store.topic_titles()
        ctx_windows = (_ctx_windows(self.path.with_name(
            f"{self.config.bot_name}.claude_ctx.json"))
            if self.config.agent_kind == "claude-code" else {})
        alive = self._sessions()
        lanes = []
        for lane in self.lane_store.list_all():
            key = LaneKey(lane.chat_id, lane.thread_id)
            tmux_session = session_name(self.config.agent_kind, f"topic {lane.thread_id}",
                                        lane.thread_id, lane.chat_id)
            lanes.append({
                "chat": lane.chat_id,
                "thread": lane.thread_id,
                "title": titles.get(key),
                "agent": lane.agent,
                "cwd": str(lane_path(self.root, lane.chat_id, lane.thread_id)),
                "tmux_session": tmux_session,
                "alive": tmux_session in alive,
                "status": lane.status,
                "busy": lane.busy,
                "last_activity": _seconds(lane.last_activity),
                "last_compact": self._last_compact(lane),
                "ctx_tokens": lane.ctx_tokens,
                "ctx_window": ctx_windows.get(str(lane_path(
                    self.root, lane.chat_id, lane.thread_id).resolve())),
            })
        return {
            "schema": SCHEMA,
            "bot": self.config.bot_name,
            "version": __version__,
            "pid": os.getpid(),
            "config": str(self.config_path),
            "tmux_tmpdir": os.environ.get("TMUX_TMPDIR"),
            "started_at": self._started_at,
            "updated_at": int(self._clock()),
            "heartbeat_seconds": self.heartbeat_seconds,
            "lanes": lanes,
        }

    def write(self) -> None:
        """Atomically replace the file; a failure is logged, never raised."""
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            data = json.dumps(self.snapshot(), indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(tmp, self.path)
        except Exception as exc:
            log.warning("could not write status file %s: %s", self.path, exc)

    def _loop(self) -> None:
        while True:
            self._changed.clear()
            self.write()
            if self._stop.is_set():
                return
            if self._changed.wait(self.heartbeat_seconds):
                self._stop.wait(_COALESCE_SECONDS)

    def start(self) -> None:
        self.lane_store.on_change = self.poke
        self._thread = threading.Thread(target=self._loop, daemon=True, name="status-writer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._changed.set()
