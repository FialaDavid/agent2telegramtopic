# Ported from agent2telegram attach.py (MIT)
"""Single-bot Telegram inbound multiplexer."""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from . import routing
from .config import lane_state_dir
from .redaction import update_metadata
from .telegram import TelegramError, is_network_error
from . import mentions, notices

log = logging.getLogger(__name__)
PROCESSED_UPDATE_LEDGER_LIMIT = 500
QUEUE_ACK_COOLDOWN = 30.0


class Multiplexer:
    def __init__(self, client, allowed_users, state_dir: Path,
                 lane_factory: Callable[[routing.LaneKey], object], *, poll_timeout=30,
                 log_=None, lane_states_root: Path | None = None,
                 lane_store=None, max_live: int | None = None,
                 compact_min_tokens: int = 30000, idle_minutes: int = 45,
                 watcher_interval: float = 60.0):
        self.tg = client
        self.client = client
        self.allowed = frozenset(allowed_users)
        self.state_dir = Path(state_dir)
        self.lane_factory = lane_factory
        self.poll_timeout = poll_timeout
        self.log = log_ or log
        self._offset_file = self.state_dir / "offset"
        self._processed_updates_file = self.state_dir / "processed_updates"
        self._processed_update_ids, self._processed_update_order = self._read_processed_updates()
        self._lanes = {}
        self._stop = threading.Event()
        self._last_queue_ack = {}
        # Delta (9.5): use the bot name and home through config's canonical lane-state helper;
        # the injected root lets tests point the startup scan at an isolated tree.
        if lane_states_root is None:
            home = Path.home()
            lane_states_root = lane_state_dir(self.state_dir.name, 0, 0, home).parent.parent
        self.lane_states_root = Path(lane_states_root)
        self._lane_lock = threading.RLock()
        self._started_lanes = set()
        self._outbound_started_lanes = set()
        # The bot's own user id (the token prefix); set by app.main so its own service messages
        # (e.g. the forum_topic_edited a rename produces) are not logged as foreign updates.
        self.bot_id = None
        # False when the last getMe said topics are off; the first allowed message then re-checks
        # and tells the user once per run how to turn them on (see _warn_topics_disabled).
        self.topics_enabled = True
        self._topics_notice_sent = False
        self.lane_store = lane_store
        self.max_live = max_live
        self.compact_min_tokens = compact_min_tokens
        self.idle_minutes = idle_minutes
        self.watcher_interval = watcher_interval
        self._slot_wakeup = threading.Event()
        self._slot_thread = None
        self._slot_lock = threading.RLock()
        self._watcher_thread = None

    def _idle_ctx_tokens(self, bridge):
        reader = getattr(bridge, "_internal_context_tokens", None)
        return reader() if callable(reader) else None

    def _idle_watcher_loop(self):
        from .watcher import IdleWatcher

        watcher = IdleWatcher(
            self.lane_store,
            lambda key: self._lanes.get(key),
            self._idle_ctx_tokens,
            idle_minutes=self.idle_minutes,
            min_tokens=self.compact_min_tokens,
            log=self.log,
        )
        while not self._stop.wait(self.watcher_interval):
            try:
                watcher.tick()
            except Exception:
                self.log.exception("Idle watcher tick failed")

    # Ported from agent2telegram attach.py:_ensure_update_state (MIT).
    def _ensure_update_state(self) -> None:
        # Focused tests may build the object without calling our __init__.
        if not hasattr(self, "_offset_file"):
            self._offset_file = self.state_dir / "offset"
        if not hasattr(self, "_processed_updates_file"):
            self._processed_updates_file = self.state_dir / "processed_updates"
        if not hasattr(self, "_processed_update_ids") or not hasattr(self, "_processed_update_order"):
            self._processed_update_ids, self._processed_update_order = self._read_processed_updates()
        # Delta (9.5): lane inboxes are owned by LaneBridge and opened per routed lane.

    # Ported from agent2telegram attach.py:_load_offset (MIT).
    def _load_offset(self) -> int:
        self._ensure_update_state()
        try:
            return int(json.loads(self._offset_file.read_text("utf-8"))["offset"])
        except Exception:
            return 0

    # Ported from agent2telegram attach.py:_save_offset (MIT).
    def _save_offset(self, offset: int) -> None:
        self._ensure_update_state()
        try:
            self._offset_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._offset_file.parent / (self._offset_file.name + ".tmp")
            tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
            tmp.replace(self._offset_file)
        except OSError as e:
            self.log.warning("could not persist attach offset: %s", e)

    # Ported from agent2telegram attach.py:_read_processed_updates (MIT).
    def _read_processed_updates(self):
        ids, order = set(), deque()
        try:
            raw = json.loads(self._processed_updates_file.read_text("utf-8")).get("update_ids", [])
        except Exception:
            raw = []
        for value in raw[-PROCESSED_UPDATE_LEDGER_LIMIT:]:
            try:
                update_id = int(value)
            except (TypeError, ValueError):
                continue
            if update_id not in ids:
                ids.add(update_id)
                order.append(update_id)
        return ids, order

    # Ported from agent2telegram attach.py:_persist_processed_updates (MIT).
    def _persist_processed_updates(self) -> None:
        self._ensure_update_state()
        try:
            self._processed_updates_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._processed_updates_file.parent / (self._processed_updates_file.name + ".tmp")
            tmp.write_text(json.dumps({"update_ids": list(self._processed_update_order)}), encoding="utf-8")
            tmp.replace(self._processed_updates_file)
        except OSError as e:
            self.log.warning("could not persist processed updates: %s", e)

    # Ported from agent2telegram attach.py:_mark_update_processed (MIT).
    def _mark_update_processed(self, update_id) -> None:
        if update_id is None:
            return
        self._ensure_update_state()
        try:
            update_id = int(update_id)
        except (TypeError, ValueError):
            return
        if update_id in self._processed_update_ids:
            return
        self._processed_update_ids.add(update_id)
        self._processed_update_order.append(update_id)
        while len(self._processed_update_order) > PROCESSED_UPDATE_LEDGER_LIMIT:
            self._processed_update_ids.discard(self._processed_update_order.popleft())
        self._persist_processed_updates()

    # Ported from agent2telegram attach.py:_update_was_processed (MIT).
    def _update_was_processed(self, update_id) -> bool:
        if update_id is None:
            return False
        self._ensure_update_state()
        try:
            return int(update_id) in self._processed_update_ids
        except (TypeError, ValueError):
            return False

    def _lane(self, key):
        with self._lane_lock:
            if key not in self._lanes:
                lane = self.lane_factory(key)
                self._lanes[key] = lane
            lane = self._lanes[key]
            if self.lane_store is not None and self.lane_store.get(key) is None:
                # Each created bridge immediately has a durable row, before worker callbacks.
                from .state import Lane
                self.lane_store.upsert(Lane(key.chat_id, key.thread_id, f"topic {key.thread_id}",
                    getattr(lane, "agent", "") or "", getattr(lane, "session_id", "") or "",
                    "parked", False, time.time(), None, 0))
            return lane

    def _start_lane(self, key):
        lane = self._lane(key)
        with self._lane_lock:
            if key in self._started_lanes:
                return lane
            if getattr(lane, "_starting", False):
                return lane
            lane._starting = True
            if self.lane_store is not None:
                self.lane_store.set_status(key, "live")
        try:
            activate = getattr(lane, "_activate_session", None)
            if callable(activate):
                activate()
            prepare_start = getattr(lane, "_prepare_start", None)
            if callable(prepare_start):
                prepare_start()
            # An outbound loop may already be running from durable outbox recovery.
            # Keep its inbox replay dormant until tmux is fully activated.
            lane._defer_inbound_replay = False
            lane._start_inbound_worker()
            if self.lane_store is not None:
                self.lane_store.set_status(key, "live")
            # Submit durable records before publishing this lane as started. Updates
            # arriving during replay are then queued after the older inbox records.
            lane._replay_pending_inbound()
            self._start_outbound(lane)
            threading.Thread(target=lane._typing_loop, daemon=True).start()
            if getattr(lane, "agent", None) == "codex":
                # Codex logs tools to the rollout only at completion → scrape the TUI for LIVE bubbles.
                threading.Thread(target=lane._tui_scrape_loop, daemon=True).start()
            with self._lane_lock:
                self._started_lanes.add(key)
                lane._starting = False
                lane._replay_pending_inbound()
        except Exception:
            with self._lane_lock:
                self._started_lanes.discard(key)
                lane._starting = False
            # Activation may have created a real tmux process. Roll it back before
            # freeing its persisted slot, otherwise another lane can exceed max_live.
            session = getattr(lane, "_session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    self.log.exception("could not roll back failed lane activation %s", key)
            alive = getattr(session, "alive", False) if session is not None else False
            alive = alive() if callable(alive) else alive
            if self.lane_store is not None:
                with self._lane_lock:
                    if alive:
                        self.lane_store.set_status(key, "live")
                    else:
                        self.lane_store.set_status(key, "waiting" if self._pending(lane) else "parked")
            raise
        return lane

    def _start_outbound(self, lane):
        """Start the lane's delivery loop once, including outbound-only recovery."""
        key = lane.key
        with self._lane_lock:
            if key in self._outbound_started_lanes:
                return
            self._outbound_started_lanes.add(key)
        try:
            threading.Thread(target=lane._outbound_loop, daemon=True).start()
        except Exception:
            with self._lane_lock:
                self._outbound_started_lanes.discard(key)
            raise

    def _pending(self, lane):
        inbox = lane._ensure_inbox()
        return inbox is not None and bool(inbox.pending())

    def _set_waiting(self, lane, acknowledge=True):
        if self.lane_store is not None:
            self.lane_store.set_status(lane.key, "waiting")
            self.lane_store.set_busy(lane.key, False)
        if acknowledge:
            try:
                lane.tg.send_message(lane.key.chat_id, f"This topic is queued and will resume when a session is available.")
            except Exception as exc:
                self.log.warning("could not send queued notice: %s", exc)
        self._slot_wakeup.set()

    def _live_count(self):
        if self.lane_store is None:
            return 0
        return len(self.lane_store.list_live())

    def _slot_loop(self):
        while not self._stop.is_set():
            self._slot_wakeup.wait(1.0)
            self._slot_wakeup.clear()
            if self._stop.is_set():
                break
            with self._slot_lock:
                if self.lane_store is None:
                    continue
                waiting = [row for row in self.lane_store.list_waiting()]
                for waiting_row in waiting:
                    if self._live_count() >= (self.max_live or 4):
                        live = self.lane_store.list_live()
                        idle = []
                        for live_row in live:
                            candidate = self._lane(routing.LaneKey(live_row.chat_id, live_row.thread_id))
                            if (not live_row.busy and not getattr(candidate, "_internal_turn", False)
                                    and not getattr(candidate, "_turn_active", threading.Event()).is_set()
                                    and not getattr(candidate, "_inbound_processing", threading.Event()).is_set()):
                                idle.append(live_row)
                        if not idle:
                            break
                        victim = min(idle, key=lambda live_row: live_row.last_activity)
                        victim_lane = self._lane(routing.LaneKey(victim.chat_id, victim.thread_id))
                        # In-flight accepted updates remain durable for the replacement bridge.
                        # Serialize the final eligibility check with both enqueue and injection.
                        victim_lane._ensure_inbound_worker_state()
                        victim_key = routing.LaneKey(victim.chat_id, victim.thread_id)
                        current = self.lane_store.get(victim_key)
                        # Inbox inspection can wait on disk, so do it outside the
                        # worker lock. Recheck the in-memory claim before closing.
                        try:
                            inbox = victim_lane._ensure_inbox()
                            pending = inbox is None or bool(inbox.pending())
                        except Exception:
                            pending = True
                            self.log.exception("could not inspect pending inbox for lane %s", victim)
                        if pending:
                            continue
                        with victim_lane._inbound_worker_lock:
                            if (current is None or current.busy
                                    or getattr(victim_lane, "_internal_turn", False)
                                    or getattr(victim_lane, "_turn_active", threading.Event()).is_set()
                                    or getattr(victim_lane, "_inbound_processing", threading.Event()).is_set()
                                    or not victim_lane._inbound_queue.empty()):
                                continue
                            victim_lane._closing = True
                        try:
                            event = victim_lane.compact_internal() if victim.ctx_tokens >= self.compact_min_tokens else None
                        except Exception:
                            victim_lane._closing = False
                            victim_lane._replay_pending_inbound()
                            self.log.exception("could not start compaction for lane %s", victim)
                            continue
                        if victim.ctx_tokens >= self.compact_min_tokens:
                            # A turn may have started after the idle snapshot. Never close
                            # the lane unless compaction successfully reserved its turn.
                            if event is None:
                                victim_lane._closing = False
                                victim_lane._replay_pending_inbound()
                                continue
                            if not event.wait(300):
                                self.log.warning("could not finish compaction for lane %s", victim)
                                victim_lane._closing = False
                                victim_lane._replay_pending_inbound()
                                continue
                        session = getattr(victim_lane, "_session", None)
                        if session is not None:
                            try:
                                session.close()
                            except Exception:
                                self.log.exception("could not close lane %s", victim)
                        alive = getattr(session, "alive", False) if session is not None else False
                        alive = alive() if callable(alive) else alive
                        if alive:
                            victim_lane._closing = False
                            self.log.error("could not park lane %s; tmux session remains alive", victim)
                            victim_lane._replay_pending_inbound()
                            break
                        victim_key = routing.LaneKey(victim.chat_id, victim.thread_id)
                        try:
                            with self._lane_lock:
                                pending = self._pending(victim_lane)
                                self.lane_store.set_status(victim_key, "waiting" if pending else "parked")
                                self.lane_store.set_busy(victim_key, False)
                        except Exception:
                            self.log.exception("could not inspect pending inbox for lane %s", victim)
                            with self._lane_lock:
                                self.lane_store.set_status(victim_key, "waiting")
                                self.lane_store.set_busy(victim_key, False)
                        stop = getattr(victim_lane, "stop", None)
                        if callable(stop):
                            stop()
                        self._retire_parked_lane(routing.LaneKey(victim.chat_id, victim.thread_id), victim_lane)
                    key = routing.LaneKey(waiting_row.chat_id, waiting_row.thread_id)
                    try:
                        lane = self._start_lane(key)
                    except Exception:
                        self.log.exception("could not activate waiting lane %s; it remains queued", key)
                        continue

    def _start_slot_manager(self):
        if self.lane_store is not None and self._slot_thread is None:
            self._slot_thread = threading.Thread(target=self._slot_loop, daemon=True)
            self._slot_thread.start()

    def _retire_parked_lane(self, key, bridge=None) -> None:
        """Forget a bridge after its session has been killed by its park path."""
        with self._lane_lock:
            current = self._lanes.get(key)
            if current is None or (bridge is not None and current is not bridge):
                return
            self._lanes.pop(key, None)
            self._started_lanes.discard(key)
            self._outbound_started_lanes.discard(key)

    # Delta (9.5): scan per-lane inbox directories before polling; reconstruct chat_id from
    # the durable Telegram update because thread_id alone is insufficient to form LaneKey.
    def _replay_startup_lanes(self):
        live_keys = set()
        if self.lane_store is not None:
            for row in self.lane_store.list_all():
                key = routing.LaneKey(row.chat_id, row.thread_id)
                try:
                    lane = self._lane(key)
                    alive_check = getattr(lane, "_session_alive", None)
                    session = getattr(lane, "_session", None)
                    alive = alive_check() if callable(alive_check) else (getattr(session, "alive", False) if session is not None else False)
                    if callable(alive):
                        alive = alive()
                    if alive:
                        live_keys.add(key)
                except Exception:
                    self.log.exception("could not inspect lane %s during startup reconciliation", key)
            self.lane_store.reconcile(live_keys)
            for key in live_keys:
                try:
                    self._start_lane(key)
                except Exception:
                    # One lane's failed activation/replay must not prevent other
                    # live sessions from reconciling on this startup.
                    self.log.exception("could not start live lane %s during startup reconciliation", key)
        if not self.lane_states_root.exists():
            return
        for chat_path in sorted(p for p in self.lane_states_root.iterdir() if p.is_dir()):
            try:
                chat_id = int(chat_path.name)
            except ValueError:
                continue
            for thread_path in sorted(p for p in chat_path.iterdir() if p.is_dir()):
                try:
                    thread_id = int(thread_path.name)
                except ValueError:
                    continue
                # Pending durable work is recovered without activating sessions when
                # it consists only of outbound deliveries.
                pending_dir = thread_path / "queue" / "inbox"
                outbox_dir = thread_path / "queue" / "outbox"
                try:
                    pending_files = sorted(p for p in pending_dir.glob("*.json") if p.is_file())
                except OSError:
                    pending_files = []
                try:
                    pending_outbox = any(p.is_file() for p in outbox_dir.glob("*.json"))
                except OSError:
                    pending_outbox = False
                if not pending_files and not pending_outbox:
                    continue
                key = routing.LaneKey(chat_id, thread_id)
                try:
                    # Delta (9.1): ledger replayed reservations before workers can delete them,
                    # so a refetched update after reserve-before-offset crash is deduplicated.
                    for record_path in pending_files:
                        try:
                            update_id = json.loads(record_path.read_text("utf-8")).get("update", {}).get("update_id")
                            self._mark_update_processed(update_id)
                        except (OSError, ValueError, AttributeError):
                            continue
                    lane = self._lane(key)
                    # Outbound recovery is safe while parked, but its periodic tick
                    # must not replay newly arrived inbox work until a session starts.
                    # A live lane was already activated above; keep its inbound retry
                    # enabled when this scan finds durable records for it.
                    lane._defer_inbound_replay = key not in live_keys
                    if self.lane_store is not None and key not in live_keys:
                        if pending_files:
                            self.lane_store.set_status(key, "waiting")
                            self.lane_store.set_busy(key, False)
                            self._slot_wakeup.set()
                        if pending_outbox:
                            self._start_outbound(lane)
                    else:
                        if pending_files:
                            self._start_lane(key)
                        elif pending_outbox:
                            self._start_outbound(lane)
                except Exception:
                    self.log.exception("could not start lane %s during startup replay", key)

    def _warn_topics_disabled(self, upd: dict) -> None:
        # getMe may report has_topics_enabled only after the user has opened the bot chat, so a
        # startup "disabled" is re-checked on the first message. Inline and never raises: the
        # message is processed normally (in the single non-topic chat) either way.
        msg = upd.get("message")
        if self.topics_enabled is not False or self._topics_notice_sent or not isinstance(msg, dict):
            return
        self._topics_notice_sent = True
        try:
            if self.client.get_me().get("has_topics_enabled") is True:
                self.topics_enabled = True
                return
            self.log.warning("Topics (Threaded Mode) are still not enabled for this bot; telling the user")
            self.client.send_message(routing.LaneKey(msg["chat"]["id"], routing.GENERAL_THREAD_ID),
                                     notices.TOPICS_DISABLED, parse_mode=None)
        except Exception:
            self.log.exception("could not check or announce disabled topics")

    # Delta (a): route to a per-topic LaneBridge by LaneKey instead of a2t's single session.
    # Delta (b): apply agent2telegramtopic' existing private-chat and sender allowlist at the top.
    # Delta (c): keep offset and processed ledger in the bot-level state directory.
    # Ported from agent2telegram attach.py:_handle_update_once (MIT).
    def _handle_update_once(self, upd: dict, offset: int) -> int:
        # Delta (9.6): this bot-level multiplexer has no shared inbox; lane inboxes are opened
        # after routing, while update ledger state is initialized before the handling try.
        self._ensure_update_state()
        raw_id = upd.get("update_id")
        try:
            update_id = int(raw_id)
        except (TypeError, ValueError):
            self.log.warning("skipping malformed Telegram update without a valid update_id: %s",
                             update_metadata(upd))
            return offset
        # Delta (9.1): retain a2t's update_id None/old-offset guards verbatim.
        next_offset = max(offset, update_id + 1) if update_id is not None else offset
        if update_id is not None and update_id < offset:
            return offset
        if self._update_was_processed(update_id):
            self._save_offset(next_offset)
            return next_offset

        # Delta (b): agent2telegramtopic' private-chat and sender allowlist is applied before lane creation.
        allowed = routing.is_allowed(upd, self.allowed)
        if not allowed:
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") if isinstance(msg, dict) else {}
            sender = msg.get("from") if isinstance(msg, dict) else {}
            # The bot's own service messages (a topic-edit rename echoes back) are expected noise.
            own = (isinstance(sender, dict) and sender.get("is_bot") is True
                   and self.bot_id is not None and sender.get("id") == self.bot_id)
            self.log.log(logging.DEBUG if own else logging.WARNING,
                         "Ignoring disallowed Telegram update update_id=%s chat_id=%s chat_type=%s sender_id=%s %s",
                             update_id, chat.get("id") if isinstance(chat, dict) else None,
                             chat.get("type") if isinstance(chat, dict) else None,
                             sender.get("id") if isinstance(sender, dict) else None, update_metadata(upd))
            self._mark_update_processed(update_id)
            self._save_offset(next_offset)
            return next_offset
        self._warn_topics_disabled(upd)
        reaction = upd.get("message_reaction")
        if isinstance(reaction, dict):
            # Delta (B2): a reaction carries no message_thread_id; its topic is the one the
            # reacted-to message was recorded in. A reaction on an unmapped message is dropped.
            thread_id = (self.lane_store.thread_for_message(reaction["chat"]["id"], reaction.get("message_id"))
                         if self.lane_store is not None else None)
            routed = (routing.InboundMessage(routing.LaneKey(reaction["chat"]["id"], thread_id), "", None, None)
                      if thread_id is not None else None)
            if routed is None:
                self.log.info("dropping reaction on unmapped message: update_id=%s %s",
                              update_id, update_metadata(upd))
                self._mark_update_processed(update_id)
                self._save_offset(next_offset)
                return next_offset
        else:
            routed = routing.route_update(upd)
            event = routing.topic_event(upd)
            if event is not None and event[1] == "created" and self.lane_store is not None:
                # The user just created this topic; its first message gets the naming hint. A topic's
                # thread id is the id of its creation service message if that carries no thread id.
                m = upd["message"]
                thread_id = m.get("message_thread_id", m.get("message_id"))
                if type(thread_id) is int and thread_id != routing.GENERAL_THREAD_ID:
                    self.lane_store.mark_new_topic(routing.LaneKey(event[0].chat_id, thread_id))
        if routed is None:
            # Delta (9.3): allowed but unroutable updates are consumed without creating a lane.
            self.log.info("ignoring allowed Telegram update without a supported content route: update_id=%s %s",
                          update_id, update_metadata(upd))
            self._mark_update_processed(update_id)
            self._save_offset(next_offset)
            return next_offset
        key = routed.key
        # Delta (B2): remember which topic this inbound message is in, for later reactions to it.
        msg = upd.get("message") or upd.get("edited_message")
        if self.lane_store is not None and isinstance(msg, dict) and isinstance(msg.get("message_id"), int):
            self.lane_store.record_message(key.chat_id, msg["message_id"], key.thread_id)
        # Delta (a): route to the selected LaneBridge's LaneKey-scoped durable inbox.
        try:
            with self._lane_lock:
                lane = self._lane(key)
        except Exception:
            self.log.exception("could not start lane %s; not advancing the offset", key)
            return offset
        inbox = lane._ensure_inbox()
        if inbox is None:
            self.log.error("update %s: durable storage unavailable, not advancing the offset", update_id)
            return offset
        try:
            record_id = inbox.reserve(upd)
        except Exception as e:
            self.log.error("could not store update %s, not advancing the offset: %s", update_id, e)
            return offset
        # Delta (9.1): commit bot offset immediately after durable reservation, in a2t order.
        self._save_offset(next_offset)
        internal = bool(getattr(lane, "_internal_turn", False))
        acknowledge_waiting = None
        acknowledge_busy = False
        start_unmanaged = False
        submit = False
        with self._lane_lock:
            was_started = key in self._started_lanes
            starting = getattr(lane, "_starting", False)
            row = self.lane_store.get(key) if self.lane_store is not None else None
            status = row.status if row is not None else "live"
            closing = getattr(lane, "_closing", False)
            if starting:
                # The durable record will be picked up by _start_lane's final replay.
                # Keep the live status and avoid a queued notice during activation.
                pass
            elif self.lane_store is not None and internal:
                pass
            elif self.lane_store is not None and status in {"parked", "waiting"}:
                # Keep the status transition atomic with the post-reserve decision.
                self.lane_store.set_status(key, "waiting")
                self.lane_store.set_busy(key, False)
                acknowledge_waiting = status != "waiting" and self._live_count() >= self.max_live
            elif was_started:
                acknowledge_busy = True
            elif self.lane_store is None:
                start_unmanaged = True
            else:
                self.lane_store.set_status(key, "waiting")
                self.lane_store.set_busy(key, False)
                acknowledge_waiting = self._live_count() >= self.max_live
            submit = was_started and not internal and not closing
        if start_unmanaged:
            self._start_lane(key)
        if acknowledge_busy:
            self._maybe_ack_queued(upd, lane)
        self.log.info("IN  id=%s update=%s %s", record_id, update_id, update_metadata(upd))
        if acknowledge_waiting is not None:
            if acknowledge_waiting:
                try:
                    lane.tg.send_message(lane.key.chat_id, "This topic is queued and will resume when a session is available.")
                except Exception as exc:
                    self.log.warning("could not send queued notice: %s", exc)
            self._slot_wakeup.set()
        if submit:
            # Acknowledge before waking the worker: otherwise an idle lane can begin
            # this very message and look busy to _maybe_ack_queued.
            with self._lane_lock:
                if key in self._started_lanes and not getattr(lane, "_closing", False):
                    lane._submit_inbound_update(upd, record_id)
        self._mark_update_processed(update_id)
        return next_offset

    # Ported from agent2telegram attach.py:_maybe_ack_queued (MIT).
    # Delta (a): busy state and acknowledgement are scoped to the selected topic lane.
    def _maybe_ack_queued(self, upd: dict, lane) -> None:
        # Internal lifecycle commands are private, including their busy period.
        if getattr(lane, "_internal_turn", False):
            return
        if not lane._busy_for_notice():
            self.log.debug("ACK not sent: no recent evidence of work in progress")
            return
        msg = upd.get("message") or upd.get("edited_message") or {}
        # Delta (9.6): retain a2t's sender allowlist and accepts_message gates.
        if msg.get("from", {}).get("id") not in self.allowed:
            return
        if not mentions.accepts_message(lane.cfg, msg):
            return
        # Delta (b): the private-chat policy has already accepted this message.
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id is None:
            return
        now = time.monotonic()
        last_ack = self._last_queue_ack.get(lane.key)
        if last_ack is not None and now - last_ack < QUEUE_ACK_COOLDOWN:
            return
        self._last_queue_ack[lane.key] = now
        try:
            lane.tg.send_message(chat_id, notices.busy_text(lane.cfg))
            self.log.info("receipt ACK sent (another turn is running)")
        except Exception as e:
            self.log.warning("could not send the receipt ACK: %s", e)

    # Delta (a): poll state belongs to the bot-level multiplexer, not a single session.
    # Delta (c): offset and processed ledger are stored in the bot-level state directory.
    # Ported from agent2telegram attach.py:_inbound_loop (MIT).
    def _inbound_loop(self) -> None:
        # Delta (9.5): replay all durable pending lane records before asking Telegram for updates.
        self._replay_startup_lanes()
        self._start_slot_manager()
        offset = self._load_offset()
        transient_fails = 0
        outage_alerted = False
        while not self._stop.is_set():
            try:
                updates = self.tg._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.poll_timeout,
                     "allowed_updates": json.dumps(["message", "edited_message", "message_reaction"])},
                    timeout=self.poll_timeout + 15,
                )
            except (TelegramError, OSError) as e:
                if is_network_error(e):
                    transient_fails += 1
                    if transient_fails >= 10 and not outage_alerted:
                        self.log.error("getUpdates network outage (%d in a row) is lasting: %s", transient_fails, e)
                        outage_alerted = True
                    else:
                        self.log.warning("getUpdates transient network error (%d): %s", transient_fails, e)
                    self._stop.wait(min(3 * transient_fails, 30))
                    continue
                self.log.error("getUpdates failed: %s", e)
                self._stop.wait(3)
                continue
            if outage_alerted:
                self.log.info("getUpdates network recovered after %d errors", transient_fails)
            transient_fails = 0
            outage_alerted = False
            for upd in updates:
                try:
                    offset = self._handle_update_once(upd, offset)
                    try:
                        update_id = int(upd.get("update_id"))
                    except (TypeError, ValueError):
                        update_id = None
                    # Delta (9.2): failure to advance means refetch this update; do not pass it.
                    if update_id is not None and offset <= update_id:
                        break
                except Exception as e:
                    self.log.error("inbound error: error=%s %s", type(e).__name__, update_metadata(upd))
                    # Delta (9.2): a failed update prevents later updates in this batch advancing.
                    break

    def stop(self):
        self._stop.set()
        self._slot_wakeup.set()

    def run_forever(self, poll_timeout=None):
        if poll_timeout is not None:
            self.poll_timeout = poll_timeout
        if self.lane_store is not None and self._watcher_thread is None:
            self._watcher_thread = threading.Thread(target=self._idle_watcher_loop, daemon=True)
            self._watcher_thread.start()
        self._inbound_loop()
