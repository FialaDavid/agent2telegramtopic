import json
import threading
import time

from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import Lane, LaneStore
from agent2telegramtopic.watcher import IdleWatcher


class FakeBridge:
    def __init__(self, ctx=40000):
        self.ctx = ctx
        self.compacts = 0
        self.done = threading.Event()
        self.internal = False
        self._internal_turn = False
        self._internal_lock = threading.RLock()

    def _internal_context_tokens(self):
        return self.ctx

    def compact_internal(self):
        self.compacts += 1
        self.internal = True
        self._internal_turn = True
        return self.done


def make_lane(*, thread_id=7, last_activity=1000, busy=False, ctx_tokens=40000,
              last_compact=None, status="live"):
    return Lane(123, thread_id, f"Topic {thread_id}", "codex", f"session-{thread_id}",
                status, busy, last_activity, last_compact, ctx_tokens)


def make_watcher(lanes, bridges, now):
    store = LaneStore(":memory:")
    for lane in lanes:
        store.upsert(lane)
    watcher = IdleWatcher(store, lambda key: bridges.get(key),
                          lambda bridge: bridge._internal_context_tokens(),
                          idle_minutes=45, min_tokens=30000, clock=lambda: now[0])
    return watcher, store


def test_46_minutes_and_40k_enqueues_once_without_waiting_or_telegram():
    lane = make_lane()
    key = LaneKey(123, 7)
    bridge = FakeBridge()
    now = [1000 + 46 * 60]
    watcher, store = make_watcher([lane], {key: bridge}, now)

    started = time.monotonic()
    assert watcher.tick() == [key]
    assert time.monotonic() - started < 0.1
    assert bridge.compacts == 1
    assert not bridge.done.is_set()
    # A completed user turn makes the lane eligible again.
    completed_at = 1000 + 46 * 60
    assert watcher.tick() == []
    assert bridge.compacts == 1
    bridge._internal_turn = False
    store.touch(key, completed_at + 1)
    now[0] += 47 * 60
    assert watcher.tick() == [key]


def test_failed_compact_start_does_not_mark_idle_period_complete():
    lane = make_lane()
    key = LaneKey(123, 7)
    bridge = FakeBridge()
    bridge.compact_internal = lambda: None
    watcher, store = make_watcher([lane], {key: bridge}, [1000 + 46 * 60])

    assert watcher.tick() == []
    assert store.get(key).last_compact is None


def test_final_eligibility_check_observes_a_just_finished_user_turn():
    lane = make_lane()
    key = LaneKey(123, 7)
    bridge = FakeBridge()
    now = [1000 + 46 * 60]
    watcher, store = make_watcher([lane], {key: bridge}, now)

    def context_after_turn(_bridge):
        store.touch(key, now[0])
        return 40000

    watcher.ctx_reader = context_after_turn
    assert watcher.tick() == []
    assert bridge.compacts == 0


def test_44_minutes_under_threshold_and_busy_lanes_are_skipped():
    lanes = [make_lane(thread_id=1), make_lane(thread_id=2),
             make_lane(thread_id=3, busy=True)]
    keys = [LaneKey(123, i) for i in (1, 2, 3)]
    bridges = {key: FakeBridge(ctx) for key, ctx in zip(keys, (40000, 20000, 40000))}
    now = [1000 + 46 * 60]
    lanes[0].last_activity = now[0] - 44 * 60
    watcher, _ = make_watcher(lanes, bridges, now)

    assert watcher.tick() == []
    assert all(bridge.compacts == 0 for bridge in bridges.values())


def test_watcher_delivers_internal_compact_without_telegram(tmp_path):
    from agent2telegramtopic.config import lane_state_dir
    from agent2telegramtopic.lane import LaneBridge

    class Telegram:
        def __init__(self):
            self.messages = []

        def send_message(self, *args, **kwargs):
            self.messages.append((args, kwargs))

        def send_chat_action(self, *args, **kwargs):
            pass

    class Session:
        def __init__(self):
            self.commands = []
            self.submitted = threading.Event()

        def send_command(self, command):
            self.commands.append(command)
            self.submitted.set()

    now = 1000 + 46 * 60
    key = LaneKey(123, 8)
    lane = make_lane(thread_id=8, last_activity=1000)
    store = LaneStore(":memory:")
    store.upsert(lane)
    session, tg = Session(), Telegram()
    state = lane_state_dir("test", key.chat_id, key.thread_id, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, key, state / "sent", state / "queue",
                        agent="codex", session=session, lane_store=store)
    bridge._transcript = tmp_path / "rollout.jsonl"
    bridge._transcript.write_text("")
    bridge._last_resolve = float("inf")
    watcher = IdleWatcher(store, lambda _key: bridge, lambda _bridge: 40000,
                          clock=lambda: now)

    assert watcher.tick() == [key]
    assert session.submitted.wait(1)
    assert session.commands == ["/compact"]
    assert tg.messages == []
    # A matching Codex compact completion persists the marker that prevents a
    # second compaction in this idle period.
    with bridge._transcript.open("a") as f:
        for rec in ({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "idle-compact"}},
                    {"type": "compacted"},
                    {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "idle-compact"}},
                    {"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 12000}}}}):
            f.write(json.dumps(rec) + "\n")
    bridge.outbound_tick()
    assert store.get(key).last_compact is not None
    assert watcher.tick() == []
    assert session.commands == ["/compact"]
