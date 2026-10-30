from agent2telegramtopic.policy import choose_eviction, needs_compact, should_compact_before_park
from agent2telegramtopic.config import Config
from agent2telegramtopic.state import Lane


DEFAULTS = Config(bot_name="test", agent_kind="codex", allowed_users=frozenset({9}))
IDLE_SECONDS = DEFAULTS.idle_compact_minutes * 60
MIN_TOKENS = DEFAULTS.compact_min_tokens
MAX_LIVE = DEFAULTS.max_live_lanes


def lane(thread_id, *, activity=0, busy=False, status="live", tokens=MIN_TOKENS,
         compact=None):
    return Lane(1, thread_id, f"Topic {thread_id}", "codex", f"session-{thread_id}",
                status, busy, activity, compact, tokens)


def test_choose_eviction_free_slot_returns_none():
    assert choose_eviction([lane(1)], MAX_LIVE) is None


def test_choose_eviction_selects_oldest_nonbusy_lane():
    older_busy = lane(1, activity=1, busy=True)
    newer_idle = lane(2, activity=3)
    older_idle = lane(3, activity=2)
    assert choose_eviction([older_busy, newer_idle, older_idle, lane(4, activity=4)], MAX_LIVE) == older_idle


def test_choose_eviction_queues_when_all_lanes_busy():
    assert choose_eviction([lane(i, busy=True) for i in range(MAX_LIVE)], MAX_LIVE) == "queue"


def test_needs_compact_false_before_idle_threshold_or_under_token_threshold():
    assert not needs_compact(lane(1, activity=100, tokens=MIN_TOKENS),
                             100 + IDLE_SECONDS - 0.01, IDLE_SECONDS, MIN_TOKENS)
    assert not needs_compact(lane(1, activity=0, tokens=MIN_TOKENS - 1),
                             IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)


def test_needs_compact_rejects_busy_and_parked_lanes():
    assert not needs_compact(lane(1, busy=True), IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)
    assert not needs_compact(lane(1, status="parked"), IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)


def test_needs_compact_only_once_per_idle_period():
    assert not needs_compact(lane(1, activity=100, compact=100),
                             100 + IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)
    assert needs_compact(lane(1, activity=101, compact=100),
                         101 + IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)


def test_needs_compact_exact_boundaries_are_inclusive():
    assert needs_compact(lane(1, activity=100, tokens=MIN_TOKENS),
                         100 + IDLE_SECONDS, IDLE_SECONDS, MIN_TOKENS)


def test_should_compact_before_park_uses_token_threshold():
    assert should_compact_before_park(lane(1, tokens=MIN_TOKENS), MIN_TOKENS)
    assert not should_compact_before_park(lane(1, tokens=MIN_TOKENS - 1), MIN_TOKENS)
