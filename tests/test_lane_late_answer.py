import json
import threading
import time
import pytest

import agent2telegramtopic.lane as lane_mod
from agent2telegramtopic.config import lane_state_dir
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey

class Telegram:
    def __init__(self):
        self.messages = []

    def send_message(self, key, text):
        self.messages.append((key, text))

    def send_chat_action(self, key, action):
        del action
        self.typings = getattr(self, "typings", []) + [key]


def tick(bridge):
    bridge.outbound_tick()


def assistant(text, uid, stamp):
    return {"type": "assistant", "uuid": uid, "timestamp": stamp,
            "message": {"content": [{"type": "text", "text": text}]}}


def setup(tmp_path, client):
    state = lane_state_dir("test-bot", 5, 31, tmp_path)
    state.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "lane" / "claude.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")
    marker = state / "lane_session.json"
    marker.write_text(json.dumps({"session_id": "lane", "transcript_path": str(transcript)}))
    bridge = LaneBridge(client, LaneKey(5, 31), state / "attach_sent.txt", state / "queue",
                        agent="claude-code", session_marker=marker)
    # The tests below drive the existing transcript and backstop directly.
    bridge._transcript = transcript
    bridge._tpos = transcript.stat().st_size
    return bridge, transcript, state


def test_backstop_ignores_previous_turn_and_strips_marker(tmp_path):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    path.write_text(json.dumps(assistant("old answer", "old", "2020-01-01T00:00:00Z")) + "\n")
    tick(bridge)  # Existing transcript is history at attach time.
    bridge._begin_turn()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2))
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "Telegram: prompt"}}) + "\n")
        f.write(json.dumps(assistant("[TG] current answer", "new", stamp)) + "\n")
    (state / "turn_end").touch()
    tick(bridge)
    tick(bridge)
    assert [text for _, text in tg.messages] == ["current answer"]
    assert tg.messages[0][0].thread_id == 31


def test_late_transcript_answer_is_found_during_backstop_retry(tmp_path, monkeypatch):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    bridge._begin_turn()
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "Telegram: prompt"}}) + "\n")
    (state / "turn_end").touch()
    monkeypatch.setattr(lane_mod, "BACKSTOP_RETRY_DELAY", 0.05)

    def write_late_answer():
        time.sleep(0.08)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2))
        with path.open("a") as f:
            f.write(json.dumps(assistant("[TG] arrived after stop", "late", stamp)) + "\n")

    writer = threading.Thread(target=write_late_answer)
    writer.start()
    tick(bridge)
    writer.join()
    tick(bridge)
    assert [text for _, text in tg.messages] == ["arrived after stop"]


def test_turn_end_without_current_answer_does_not_replay_old_text(tmp_path, monkeypatch, caplog):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    path.write_text(json.dumps(assistant("stale", "stale", "2020-01-01T00:00:00Z")) + "\n")
    tick(bridge)
    bridge._begin_turn()
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "Telegram: prompt"}}) + "\n")
    (state / "turn_end").touch()
    monkeypatch.setattr(lane_mod, "BACKSTOP_RETRY_DELAY", 0)
    tick(bridge)
    tick(bridge)
    assert len(tg.messages) == 1
    assert "without an answer" in tg.messages[0][1]
    assert any("ended without an answer" in record.message for record in caplog.records)


def test_compaction_summary_and_meta_records_are_not_user_events():
    reader = lane_mod.readers.ClaudeCodeReader()
    rec = {"type": "user", "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
           "message": {"content": "summary"}}
    assert list(reader.parse(rec)) == []
    assert reader.user_text(rec) is None


def test_meta_record_yields_no_user_event():
    reader = lane_mod.readers.ClaudeCodeReader()
    rec = {"type": "user", "isMeta": True, "message": {"content": "<local-command>"}}
    assert list(reader.parse(rec)) == []


def test_real_prompt_still_yields_user_event():
    reader = lane_mod.readers.ClaudeCodeReader()
    assert [(e.kind, e.text) for e in reader.parse(
        {"type": "user", "message": {"content": "[TG] prompt"}})] == [("user", "[TG] prompt")]


def test_marker_rule_for_late_and_local_text(tmp_path, caplog):
    tg = Telegram()
    bridge, _, _ = setup(tmp_path, tg)
    bridge._turn_from_tg = False
    bridge._handle_event(lane_mod.readers.Ev("text", text="[tg] late answer", key="late"))
    bridge._flush_pending()
    assert [text for _, text in tg.messages] == ["late answer"]
    with caplog.at_level("INFO", logger=lane_mod.log.name):
        bridge._handle_event(lane_mod.readers.Ev("text", text="local only", key="local"))
    assert len(tg.messages) == 1
    assert any("NOT forwarded" in rec.message for rec in caplog.records)
    bridge._marker = ""
    bridge._handle_event(lane_mod.readers.Ev("text", text="private", key="private"))
    assert len(tg.messages) == 1
    bridge._marker = "[TG]"
    assert bridge._has_marker("\n  [tg] yes")
    assert not bridge._has_marker("prefix [tg] no")
    assert not bridge._has_marker("")


def test_empty_marker_forwards_nothing(tmp_path):
    tg = Telegram()
    bridge, _, _ = setup(tmp_path, tg)
    bridge._marker = ""
    bridge._turn_from_tg = False
    bridge._handle_event(lane_mod.readers.Ev("text", text="internal terminal work", key="private"))
    assert tg.messages == []


def test_backstop_skips_already_sent_text(tmp_path, caplog):
    tg = Telegram()
    bridge, path, _ = setup(tmp_path, tg)
    old = assistant("old", "old", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    path.write_text(json.dumps(old) + "\n")
    bridge._sent_keys.add("old")
    bridge._turn_active.set()
    bridge._turn_text_sent = False
    assert bridge._last_assistant_text() is None
    bridge._turn_from_tg = True
    bridge._turn_text_sent = False
    with caplog.at_level("ERROR", logger=lane_mod.log.name):
        bridge._finish_turn()
    bridge._flush_pending()
    assert any("without an answer" in rec.message for rec in caplog.records)
    assert tg.messages == [(bridge.key, "⚠️ The agent ended this turn without an answer. Please send your message again.")]
    assert not bridge._turn_text_sent, "an unanswered turn must not be marked answered"


def test_unsent_recent_text_written_before_turn_began_is_ignored(tmp_path):
    tg = Telegram()
    bridge, path, _ = setup(tmp_path, tg)
    old_line = json.dumps(assistant("previous terminal output", "pre-turn", time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n"
    path.write_text(old_line)
    bridge._tpos = len(old_line.encode("utf-8"))
    bridge._begin_turn()
    assert bridge._last_assistant_text() is None
    new_line = json.dumps(assistant("[TG] current", "current", time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1)))) + "\n"
    with path.open("a") as f:
        f.write(new_line)
    assert bridge._last_assistant_text() == "[TG] current"
    assert bridge._last_backstop_key == "current"


def test_shorter_transcript_still_yields_late_answer(tmp_path):
    tg = Telegram()
    bridge, path, _ = setup(tmp_path, tg)
    bridge._turn_tpos = 900_000
    bridge._turn_active.set()
    path.write_text(json.dumps(assistant("[TG] answer", "short", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n")
    assert bridge._last_assistant_text() == "[TG] answer"


def test_transcript_switch_resets_turn_offset(tmp_path):
    tg = Telegram()
    bridge, path, _ = setup(tmp_path, tg)
    bridge._turn_tpos = 12345
    bridge._turn_active.set()
    bridge._turn_started_wall = time.time()
    other = tmp_path / "lane" / "next.jsonl"
    other.write_text(json.dumps({"type":"assistant", "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "message":{"content":[{"type":"text","text":"x"}]}})+"\n")
    bridge._lane_transcript = lambda: other
    bridge._last_resolve = time.monotonic() - 10.0
    bridge._maybe_reresolve()
    assert bridge._transcript == other
    assert bridge._turn_tpos != 12345
    assert bridge._turn_tpos <= other.stat().st_size


def test_hooked_lane_allows_long_thinking_pause(tmp_path):
    bridge, _, _ = setup(tmp_path, Telegram())
    bridge._turn_end = None
    assert bridge._idle_limit() == lane_mod.IDLE_DONE
    bridge._turn_end = tmp_path / "turn_end"
    assert bridge._idle_limit() == lane_mod.IDLE_DONE_HOOKED
    assert lane_mod.IDLE_DONE_HOOKED >= 300


def test_late_answer_after_thinking_pause_is_delivered_once_by_outbound_tick(tmp_path, monkeypatch):
    tg = Telegram()
    bridge, path, state = setup(tmp_path, tg)
    bridge._begin_turn()
    with path.open("a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "Telegram: prompt"}}) + "\n")
    monkeypatch.setattr(lane_mod, "BACKSTOP_RETRY_DELAY", 0.03)
    (state / "turn_end").touch()
    first_probe = threading.Event()
    original_probe = bridge._last_assistant_text
    probes = [0]

    def signal_retry_probe():
        answer = original_probe()
        probes[0] += 1
        if probes[0] == 2:
            first_probe.set()
        return answer

    bridge._last_assistant_text = signal_retry_probe

    def write_late_answer():
        assert first_probe.wait(1)
        with path.open("a") as f:
            f.write(json.dumps(assistant("[TG] after thinking", "late", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()+1)))) + "\n")

    writer = threading.Thread(target=write_late_answer)
    writer.start()
    bridge.outbound_tick()
    writer.join()
    bridge.outbound_tick()
    assert [text for _, text in tg.messages] == ["after thinking"]
    assert tg.messages[0][0].thread_id == 31


def test_typing_is_throttled_and_only_during_open_telegram_turn(tmp_path):
    tg = Telegram()
    tg.typings = []
    bridge, _, _ = setup(tmp_path, tg)
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    bridge._claude_hook_ready = True
    bridge._last_typing = time.monotonic()
    call_times = []
    original_send_chat_action = tg.send_chat_action
    def record_typing(key, action):
        call_times.append(time.monotonic())
        original_send_chat_action(key, action)
    tg.send_chat_action = record_typing
    waits = [0]
    waits_seen = []
    def counted_wait(seconds):
        waits_seen.append(seconds)
        waits[0] += 1
        if waits[0] == 3:
            bridge._stop.set()
    bridge._stop.wait = counted_wait
    bridge._typing_loop()
    assert len(tg.typings) == 3
    assert waits[0] == 3
    assert waits_seen == [lane_mod.TYPING_INTERVAL] * 3
    assert len(call_times) == 3
    assert all(key == bridge.key for key in tg.typings)
    bridge._stop.clear()
    bridge._turn_active.clear()
    bridge._turn_from_tg = True
    bridge._stop.wait = lambda _: bridge._stop.set()
    before = len(tg.typings)
    bridge._typing_loop()
    assert len(tg.typings) == before


def test_typing_stops_after_unhooked_idle_window(tmp_path):
    bridge, _, _ = setup(tmp_path, Telegram())
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    bridge._claude_hook_ready = False
    bridge._last_activity = time.monotonic() - lane_mod.IDLE_DONE - 1
    bridge._turn_started = bridge._last_activity
    assert not bridge._busy_for_notice()


def test_typing_send_error_does_not_stop_loop(tmp_path):
    class FailingTelegram(Telegram):
        def send_chat_action(self, key, action):
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            super().send_chat_action(key, action)
    tg = FailingTelegram()
    bridge, _, _ = setup(tmp_path, tg)
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    count = [0]
    def wait(_):
        count[0] += 1
        if count[0] == 2:
            bridge._stop.set()
    bridge._stop.wait = wait
    bridge._typing_loop()
    assert tg.calls == 2
    assert tg.typings == [bridge.key]


def test_two_lanes_type_only_in_their_topics(tmp_path):
    tg = Telegram()
    a = LaneBridge(tg, LaneKey(5, 31), tmp_path/"a"/"sent", tmp_path/"a"/"q")
    b = LaneBridge(tg, LaneKey(5, 32), tmp_path/"b"/"sent", tmp_path/"b"/"q")
    a._turn_active.set(); b._turn_active.set()
    a._turn_from_tg = b._turn_from_tg = True
    for bridge in (a, b):
        bridge._stop.wait = lambda _: bridge._stop.set()
        bridge._typing_loop()
    assert tg.typings == [a.key, b.key]
