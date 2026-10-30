# Ported from agent2telegram background.py (MIT)
"""Background progress for Claude Code sub-agents (attach mode, opt-in).

A Claude Code sub-agent keeps working after the main turn that launched it has ended. The
bridge's status bubble belongs to a turn, so for the rest of that work Telegram shows nothing
and the agent looks dead, while the terminal shows every step.

Claude Code writes each sub-agent's transcript next to the main one::

    <projects>/<session-id>.jsonl                          main conversation
    <projects>/<session-id>/subagents/agent-<id>.jsonl     sub-agent (same record format)
    <projects>/<session-id>/subagents/agent-<id>.meta.json {"description": ..., ...}

:class:`SubagentWatcher` polls that directory and keeps ONE Telegram message listing the running
sub-agents with their latest tool call, edited in place (throttled). The message is deleted when
the last one finishes or goes silent — the main agent reports the result itself, so this message
is purely technical progress. Its id is persisted so a restart can delete an orphan.

Only one short line per step is ever sent (the same tool summaries as the status bubble), never
tool output.
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from . import readers

log = logging.getLogger(__name__)

#: A sub-agent whose transcript was written within this window counts as running.
ACTIVE_WINDOW = 90.0
#: A tracked sub-agent silent for this long is dropped (killed, stuck, or the harness died).
STALE_AFTER = 300.0
#: Minimum seconds between two edits of the progress message.
EDIT_INTERVAL = 10.0
#: A text-only record without ``stop_reason: end_turn`` may be an intro before a tool call that is
#: still being generated. Treat it as the final answer only after this much silence.
FINISH_GRACE = 20.0
#: How many sub-agents the message lists; the rest are summarised as a count.
MAX_LISTED = 5
#: On first sight only the tail of a (possibly large) transcript is read for the latest step.
FIRST_READ_BYTES = 256 * 1024

_LABELS = {
    "en": {"head": "🤖 In background", "more": "+{n} more", "wait": "⏳ working…"},
    "cs": {"head": "🤖 Na pozadí", "more": "+{n} další", "wait": "⏳ pracuje…"},
}


@dataclass
class _Agent:
    path: Path
    description: str
    started: float          # wall-clock epoch of the sub-agent's first record
    pos: int = 0            # byte offset read so far
    mtime: float = 0.0
    last_tool: str = ""
    final_text: bool = False  # the latest assistant record is text-only (a final answer candidate)
    end_turn: bool = False    # ... and carries stop_reason end_turn


def _epoch(rec: dict) -> float | None:
    ts = rec.get("timestamp") if isinstance(rec, dict) else None
    if not isinstance(ts, str) or not ts:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class SubagentWatcher:
    """Poll a session's ``subagents/`` directory and mirror running sub-agents into one message.

    ``transcript`` is a callable returning the main transcript path (it can change when the
    bridge re-resolves it). All Telegram failures are logged and swallowed: progress is cosmetic
    and must never break the bridge's forward loop."""

    def __init__(self, tg, chat_id, *, transcript, state_path: Path | None,
                 language: str = "en", edit_interval: float = EDIT_INTERVAL,
                 stale_after: float = STALE_AFTER, active_window: float = ACTIVE_WINDOW,
                 finish_grace: float = FINISH_GRACE, clock=time.time) -> None:
        self.tg = tg
        self.chat_id = chat_id
        self._transcript = transcript
        self._state_path = state_path
        self._labels = _LABELS.get(language, _LABELS["en"])
        self.edit_interval = float(edit_interval)
        self.stale_after = float(stale_after)
        self.active_window = float(active_window)
        self.finish_grace = float(finish_grace)
        self._clock = clock
        self._reader = readers.ClaudeCodeReader()
        self._agents: dict[str, _Agent] = {}
        # Sub-agents that finished or went stale, with the file size at that moment. They are
        # tracked again only when the file grows (the main agent resumed them).
        self._closed: dict[str, int] = {}
        self._mid: int | None = None
        self._shown = ""
        self._last_edit = 0.0

    # ---- persistence -------------------------------------------------------
    def _persist(self, mid: int | None) -> None:
        if self._state_path is None:
            return
        try:
            if mid is None:
                self._state_path.unlink()
            else:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_path.write_text(str(mid), "utf-8")
        except OSError:
            pass

    def cleanup_orphan(self) -> None:
        """Delete a progress message left behind by a previous run (crash or restart)."""
        if self._state_path is None or self.chat_id is None:
            return
        try:
            mid = int(self._state_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            return
        try:
            self.tg.delete_message(self.chat_id, mid)
        except Exception as e:
            log.warning("background progress: orphan cleanup failed: %s", e)
        self._persist(None)

    # ---- scanning ----------------------------------------------------------
    def _dir(self) -> Path | None:
        try:
            main = self._transcript()
        except Exception:
            return None
        if not main:
            return None
        main = Path(main)
        return main.parent / main.stem / "subagents"

    @staticmethod
    def _description(path: Path) -> str:
        meta = path.with_name(path.name[: -len(".jsonl")] + ".meta.json")
        try:
            desc = json.loads(meta.read_text("utf-8")).get("description") or ""
        except (OSError, ValueError, AttributeError):
            desc = ""
        return readers._short(desc or path.stem, 60)

    @staticmethod
    def _first_epoch(path: Path) -> float | None:
        try:
            with open(path, "rb") as f:
                line = f.readline(1_000_000)
            return _epoch(json.loads(line.decode("utf-8", "ignore")))
        except (OSError, ValueError):
            return None

    def _read_new(self, a: _Agent, size: int) -> None:
        start = a.pos
        cut = start == 0 and size > FIRST_READ_BYTES
        if cut:
            start = size - FIRST_READ_BYTES          # first sight of a big file: the tail will do
        try:
            with open(a.path, "rb") as f:
                f.seek(start)
                chunk = f.read(max(0, size - start))
        except OSError:
            return
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        a.pos = start + nl + 1
        lines = chunk[:nl].split(b"\n")
        if cut:
            lines = lines[1:]                        # the first line of a tail is cut mid-record
        for raw in lines:
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            self._apply(a, rec)

    def _apply(self, a: _Agent, rec: dict) -> None:
        typ = rec.get("type")
        if typ == "user":
            if self._reader.user_text(rec):          # a new instruction → working again
                a.final_text = a.end_turn = False
            return
        if typ != "assistant":
            return
        blocks = (rec.get("message") or {}).get("content")
        blocks = blocks if isinstance(blocks, list) else []
        kinds = {b.get("type") for b in blocks if isinstance(b, dict)}
        for ev in self._reader.parse(rec):
            if ev.kind == "tool" and ev.text:
                a.last_tool = ev.text
        if "tool_use" in kinds:
            a.final_text = a.end_turn = False
        elif "text" in kinds:
            a.final_text = True
            a.end_turn = (rec.get("message") or {}).get("stop_reason") == "end_turn"

    def _finished(self, a: _Agent, now: float) -> bool:
        if not a.final_text:
            return False
        return a.end_turn or now - a.mtime >= self.finish_grace

    def scan(self) -> None:
        """Refresh the set of running sub-agents from disk."""
        d = self._dir()
        now = self._clock()
        seen: set[str] = set()
        if d is not None and d.is_dir():
            for path in d.glob("agent-*.jsonl"):
                aid = path.name[len("agent-"): -len(".jsonl")]
                try:
                    st = path.stat()
                except OSError:
                    continue
                seen.add(aid)
                a = self._agents.get(aid)
                if a is None:
                    if now - st.st_mtime > self.active_window:
                        continue                     # history, not running work
                    if aid in self._closed and st.st_size <= self._closed[aid]:
                        continue                     # finished and not resumed
                    started = self._first_epoch(path) or st.st_mtime
                    a = _Agent(path=path, description=self._description(path), started=started)
                    self._agents[aid] = a
                    self._closed.pop(aid, None)
                if st.st_size > a.pos:
                    self._read_new(a, st.st_size)
                a.mtime = st.st_mtime
        for aid in list(self._agents):
            a = self._agents[aid]
            if aid not in seen or self._finished(a, now) or now - a.mtime > self.stale_after:
                try:
                    self._closed[aid] = a.path.stat().st_size
                except OSError:
                    self._closed[aid] = a.pos
                del self._agents[aid]

    # ---- rendering / Telegram ---------------------------------------------
    def render(self) -> str:
        now = self._clock()
        agents = sorted(self._agents.values(), key=lambda a: a.started)
        parts = []
        for a in agents[:MAX_LISTED]:
            head = f"{self._labels['head']}: {a.description} · {_elapsed(now - a.started)}"
            parts.append(head + "\n" + (a.last_tool or self._labels["wait"]))
        if len(agents) > MAX_LISTED:
            parts.append(self._labels["more"].format(n=len(agents) - MAX_LISTED))
        return "\n\n".join(parts)

    def tick(self) -> None:
        """One poll: scan, then create / edit (throttled) / delete the progress message."""
        if self.chat_id is None:
            return
        self.scan()
        text = self.render()
        now = self._clock()
        if not text:
            if self._mid is not None:
                try:
                    self.tg.delete_message(self.chat_id, self._mid)
                except Exception as e:
                    log.warning("background progress: delete failed: %s", e)
                self._mid = None
                self._shown = ""
                self._persist(None)
            return
        if text == self._shown or now - self._last_edit < self.edit_interval:
            return
        body = f"<i>{html.escape(text)}</i>"
        self._last_edit = now                        # also throttles retries after a failure
        try:
            if self._mid is None:
                mid = self.tg.send_plain_id(self.chat_id, body, parse_mode="HTML")
                if mid:
                    self._mid = mid
                    self._persist(mid)
                    self._shown = text
            else:
                self.tg.edit_plain(self.chat_id, self._mid, body, parse_mode="HTML")
                self._shown = text
        except Exception as e:
            log.warning("background progress: send/edit failed: %s", e)

    def close(self) -> None:
        """Delete the progress message (bridge shutdown)."""
        self._agents.clear()
        if self._mid is not None and self.chat_id is not None:
            try:
                self.tg.delete_message(self.chat_id, self._mid)
            except Exception as e:
                log.warning("background progress: delete failed: %s", e)
        self._mid = None
        self._shown = ""
        self._persist(None)
