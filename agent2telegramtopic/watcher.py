"""Nonblocking idle lane compaction scheduling."""

import logging
import time
from typing import Callable

from agent2telegramtopic.policy import needs_compact
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import LaneStore


class IdleWatcher:
    def __init__(self, store: LaneStore, lane_provider: Callable[[LaneKey], object],
                 ctx_reader: Callable[[object], int | None], *, idle_minutes: int = 45,
                 min_tokens: int = 30000, clock: Callable[[], float] = time.time,
                 log: logging.Logger | None = None):
        self.store = store
        self.lane_provider = lane_provider
        self.ctx_reader = ctx_reader
        self.idle_minutes = idle_minutes
        self.min_tokens = min_tokens
        self.clock = clock
        self.log = log or logging.getLogger("agent2telegramtopic.watcher")

    def tick(self) -> list[LaneKey]:
        compacted = []
        for lane in self.store.list_live():
            key = LaneKey(lane.chat_id, lane.thread_id)
            bridge = self.lane_provider(key)
            if bridge is None:
                continue
            active_turn = getattr(bridge, "_turn_active", None)
            if (lane.busy or (active_turn is not None and active_turn.is_set())
                    or getattr(bridge, "_internal_turn", False)
                    or getattr(bridge, "_closing", False)):
                continue
            try:
                ctx = self.ctx_reader(bridge)
            except Exception:
                ctx = None
            if ctx is None:
                self.log.debug("Idle compaction skipped for lane %s: context tokens unknown", key)
                continue
            if ctx != lane.ctx_tokens:
                self.store.set_ctx_tokens(key, ctx)
                lane.ctx_tokens = ctx
            if not needs_compact(lane, self.clock(), self.idle_minutes * 60, self.min_tokens):
                continue

            before = ctx
            # Serialize the final eligibility check and reservation with /close. The
            # close handler sets _closing under this same lock before it starts parking.
            lock = getattr(bridge, "_internal_lock", None)
            if lock is None:
                continue
            try:
                with lock:
                    # The lane may have finished a user turn after the initial
                    # scan. Its completion updates last_activity; evaluate that
                    # persisted state again while holding the submission lock.
                    current = self.store.get(key)
                    if (current is None
                            or not needs_compact(current, self.clock(),
                                                 self.idle_minutes * 60, self.min_tokens)
                            or getattr(bridge, "_closing", False)
                            or getattr(bridge, "_turn_active", None) is not None
                            and bridge._turn_active.is_set()
                            or getattr(bridge, "_internal_turn", False)):
                        continue
                    done = bridge.compact_internal()
            except Exception:
                self.log.exception("Could not start idle compaction for lane %s", key)
                continue
            if done is None:
                continue
            # Completion owns the persisted last_compact marker. Reserving it here would
            # suppress retries after submission failures/timeouts and could overwrite the
            # completed context count if the turn finishes quickly.
            self.log.info("Started idle compaction for lane %s (%s): before=%s after=pending",
                          key, current.name, before)
            compacted.append(key)
        return compacted
