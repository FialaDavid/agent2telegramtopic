"""Pure lane scheduling and compaction decisions."""

from typing import Literal

from agent2telegramtopic.state import Lane


def choose_eviction(live: list[Lane], max_live: int) -> Lane | None | Literal["queue"]:
    if len(live) < max_live:
        return None
    idle = [lane for lane in live if not lane.busy]
    if not idle:
        return "queue"
    return min(idle, key=lambda lane: lane.last_activity)


def needs_compact(lane: Lane, now: float, idle_seconds: float, min_tokens: int) -> bool:
    return (
        lane.status == "live"
        and not lane.busy
        and now - lane.last_activity >= idle_seconds
        and lane.ctx_tokens >= min_tokens
        and (lane.last_compact is None or lane.last_compact < lane.last_activity)
    )


def should_compact_before_park(lane: Lane, min_tokens: int) -> bool:
    return lane.ctx_tokens >= min_tokens
