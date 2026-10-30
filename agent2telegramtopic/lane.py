# Ported from agent2telegram attach.py (MIT)
"""Per-topic durable reply delivery.

Delta (a): state paths are supplied per lane and use agent2telegramtopic lane state files.
Delta (b): LaneTelegram binds every send to one LaneKey; the outbox chat is checked too.
Delta (d): the owner chat is this lane's chat_id.
"""

import html
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import threading
import time
import math
from datetime import datetime, timezone

from .background import SubagentWatcher, _epoch as _record_wall_time
from .durable import DurableInbox, DurableOutbox
from .config import save_elevenlabs_key
from .inbound import sender_label
from .redaction import message_metadata, update_metadata
from .routing import LaneKey
from .telegram import split_message
from . import readers, transcript, tts
from .session import BLOCKED_BY_OVERLAY, SessionError

log = logging.getLogger(__name__)
_BOT_ELEVENLABS_KEYS = {}

BOT_COMMANDS = [
    {"command": "start", "description": "Intro and what you can send"},
    {"command": "help", "description": "Intro and what you can send"},
    {"command": "status", "description": "Bridge health and queue status"},
    {"command": "new", "description": "Start a fresh conversation"},
    {"command": "setkey", "description": "Enable voice (your ElevenLabs API key)"},
    {"command": "voice", "description": "Toggle spoken (voice-note) replies"},
    {"command": "id", "description": "Show your Telegram id"},
    {"command": "close", "description": "Park this topic's session"},
]

# Turn-half deltas from agent2telegram attach.py (MIT):
# (1) Telegram sends use LaneTelegram, adding message_thread_id for this LaneKey.
# (2) Transcript selection uses the step-6 per-lane identity and _seek_to_turn.
# (3) turn_end and signal paths use the step-4/5 lane state directory.
# (4) text delivery remains durable through the step-5 per-lane ledger/outbox;
# (5) text, error, file, reaction and tool (status bubble) events are handled;
# (6) startup positioning remains in step 6; this port adds no startup positioning block.

# Ported from agent2telegram attach.py (MIT).
BACKSTOP_RETRY_ATTEMPTS = 5
BACKSTOP_RETRY_DELAY = 0.35
BACKSTOP_TURN_GRACE = 5.0
TYPING_INTERVAL = 1.5
OUTBOUND_TICK = 0.15
OUTBOX_MAX_ATTEMPTS = 3
IDLE_DONE = 90.0
IDLE_DONE_HOOKED = 600.0
INTERNAL_COMMAND_TIMEOUT = 300.0
SUSPICIOUS_TURN_SECONDS = 3.0
# Ported from agent2telegram attach.py (MIT).
INJECT_ATTEMPTS = 3
INJECT_RETRY_WAIT = 1.5
INBOUND_MAX_ATTEMPTS = 5
# How often a message left in durable storage is retried WHILE RUNNING. Without this it waited
# for a restart — for a service running for weeks, effectively forever (review finding #1).
INBOUND_RETRY_INTERVAL = 30.0
MAX_INBOUND_PROMPT_CHARS = 12_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
#: Background progress (opt-in, Claude Code): how often the sub-agent watcher polls, and how old a
#: transcript record may be to still count as live work. Records older than this — or written
#: before the bridge started — are history being replayed, never shown as a status bubble.
BACKGROUND_TICK = 2.0
BACKGROUND_LIVE_WINDOW = 120.0

#: Codex only: hold the first scraped tool bubble until the turn's intro text has been forwarded
#: (agents usually say what they'll do, THEN call the tool). The scraper is live but the Codex
#: transcript text lags, so without this the bubble jumps ahead of the intro line. After this
#: grace we show bubbles anyway, since some turns call a tool with no intro text.
TUI_BUBBLE_GRACE = 3.0
#: Deduplicate repeated deliveries of the same reaction on the same message.
#: Keep the window short so a user can remove and intentionally add a reaction again.
REACTION_DEDUP_S = 60.0
VOICE_MODE_HINT = (
    "[voice mode ON — your reply will be SPOKEN, not read. Default: keep it short (a few "
    "sentences, ~20 seconds), say the one thing that matters and offer details only if asked. "
    "Exception: when the user explicitly asks for a long narration (a full summary, a whole list, "
    "a briefing to listen to while driving), speak it in full — up to about five minutes. "
    "Conversational tone, numbers as words, no markdown, tables, code, file paths or URLs.]"
)
VOICE_MAX_CHARS = 1200


def _ogg_duration(path: str) -> int | None:
    """Length of an audio file in whole seconds via ffprobe; None when unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return int(round(float(result.stdout.strip()))) if result.returncode == 0 and result.stdout.strip() else None
    except Exception:
        return None
REPLY_QUOTE_CHARS = 300
def _epoch(value) -> float | None:
    """Epoch seconds from either a unix number or an ISO-8601 string, else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# Ported from agent2telegram/attach.py::_written_after (MIT), with its _epoch parser.
def _written_after(rec: dict, floor: float) -> bool:
    ts = _epoch(rec.get("timestamp"))
    return ts is not None and ts >= floor


#: Codex renders tool activity live in its TUI but only writes it to the rollout at completion.
#: For Codex (attach), we scrape the tmux pane for these lines so tool bubbles appear LIVE —
#: matching Claude Code (which logs tool_use to its transcript immediately). Claude needs no scrape.
# Ported from agent2telegram attach.py:_TUI_VERBS/_extract_tui_tools (MIT).
_TUI_VERBS = {"Read": "📄", "List": "📂", "Search": "🔎", "Ran": "🛠️",
              "Edit": "✏️", "Wrote": "✏️", "Added": "✏️", "Updated": "✏️",
              "Deleted": "🗑️", "Removed": "🗑️"}


def _extract_tui_tools(pane: str) -> list:
    """Pull live tool/web-search lines out of a Codex TUI capture, as bubble summaries."""
    out = []
    for raw in pane.splitlines():
        s = raw.strip()
        m = re.search(r"Searched the web for\s+(.+)", s)
        if m:
            out.append("🔎 Web search: " + readers._short(m.group(1)))
            continue
        if "Searching the web" in s:
            out.append("🔎 Searching the web")
            continue
        # Codex prints the call on a bullet line ("● Ran df -h /", "● Read foo.py") and its
        # output nested under "└ …". Strip any leading bullet/branch markers so we catch the
        # verb on either line; the verb whitelist keeps plain agent text (other words) out.
        body = s.lstrip("└├│•●▪▸·*- \t")
        if not body:
            continue
        verb = body.split(" ", 1)[0]
        if verb in _TUI_VERBS:
            rest = body[len(verb):].strip()
            out.append(f"{_TUI_VERBS[verb]} {verb} {readers._short(rest)}".rstrip())
    return out


class LaneTelegram:
    """Bind Telegram sends to one topic (delta b)."""

    def __init__(self, client, key: LaneKey, store=None):
        self.client = client
        self.key = key
        self.store = store

    # Delta (B2): reaction updates carry no message_thread_id, so every message this lane sends
    # is recorded as belonging to its topic.
    def _record(self, sent):
        if self.store is None:
            return sent
        for item in sent if isinstance(sent, list) else [sent]:
            message_id = item.get("message_id") if isinstance(item, dict) else item
            if isinstance(message_id, int):
                self.store.record_message(self.key.chat_id, message_id, self.key.thread_id)
        return sent

    def send_message(self, chat, text, **kwargs):
        del chat
        return self._record(self.client.send_message(self.key, text, **kwargs))

    def send_plain_id(self, chat, text, **kwargs):
        del chat
        return self._record(self.client.send_plain_id(self.key, text, **kwargs))

    def edit_plain(self, chat, message_id, text, **kwargs):
        del chat
        return self.client.edit_plain(self.key, message_id, text, **kwargs)

    def send_chat_action(self, chat, action):
        del chat
        return self.client.send_chat_action(self.key, action)

    def send_file(self, chat, path, **kwargs):
        del chat
        return self._record(self.client.send_file(self.key, path, **kwargs))

    def send_voice(self, chat, path, **kwargs):
        del chat
        return self._record(self.client.send_voice(self.key, path, **kwargs))

    def get_file_path(self, file_id):
        return self.client.get_file_path(file_id)

    def download(self, file_path, **kwargs):
        return self.client.download(file_path, **kwargs)

    def set_my_commands(self, commands):
        return self.client.set_my_commands(commands)

    def delete_message(self, chat, message_id):
        del chat
        return self.client.delete_message(self.key, message_id)

class LaneBridge:
    def __init__(self, tg, key: LaneKey, sent_path: Path, queue_root: Path, *,
                 agent: str | None = None, lane_cwd: Path | None = None,
                 tmux_session: str | None = None,
                 session_marker: Path | None = None,
                 session_id: str | None = None,
                 session=None,
                 lane_store=None,
                 on_close=None,
                 on_parked=None,
                 config=None,
                 codex_sessions_root: Path = Path("~/.codex/sessions").expanduser()):
        self.key = key
        self.tg = LaneTelegram(tg, key, lane_store)
        self._on_close = on_close
        self._on_parked = on_parked
        self._session = session
        self._lane_store = lane_store
        # Delta (d): one owner chat for the lifetime of this lane.
        self._owner_chat = self.key.chat_id
        # Delta (a): lane-scoped paths, passed from config.lane_state_file(...).
        self._sent_path = Path(sent_path)
        self._queue_root = Path(queue_root)
        self._voice_state_path = self._sent_path.parent / "voice_mode"
        self._voice_on = self._load_voice_state()
        # Ported a2t attach_sent loader; the ledger survives process restarts.
        try:
            self._sent_keys = set(self._sent_path.read_text("utf-8").split())
        except OSError:
            self._sent_keys = set()
        self._outbox = None
        self._status = {"mid": None, "shown": ""}   # live one-line tool-call status bubble
        # Persist the bubble's message_id so a restart/crash mid-turn can delete the orphan it
        # would otherwise leave behind in the chat.
        # Delta (a): per-lane file in this lane's state directory.
        self._status_path = self._sent_path.parent / "status_bubble"
        # Same for the background progress message (per lane, like the heartbeat).
        self._bg_state_path = self._sent_path.parent / f"background_progress_{key.thread_id}"
        self._bg_status = False          # the status bubble belongs to a turn NOT started from Telegram
        self._bg_status_last = 0.0       # monotonic ts of that turn's last tool call
        self._bg_arm = None              # (transcript, size at start/switch): only records after it are live
        self._rec_live = False           # the transcript record being handled is live background work
        self._bg_watcher = None          # SubagentWatcher (sub-agent progress message)
        self._seen_tools = set()
        self._tui_seen: set = set()          # Codex TUI scrape: tool lines already shown this turn
        self._turn_text_sent = False
        self._turn_is_reaction = False       # turn opened by a reaction → exempt from the backstop
        self._reaction_seen: dict = {}   # (chat, msg, emoji) -> first reaction timestamp
        self._turn_error = ""
        self.agent = agent
        self.tmux_session = tmux_session or ""
        self.cfg = self
        self.inbound_sender_label = False   # config default (a2t config.py)
        if config is not None:
            for name in ("bot_name", "elevenlabs_api_key", "elevenlabs_language",
                         "tts_speed", "tts_voice_id", "tts_model_id", "file_marker", "outbox_dirs",
                         "background_progress", "inbound_sender_label"):
                setattr(self, name, getattr(config, name))
        if getattr(self, "bot_name", None):
            _BOT_ELEVENLABS_KEYS.setdefault(self.bot_name, getattr(self, "elevenlabs_api_key", ""))
        self.lane_cwd = Path(lane_cwd) if lane_cwd is not None else None
        self.session_marker = Path(session_marker) if session_marker is not None else None
        self.codex_sessions_root = Path(codex_sessions_root).expanduser()
        self.session_id = session_id
        self._reader = readers.for_agent(agent or "claude-code")
        self._turn_from_tg = False
        self._marker = "[TG]"
        self._origins = tuple({p for p in ("Telegram:".strip(), "Telegram:", "[TG]") if p})
        self._transcript: Path | None = None
        self._tpos = 0
        self._turn_tpos = 0
        self._last_resolve = 0.0
        self._turn_active = threading.Event()
        self._claude_hook_ready = self.agent != "claude-code"
        self._turn_started_wall = time.time()
        self._last_activity = time.monotonic()
        self._turn_started = self._last_activity
        self._turn_begun_at = self._last_activity
        self._last_typing = self._last_activity
        self._resolved_session_id = None
        # Delta (a): all turn markers and status state are scoped to this lane's state directory.
        self._turn_end = self._sent_path.parent / "turn_end"
        self._signal = self._sent_path.parent / "answer.txt"
        # Delta (b): the heartbeat lives beside this lane's turn markers.
        self._heartbeat = self._signal.parent / f"outbound_heartbeat_{key.thread_id}"
        self._pending_turn_end = False
        self._internal_turn = False
        self._internal_turn_done = threading.Event()
        self._internal_lock = threading.RLock()
        self._internal_deadline = None
        self._internal_turn_id = None
        self._internal_compacted_turn_id = None
        self._internal_timed_out = False
        self._compact_ctx_before = None
        self._compact_done = self._sent_path.parent / "compact_done"
        self._typing_count = 0
        self._max_gap = 0.0
        self._stop = threading.Event()
        self._closing = False
        # Ported from agent2telegram attach.py::_init_inbound_worker_state (MIT).
        self._inbox = None
        self._inbound_queue = queue.Queue()
        self._inbound_worker_started = False
        self._inbound_worker_lock = threading.RLock()
        self._inbound_processing = threading.Event()
        # Ported from agent2telegram attach.py::__init__ (MIT).
        self._last_pane_warning = 0.0

    # Ported from agent2telegram attach.py (MIT).
    def _ensure_inbox(self):
        inbox = getattr(self, "_inbox", None)
        if inbox is None:
            try:
                inbox = DurableInbox(self._queue_root)
            except Exception as e:
                log.error("could not open the durable inbox: %s", e)
                inbox = None
            self._inbox = inbox
        return inbox

    def _ensure_inbound_worker_state(self) -> None:
        if not hasattr(self, "_inbound_queue"):
            self._inbound_queue = queue.Queue()
        if not hasattr(self, "_inbound_worker_started"):
            self._inbound_worker_started = False
        if not hasattr(self, "_inbound_worker_lock"):
            self._inbound_worker_lock = threading.RLock()

    def _start_inbound_worker(self) -> None:
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            if self._inbound_worker_started:
                return
            threading.Thread(target=self._inbound_worker_loop, daemon=True).start()
            self._inbound_worker_started = True

    def _submit_inbound_update(self, upd: dict, record_id: str | None = None) -> None:
        self._start_inbound_worker()
        self._ensure_inbound_worker_state()
        with self._inbound_worker_lock:
            if record_id:
                if record_id in self._inbound_inflight():
                    return
                # Keep the durable record authoritative across replay/poller races:
                # a worker may have completed it after replay took its snapshot.
                inbox = getattr(self, "_inbox", None)
                if inbox is not None and not any(rec.record_id == record_id for rec in inbox.pending()):
                    return
                self._inbound_inflight().add(record_id)
            self._inbound_queue.put((upd, record_id))

    # Ported from agent2telegram attach.py:1385 (MIT).
    def _inbound_inflight(self) -> set:
        if not hasattr(self, "_inflight_ids"):
            self._inflight_ids = set()
        return self._inflight_ids

    # Ported from agent2telegram attach.py:1392 (MIT).
    def _replay_pending_inbound(self) -> int:
        inbox = self._ensure_inbox()
        if inbox is None:
            return 0
        inflight = set(self._inbound_inflight())
        try:
            waiting = inbox.pending()
        except Exception as e:
            log.error("could not read the leftover inbound messages: %s", e)
            return 0
        waiting = [r for r in waiting if r.record_id not in inflight]
        submitted = 0
        for rec in waiting:
            self._submit_inbound_update(rec.update, rec.record_id)
            submitted += 1
        if submitted:
            log.info("delivering %d message(s) left over from before restart", submitted)
        return submitted

    # Ported from agent2telegram attach.py:1415 (MIT).
    def _inbound_worker_loop(self) -> None:
        while not self._stop.is_set() or not self._inbound_queue.empty():
            try:
                item = self._inbound_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            upd, record_id = item if isinstance(item, tuple) else (item, None)
            try:
                # Claim under the worker lock, then release it before any turn wait or
                # injection so another topic can continue through the poller.
                with self._inbound_worker_lock:
                    if self._closing:
                        self._inbound_inflight().discard(record_id)
                        continue
                    self._inbound_processing.set()
                waited_for_internal = False
                timed_out = False
                while True:
                    with self._internal_lock:
                        if self._internal_timed_out:
                            timed_out = True
                            break
                        if not self._internal_turn:
                            if self._closing:
                                self._inbound_inflight().discard(record_id)
                                delivered = None
                            else:
                                if waited_for_internal and self._transcript and self._transcript.exists():
                                    self._tpos = self._transcript.stat().st_size
                                self._inbound_active_record_id = record_id
                                delivered = self._handle(upd)
                            break
                        done = self._internal_turn_done
                        deadline = self._internal_deadline
                    waited_for_internal = True
                    remaining = max(0.0, deadline - time.monotonic()) if deadline else 0.0
                    if not done.wait(remaining):
                        self._expire_internal_turn(done)
                if timed_out:
                    continue
                if delivered is None:
                    continue
            except Exception as e:
                log.error("inbound worker error: error=%s id=%s %s",
                          type(e).__name__, record_id, update_metadata(upd))
                self._inbound_failed(record_id, str(e))
            else:
                if timed_out:
                    continue
                if delivered is not False:
                    log.info("IN delivered to session id=%s", record_id)
                if delivered is False:
                    self._inbound_failed(record_id, "not delivered to the session")
                else:
                    self._inbound_done(record_id)
            finally:
                self._inbound_active_record_id = None
                self._inbound_processing.clear()
                self._inbound_queue.task_done()

    # Ported from agent2telegram attach.py:1445-1468 (MIT).
    def _inbound_done(self, record_id: str | None) -> None:
        self._ensure_inbound_worker_state()
        self._complete_inbound_record(record_id)

    def _complete_inbound_record(self, record_id: str | None) -> None:
        # ORDER MATTERS. Delete the durable record before releasing its in-flight claim so a
        # concurrent replay cannot submit the record twice.
        inbox = getattr(self, "_inbox", None)
        if record_id:
            getattr(self, "_inbound_failure_notices", {}).pop(record_id, None)
        try:
            if record_id and inbox is not None:
                inbox.done(record_id)
        except Exception as e:
            log.warning("cleanup of delivered record %s failed: %s", record_id, e)
        finally:
            with self._inbound_worker_lock:
                self._inbound_inflight().discard(record_id)

    # Ported from agent2telegram attach.py:1470 (MIT).
    def _inbound_failed(self, record_id: str | None, reason: str) -> None:
        self._inbound_inflight().discard(record_id)
        inbox = getattr(self, "_inbox", None)
        if not record_id or inbox is None:
            return
        try:
            attempts = inbox.fail(record_id, reason)
            if attempts >= INBOUND_MAX_ATTEMPTS:
                text = ""
                if not getattr(self, "_inbound_failure_notices", {}).get(record_id):
                    try:
                        record = next((item for item in inbox.pending()
                                       if item.record_id == record_id), None)
                        if record is not None:
                            message = record.update.get("message") or record.update.get("edited_message") or {}
                            text = (message.get("text") if isinstance(message, dict) else None) or ""
                    except Exception:
                        pass
                inbox.give_up(record_id, f"{reason} (after {attempts} attempts)")
                notice = getattr(self, "_inbound_failure_notices", {}).pop(record_id, None)
                if notice is not None:
                    kind, text, notice_reason = notice
                    if kind == "unsafe":
                        self._notify_unsafe_pane(notice_reason)
                    else:
                        self._notify_inject_failed(text, notice_reason)
                else:
                    self._notify_inject_failed(text, reason)
                log.error("update %s could not be delivered even on attempt %d → dead-letter",
                          record_id, attempts)
        except Exception as e:
            log.warning("marking undelivered record %s failed: %s", record_id, e)

    # Ported from agent2telegram attach.py (MIT).
    def _handle(self, upd: dict) -> bool:
        # Reactions (e.g. ❤️) → quick-feedback line.
        # Ported from agent2telegram attach.py:_handle reaction branch (MIT).
        # Delta (b): the sender allowlist and the message→topic routing happen in the multiplexer.
        mr = upd.get("message_reaction")
        if mr:
            emojis = "".join(r.get("emoji", "") for r in mr.get("new_reaction", [])
                             if r.get("type") == "emoji")
            if emojis:
                # Telegram can redeliver the SAME reaction as multiple updates. Without
                # deduplication each update would ask the agent for another reply.
                # The reaction itself is identified by chat + message +
                # emoji, so a repeat of that triple inside a short window is a duplicate, never
                # a second opinion. This is identity matching, not guessing from content.
                key = (mr.get("chat", {}).get("id"), mr.get("message_id"), emojis)
                now = time.time()
                seen = self._reaction_seen.get(key)
                if seen is not None and now - seen < REACTION_DEDUP_S:
                    log.info("reaction %s on #%s ignored as a duplicate (%.1fs after the first)",
                             emojis, mr.get("message_id"), now - seen)
                    return True
                self._reaction_seen[key] = now
                if len(self._reaction_seen) > 512:      # bound cache growth
                    for k in sorted(self._reaction_seen, key=self._reaction_seen.get)[:256]:
                        self._reaction_seen.pop(k, None)

                # A reaction DOES deserve an answer, but a one-liner — being left on read feels
                # like the bridge swallowed it. The prompt therefore asks for a very short reply.
                #
                # The backstop exemption below is the safety net for when the agent still stays
                # silent. Without it the backstop ("a Telegram turn must never go unanswered")
                # grabs the last assistant text and can forward an INTERNAL note — that is how
                # "No response requested." reached a user on a live bridge (2026-08-02). Silence
                # is the lesser evil; the log line in _finish_turn keeps it visible.
                # Only when no turn was running: a reaction landing mid-turn must not disarm the
                # backstop for the real question underneath it.
                turn_running = self._turn_active.is_set()
                self._begin_turn()
                if not turn_running:
                    self._turn_is_reaction = True
                text = (
                    f"{emojis} reacted {emojis} to your message #{mr.get('message_id')} "
                    f"— quick feedback. Always answer, but with ONE very short line "
                    f"(a few words or an emoji), nothing more.")
                if self.cfg.inbound_sender_label:
                    # Delta: the `language` config key is not ported; "en" is its a2t default.
                    text = f"{sender_label({'from': mr.get('user'), 'chat': mr.get('chat')}, getattr(self.cfg, 'language', 'en'))} {text}"
                # Delta (state): like the message path below, a failed injection must not
                # leave this lane marked busy.
                try:
                    delivered = self._inject(text)
                    if not delivered:
                        self._turn_active.clear()
                        if self._lane_store is not None:
                            self._lane_store.set_busy(self.key, False)
                    return delivered
                except BaseException:
                    self._turn_active.clear()
                    if self._lane_store is not None:
                        self._lane_store.set_busy(self.key, False)
                    raise
            return True

        message = upd.get("message") or upd.get("edited_message") or {}
        text0 = (message.get("text") or "").strip()
        if text0.startswith("/") and not (message.get("voice") or message.get("audio")
                                           or message.get("photo") or message.get("document")):
            if self._handle_command(text0, self.key.chat_id, message.get("message_id")):
                return True
        text = (message.get("text") or message.get("caption") or "").strip()
        if message.get("voice") or message.get("audio"):
            text = self._transcribe(message.get("voice") or message.get("audio"), self.key.chat_id) or text
            if not text:
                return True
            text = f"[voice transcript – may contain recognition errors]\n{text}"
        elif message.get("photo") or message.get("document"):
            note = self._download_note(message, self.key.chat_id)
            if not note:
                return True
            text = f"{text}\n{note}".strip()
        if text:
            text = self._prepend_reply_context(message, text)
        # Match a2t's attach.py: hint insertion and injection are both conditional on
        # actual user text. An empty text/caption must not become a prompt by itself.
        if text and self._voice_reply_on():
            text = f"{VOICE_MODE_HINT}\n{text}"
        if text and self.cfg.inbound_sender_label:
            # Delta: the `language` config key is not ported; "en" is its a2t default.
            text = f"{sender_label(message, getattr(self.cfg, 'language', 'en'))} {text}"
        text = self._limit_inbound_prompt(text, self.key.chat_id)
        if not text:
            return True
        try:
            self._begin_turn()
            delivered = self._inject(text)
            if not delivered:
                self._turn_active.clear()
                if self._lane_store is not None:
                    self._lane_store.set_busy(self.key, False)
            return delivered
        except BaseException:
            self._turn_active.clear()
            if self._lane_store is not None:
                self._lane_store.set_busy(self.key, False)
            raise

    # Ported from agent2telegram attach.py:1906 (MIT); Delta: the config file stores the ElevenLabs key.
    def _handle_command(self, text: str, chat_id: int, message_id: int | None = None) -> bool:
        parts = text.split(maxsplit=1)
        cmd = parts[0].lstrip('/').split('@')[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        labels = {"codex": "Codex", "claude-code": "Claude Code"}
        agent = labels.get(self.agent, self.agent or "agent")
        if cmd in ("start", "help"):
            voice = "on" if self._elevenlabs_key() else "off — enable with /setkey"
            self.tg.send_message(chat_id,
                f"👋 You're connected to a live *{agent}* session via Agent2TelegramTopic.\n\n"
                "Just send a message — it goes straight to the agent. You can also send photos and files.\n\n"
                f"🎤 Voice transcription: {voice}.\n\n"
                "Commands: /help · /status · /new · /id · /setkey · /voice · /close")
            return True
        if cmd == "id":
            self.tg.send_message(chat_id, f"Your Telegram id: `{chat_id}`")
            return True
        if cmd == "status":
            self.tg.send_message(chat_id, self._status_text(agent), parse_mode=None)
            return True
        if cmd == "new":
            return self._cmd_new(chat_id)
        if cmd == "setkey":
            return self._set_voice_key(arg, chat_id, message_id)
        if cmd == "voice":
            return self._toggle_voice(chat_id)
        if cmd == "close":
            # New agent2telegramtopic code: lifecycle parking is wired in step 10.
            try:
                if self._on_close is None:
                    raise RuntimeError("lane close callback is not configured")
                with self._internal_lock:
                    self._closing = True
                self._on_close(self.key)
            except Exception as exc:
                self._closing = False
                log.error("/close failed: error=%s", type(exc).__name__)
                self.tg.send_message(chat_id, "⚠️ Couldn't close this topic's session. Try again.")
            return True
        return False

    def _elevenlabs_key(self):
        return _BOT_ELEVENLABS_KEYS.get(getattr(self, "bot_name", None),
                                        getattr(self.cfg, "elevenlabs_api_key", ""))

    @staticmethod
    def _age_str(seconds):
        if seconds is None: return "n/a"
        seconds = int(max(0, seconds))
        if seconds < 60: return f"{seconds}s"
        if seconds < 3600: return f"{seconds // 60}m"
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"

    @staticmethod
    def _mtime_age(path):
        try: return max(0.0, time.time() - path.stat().st_mtime) if path else None
        except OSError: return None

    def _status_text(self, agent):
        try: alive = self._session.alive
        except Exception: alive = False
        try:
            inbox = self._ensure_inbox()
            pending_in = len(inbox.pending()) if inbox is not None else None
            dead = len(inbox.dead_letters()) if inbox is not None else None
        except Exception: pending_in = dead = None
        try:
            outbox = self._ensure_outbox()
            pending_out = len(outbox.pending_chunks()) if outbox is not None else 0
        except Exception: pending_out = None
        transcript_path = getattr(self, "_transcript", None)
        return "\n".join((f"📟 {agent} bridge",
            f"• Session '{self.tmux_session}': {'alive ✓' if alive else 'GONE ✗'}",
            f"• Transcript: {transcript_path.name if transcript_path else 'unproven / unavailable'}",
            f"• Last activity: {self._age_str(self._mtime_age(transcript_path))} ago",
            f"• Durable inbox: {pending_in if pending_in is not None else 'unavailable'} pending; dead-letter: {dead if dead is not None else 'unavailable'}",
            f"• Durable outbox: {pending_out if pending_out is not None else 'unavailable'} chunks pending",
            f"• Heartbeat: {self._age_str(self._mtime_age(self._heartbeat))} ago",
            f"• Voice replies: {'on' if self._voice_reply_on() else 'off'}; transcription: {'on' if self._elevenlabs_key() else 'off'}"))

    def _set_voice_key(self, key, chat_id, message_id):
        if not key:
            self.tg.send_message(chat_id, "Usage: `/setkey <your ElevenLabs API key>` — enables voice-message transcription.\nI'll delete your message right after so the key isn't left in the chat.")
            return True
        from .stt import looks_like_api_key
        if not looks_like_api_key(key):
            if message_id is not None: self.tg.delete_message(chat_id, message_id)
            self.tg.send_message(chat_id, "That doesn't look like an ElevenLabs key — they start with `sk_`. Grab a fresh one and send `/setkey sk_…` again. (Your message is deleted either way.)")
            return True
        if message_id is not None: self.tg.delete_message(chat_id, message_id)
        try:
            save_elevenlabs_key(key)
        except Exception:
            log.error("setkey: could not write the config file")
            self.tg.send_message(chat_id, "⚠️ Couldn't save the key to the config file. Try again.")
            return True
        self.cfg.elevenlabs_api_key = key
        _BOT_ELEVENLABS_KEYS[getattr(self, "bot_name", "")] = key
        self.tg.send_message(chat_id, "✅ Voice transcription enabled — key saved. I deleted your message so the key isn't left in the chat history. Send a voice note to try it.")
        return True

    # Ported from agent2telegram attach.py:1940 (MIT).
    # Delta: omit _claude_pinned and _clear_claude_pin_probe because lane identity is assigned and
    # has no pin probe. Retain _transcript: clearing it would make _maybe_reresolve re-adopt and
    # _seek_to_turn the old transcript before the Stop-hook marker moves.
    def _cmd_new(self, chat_id: int) -> bool:
        reset = '/clear' if self.cfg.agent == 'claude-code' else '/new'
        try:
            self._session.send_command(reset)
        except Exception as exc:
            log.error("/new failed: %s", exc)
            if BLOCKED_BY_OVERLAY in str(exc):
                self.tg.send_message(
                    chat_id,
                    "⚠️ Couldn't start a fresh conversation — the panel has an interactive "
                    "dialog open and Escape could not close it. Clear it in the tmux pane "
                    "yourself, then try /new again.")
            else:
                self.tg.send_message(chat_id, "⚠️ Couldn't start a fresh conversation (the session may be busy or gone). Try again.")
            return True
        self.tg.send_message(chat_id, f"🧹 Started a fresh conversation — sent {reset} to the session.")
        return True

    def _prepend_reply_context(self, msg: dict, text: str) -> str:
        replied_to = msg.get("reply_to_message") or {}
        quote = (replied_to.get("text") or replied_to.get("caption") or "").strip()
        if not quote:
            return text
        quote = " ".join(quote.split())
        if len(quote) > REPLY_QUOTE_CHARS:
            quote = quote[:REPLY_QUOTE_CHARS] + "…"
        return f"[replying to: {quote}]\n{text}"

    def _reject_audio_too_large(self, chat_id: int, size: int | None) -> None:
        mb = (int(size or 0) + 1024 * 1024 - 1) // (1024 * 1024)
        self.tg.send_message(chat_id, f"🎤 That audio is ~{mb} MB. Voice/audio limit is 25 MB — "
                             "please send a shorter clip or write it as text.")

    # Ported from agent2telegram attach.py:2957-2995 (MIT).
    def _download_note(self, msg: dict, chat_id: int) -> str:
        import re
        if msg.get("photo"):
            photo = msg["photo"][-1]
            file_id, default = photo["file_id"], "image.jpg"
            size = photo.get("file_size")
        else:
            doc = msg["document"]
            file_id, default = doc["file_id"], doc.get("file_name") or "file"
            size = doc.get("file_size")
        if size and size > 20 * 1024 * 1024:
            self.tg.send_message(chat_id,
                f"⚠️ That file is ~{size // 1024 // 1024} MB. Telegram bots can only receive files up "
                "to 20 MB — please share a link (Drive/Dropbox) or a smaller/zipped version.")
            log.warning("attachment '%s' too big for the Bot API: %s bytes", default, size)
            return ""
        try:
            fp = self.tg.get_file_path(file_id)
            data = self.tg.download(fp)
        except Exception as e:
            log.warning("attachment '%s' download failed: %s", default, e)
            self.tg.send_message(chat_id,
                "⚠️ Couldn't download the attachment (too large, or a transient error — try again).")
            return ""
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(default).name) or "file"
        if "." not in name and (ext := Path(fp).suffix):
            name += ext
        # Delta: per-lane attachments dir (lane state dir) instead of a2t's global one.
        d = self._queue_root.parent / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / name
        if dest.exists():
            i = 1
            while (cand := d / f"{dest.stem}-{i}{dest.suffix}").exists():
                i += 1
            dest = cand
        dest.write_bytes(data)
        log.info("saved attachment -> %s (%d bytes)", dest, len(data))
        return f"[The user attached a file saved at: {dest} — open and use it as appropriate.]"

    def _transcribe(self, media: dict, chat_id: int) -> str | None:
        from . import stt
        if not self._elevenlabs_key():
            self.tg.send_message(chat_id,
                "🎤 Voice transcription isn't enabled yet. Add your ElevenLabs key with "
                "`/setkey <your-key>` (I'll delete the message right after) — then resend the voice note.")
            return None
        try:
            size = int(media.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_AUDIO_BYTES:
            self._reject_audio_too_large(chat_id, size)
            log.warning("voice/audio too large for STT: %s bytes", size)
            return None
        try:
            fp = self.tg.get_file_path(media["file_id"])
            audio = self.tg.download(fp)
            if len(audio) > MAX_AUDIO_BYTES:
                self._reject_audio_too_large(chat_id, len(audio))
                log.warning("downloaded voice/audio too large for STT: %s bytes", len(audio))
                return None
            return stt.transcribe(audio, api_key=self._elevenlabs_key(),
                                  language=getattr(self.cfg, "elevenlabs_language", "") or None,
                                  filename=Path(fp).name or "voice.ogg")
        except stt.EmptyTranscript:
            log.warning("voice note had no speech")
            self._send_final(
                "🎤 I couldn't hear any speech in that voice note. Please record it again.",
                turn_text=False,
            )
            return None
        except Exception as e:
            log.error("transcription failed: %s", e)
            self._send_final(
                f"🎤 Voice transcription failed ({e}). Please resend it or type your message instead.",
                turn_text=False,
            )
            return None

    # Delta (10.2): lifecycle compaction is not a Telegram-originated turn.
    def compact_internal(self) -> threading.Event | None:
        """Submit /compact and return its agent-specific completion event."""
        with self._internal_lock:
            if self._turn_active.is_set():
                return None
            self._consume_turn_end()
            # Ignore transcript records already present before submission.
            if self._transcript and self._transcript.exists():
                self._tpos = self._transcript.stat().st_size
            self._internal_turn_done = threading.Event()
            self._compact_ctx_before = self._internal_context_tokens()
            self._internal_turn_id = None
            self._internal_compacted_turn_id = None
            if self._compact_done:
                try:
                    self._compact_done.unlink()
                except FileNotFoundError:
                    pass
            self._internal_turn = True
            self._internal_deadline = time.monotonic() + INTERNAL_COMMAND_TIMEOUT
            self._turn_active.set()
            self._turn_from_tg = False
            self._turn_started = time.monotonic()
            self._turn_started_wall = time.time()
            self._turn_begun_at = self._turn_started
            self._turn_tpos = self._tpos
            self._pending_turn_end = False
            done = self._internal_turn_done
        threading.Thread(target=self._internal_timeout_loop, args=(done,), daemon=True).start()
        # TmuxSession.send_command waits for the TUI to submit. Keep the watcher tick
        # nonblocking and let the independent timeout cover a stalled submission.
        threading.Thread(target=self._submit_internal_compact, args=(done,), daemon=True).start()
        return done

    def _submit_internal_compact(self, done: threading.Event) -> None:
        try:
            self._session.send_command("/compact")
        except Exception as exc:
            log.warning("internal /compact command failed: %s", type(exc).__name__)
            # Submission may fail after Enter; hold inbound until completion or timeout.

    def _internal_timeout_loop(self, done: threading.Event) -> None:
        deadline = self._internal_deadline
        if deadline is not None and not done.wait(max(0.0, deadline - time.monotonic())):
            self._expire_internal_turn(done)

    def _internal_context_tokens(self):
        if self._transcript is None or not self._transcript.exists():
            return None
        if self.agent == "codex":
            return transcript.codex_ctx_tokens(self._transcript)
        return transcript.claude_ctx_tokens(self._transcript)

    def _record_internal_compaction(self) -> None:
        after = self._internal_context_tokens()
        log.info("internal /compact context tokens before=%s after=%s",
                 self._compact_ctx_before, after)
        if after is not None and self._lane_store is not None:
            self._lane_store.mark_compacted(self.key, time.time(), after)
        self._compact_ctx_before = None

    def _expire_internal_turn(self, done: threading.Event) -> None:
        with self._internal_lock:
            if done is not self._internal_turn_done or not self._internal_turn or done.is_set():
                return
            log.error("internal command timed out after %.0fs", INTERNAL_COMMAND_TIMEOUT)
            self._internal_timed_out = True
            if self._transcript and self._transcript.exists():
                self._tpos = self._transcript.stat().st_size
            try:
                if self._session is not None:
                    try:
                        self._session.close()
                    except Exception as exc:
                        log.error("could not close timed-out lane session: %s", type(exc).__name__)
                alive_check = getattr(self._session, "alive", None) if self._session is not None else None
                alive = bool(alive_check() if callable(alive_check) else alive_check)
                if alive:
                    log.error("timed-out lane session is still alive; holding inbound injection")
                    self._stop.set()
                    return
                if self._on_parked is not None:
                    try:
                        self._on_parked(self.key)
                    except Exception as exc:
                        log.error("could not persist timed-out lane as parked: %s", type(exc).__name__)
                self._stop.set()
                # A timeout is not a completed compaction: clear turn bookkeeping without
                # persisting last_compact/context as a successful result.
                self._turn_active.clear()
                self._turn_from_tg = False
                self._internal_turn = False
                self._internal_deadline = None
            finally:
                # Release waiters even when close/check/callback fails. _internal_timed_out
                # keeps the inbound worker from injecting into an uncertain session.
                done.set()

    # Ported from agent2telegram attach.py::_limit_inbound_prompt (MIT).
    def _limit_inbound_prompt(self, text: str, chat_id: int) -> str | None:
        if len(text) <= MAX_INBOUND_PROMPT_CHARS:
            return text
        # Delta (b): notice is sent to this lane's topic through LaneTelegram.
        self.tg.send_message(chat_id,
            f"⚠️ That message is too long after processing ({len(text)} characters). "
            f"Please shorten it to {MAX_INBOUND_PROMPT_CHARS} characters or less.")
        log.warning("inbound prompt too long: %d characters", len(text))
        return None

    # Ported from agent2telegram attach.py:1808 (MIT).
    def _inject(self, text: str) -> bool:
        # Delta (a): Claude pin-probe arming/clearing is omitted because lane identity is assigned.
        self._turn_active.set()
        self._last_activity = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(1, INJECT_ATTEMPTS + 1):
            try:
                self._session.inject(text)
                if attempt > 1:
                    log.info("inject succeeded on attempt %d", attempt)
                return True
            except SessionError as e:
                last_error = e
                if "refusing to inject" in str(e):
                    log.error("inject failed: error=%s %s", type(e).__name__, message_metadata(text))
                    self._turn_active.clear()
                    self._defer_or_notify_inject_failure("unsafe", text, str(e))
                    return False
            except Exception as e:
                last_error = e
            if attempt < INJECT_ATTEMPTS:
                self._stop.wait(INJECT_RETRY_WAIT)
        log.error("inject failed after %d attempts: error=%s %s", INJECT_ATTEMPTS,
                  type(last_error).__name__, message_metadata(text))
        self._turn_active.clear()
        self._defer_or_notify_inject_failure("generic", text, str(last_error))
        return False

    def _defer_or_notify_inject_failure(self, kind: str, text: str, reason: str) -> None:
        record_id = getattr(self, "_inbound_active_record_id", None)
        if record_id:
            notices = getattr(self, "_inbound_failure_notices", None)
            if notices is None:
                notices = self._inbound_failure_notices = {}
            notices[record_id] = (kind, text, reason)
        elif kind == "unsafe":
            self._notify_unsafe_pane(reason)
        else:
            self._notify_inject_failed(text, reason)

    # Ported from agent2telegram attach.py:1853 (MIT).
    def _notify_inject_failed(self, text: str, reason: str) -> None:
        # Delta (b): lane chat guard; LaneTelegram adds message_thread_id.
        if not self.key.chat_id:
            return
        preview = text.strip().replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:80] + "…"
        if BLOCKED_BY_OVERLAY in reason:
            body = ("⚠️ Couldn't deliver your message — the session panel has an interactive dialog "
                    "open (for example a Claude Code prompt) and Escape could not close it, so "
                    f"nothing was typed after {INJECT_ATTEMPTS} attempts.\n\n"
                    f"Message: {preview}\n{reason}\n\n"
                    "Please clear the dialog in the tmux pane yourself, then send it again.")
        else:
            body = ("⚠️ Couldn't deliver your message to the agent session — the write to tmux "
                    f"failed after {INJECT_ATTEMPTS} attempts.\n\n"
                    f"Message: {preview}\n{reason}\n\n"
                    "The agent may be frozen. Check the tmux pane, then send it again.")
        try:
            self.tg.send_message(self.key.chat_id, body)
            log.info("inject failure reported to the owner %s", message_metadata(text))
        except Exception as e:
            log.warning("inject-failure notification failed: error=%s %s",
                        type(e).__name__, message_metadata(text))

    # Ported from agent2telegram attach.py:1889 (MIT).
    def _notify_unsafe_pane(self, reason: str) -> None:
        # Delta (b): lane chat guard; LaneTelegram adds message_thread_id.
        if not self.key.chat_id:
            return
        now = time.monotonic()
        if now - self._last_pane_warning < 60:
            return
        self._last_pane_warning = now
        try:
            self.tg.send_message(self.key.chat_id,
                "⚠️ agent2telegramtopic blocked a Telegram message because the configured tmux "
                "session no longer appears to be running the expected agent.\n\n"
                f"{reason}\n\nRestart the agent in that tmux pane, then resend the message.")
        except Exception as e:
            log.warning("unsafe-pane owner notification failed: error=%s", type(e).__name__)

    # Ported from agent2telegram/attach.py::_begin_turn (MIT). Deltas: per-lane turn-end path,
    # LaneTelegram, and step-6 transcript identity.
    def _begin_turn(self) -> None:
        if self._turn_active.is_set():
            # Delta (10.2): Telegram may arrive during compaction, but the active internal
            # turn remains private until its own completion marker is observed.
            if self._internal_turn:
                self._turn_from_tg = True
                return
            log.info("TURN CONTINUE t=%.2f (Telegram steering while active)", time.time())
            return
        self._consume_turn_end()
        now = time.monotonic()
        self._turn_active.set()
        self._turn_from_tg = True
        self._last_activity = now
        if self._lane_store is not None:
            self._lane_store.set_busy(self.key, True)
            self._lane_store.touch(self.key, time.time())
        self._turn_started = now
        self._turn_started_wall = time.time()
        self._turn_begun_at = now
        self._turn_error = ""
        self._typing_count = 1
        self._last_typing = now
        self._turn_text_sent = False
        self._turn_is_reaction = False           # set by the reaction branch right after this
        self._max_gap = 0.0
        self._turn_tpos = self._tpos
        self._pending_turn_end = False
        # Seed the TUI dedup with tool lines ALREADY on screen from previous turns, so the
        # scraper only emits calls that appear DURING this turn — otherwise stale lines still
        # visible in the pane get re-sent as bubbles under the new turn.
        if self.agent == "codex" and self._session is not None:
            try:
                self._tui_seen = set(_extract_tui_tools(self._session._capture()))
            except Exception:
                self._tui_seen = set()
        else:
            self._tui_seen = set()
        self.tg.send_chat_action(self._owner_chat, "typing")
        log.info("TURN START t=%.2f", time.time())

    # Ported from agent2telegram/attach.py::_busy_for_notice (MIT).
    def _busy_for_notice(self) -> bool:
        """Display-only confidence: suppress typing after unhooked silence."""
        if not self._turn_active.is_set():
            return False
        if getattr(self, "_claude_hook_ready", True):
            return True
        return time.monotonic() - max(self._last_activity, self._turn_started) < IDLE_DONE

    # Ported from agent2telegram/attach.py::_typing_loop (MIT). Delta (10.2): internal turns
    # suppress Telegram typing. Delta (a): typing uses
    # LaneTelegram, which scopes the action to this lane's message_thread_id. Task-required
    # change: catch send errors and continue the typing loop, as required by step 7c.
    def _typing_loop(self) -> None:
        while not self._stop.is_set():
            if (not self._internal_turn and self._busy_for_notice() and self._turn_from_tg
                    and self._owner_chat is not None):
                now = time.monotonic()
                gap = now - self._last_typing
                if gap > self._max_gap:
                    self._max_gap = gap
                try:
                    self.tg.send_chat_action(self._owner_chat, "typing")
                    self._last_typing = now
                    self._typing_count += 1
                except Exception as e:
                    log.warning("typing action failed: %s", e)
            self._stop.wait(TYPING_INTERVAL)

    # Ported from agent2telegram attach.py:_tui_scrape_loop (MIT).
    def _tui_scrape_loop(self) -> None:
        """Codex only: scrape the tmux pane for live tool/web-search lines → status bubbles, so
        Codex (whose rollout logs tools only at completion) shows them live like Claude Code."""
        while not self._stop.is_set():
            if self._turn_active.is_set() and self._turn_from_tg and self._owner_chat is not None:
                # Hold bubbles until the intro text is forwarded (so the bubble doesn't jump ahead
                # of "I'll search the web…"), then release; after a short grace show them anyway.
                ready = self._turn_text_sent or \
                    (time.monotonic() - self._turn_started) >= TUI_BUBBLE_GRACE
                if ready:
                    try:
                        for summary in _extract_tui_tools(self._session._capture()):
                            if summary not in self._tui_seen:
                                self._tui_seen.add(summary)
                                self._status_push(summary)
                    except Exception as e:
                        log.debug("tui scrape: %s", e)
            self._stop.wait(1.0)

    # Ported from agent2telegram/attach.py::_drain_transcript (MIT). Delta: transcript is the
    # step-6 lane identity.
    def _drain_transcript(self) -> None:
        # Delta: the selected transcript is this lane's identity from _lane_transcript.
        if not self._transcript or not self._transcript.exists():
            return
        size = self._transcript.stat().st_size
        if size < self._tpos:
            self._tpos = 0
        if size == self._tpos:
            return
        with open(self._transcript, "rb") as f:
            f.seek(self._tpos)
            chunk = f.read()
        nl = chunk.rfind(b"\n")
        if nl == -1:
            return
        consumed = 0
        try:
            for raw in chunk[:nl].split(b"\n"):
                rec_off = self._tpos + consumed
                consumed += len(raw) + 1
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._internal_turn and self.agent == "codex":
                    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                    # Codex's rollout compacted record has no turn_id. Its task_started
                    # event immediately preceding it identifies the internal turn.
                    if payload.get("type") == "task_started":
                        self._internal_turn_id = payload.get("turn_id")
                    if (rec.get("type") == "compacted" and self._internal_turn_id is not None):
                        self._internal_compacted_turn_id = self._internal_turn_id
                    if (payload.get("type") == "task_complete"
                            and self._internal_compacted_turn_id is not None
                            and payload.get("turn_id") == self._internal_compacted_turn_id):
                        self._tpos += consumed
                        self._finish_turn()
                        return
                    # Compaction transcript records are private until its matching completion.
                    continue
                self._rec_live = self._record_is_live(rec_off, rec)
                for ev in self._reader.parse(rec):
                    self._handle_event(ev)
                    # Delta (10.2): stop at a Codex completion boundary. The next tick
                    # finishes this turn before reading any records from the next one.
                    if self._internal_turn and self.agent == "codex" and ev.kind == "turn_end":
                        self._tpos += consumed
                        self._finish_turn()
                        return
        finally:
            self._rec_live = False
        self._tpos += nl + 1
        if self._internal_turn and self.agent == "claude-code" and self._compact_done.exists():
            self._finish_turn()
        if self._turn_from_tg:
            self._last_activity = time.monotonic()

    # Ported from agent2telegram/attach.py::_handle_event (MIT). Deltas: per-lane Telegram delivery
    # and step-6 transcript identity.
    def _handle_event(self, ev) -> None:
        if ev.kind == "user":
            if self._is_tg_origin(ev.text):
                self._turn_from_tg = True
            elif not self._turn_active.is_set():
                self._turn_from_tg = False
            return
        if ev.kind == "turn_start":
            if not self._turn_active.is_set():
                self._turn_from_tg = False
            return
        if ev.kind == "turn_end":
            if not self._internal_turn:
                self._pending_turn_end = True
            return
        # Delta (10.2): lifecycle output, including errors, is private to the lane.
        if self._internal_turn:
            return
        if self._owner_chat is None:
            return
        if ev.kind == "error" and self._turn_from_tg:
            self._report_agent_error(ev)
            return
        if ev.kind == "tool" and not self._turn_from_tg and self._rec_live:
            # Live tool call of a turn NOT started from Telegram (opt-in background progress).
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text, background=True)
            return
        if not self._turn_from_tg:
            if ev.kind == "text" and self._has_marker(ev.text):
                log.info("text carries [TG] outside a Telegram turn → forwarded anyway")
            else:
                if ev.kind == "text" and ev.text.strip() and ev.key not in self._sent_keys:
                    log.info("text NOT forwarded (turn not from Telegram) id=%s %s", ev.key,
                             message_metadata(ev.text))
                return
        if ev.kind == "text":
            out = self._strip_marker(ev.text)
            if out and ev.key not in self._sent_keys:
                # A new progress message → delete the current technical bubble so the next tool
                # calls re-create it BELOW this message (the bubble always trails at the bottom).
                self._status_clear()
                self._send_final(out, key=ev.key)
        elif ev.kind == "files":
            if ev.key in self._seen_tools:
                return
            self._seen_tools.add(ev.key)
            self._send_files(list(ev.files))
        elif ev.kind == "tool":
            if self.agent == "codex":
                return                            # Codex tools come live from the TUI scraper
            if ev.key and ev.key not in self._seen_tools:
                self._seen_tools.add(ev.key)
                self._status_push(ev.text)

    # Ported from agent2telegram attach.py (MIT).
    # Delta (a): _send_final delivers through this lane's durable outbox and topic-bound adapter;
    # the lane's tmux session name is supplied as its cfg-compatible identity.
    # Delta (9.7): the live lane's outbound and typing loops deliver replies and refresh typing.
    def _report_agent_error(self, ev) -> None:
        """Tell the owner the agent refused this turn, in the agent's own words.

        Counts as the turn's reply (``_send_final`` sets ``_turn_text_sent``), which is the
        point: a refused turn now ends with the TRUE reason instead of the backstop reaching
        into the transcript for something that reads like an answer.
        """
        who = getattr(self.cfg, "tmux_session", "") or getattr(self.cfg, "agent", "the agent")
        detail = (ev.text or "").strip()
        if ev.code == readers.USAGE_LIMIT:
            head = f"⛔ Usage limit reached — '{who}' did not answer your message."
            tail = "Send it again once the limit resets."
        else:
            head = f"⚠️ '{who}' reported an error instead of answering:"
            tail = "If no answer follows, send your message again."
        self._turn_error = ev.code or "error"
        log.warning("agent refused the turn (%s): %r", self._turn_error, detail[:120])
        self._send_final("\n\n".join(p for p in (head, detail, tail) if p),
                         key=ev.key or None)

    # Ported from agent2telegram/attach.py::_has_marker (MIT); marker is lane-configured.
    def _has_marker(self, text: str) -> bool:
        marker = (self._marker or "").lower()
        if not marker:
            return False
        for line in text.splitlines():
            s = line.strip()
            if s:
                return s.lower().startswith(marker)
        return False

    # Ported from agent2telegram attach.py::_is_tg_origin (MIT).
    def _is_tg_origin(self, text: str) -> bool:
        return any(origin in text for origin in self._origins)

    # Ported from agent2telegram/attach.py::_strip_marker (MIT); marker is lane-configured.
    def _strip_marker(self, text: str) -> str:
        marker = self._marker.lower()
        lines = text.splitlines()
        for i, line in enumerate(lines):
            s = line.lstrip()
            if s.lower().startswith(marker):
                lines[i] = s[len(self._marker):].lstrip()
        return "\n".join(lines).strip()

    # Ported from agent2telegram/attach.py::_last_assistant_text (MIT). Delta: lane transcript.
    def _last_assistant_text(self) -> str | None:
        if not self._transcript:
            return None
        last = None
        last_key = None
        try:
            size = self._transcript.stat().st_size
            start = int(getattr(self, "_turn_tpos", 0) or 0)
            if start > size:
                start = 0
            with open(self._transcript, "rb") as f:
                f.seek(max(0, size - 2_000_000, start))
                tail = f.read()
        except OSError:
            return None
        floor = getattr(self, "_turn_started_wall", 0.0) - BACKSTOP_TURN_GRACE
        sent = getattr(self, "_sent_keys", set())
        for raw in tail.split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", "ignore"))
            except (ValueError, json.JSONDecodeError):
                continue
            if floor > 0 and not _written_after(rec, floor):
                continue
            try:
                for ev in self._reader.parse(rec):
                    if ev.kind == "text" and ev.text and ev.text.strip():
                        if ev.key and ev.key in sent:
                            continue
                        last, last_key = ev.text, ev.key
            except Exception:
                continue
        self._last_backstop_key = last_key
        return last

    # Ported from agent2telegram/attach.py::_finish_turn (MIT). Deltas: lane-scoped signal/turn-end
    # paths, LaneTelegram, and the step-7a empty-turn notice.
    # Delta (10.2): internal completion signals do not produce an empty-turn notice.
    def _finish_turn(self) -> None:
        # Completion and marker cleanup are one atomic transition with respect to
        # timeout and the inbound worker.
        with self._internal_lock:
            self._finish_turn_locked()

    def _finish_turn_locked(self) -> None:
        """Drop the technical bubble and stop the typing indicator at the real end of a turn."""
        self._status_clear()
        was_active = self._turn_active.is_set()
        internal = self._internal_turn
        from_tg_at_end = self._turn_from_tg
        text_sent_at_end = self._turn_text_sent
        reaction_at_end = self._turn_is_reaction
        if (not internal and was_active and self._turn_from_tg and not self._turn_text_sent
                and not self._turn_is_reaction
                and self._owner_chat is not None and getattr(self, "_turn_end_backstop_enabled", True)):
            source = "transcript"
            out = ""
            if self._has_turn_end_backstop_source():
                answer = self._retry_last_assistant_text()
                out = self._strip_marker(answer) if answer else ""
                if not out and not self._turn_text_sent:
                    source = "signal"
                    answer = self._consume_signal_text()
                    out = self._strip_marker(answer) if answer else ""
            if out and not self._turn_text_sent:
                self._send_final(out, key=getattr(self, "_last_backstop_key", "") or None)
                log.info("TURN END backstop → forwarded final answer from %s %s",
                         source, message_metadata(out))
            elif not self._turn_text_sent:
                dur = time.monotonic() - self._turn_started
                log.error("TURN END backstop: Telegram turn ended without an answer; "
                          "dur=%.1fs typing_count=%d transcript_configured=%s signal_configured=%s",
                          dur, self._typing_count, self._transcript is not None,
                          self._signal is not None)
                # SPEC v0.4 step 7 requires one empty-turn notice; do not mark that notice as
                # the agent's answer, so a later turn-end retry cannot mistake it for one.
                self._send_final(
                    "⚠️ The agent ended this turn without an answer. Please send your message again.",
                    turn_text=False)
        elif (not internal and was_active and self._turn_from_tg and not self._turn_text_sent
                and self._owner_chat is not None
                and not self._turn_is_reaction):
            log.error("TURN END: Telegram turn ended unanswered and the backstop did not run (backstop_enabled=%s)",
                      getattr(self, "_turn_end_backstop_enabled", True))
        elif (not internal and was_active and self._turn_is_reaction
                and not self._turn_text_sent):
            # Intended silence, but it must still be visible in the log — otherwise a genuinely
            # lost reply would look exactly the same in the daily traffic analysis.
            log.info("TURN END reaction turn without a reply (backstop deliberately skipped)")
        self._turn_active.clear()
        if self._lane_store is not None:
            self._lane_store.set_busy(self.key, False)
            self._lane_store.touch(self.key, time.time())
            if not internal:
                tokens = self._internal_context_tokens()
                if tokens is not None:
                    self._lane_store.set_ctx_tokens(self.key, tokens)
        if internal:
            self._record_internal_compaction()
            # Delta (10.10): discard the entire internal-command tail, including a Codex
            # task_complete that follows its compacted record, before queued prompts resume.
            if self._transcript and self._transcript.exists():
                self._tpos = self._transcript.stat().st_size
            self._internal_turn = False
            self._internal_turn_id = None
            self._internal_compacted_turn_id = None
        self._pending_turn_end = False
        self._consume_turn_end()
        if was_active:
            log.info("TURN END t=%.2f dur=%.1fs typing_fired=%d max_gap=%.2fs "
                     "from_tg=%s text_sent=%s reaction=%s agent_error=%s",
                     time.time(), time.monotonic() - self._turn_started,
                     self._typing_count, self._max_gap, from_tg_at_end, text_sent_at_end,
                     reaction_at_end, getattr(self, "_turn_error", "") or "-")
        if internal:
            self._internal_turn_done.set()
            # Updates accepted during a park compaction remain in the durable inbox for
            # the replacement bridge; replaying here could inject into the dying session.
            if not self._closing:
                self._replay_pending_inbound()

    def stop(self) -> None:
        """Stop this bridge's background workers before its session is retired."""
        self._stop.set()
        self._internal_turn_done.set()

    # Ported from agent2telegram/attach.py::_consume_turn_end (MIT); delta: lane-scoped path.
    def _consume_turn_end(self) -> None:
        try:
            self._turn_end.unlink()
        except OSError:
            pass

    # Ported from agent2telegram/attach.py::_consume_signal_text (MIT); delta: lane-scoped path.
    def _consume_signal_text(self) -> str | None:
        signal = self._signal
        if not signal or not signal.exists():
            return None
        try:
            answer = signal.read_text("utf-8").strip()
            signal.unlink()
        except OSError:
            return None
        return answer or None

    # ---- background progress (opt-in, Claude Code) ------------------------------------
    # Ported from agent2telegram attach.py:_background_enabled (MIT).
    def _background_enabled(self) -> bool:
        cfg = getattr(self, "cfg", None)
        return (getattr(cfg, "background_progress", False) is True
                and getattr(cfg, "agent", "") == "claude-code")

    # Ported from agent2telegram attach.py:_arm_background (MIT).
    def _arm_background(self) -> None:
        """Remember where the transcript ends NOW. Records up to here are history (a startup
        rewind or a freshly adopted transcript replays them) and never produce a bubble."""
        t = getattr(self, "_transcript", None)
        try:
            size = t.stat().st_size if t is not None else 0
        except OSError:
            size = 0
        self._bg_arm = (t, size)

    # Ported from agent2telegram attach.py:_record_is_live (MIT).
    def _record_is_live(self, offset: int, rec: dict) -> bool:
        """Is this record live work worth a background bubble? Appended after the bridge armed
        (not a replay) AND stamped recently (not an old record in a re-read file)."""
        if not self._background_enabled() or self._bg_arm is None:
            return False
        path, size = self._bg_arm
        if path != getattr(self, "_transcript", None) or offset < size:
            return False
        ts = _record_wall_time(rec)
        return ts is not None and abs(time.time() - ts) <= BACKGROUND_LIVE_WINDOW

    # Ported from agent2telegram attach.py:_expire_background_status (MIT).
    def _expire_background_status(self) -> None:
        """Safety net for a background bubble whose turn end was never signalled."""
        if not self._bg_status or self._turn_active.is_set():
            return
        limit = float(getattr(self.cfg, "background_stale_after", 300.0) or 300.0)
        if time.monotonic() - self._bg_status_last > limit:
            log.info("background status bubble silent for %.0f s → removed", limit)
            self._status_clear()

    # Ported from agent2telegram attach.py:_start_background (MIT).
    def _start_background(self) -> None:
        """Remove an orphaned sub-agent progress message and, if enabled, start the watcher."""
        watcher = SubagentWatcher(
            self.tg, self._owner_chat,
            transcript=lambda: self._transcript,
            state_path=self._bg_state_path,
            language=getattr(self.cfg, "language", "en"),
            edit_interval=getattr(self.cfg, "background_edit_interval", 10.0),
            stale_after=getattr(self.cfg, "background_stale_after", 300.0))
        watcher.cleanup_orphan()             # even when disabled: a leftover from an earlier run
        if not self._background_enabled():
            return
        self._bg_watcher = watcher
        threading.Thread(target=self._background_loop, daemon=True).start()
        log.info("background progress on (sub-agents + non-Telegram turns)")

    # Ported from agent2telegram attach.py:_background_loop (MIT).
    def _background_loop(self) -> None:
        """Own thread: a flood-control wait on an edit must never stall reply forwarding."""
        while not self._stop.is_set():
            try:
                self._bg_watcher.tick()
            except Exception as e:
                log.error("background progress error: %s", e)
            self._stop.wait(BACKGROUND_TICK)

    # ---- live tool-call status bubble (shown during the turn, deleted at the end) ------
    # Ported from agent2telegram attach.py:_status_push (MIT).
    def _status_push(self, line: str, *, background: bool = False) -> None:
        # Single line, emoji at the start, rendered in italics. One bubble is edited in place
        # across a run of consecutive tool calls; it's deleted when the next progress message
        # arrives (then re-created below it) and at turn end — so it always trails at the bottom.
        if background:
            # A turn not started from Telegram (background-task notification, terminal). Only
            # live records get here (see _record_is_live), and such a bubble has two ways out:
            # the Stop-hook turn end, or _expire_background_status after a silence.
            self._bg_status = True
            self._bg_status_last = time.monotonic()
        if self._owner_chat is None or not line or line == self._status["shown"]:
            return
        # No live turn → no bubble. The bubble is technical progress and is deleted at turn end;
        # one created outside a turn has nothing to delete it and hangs in the chat until the NEXT
        # turn happens to finish. Seen in practice: "Editing MEMORY.md" stuck for eight
        # minutes after a bridge restart drained the transcript with no turn running (2026-08-02).
        # The background path is the one exception, and it is guarded against exactly that replay.
        if not self._turn_active.is_set() and not background:
            return
        body = f"<i>{html.escape(line)}</i>"
        if self._status["mid"] is None:
            mid = self.tg.send_plain_id(self._owner_chat, body, parse_mode="HTML")
            if mid:
                self._status["mid"] = mid
                self._status["shown"] = line
                self._persist_status(mid)
        else:
            self.tg.edit_plain(self._owner_chat, self._status["mid"], body, parse_mode="HTML")
            self._status["shown"] = line

    # Ported from agent2telegram attach.py:_status_clear (MIT).
    def _status_clear(self) -> None:
        if self._status["mid"] is not None and self._owner_chat is not None:
            try:
                self.tg.delete_message(self._owner_chat, self._status["mid"])
            except Exception as e:
                log.warning("status bubble cleanup failed: %s", e)
        self._status = {"mid": None, "shown": ""}
        self._seen_tools.clear()
        self._bg_status = False
        self._persist_status(None)

    # Ported from agent2telegram attach.py:_persist_status (MIT).
    def _persist_status(self, mid: int | None) -> None:
        if self._status_path is None:
            return
        try:
            if mid is None:
                self._status_path.unlink()
            else:
                self._status_path.parent.mkdir(parents=True, exist_ok=True)
                self._status_path.write_text(str(mid), "utf-8")
        except OSError:
            pass

    # Ported from agent2telegram attach.py:_cleanup_orphan_status (MIT).
    def _cleanup_orphan_status(self) -> None:
        """Delete a status bubble left over from a previous run that died mid-turn."""
        if self._status_path is None or self._owner_chat is None:
            return
        try:
            mid = int(self._status_path.read_text("utf-8").strip())
        except (OSError, ValueError):
            return
        self.tg.delete_message(self._owner_chat, mid)
        try:
            self._status_path.unlink()
        except OSError:
            pass

    # Ported from agent2telegram/attach.py::_drain_signal (MIT); deltas: lane-scoped signal
    # file and LaneTelegram delivery.
    def _drain_signal(self) -> None:
        answer = self._consume_signal_text()
        if not answer:
            return
        if answer and self._owner_chat is not None:
            self._status_clear()                         # final message → drop the technical bubble
            self._send_final(answer)
            self._turn_active.clear()

    # Ported from agent2telegram/attach.py::_wait_backstop_retry (MIT).
    def _wait_backstop_retry(self) -> None:
        stop = getattr(self, "_stop", None)
        if stop is not None:
            stop.wait(BACKSTOP_RETRY_DELAY)
        else:
            time.sleep(BACKSTOP_RETRY_DELAY)

    # Ported from agent2telegram/attach.py::_has_turn_end_backstop_source (MIT); lane paths.
    def _has_turn_end_backstop_source(self) -> bool:
        return (getattr(self, "_transcript", None) is not None
                or getattr(self, "_signal", None) is not None)

    # Ported from agent2telegram/attach.py::_retry_last_assistant_text (MIT).
    def _retry_last_assistant_text(self) -> str | None:
        last = self._last_assistant_text()
        if last and last.strip():
            return last
        for attempt in range(BACKSTOP_RETRY_ATTEMPTS):
            if self._turn_text_sent:
                return None
            try:
                self._drain_transcript()
            except Exception as e:
                log.debug("turn-end backstop transcript drain failed: %s", e)
            if self._turn_text_sent:
                return None
            last = self._last_assistant_text()
            if last and last.strip():
                return last
            if attempt != BACKSTOP_RETRY_ATTEMPTS - 1:
                self._wait_backstop_retry()
        return None

    def _lane_transcript(self) -> Path | None:
        if self.agent == "claude-code":
            # Delta (b): resolve Claude identity from this lane's marker, then its initial id.
            try:
                marker = json.loads(self.session_marker.read_text("utf-8")) if self.session_marker else {}
                path = Path(marker["transcript_path"]).expanduser()
                if path.is_file():
                    self._resolved_session_id = marker.get("session_id")
                    return path
            except (OSError, ValueError, KeyError, TypeError):
                pass
            sid = self.session_id
            if sid and self.lane_cwd is not None:
                path = transcript.claude_transcript_path(sid, self.lane_cwd)
                if path.is_file():
                    self._resolved_session_id = sid
                    return path
            self._resolved_session_id = None
            return None
        if self.agent == "codex" and self.lane_cwd is not None:
            path = transcript.find_codex_rollout(self.codex_sessions_root, self.lane_cwd, 0.0)
            self._resolved_session_id = transcript.codex_rollout_id(path) if path else None
            return path
        self._resolved_session_id = None
        return None

    # Ported from agent2telegram attach.py (MIT).
    def _maybe_reresolve(self) -> None:
        """Keep the tailed transcript pointed at our tmux session's own log (auto mode only).

        Agents write the transcript on the first message (not at launch), and a session restart
        starts a new one — so we re-check periodically. We switch when a better match appears, but
        never abandon a transcript we're already on for an in-flight turn. A no-op when the config
        gives an explicit transcript path (the path resolves to itself)."""
        # Step-6 (a): remove explicit-path and Claude repin branches; all agents use throttling.
        now = time.monotonic()
        if now - self._last_resolve < 3.0:
            return
        self._last_resolve = now
        newest = self._lane_transcript()
        if not newest:
            if self.agent == "codex" and self._transcript is not None:
                # Delta (b): rollout matching is scoped to this lane cwd, not an a2t tmux pane.
                log.error("no rollout is proven for lane cwd %r; refusing to tail stale transcript",
                          self.lane_cwd)
                self._transcript = None
                self._tpos = 0
            return
        if newest == self._transcript:
            return
        # Delta (e): keep the id read with the selected identity, avoiding a second marker read.
        # Delta (9.14): persist an adopted Codex rollout for the next lane launch.
        if self.agent == "codex" and self._resolved_session_id:
            marker = self.session_marker
            if marker is not None and self._resolved_session_id != self.session_id:
                try:
                    try:
                        marker_data = json.loads(marker.read_text("utf-8"))
                        if not isinstance(marker_data, dict):
                            marker_data = {}
                    except (OSError, ValueError):
                        marker_data = {}
                    marker_data["session_id"] = self._resolved_session_id
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        "w", encoding="utf-8", dir=marker.parent,
                        prefix=f".{marker.name}.", delete=False,
                    ) as temp:
                        temp.write(json.dumps(marker_data) + "\n")
                        temp_path = Path(temp.name)
                    os.replace(temp_path, marker)
                except OSError as exc:
                    log.error("could not record adopted Codex rollout for lane %s: %s", self.key, exc)
                    # Leave the transcript and session identity untouched so the next poll retries
                    # adoption instead of considering this rollout already resolved.
                    return
        log.info("transcript → %s", newest.name)
        self._transcript = newest
        self.session_id = self._resolved_session_id
        # Step-6 (c): lane cwd is always known, so the a2t unknown-cwd mid-turn guard is dropped.
        self._seek_to_turn()
        # _seek_to_turn finds the current turn by record timestamp. When it finds none (it left
        # the cursor at the end) but a turn IS in flight, the rollout may already hold that turn
        # (e.g. the first reply after /new): rewind to the latest user boundary, as on startup.
        try:
            at_end = self._tpos >= newest.stat().st_size
        except OSError:
            at_end = False
        if at_end and self._turn_active.is_set():
            self._resume_position()
            self._turn_tpos = self._tpos
        self._arm_background()

    def _prepare_start(self) -> None:
        """Resolve the lane transcript and recover an unfinished turn before polling it."""
        self._maybe_reresolve()
        if self._sent_keys and self._transcript is not None:
            self._resume_position()
        self._arm_background()              # everything already in the transcript is history
        self._cleanup_orphan_status()       # remove a bubble orphaned by a prior crash/restart
        self._start_background()

    # Ported from agent2telegram/attach.py::_seek_to_turn (MIT); delta: step-6 lane transcript
    # identity supplies the selected file, while startup positioning remains in step 6.
    def _seek_to_turn(self) -> None:
        """Position the cursor in a freshly adopted transcript so that only the CURRENT turn is
        read from it. Everything already in the file is history — a resumed session replays its
        whole past, and starting at byte 0 forwarded 22 old messages as one answer (2026-09-04).

        Outside a turn: start at the end. Inside a turn: start at the first record stamped at or
        after the turn began (records carry a wall-clock ``timestamp``); if nothing in the file is
        that new yet, start at the end and let the drain pick the answer up as it is written.
        """
        try:
            size = self._transcript.stat().st_size
        except OSError:
            size = 0
        pos = size
        if self._turn_active.is_set():
            t0 = float(getattr(self, "_turn_started_wall", 0.0) or 0.0) - 2.0
            start = max(0, size - 5_000_000)
            try:
                with open(self._transcript, "rb") as f:
                    f.seek(start)
                    tail = f.read()
            except OSError:
                tail = b""
            cur = start
            for raw in tail.split(b"\n"):
                line_end = cur + len(raw) + 1
                ts = self._record_epoch(raw)
                if ts is not None and ts >= t0:
                    pos = cur
                    break
                cur = line_end
        self._tpos = pos
        self._turn_tpos = pos

    @staticmethod
    def _record_epoch(raw: bytes) -> float | None:
        """Wall-clock time of a transcript record (Codex and Claude Code both write an ISO
        ``timestamp``), or None when the line has none."""
        try:
            rec = json.loads(raw.decode("utf-8", "ignore"))
        except (ValueError, json.JSONDecodeError):
            return None
        ts = rec.get("timestamp") if isinstance(rec, dict) else None
        if not isinstance(ts, str) or not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    # Ported from agent2telegram/attach.py::_resume_position (MIT); delta: step-6 lane reader and
    # transcript identity recover this lane's Telegram turn from its selected transcript.
    def _resume_position(self) -> None:
        """Find the most recent non-empty user message and rewind ``_tpos`` to just after it,
        so the current turn's assistant messages are re-read on startup. Combined with the persisted ledger
        this re-delivers a reply that was written while we were down, without re-sending anything
        already delivered. Also recovers the turn's Telegram origin."""
        size = self._tpos
        start = max(0, size - 5_000_000)        # large window: tool outputs can be big
        try:
            with open(self._transcript, "rb") as f:
                f.seek(start)
                tail = f.read()
        except OSError:
            return
        pos = start
        last_user_end = None
        from_tg = self._turn_from_tg
        for raw in tail.split(b"\n"):
            line_end = pos + len(raw) + 1       # +1 for the newline separator
            pos = line_end
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (json.JSONDecodeError, ValueError):
                continue
            utext = self._reader.user_text(rec)
            if utext and utext.strip():
                from_tg = utext.lstrip().startswith(self._origins)
                last_user_end = min(line_end, size)
        if last_user_end is not None:
            self._tpos = last_user_end
            self._turn_from_tg = from_tg

    # Ported from agent2telegram/attach.py::_outbound_loop (MIT). One pass is
    # exposed as outbound_tick; its wait remains in the loop wrapper.
    # Delta (d): transcript selection is the step-6 lane identity via _maybe_reresolve.
    # The inbound retry/replay block belongs to step 8.
    # Delta (10.2): finish an internal turn at its completion marker before reading later records.
    def outbound_tick(self) -> None:
        try:
            self._maybe_reresolve()
            self._flush_pending()
            self._drain_transcript()
            if self._internal_turn:
                self._consume_signal_text()
            else:
                self._drain_signal()
            if self._internal_turn and self.agent == "claude-code" and self._compact_done.exists():
                self._finish_turn()
            elif self._pending_turn_end:
                if self._turn_begun_at and time.monotonic() - self._turn_begun_at < SUSPICIOUS_TURN_SECONDS:
                    log.warning("turn ending after %.1fs — too fast for real work; either a "
                                "stale end-of-turn signal or the agent refused the turn "
                                "(see any agent_error on the TURN END line)",
                                time.monotonic() - self._turn_begun_at)
                self._finish_turn()
            elif (not self._internal_turn and self._turn_end is not None
                  and self._turn_end.exists()):
                self._claude_hook_ready = True
                self._end_turn()
            elif (not self._internal_turn and self._turn_active.is_set()
                  and time.monotonic() - self._last_activity > self._idle_limit()):
                log.warning("turn ended after inactivity (%.0f s without activity); no hook signal",
                            self._idle_limit())
                if self.agent == "claude-code":
                    self._claude_hook_ready = False
                self._end_turn()
            # Live retry: messages that failed to deliver are retried while running too.
            # Replay only at startup meant, for a long-running service, waiting forever.
            # An outbound-only startup recovery can run before a parked lane gets a
            # tmux session. Keep inbox replay dormant until the lane is activated.
            if (not getattr(self, "_defer_inbound_replay", False)
                    and time.monotonic() - getattr(self, "_last_inbound_retry", 0.0) > INBOUND_RETRY_INTERVAL):
                self._last_inbound_retry = time.monotonic()
                self._replay_pending_inbound()
            self._expire_background_status()
            self._beat()
        except Exception as e:
            log.error("outbound error: %s", e)

    # Ported from agent2telegram attach.py::_end_turn (MIT); transcript drain before finish.
    def _end_turn(self) -> None:
        self._drain_transcript()
        self._finish_turn()

    def _outbound_loop(self) -> None:
        while not self._stop.is_set():
            self.outbound_tick()
            self._stop.wait(OUTBOUND_TICK)

    def _beat(self) -> None:
        """Ported from a2t; touch the per-lane outbound heartbeat after a full cycle."""
        if self._heartbeat is None:
            return
        try:
            self._heartbeat.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass

    def _idle_limit(self) -> float:
        return IDLE_DONE_HOOKED if self._turn_end is not None else IDLE_DONE

    def _mark_sent(self, uuid: str) -> bool:
        """Record a delivered message uuid in memory and on disk. Returns whether the disk write
        succeeded.

        The caller relies on the return value: an OSError must leave the record queued so a
        restart cannot send the same reply again.
        """
        if not uuid:
            return True
        if uuid in self._sent_keys:
            return True
        self._sent_keys.add(uuid)
        try:
            self._sent_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._sent_path, "a", encoding="utf-8") as f:
                f.write(uuid + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except OSError as e:
            log.error("could not write the delivered-message ledger (%s) — keeping the record queued so the reply is not sent twice", e)
            return False

    # Ported from agent2telegram attach.py (MIT).
    def _load_voice_state(self) -> bool:
        try:
            return self._voice_state_path.read_text("utf-8").strip() == "on"
        except OSError:
            return False

    def _voice_reply_on(self) -> bool:
        """Voice replies follow the switch; TTS provider selection happens at send time."""
        return bool(getattr(self, "_voice_on", False))

    def _set_voice_state(self, on: bool) -> None:
        self._voice_on = on
        try:
            self._voice_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._voice_state_path.parent / (self._voice_state_path.name + ".tmp")
            tmp.write_text("on" if on else "off", encoding="utf-8")
            tmp.replace(self._voice_state_path)
        except OSError as e:
            log.warning("could not persist voice-mode state: %s", e)

    def _try_send_voice(self, text: str) -> bool:
        """Render text and send a topic-bound Telegram voice note; failures fall back to text."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log.warning("voice reply skipped: ffmpeg not on PATH")
            return False
        try:
            spoken = tts.sanitize_for_speech(text)
            audio = tts.synthesize(
                spoken, api_key=self._elevenlabs_key(), voice_id=self.cfg.tts_voice_id,
                model_id=self.cfg.tts_model_id)
        except Exception as e:
            log.warning("voice reply TTS failed: error=%s %s", type(e).__name__,
                        message_metadata(text, kind="voice"))
            return False
        tmpdir = tempfile.mkdtemp(prefix="a2tt_voice_")
        try:
            source_path = os.path.join(tmpdir, "reply.audio")
            ogg_path = os.path.join(tmpdir, "reply.ogg")
            with open(source_path, "wb") as fh:
                fh.write(audio)
            audio_filters = []
            if self.cfg.tts_speed != 1.0:
                audio_filters.append(f"atempo={self.cfg.tts_speed:g}")
            audio_filters.extend(["adelay=700", "apad=pad_dur=2", "loudnorm=I=-16:TP=-1.5:LRA=11"])
            ffmpeg_args = [ffmpeg, "-y", "-i", source_path, "-af", ",".join(audio_filters),
                           "-ar", "48000", "-ac", "1", "-c:a", "libopus", "-b:a", "48k",
                           "-application", "voip", ogg_path]
            result = subprocess.run(ffmpeg_args, capture_output=True, timeout=60)
            if result.returncode != 0 or not os.path.exists(ogg_path) or os.path.getsize(ogg_path) == 0:
                log.warning("voice reply ffmpeg conversion failed (rc=%s)", result.returncode)
                return False
            duration = _ogg_duration(ogg_path)
            log.info("voice reply converted ogg_bytes=%d duration_s=%s",
                     os.path.getsize(ogg_path), duration)
            self.tg.send_voice(self._owner_chat, ogg_path, duration=duration)
            log.info("FWD (voice) %s", message_metadata(text, kind="voice", attachments=1))
            return True
        except Exception as e:
            log.warning("voice reply send failed: error=%s %s", type(e).__name__,
                        message_metadata(text, kind="voice", attachments=1))
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _toggle_voice(self, chat_id: int) -> bool:
        new_state = not bool(getattr(self, "_voice_on", False))
        self._set_voice_state(new_state)
        self.tg.send_message(chat_id,
            "🔊 Voice replies ON — I'll answer with voice notes. Long or table-heavy replies still "
            "come as text. /voice again to turn off."
            if new_state else "🔇 Voice replies OFF — back to text.")
        return True

    def _send_final(self, text: str, key: str | None = None, *, turn_text: bool = True) -> None:
        """Forward one reply RELIABLY. Marks the dedup ledger only AFTER a confirmed send; on any
        send failure the reply is queued to disk and the outbound loop keeps retrying until Telegram
        confirms. Order is preserved: if anything is already queued, this appends behind it."""
        contains_file_marker = bool(getattr(self.cfg, "file_marker", "[tg-file]") and
                                    getattr(self.cfg, "file_marker", "[tg-file]").lower() in text.lower())
        if getattr(self.cfg, "file_marker", "[tg-file]") and \
                getattr(self.cfg, "file_marker", "[tg-file]").lower() in text.lower():
            text, extracted = self._extract_files(text)
        else:
            extracted = []
        files = extracted
        if (not text and not files) or (key and key in self._sent_keys):
            return
        if turn_text and text and self._voice_reply_on() and not contains_file_marker:
            if len(text) > VOICE_MAX_CHARS:
                text = f"{text}\n\n🗣️→📝 (too long to read aloud — sent as text)"
            elif self._try_send_voice(text):
                if key:
                    self._mark_sent(key)
                self._turn_text_sent = True
                return
            else:
                text = f"🗣️→📝 (voice unavailable — sent as text)\n{text}"
        outbox = self._ensure_outbox()
        if outbox is None:
            log.error("durable outbox unavailable; outgoing reply was not sent")
            return
        chunks = split_message(text)
        try:
            outbox.enqueue(chunks, files or [], key, chat=self.key.chat_id)
        except Exception as e:
            log.error("could not persist the outgoing reply; reply is lost: %s", e)
            return
        if turn_text:
            self._turn_text_sent = True

    def _ensure_outbox(self) -> "DurableOutbox | None":
        # The durable queue remains separate from the user-facing file outbox.
        if self._outbox is None:
            try:
                self._outbox = DurableOutbox(self._queue_root)
            except Exception as e:
                log.error("could not open the durable outbox: %s", e)
        return self._outbox

    def _flush_pending(self) -> None:
        """Deliver the queue FIFO, stopping at the first failure to preserve order. Called every
        outbound cycle, so a network hiccup self-heals within one tick."""
        outbox = self._ensure_outbox()
        while outbox is not None:
            rec = outbox.head()
            if rec is None:
                break
            # Delta (b): reject malformed/legacy unbound and foreign records; never cross lanes.
            if rec.chat != self.key.chat_id:
                log.error("outbox record %s targets a foreign chat; dead-lettering", rec.record_id)
                outbox.give_up(rec.record_id, f"foreign chat {rec.chat!r} for lane {self.key.chat_id}")
                continue
            for index, chunk in rec.pending_chunks:
                try:
                    self.tg.send_message(rec.chat, chunk)
                except Exception as e:
                    log.warning("delivery of part %d still failing: error=%s %s",
                                index, type(e).__name__, message_metadata(chunk))
                    return
                outbox.mark_chunk_sent(rec.record_id, index)
            gave_up = False
            for file_path in rec.pending_files:
                try:
                    self._send_one_file(file_path, rec.chat)
                except Exception as e:
                    attempts = outbox.fail(rec.record_id, f"{file_path}: {e}")
                    log.warning("delivery of attachment %s failed (%d/%d): %s",
                                file_path, attempts, OUTBOX_MAX_ATTEMPTS, e)
                    if attempts >= OUTBOX_MAX_ATTEMPTS:
                        self._report_permanent_refusal(Path(file_path).name, str(e), rec.chat)
                        outbox.give_up(rec.record_id, f"attachment {file_path}: {e}")
                        log.error("attachment %s gave up after %d attempts → dead-letter",
                                  file_path, attempts)
                        gave_up = True
                        break
                    return
                outbox.mark_file_sent(rec.record_id, file_path)
            if gave_up:
                continue
            # The ledger write failed → the record STAYS in the queue. Delivered parts are
            # marked, so they are not resent; the next cycle just tries to finish it.
            if rec.key and not self._mark_sent(rec.key):
                return
            outbox.done(rec.record_id)
            # The record id and a content-free fingerprint are logged ON PURPOSE: the user
            # received one reply twice and the log could not tell whether it was the same
            # message delivered twice or two different ones. Without that, any fix is a guess.
            log.info("FWD (delivered) id=%s %d parts, %s", rec.record_id, len(rec.chunks),
                     message_metadata("".join(rec.chunks),
                                      kind="text" if rec.chunks else "file",
                                      attachments=len(rec.files)))

    def _allowed_outbox_dirs(self):
        from .config import outbox_path
        dirs = [outbox_path(getattr(self.cfg, "bot_name", ""))]
        for raw in getattr(self.cfg, "outbox_dirs", []) or []:
            try:
                dirs.append(Path(raw).expanduser().resolve())
            except OSError:
                continue
        return dirs

    def _safe_outbox_path(self, raw: str):
        try:
            path = Path(raw).expanduser().resolve()
        except OSError as e:
            return None, f"cannot resolve ({e.__class__.__name__})"
        allowed = self._allowed_outbox_dirs()
        if not any(path == d or d in path.parents for d in allowed):
            return None, f"outside the allowed folders — put it in {allowed[0]} or add the folder to 'outbox_dirs'"
        if not path.is_file():
            return None, "not a regular file"
        if path.stat().st_size == 0:
            return None, "file is empty"
        return path, "ok"

    def _extract_files(self, text: str):
        marker = getattr(self.cfg, "file_marker", "[tg-file]").lower()
        keep, wanted = [], []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(marker):
                arg = stripped[len(marker):].strip().strip('"').strip("'")
                if arg:
                    wanted.append(arg)
                continue
            keep.append(line)
        return "\n".join(keep).strip(), wanted

    def _send_files(self, paths: list[str]) -> None:
        for raw in paths:
            resolved, reason = self._safe_outbox_path(raw)
            if resolved is None:
                log.warning("refusing to send %s: %s",
                            message_metadata(raw, kind="file", attachments=1), reason)
                self._send_final(f"⚠️ Couldn't send {Path(raw).name}: {reason}", turn_text=False)
                continue
            try:
                self.tg.send_file(self.key.chat_id, resolved)
            except Exception as e:
                log.warning("file send failed (%s): %s", resolved.name, e)
                self._send_final(f"⚠️ Couldn't send {resolved.name}: {e}", turn_text=False)

    def _send_one_file(self, raw: str, chat: int | None = None) -> None:
        if chat is None:
            chat = self.key.chat_id
        resolved, reason = self._safe_outbox_path(raw)
        if resolved is None:
            self._report_permanent_refusal(Path(raw).name, reason, chat)
            return
        try:
            self.tg.send_file(chat, resolved)
        except OSError as e:
            self._report_permanent_refusal(resolved.name, str(e), chat)

    def _report_permanent_refusal(self, name: str, reason: str, chat: int | None = None) -> None:
        log.warning("refusing to send attachment %s: %s", name, reason)
        try:
            self.tg.send_message(chat if chat is not None else self.key.chat_id,
                                 f"⚠️ Couldn't send {name}: {reason}")
        except Exception as e:
            log.warning("could not report the rejected attachment: %s", e)
