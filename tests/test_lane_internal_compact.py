import json
import threading
import time

import pytest

import agent2telegramtopic.lane as lane_module
from agent2telegramtopic.config import lane_state_dir
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import Lane, LaneStore


class Telegram:
    def __init__(self):
        self.messages = []
        self.typings = []

    def send_message(self, key, text):
        self.messages.append((key, text))

    def send_chat_action(self, key, action):
        del action
        self.typings.append(key)


class Session:
    def __init__(self, transcript=None):
        self.commands = []
        self.command_submitted = threading.Event()
        self.injected = []
        self.transcript = transcript
        self.closed = False
        self.alive = False

    def send_command(self, command):
        self.commands.append(command)
        self.command_submitted.set()

    def inject(self, text):
        self.injected.append(text)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_internal_compact_waits_for_turn_end_and_suppresses_transcript(tmp_path, agent):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 2, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    bridge = LaneBridge(tg, LaneKey(1, 2), state / "sent", state / "queue",
                        agent=agent, session=session)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    typing_thread = threading.Thread(target=bridge._typing_loop, daemon=True)
    typing_thread.start()

    done = bridge.compact_internal()
    assert isinstance(done, threading.Event)
    assert session.command_submitted.wait(1)
    assert session.commands == ["/compact"]
    assert not done.is_set()

    if agent == "claude-code":
        records = [
            {"type": "assistant", "uuid": "private", "message": {"content": [
                {"type": "text", "text": "private compaction summary"}]}},
        ]
    else:
        records = [
            {"type": "event_msg", "payload": {"type": "agent_message",
                                                   "message": "private compaction summary"}},
        ]
    with transcript.open("a") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")
    bridge.outbound_tick()
    assert not done.is_set()

    if agent == "claude-code":
        bridge._compact_done.touch()
    else:
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "compact-1"}}) + "\n")
            f.write(json.dumps({"type": "compacted"}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "compact-1"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                       "message": "Telegram: next prompt"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message",
                                                       "message": "next turn answer"}}) + "\n")
    bridge.outbound_tick()
    assert done.is_set()
    if agent == "codex":
        bridge.outbound_tick()
        bridge.outbound_tick()
    bridge._flush_pending()
    assert tg.messages == []
    time.sleep(0.02)
    bridge._stop.set()
    typing_thread.join(timeout=1)
    assert tg.typings == []


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_inbound_waits_for_delayed_compact_marker_then_skips_private_output(tmp_path, agent):
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    tg, session = Telegram(), Session(transcript)
    state = lane_state_dir("test", 1, 4, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 4), state / "sent", state / "queue",
                        agent=agent, session=session)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    done = bridge.compact_internal()
    bridge._ensure_inbox()
    update = {"update_id": 42, "message": {"text": "hello"}}
    record_id = bridge._inbox.reserve(update)
    bridge._submit_inbound_update(update, record_id)
    if agent == "claude-code":
        records = [
            {"type": "assistant", "uuid": "private", "message": {"content": [
                {"type": "text", "text": "private compaction output"}]}},
        ]
        with transcript.open("a") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        bridge.outbound_tick()
        assert session.injected == []
        assert tg.messages == []
        bridge._compact_done.touch()
        bridge.outbound_tick()
    else:
        records = [
            {"type": "event_msg", "payload": {"type": "agent_message",
                                                   "message": "private compaction output"}},
        ]
        with transcript.open("a") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        bridge.outbound_tick()
        assert session.injected == []
        assert tg.messages == []
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "compact-1"}}) + "\n")
            f.write(json.dumps({"type": "compacted"}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "compact-1"}}) + "\n")
        bridge.outbound_tick()
    for _ in range(100):
        if session.injected:
            break
        time.sleep(0.01)
    assert session.injected == ["hello"]
    assert bridge._inbox.pending() == []
    assert bridge._tpos == transcript.stat().st_size
    # Codex's compact task_complete has already been consumed before the queued prompt resumes.
    if agent == "codex":
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": "[TG] hello"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "agent_message", "message": "answer to hello"}}) + "\n")
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "telegram-1"}}) + "\n")
    else:
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "[TG] hello"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "answer", "message": {
                "content": [{"type": "text", "text": "answer to hello"}]}}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "turn_end"}) + "\n")
    bridge.outbound_tick()
    bridge.outbound_tick()
    assert [text for _, text in tg.messages] == ["answer to hello"]
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["answer to hello"]


def test_uncertain_internal_compact_submission_holds_durable_message_until_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_module, "INTERNAL_COMMAND_TIMEOUT", 0.02)
    class BrokenSession(Session):
        def send_command(self, command):
            self.commands.append(command)
            raise RuntimeError("session unavailable")

    tg = Telegram()
    session = BrokenSession()
    state = lane_state_dir("test", 1, 3, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 3), state / "sent", state / "queue",
                        agent="codex", session=session)
    bridge._ensure_inbox()
    update = {"update_id": 93, "message": {"text": "keep me durable"}}
    record_id = bridge._inbox.reserve(update)
    done = bridge.compact_internal()
    bridge._submit_inbound_update(update, record_id)
    assert done is not None
    assert done.wait(0.5)
    assert bridge._turn_active.is_set() is False
    assert bridge._internal_timed_out
    assert session.closed
    assert [item.record_id for item in bridge._inbox.pending()] == [record_id]
    assert not session.injected
    assert tg.messages == []


def test_internal_compact_ignores_unread_prior_codex_completion(tmp_path):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 5, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "compact-1"}}) + "\n")
    bridge = LaneBridge(tg, LaneKey(1, 5), state / "sent", state / "queue",
                        agent="codex", session=session)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")

    done = bridge.compact_internal()
    bridge.outbound_tick()
    assert not done.is_set()
    assert tg.messages == []

    with transcript.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "agent_message", "message": "private compact output"}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "compact-1"}}) + "\n")
        f.write(json.dumps({"type": "compacted"}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "compact-1"}}) + "\n")
    bridge.outbound_tick()
    assert done.is_set()
    bridge._flush_pending()
    assert tg.messages == []


def test_claude_stop_does_not_complete_internal_compact(tmp_path):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 6, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 6), state / "sent", state / "queue",
                        agent="claude-code", session=session)
    done = bridge.compact_internal()
    bridge._turn_end.touch()
    bridge.outbound_tick()
    assert not done.is_set()
    bridge._compact_done.touch()
    bridge.outbound_tick()
    assert done.is_set()
    assert tg.messages == []


@pytest.mark.parametrize("same_chunk", [False, True])
def test_codex_compact_tail_does_not_discard_next_user_turn_completion(tmp_path, same_chunk):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 8, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    bridge = LaneBridge(tg, LaneKey(1, 8), state / "sent", state / "queue",
                        agent="codex", session=session)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    done = bridge.compact_internal()

    with transcript.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "compact-1"}}) + "\n")
        f.write(json.dumps({"type": "compacted"}) + "\n")
        if same_chunk:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "compact-1"}}) + "\n")
    bridge.outbound_tick()
    if same_chunk:
        assert done.is_set()
    else:
        assert not done.is_set()
        with transcript.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "compact-1"}}) + "\n")
        bridge.outbound_tick()
        assert done.is_set()

    # A fresh turn's completion still delivers its answer after compaction.
    update = {"update_id": 81, "message": {"text": "hello"}}
    bridge._ensure_inbox()
    record_id = bridge._inbox.reserve(update)
    bridge._submit_inbound_update(update, record_id)
    for _ in range(100):
        if session.injected:
            break
        time.sleep(0.01)
    assert session.injected == ["hello"]
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "user_message", "message": "[TG] hello"}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "agent_message", "message": "answer"}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "compact-1"}}) + "\n")
    bridge.outbound_tick()
    bridge.outbound_tick()
    assert [text for _, text in tg.messages] == ["answer"]
    assert not bridge._turn_active.is_set()


def test_internal_timeout_starts_at_submission_without_waiting_for_inbound(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_module, "INTERNAL_COMMAND_TIMEOUT", 0.02)
    tg, session = Telegram(), Session()
    parked = []
    state = lane_state_dir("test", 1, 7, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 7), state / "sent", state / "queue",
                        agent="codex", session=session, on_parked=parked.append)
    bridge._ensure_inbox()
    update = {"update_id": 73, "message": {"text": "wait for fresh session"}}
    record_id = bridge._inbox.reserve(update)
    done = bridge.compact_internal()
    bridge._submit_inbound_update(update, record_id)
    assert done.wait(0.5)
    for _ in range(100):
        if session.closed:
            break
        time.sleep(0.005)
    assert not bridge._turn_active.is_set()
    assert tg.messages == []
    assert session.closed
    assert parked == [LaneKey(1, 7)]
    assert not session.injected
    assert [item.record_id for item in bridge._inbox.pending()] == [record_id]


def test_timeout_keeps_live_session_blocked_and_does_not_mark_parked(tmp_path, monkeypatch):
    monkeypatch.setattr(lane_module, "INTERNAL_COMMAND_TIMEOUT", 0.02)
    tg, session = Telegram(), Session()
    session.alive = True
    parked = []
    state = lane_state_dir("test", 1, 70, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 70), state / "sent", state / "queue",
                        agent="codex", session=session, on_parked=parked.append)
    bridge._ensure_inbox()
    update = {"update_id": 74, "message": {"text": "must stay held"}}
    record_id = bridge._inbox.reserve(update)
    done = bridge.compact_internal()
    bridge._submit_inbound_update(update, record_id)
    assert done.wait(0.5)
    time.sleep(0.03)
    assert session.closed
    assert parked == []
    assert not session.injected
    assert [item.record_id for item in bridge._inbox.pending()] == [record_id]


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_compaction_logs_and_persists_context_before_and_after(tmp_path, caplog, agent):
    caplog.set_level("INFO", logger="agent2telegramtopic.lane")
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 71, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "context.jsonl"
    store = LaneStore(":memory:")
    key = LaneKey(1, 71)
    store.upsert(Lane(1, 71, "topic", agent, "session", "live", False, time.time(), None, 0))
    if agent == "claude-code":
        def add_usage(tokens):
            with transcript.open("a") as f:
                f.write(json.dumps({"type": "assistant", "message": {"usage": {
                    "input_tokens": tokens, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0}}}) + "\n")
    else:
        def add_usage(tokens):
            with transcript.open("a") as f:
                f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": tokens}}}}) + "\n")
    transcript.write_text("")
    add_usage(40000)
    bridge = LaneBridge(tg, key, state / "sent", state / "queue", agent=agent,
                        session=session, lane_store=store)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    done = bridge.compact_internal()
    add_usage(12000)
    if agent == "claude-code":
        bridge._compact_done.touch()
    else:
        with transcript.open("a") as f:
            for rec in ({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "c"}},
                        {"type": "compacted"},
                        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "c"}}):
                f.write(json.dumps(rec) + "\n")
    bridge.outbound_tick()
    assert done.is_set()
    assert store.get(key).ctx_tokens == 12000
    assert store.get(key).last_compact is not None
    assert "before=40000 after=12000" in caplog.text


def test_normal_turn_refreshes_stored_context_for_eviction_decision(tmp_path):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 72, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "normal-turn.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"usage": {
        "input_tokens": 42000, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0}}}) + "\n")
    key = LaneKey(1, 72)
    store = LaneStore(":memory:")
    store.upsert(Lane(1, 72, "topic", "claude-code", "session", "live", True,
                      time.time(), None, 0))
    bridge = LaneBridge(tg, key, state / "sent", state / "queue", agent="claude-code",
                        session=session, lane_store=store)
    bridge._transcript = transcript
    bridge._finish_turn()
    assert store.get(key).ctx_tokens == 42000


def test_successful_internal_compaction_replays_durable_inbox(tmp_path):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 73, tmp_path)
    state.mkdir(parents=True)
    bridge = LaneBridge(tg, LaneKey(1, 73), state / "sent", state / "queue",
                        agent="codex", session=session)
    replayed = []
    bridge._internal_turn = True
    bridge._internal_turn_done = threading.Event()
    bridge._replay_pending_inbound = lambda: replayed.append(True)
    bridge._finish_turn()
    assert replayed == [True]
    assert bridge._internal_turn_done.is_set()


def test_codex_compact_waits_for_matching_turn_id(tmp_path):
    tg, session = Telegram(), Session()
    state = lane_state_dir("test", 1, 9, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    bridge = LaneBridge(tg, LaneKey(1, 9), state / "sent", state / "queue",
                        agent="codex", session=session)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    done = bridge.compact_internal()
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "compact-2"}}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "compact-2"}}) + "\n")
    bridge.outbound_tick()
    assert not done.is_set()
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "compacted"}) + "\n")
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "older-turn"}}) + "\n")
    bridge.outbound_tick()
    assert not done.is_set()
    with transcript.open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "compact-2"}}) + "\n")
    bridge.outbound_tick()
    assert done.is_set()
    assert tg.messages == []


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_declined_compaction_finishes_from_transcript_and_records_it_as_done(tmp_path, agent):
    tg, session = Telegram(), Session()
    state = lane_state_dir("declined", 1, 12, tmp_path)
    state.mkdir(parents=True)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")
    store = LaneStore(tmp_path / "state.db")
    key = LaneKey(1, 12)
    store.upsert(Lane(1, 12, "topic", agent, "sid", "live", False, 1.0, None, 40000))
    bridge = LaneBridge(tg, key, state / "sent", state / "queue", agent=agent,
                        session=session, lane_store=store)
    bridge._transcript = transcript
    bridge._last_resolve = float("inf")
    done = bridge.compact_internal()
    assert session.command_submitted.wait(1)
    decline = {"type": "system", "subtype": "local_command",
               "content": "<local-command-stdout>Not enough messages to compact.</local-command-stdout>"}
    with transcript.open("a") as stream:
        if agent == "codex":
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_started", "turn_id": "compact"}}) + "\n")
        stream.write(json.dumps(decline) + "\n")
    bridge.outbound_tick()
    assert done.is_set()
    # A declined (no-op) compaction counts as done, so idle compaction is not retried
    # until there is new activity; the context count is left as it was.
    assert store.get(key).last_compact is not None
    assert store.get(key).ctx_tokens == 40000
    assert tg.messages == []


def test_summary_error_discards_partial_private_text(tmp_path):
    from agent2telegramtopic.readers import Ev

    bridge = LaneBridge(Telegram(), LaneKey(1, 13), tmp_path / "sent", tmp_path / "queue",
                        agent="codex", session=Session())
    bridge._internal_turn = True
    bridge._internal_kind = "summary"
    bridge._handle_event(Ev("text", "partial commentary", final=False))
    bridge._handle_event(Ev("error", "upstream failed", code="upstream"))
    assert bridge._summary_text == ""
    assert bridge._summary_error == "upstream failed"


def test_summary_keeps_reader_marked_final_answer_over_commentary(tmp_path):
    from agent2telegramtopic.readers import Ev

    bridge = LaneBridge(Telegram(), LaneKey(1, 15), tmp_path / "sent", tmp_path / "queue",
                        agent="codex", session=Session())
    bridge._internal_turn = True
    bridge._internal_kind = "summary"
    bridge._handle_event(Ev("text", "commentary", final=False))
    bridge._handle_event(Ev("text", "final summary", final=True))
    bridge._handle_event(Ev("text", "later nonfinal text", final=False))
    assert bridge._summary_text == "final summary"


def test_internal_timeout_does_not_retire_lane_owned_by_close(tmp_path):
    retired = []
    session = Session()
    session.alive = True
    session.close = lambda: setattr(session, "alive", False)
    bridge = LaneBridge(Telegram(), LaneKey(1, 14), tmp_path / "sent", tmp_path / "queue",
                        agent="claude-code", session=session, on_parked=retired.append)
    bridge._closing = True
    done = bridge.compact_internal()
    assert session.command_submitted.wait(1)
    bridge._expire_internal_turn(done)
    assert done.is_set()
    assert retired == []


def test_restart_kills_without_waiting_and_parks_for_resume(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore

    class RestartSession(Session):
        alive = True

        def close(self):
            self.alive = False

    key = LaneKey(1, 18)
    store = LaneStore(tmp_path / "lanes.db")
    store.upsert(Lane(1, 18, "topic 18", "codex", "same-session", "live", True, 1, None, 0))
    session = RestartSession()
    bridge = LaneBridge(Telegram(), key, tmp_path / "sent", tmp_path / "queue",
                        agent="codex", session=session, lane_store=store,
                        on_parked=lambda parked: store.set_status(parked, "parked"))
    bridge._turn_active.set()
    bridge._internal_turn = True
    assert bridge.restart_session()
    row = store.get(key)
    assert not session.alive
    assert bridge._stop.is_set()
    assert not bridge._turn_active.is_set()
    assert row.status == "waiting" and row.busy is False
    store._db.close()


def test_restart_reports_session_that_remains_alive(tmp_path):
    session = Session()
    session.alive = True
    session.close = lambda: None
    bridge = LaneBridge(Telegram(), LaneKey(1, 19), tmp_path / "sent", tmp_path / "queue",
                        agent="codex", session=session)
    bridge._turn_active.set()
    assert not bridge.restart_session()
    assert bridge._turn_active.is_set()
    assert not bridge._stop.is_set()


def test_restart_replays_durable_inbound_record_on_resumed_bridge(tmp_path):
    key = LaneKey(1, 20)
    old_session = Session()
    old_session.alive = True
    old_session.close = lambda: setattr(old_session, "alive", False)
    old = LaneBridge(Telegram(), key, tmp_path / "sent", tmp_path / "queue",
                     agent="codex", session=old_session)
    old.store_control_text("queued before restart", 8)
    assert old.restart_session()

    resumed_session = Session()
    resumed_session.alive = True
    resumed = LaneBridge(Telegram(), key, tmp_path / "sent", tmp_path / "queue",
                         agent="codex", session=resumed_session)
    resumed._replay_pending_inbound()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not resumed_session.injected:
        time.sleep(0.01)
    assert resumed_session.injected == ["queued before restart"]
