"""Tests for reporting a refused turn to the owner (commit ec47dc2, 2026-08-27).

Live on 2026-08-26: Codex was out of credits and refused four messages in a row; the owner got
the same year-old sentence back every time and was never told about the limit. These tests
exercise the real production path end to end — ``readers.*.parse()`` → ``LaneBridge.
_handle_event()`` → ``_finish_turn()`` — and assert only the OUTCOME the owner ends up seeing on
Telegram, never the internal mechanism (AGENTS.md: "state the outcome, not the mechanism").
"""
import json

from agent2telegramtopic.config import lane_state_dir
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.readers import ClaudeCodeReader, CodexReader
from agent2telegramtopic.routing import LaneKey


class Telegram:
    def __init__(self):
        self.messages = []

    def send_message(self, key, text):
        self.messages.append((key, text))

    def send_chat_action(self, key, action):
        del action
        pass


def bridge(tmp_path, tg, thread, agent):
    state = lane_state_dir("test-bot", 5, thread, tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / str(thread) / "transcript.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("")
    result = LaneBridge(tg, LaneKey(5, thread), state / "attach_sent.txt", state / "queue",
                        agent=agent)
    result._transcript = transcript
    result._tpos = transcript.stat().st_size
    result._begin_turn()
    return result


def codex_task_complete(*, codex_error_info, message):
    """Shape codex-cli actually writes for a refused turn: task_complete with an error object,
    no last_agent_message."""
    return {"type": "event_msg", "payload": {
        "type": "task_complete", "turn_id": "t-1", "last_agent_message": None,
        "error": {"codex_error_info": codex_error_info, "message": message}}}


def claude_api_error_rec(*, error, api_error_status=None, text=""):
    """Shape Claude Code writes for a refused turn: a synthetic assistant record with
    isApiErrorMessage set."""
    rec = {"type": "assistant", "isApiErrorMessage": True, "uuid": "err-1",
           "error": error, "message": {"content": [{"type": "text", "text": text}]}}
    if api_error_status is not None:
        rec["apiErrorStatus"] = api_error_status
    return rec


def finish_error(bridge, reader, rec):
    for event in reader.parse(rec):
        bridge._handle_event(event)
    bridge._finish_turn()
    bridge._flush_pending()


def test_codex_usage_limit_not_old_transcript_or_empty_turn_notice(tmp_path):
    tg = Telegram()
    b = bridge(tmp_path, tg, 31, "codex")
    reader = CodexReader()
    # An old answer sitting in the transcript from a PREVIOUS turn — the exact live bug
    # would have forwarded this instead of saying anything about the limit.
    b._transcript.write_text(json.dumps({"type": "event_msg", "payload": {
        "type": "agent_message", "message": "an old reply from a previous turn"},
        "timestamp": "2020-01-01T00:00:00Z"}) + "\n")
    finish_error(b, reader, codex_task_complete(
        codex_error_info="usage_limit_exceeded", message="You've hit your usage limit for this plan."))
    assert len(tg.messages) == 1
    key, text = tg.messages[0]
    assert key.chat_id == 5
    assert key.thread_id == 31
    assert "Usage limit reached" in text
    assert "You've hit your usage limit for this plan." in text
    assert "an old reply from a previous turn" not in text
    assert "without an answer" not in text


def test_other_codex_error_uses_generic_wording(tmp_path):
    tg = Telegram()
    b = bridge(tmp_path, tg, 31, "codex")
    finish_error(b, CodexReader(), codex_task_complete(
        codex_error_info="server_error", message="Upstream returned a 503."))
    assert len(tg.messages) == 1
    assert tg.messages[0][0].chat_id == 5
    text = tg.messages[0][1]
    assert "Usage limit reached" not in text
    assert "reported an error" in text
    assert "Upstream returned a 503." in text
    assert "without an answer" not in text


def test_claude_rate_limit_field_uses_limit_wording(tmp_path):
    tg = Telegram()
    b = bridge(tmp_path, tg, 31, "claude-code")
    finish_error(b, ClaudeCodeReader(), claude_api_error_rec(
        error="rate_limit", text="Claude usage limit reached."))
    assert len(tg.messages) == 1
    assert tg.messages[0][0].chat_id == 5
    assert tg.messages[0][0].thread_id == 31
    assert "Usage limit reached" in tg.messages[0][1]


def test_claude_429_status_uses_limit_wording(tmp_path):
    """The reason can arrive as a numeric HTTP status instead of the string 'rate_limit'."""
    tg = Telegram()
    b = bridge(tmp_path, tg, 31, "claude-code")
    finish_error(b, ClaudeCodeReader(), claude_api_error_rec(
        error="overloaded", api_error_status=429, text="Too many requests."))
    assert len(tg.messages) == 1
    assert tg.messages[0][0].chat_id == 5
    assert tg.messages[0][0].thread_id == 31
    assert "Usage limit reached" in tg.messages[0][1]


def test_claude_non_limit_error_uses_generic_wording(tmp_path):
    tg = Telegram()
    b = bridge(tmp_path, tg, 31, "claude-code")
    finish_error(b, ClaudeCodeReader(), claude_api_error_rec(
        error="not_logged_in", text="Please log in again."))
    assert len(tg.messages) == 1
    text = tg.messages[0][1]
    assert tg.messages[0][0].chat_id == 5
    assert tg.messages[0][0].thread_id == 31
    assert "Usage limit reached" not in text
    assert "reported an error" in text
    assert "Please log in again." in text


def test_error_is_delivered_once_only_to_its_lane_topic(tmp_path):
    tg = Telegram()
    a = bridge(tmp_path, tg, 41, "codex")
    b = bridge(tmp_path, tg, 42, "codex")
    # Give B an independent reply to prove its own outbox is active and topic-bound.
    for event in CodexReader().parse({"type": "event_msg", "payload": {
            "type": "agent_message", "message": "reply B"}}):
        b._handle_event(event)
    b._flush_pending()
    err = codex_task_complete(codex_error_info="server_error", message="failure A")
    finish_error(a, CodexReader(), err)
    for event in CodexReader().parse(err):
        a._handle_event(event)
    a._flush_pending()
    # Re-open A from its durable ledger/outbox after restart and replay its error event.
    restarted_a = LaneBridge(tg, LaneKey(5, 41), a._sent_path, a._queue_root,
                             agent="codex")
    restarted_a._begin_turn()
    for event in CodexReader().parse(err):
        restarted_a._handle_event(event)
    restarted_a._flush_pending()
    b._flush_pending()
    assert [key.thread_id for key, _ in tg.messages] == [42, 41]
    assert tg.messages[0][1] == "reply B"
    assert "failure A" in tg.messages[1][1]
    assert sum("failure A" in text for key, text in tg.messages if key.thread_id == 41) == 1
    assert all(key.thread_id != 42 or "failure A" not in text for key, text in tg.messages)
    assert all("without an answer" not in text for _, text in tg.messages)
