"""Lane adapted port of agent2telegram/tests/test_attach_backstop.py.

The following upstream tests are individually omitted because their features are outside the
lane text turn half and do not exist in LaneBridge:

* test_reaction_without_a_reply_forwards_nothing, test_reaction_turn_still_delivers_an_explicit_reply,
  test_reaction_prompt_asks_for_a_short_answer, and
  test_reaction_during_a_running_turn_keeps_the_backstop_armed live in tests/test_reactions.py.
* test_no_bubble_is_created_outside_a_turn and test_bubble_is_still_created_during_a_turn live in
  tests/test_status_bubble.py.

* test_empty_transcript_and_signal_logs_error_without_crashing and
  test_already_delivered_message_is_not_sent_again intentionally differ from a2t: a2t only logs
  an empty turn and asserts no Telegram send, while SPEC v0.3 item 3 requires one user-visible
  empty-turn notice; the already-delivered case asserts that notice while suppressing stale text.
"""

import json
import time
from datetime import datetime, timezone
import pytest

from agent2telegramtopic.config import lane_state_dir
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey

class Telegram:
    def __init__(self):
        self.messages, self.actions = [], []

    def send_message(self, key, text):
        self.messages.append((key, text))

    def send_chat_action(self, key, action):
        del action
        self.actions.append((key, "typing"))


def record(kind, payload, stamp=None, uid=None):
    rec = {"type": kind}
    if stamp:
        rec["timestamp"] = stamp
    if uid:
        rec["uuid"] = uid
    if kind == "user":
        rec["message"] = {"content": payload}
    else:
        rec["message"] = {"content": [{"type": "text", "text": payload}]}
    return rec


def setup(tmp_path, tg, thread=31):
    state = lane_state_dir("test-bot", 5, thread, tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "lanes" / str(thread) / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = state / "lane_session.json"
    marker.write_text(json.dumps({"session_id": f"s{thread}", "transcript_path": str(path)}))
    bridge = LaneBridge(tg, LaneKey(5, thread), state / "attach_sent.txt", state / "queue",
                        agent="claude-code", session_marker=marker)
    path.write_text("")
    bridge._transcript = path
    bridge._tpos = path.stat().st_size
    return bridge, path, state


def cycle(bridge):
    bridge._drain_transcript()
    bridge._flush_pending()


def test_empty_turn_notifies_once_without_old_answer(tmp_path):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    path.write_text(json.dumps(record("assistant", "stale", "2020-01-01T00:00:00Z", "old")) + "\n")
    cycle(bridge)
    bridge._begin_turn()
    # Old transcript content is not part of this turn; append the prompt and signal completion.
    with path.open("a") as f:
        f.write(json.dumps(record("user", "Telegram: prompt")) + "\n")
    (state / "turn_end").touch()
    cycle(bridge)
    bridge._finish_turn()
    bridge._flush_pending()

    assert len(tg.messages) == 1
    assert tg.messages[0][0].thread_id == 31
    assert "without an answer" in tg.messages[0][1]


def test_restart_mid_turn_delivers_remaining_transcript_once(tmp_path):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    bridge._begin_turn()
    with path.open("a") as f:
        f.write(json.dumps(record("user", "Telegram: prompt")) + "\n")
        f.write(json.dumps(record("assistant", "first", uid="first")) + "\n")
    cycle(bridge)
    cycle(bridge)
    assert [text for _, text in tg.messages] == ["first"]

    # Simulate an assistant record written while the bridge is down.
    with path.open("a") as f:
        f.write(json.dumps(record("assistant", "second", uid="second")) + "\n")
    restarted = LaneBridge(tg, LaneKey(5, 31), state / "attach_sent.txt", state / "queue",
                           agent="claude-code", session_marker=state / "lane_session.json")
    restarted._maybe_reresolve()
    # Resume the current user turn from its transcript boundary. The persisted sent ledger
    # suppresses the first reply while the still-undelivered second reply is recovered.
    restarted._resume_position()
    cycle(restarted)
    restarted._flush_pending()
    assert [text for _, text in tg.messages] == ["first", "second"]


def test_lane_start_recovers_reply_written_while_bridge_was_down(tmp_path):
    tg = Telegram()
    key = LaneKey(5, 31)
    state = lane_state_dir("test-bot", 5, 31, tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "lanes" / "31" / "transcript.jsonl"
    path.parent.mkdir(parents=True)
    marker = state / "lane_session.json"
    marker.write_text(json.dumps({"session_id": "s31", "transcript_path": str(path)}))
    path.write_text("")

    created = []
    def factory(_key):
        lane = LaneBridge(tg, key, state / "attach_sent.txt", state / "queue",
                          agent="claude-code", session_marker=marker)
        lane._session = object()
        lane._typing_loop = lambda: None
        created.append(lane)
        return lane

    first_mux = Multiplexer(tg, {8}, tmp_path / "bot-first", factory)
    first_mux._start_lane(key)
    bridge = first_mux._lanes[key]
    bridge._begin_turn()
    with path.open("a") as transcript_file:
        transcript_file.write(json.dumps(record("user", "Telegram: prompt")) + "\n")
        transcript_file.write(json.dumps(record("assistant", "reply 1", uid="reply-1")) + "\n")
    deadline = time.monotonic() + 2
    while len(tg.messages) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [text for _, text in tg.messages] == ["reply 1"]
    bridge.stop()
    time.sleep(0.5)  # let the first bridge's outbound loop observe the stop

    with path.open("a") as transcript_file:
        transcript_file.write(json.dumps(record("assistant", "reply 2", _stamp(time.time()), "reply-2")) + "\n")

    second_mux = Multiplexer(tg, {8}, tmp_path / "bot", factory)
    second_mux._start_lane(key)
    deadline = time.monotonic() + 2
    while len(tg.messages) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.5)  # further outbound ticks must not resend reply 2
    second_mux._lanes[key]._stop.set()

    assert [text for _, text in tg.messages] == ["reply 1", "reply 2"]


def test_two_lanes_drain_and_type_only_in_their_own_topics(tmp_path):
    tg = Telegram()
    a, path_a, _ = setup(tmp_path, tg, 41)
    b, path_b, _ = setup(tmp_path, tg, 42)
    for bridge, path, reply in ((a, path_a, "reply A"), (b, path_b, "reply B")):
        bridge._begin_turn()
        with path.open("a") as f:
            f.write(json.dumps(record("user", "Telegram: prompt")) + "\n")
            f.write(json.dumps(record("assistant", reply, uid=f"{reply}-id")) + "\n")
        cycle(bridge)
        cycle(bridge)
    assert [(key.thread_id, text) for key, text in tg.messages] == [(41, "reply A"), (42, "reply B")]
    assert tg.actions and {key.thread_id for key, _ in tg.actions} == {41, 42}


def _upstream_setup(tmp_path, thread=51):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg, thread)
    bridge._begin_turn()
    return tg, bridge, path, state


def _assistant(text, uid, stamp=None):
    rec = {"type": "assistant", "uuid": uid,
           "message": {"content": [{"type": "text", "text": text}]}}
    if stamp is not None:
        rec["timestamp"] = stamp
    return rec


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def test_retry_reads_transcript_after_initial_empty_result(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    path.write_text("")
    bridge._turn_tpos = 0
    original_wait = bridge._wait_backstop_retry
    wrote = False
    def delayed_record():
        nonlocal wrote
        if not wrote:
            wrote = True
            with path.open("a") as f:
                f.write(json.dumps(_assistant("[TG] final from transcript", "final", _stamp(time.time()))) + "\n")
        original_wait()
    monkeypatch.setattr(bridge, "_wait_backstop_retry", delayed_record)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["final from transcript"]


def test_fallback_sends_signal_file_when_transcript_stays_empty(tmp_path, monkeypatch):
    tg, bridge, _, _ = _upstream_setup(tmp_path)
    bridge._transcript = None
    bridge._signal.write_text("[TG] final from signal", "utf-8")
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["final from signal"]
    assert not bridge._signal.exists()


def test_empty_transcript_and_signal_logs_error_and_notifies_once(tmp_path, monkeypatch, caplog):
    tg, bridge, _, _ = _upstream_setup(tmp_path)
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert len(tg.messages) == 1
    assert "without an answer" in tg.messages[0][1]
    assert any("Telegram turn ended without an answer" in r.message for r in caplog.records)
    assert any("typing_count=1" in r.message for r in caplog.records)
    # A repeated end notification after completion must not create another user-visible notice.
    bridge._finish_turn()
    bridge._flush_pending()
    assert len(tg.messages) == 1


def test_already_sent_turn_text_is_not_sent_again(tmp_path, monkeypatch):
    tg, bridge, _, _ = _upstream_setup(tmp_path)
    bridge._send_final("already sent", key="sent")
    bridge._flush_pending()
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["already sent"]


def test_already_delivered_message_is_not_sent_again(tmp_path, monkeypatch, caplog):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    bridge._sent_path.parent.mkdir(parents=True, exist_ok=True)
    bridge._sent_path.write_text("delivered\n")
    # Construct a fresh bridge so its durable ledger loader sees the prior delivery.
    bridge = LaneBridge(tg, bridge.key, bridge._sent_path, bridge._queue_root,
                        agent="claude-code", session_marker=bridge.session_marker)
    bridge._transcript = path
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    bridge._turn_started_wall = time.time()
    path.write_text(json.dumps({"type":"user", "timestamp":_stamp(time.time()),
                                "message":{"content":"Telegram: prompt"}}) + "\n" +
                    json.dumps(_assistant("[TG] already delivered", "delivered", _stamp(time.time()))) + "\n")
    bridge._turn_tpos = 0
    bridge._tpos = path.stat().st_size  # The sent-key ledger suppresses this timed record.
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert len(tg.messages) == 1
    assert "without an answer" in tg.messages[0][1]
    assert not any("already delivered" in text for _, text in tg.messages)
    assert not any("forwarded final answer" in record.message for record in caplog.records)


def test_genuinely_new_answer_still_goes_out(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    bridge._sent_keys.add("old")
    path.write_text(json.dumps(_assistant("[TG] brand new answer", "new", _stamp(time.time()))) + "\n")
    bridge._turn_tpos = 0
    bridge._tpos = path.stat().st_size
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["brand new answer"]


def test_backstop_key_comes_from_transcript_scan(tmp_path):
    tg = Telegram()
    bridge, path, _ = setup(tmp_path, tg, 52)
    path.write_text("")

    bridge._last_assistant_text()

    assert bridge._last_backstop_key is None


def test_previous_turn_answer_is_never_sent(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    now = time.time()
    path.write_text(json.dumps(_assistant("old answer", "old", _stamp(now - 3600))) + "\n")
    bridge._turn_started_wall = now
    bridge._turn_tpos = 0
    bridge._tpos = path.stat().st_size
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert not any("old answer" in text for _, text in tg.messages)


def test_current_turn_answer_is_delivered(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    now = time.time()
    path.write_text(json.dumps(_assistant("[TG] current answer", "new", _stamp(now + 2))) + "\n")
    bridge._turn_started_wall = now
    bridge._turn_tpos = 0
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["current answer"]


def test_only_current_answer_survives(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    now = time.time()
    path.write_text("\n".join(json.dumps(r) for r in (
        _assistant("old answer", "old", _stamp(now - 3600)),
        _assistant("[TG] actual answer", "new", _stamp(now + 2)))) + "\n")
    bridge._turn_started_wall = now
    bridge._turn_tpos = 0
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["actual answer"]


def test_undelivered_answer_is_selected_before_later_delivered_text(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    bridge._sent_keys.add("delivered")
    now = time.time()
    path.write_text("\n".join(json.dumps(r) for r in (
        _assistant("[TG] this is the new answer", "new", _stamp(now)),
        _assistant("already delivered later", "delivered", _stamp(now + 1)))) + "\n")
    bridge._turn_tpos = 0
    bridge._tpos = path.stat().st_size
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["this is the new answer"]


def test_each_consecutive_empty_turn_gets_its_own_notice(tmp_path, monkeypatch):
    tg, bridge, _, _ = _upstream_setup(tmp_path)
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    with bridge._transcript.open("a") as f:
        f.write(json.dumps(record("user", "Telegram: first")) + "\n")
    bridge._drain_transcript()
    bridge._finish_turn()
    bridge._flush_pending()
    bridge._begin_turn()
    with bridge._transcript.open("a") as f:
        f.write(json.dumps(record("user", "Telegram: second")) + "\n")
    bridge._drain_transcript()
    bridge._finish_turn()
    bridge._flush_pending()
    assert len(tg.messages) == 2
    assert all("without an answer" in text for _, text in tg.messages)


def test_empty_turns_without_transcript_growth_each_get_a_notice(tmp_path, monkeypatch):
    tg, bridge, _, _ = _upstream_setup(tmp_path)
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    bridge._begin_turn()
    bridge._finish_turn()
    bridge._flush_pending()
    assert len(tg.messages) == 2
    assert all("without an answer" in text for _, text in tg.messages)


def test_record_without_usable_timestamp_is_never_sent(tmp_path, monkeypatch):
    tg, bridge, path, _ = _upstream_setup(tmp_path)
    path.write_text(json.dumps(_assistant("untimed answer", "untimed")) + "\n")
    bridge._turn_started_wall = time.time()
    bridge._turn_tpos = 0
    bridge._tpos = path.stat().st_size  # The untimed record is excluded by the backstop timestamp floor.
    monkeypatch.setattr("agent2telegramtopic.lane.BACKSTOP_RETRY_DELAY", 0)
    bridge._finish_turn()
    bridge._flush_pending()
    assert not any("untimed answer" in text for _, text in tg.messages)
    assert len(tg.messages) == 1
    assert "without an answer" in tg.messages[0][1]
