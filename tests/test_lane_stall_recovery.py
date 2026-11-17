import json
import time

import agent2telegramtopic.lane as lane_module
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey


class Telegram:
    def __init__(self):
        self.messages = []

    def send_message(self, key, text, **kwargs):
        self.messages.append((key, text))

    def send_chat_action(self, key, action):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Session:
    alive = True

    def __init__(self):
        self.injected = []

    def inject(self, text, **kwargs):
        self.injected.append(text)

    def close(self):
        self.alive = False

    def dialog_snapshot(self):
        return "", True


def make_bridge(tmp_path, *, stall_restart_s=900, session=None):
    tg = Telegram()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    bridge = LaneBridge(tg, LaneKey(5, 31), tmp_path / "state" / "sent",
                        tmp_path / "queue", agent="claude-code", session=session or Session())
    bridge._stall_restart_s = stall_restart_s
    bridge._transcript = transcript
    bridge._turn_end = tmp_path / "turn_end"
    bridge._last_resolve = float("inf")
    return bridge, transcript, tg


def update(message_id, text=None, **fields):
    message = {"message_id": message_id, "chat": {"id": 5}, "message_thread_id": 31,
               "from": {"id": 99}, **fields}
    if text is not None:
        message["text"] = text
    return {"update_id": message_id, "message": message}


def test_outbound_tick_restarts_and_replays_control_then_unanswered_updates(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(lane_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(lane_module.time, "monotonic", lambda: clock[0])
    old_session = Session()
    bridge, transcript, tg = make_bridge(tmp_path, session=old_session)
    sent = []
    monkeypatch.setattr(bridge, "_send_final", lambda text, **kwargs: sent.append(text))
    transcript.write_text(json.dumps({"type": "api-request", "timestamp": "2026-11-17T09:47:59Z"}) + "\n")
    bridge._handle(update(1, "original question"))
    bridge._drain_transcript()
    bridge._handle(update(2, "first unanswered"))
    bridge._handle(update(3, "second unanswered"))

    clock[0] += 601
    bridge.outbound_tick()
    notices = [text for text in sent if "No progress from the agent" in text]
    assert notices and "waiting for a model reply" in notices[0]
    from datetime import datetime, timezone
    assert f"since {datetime.fromtimestamp(1794908879, timezone.utc).astimezone():%H:%M} local" in notices[0]
    assert bridge._stall_armed
    assert "in 5 min" in notices[0]

    clock[0] += 900
    bridge.outbound_tick()
    bridge.outbound_tick()
    assert not old_session.alive
    assert not bridge._stall_armed
    recovery = next(text for _, text in tg.messages if "Agent was stuck" in text)
    assert "resending 2 unanswered message(s)" in recovery

    resumed_session = Session()
    resumed = LaneBridge(Telegram(), bridge.key, bridge._sent_path, bridge._queue_root,
                         agent="claude-code", session=resumed_session)
    assert len(resumed._ensure_inbox().pending()) == 3
    assert resumed._replay_pending_inbound() == 3
    deadline = time.perf_counter() + 2
    while time.perf_counter() < deadline and len(resumed_session.injected) < 3:
        time.sleep(0.01)
    assert len(resumed_session.injected) == 3
    since_local = datetime.fromtimestamp(1794908879, timezone.utc).astimezone().strftime("%H:%M")
    assert resumed_session.injected[0].startswith(f"[TG][AUTO] [a2tt] Your session was restarted after a stall (waiting for a model reply since {since_local}).")
    assert resumed_session.injected[1:] == ["first unanswered", "second unanswered"]


def test_passthrough_command_does_not_arm_or_get_remembered(tmp_path):
    bridge, _, tg = make_bridge(tmp_path)
    bridge._stall_progress_wall -= 7200
    bridge._last_activity -= 700
    assert bridge._handle(update(1, "/model"))
    bridge.outbound_tick()
    assert not bridge._stall_armed
    assert bridge._stall_messages == []
    assert not any("No progress" in text for _, text in tg.messages)


def test_open_dialog_skips_recovery_and_stays_armed(tmp_path, monkeypatch):
    bridge, _, _ = make_bridge(tmp_path)
    bridge._stall_armed = True
    bridge._stall_restart_s = 1
    bridge._stall_progress_wall = time.time() - 10
    bridge._dialog_bubble = object()
    restarted = []
    monkeypatch.setattr(bridge, "restart_session", lambda: restarted.append(True))
    bridge._stall_recovery_tick()
    assert restarted == []
    assert bridge._stall_armed


def test_turn_begin_does_not_clear_remembered_stall_messages(tmp_path):
    bridge, _, _ = make_bridge(tmp_path)
    bridge._stall_messages.append({"text": "keep me"})
    bridge._begin_turn()
    assert [item["text"] for item in bridge._stall_messages] == ["keep me"]


def test_only_allowlisted_transcript_records_count_as_progress_and_diagnostic(tmp_path):
    bridge, path, _ = make_bridge(tmp_path)
    path.write_text(json.dumps({"type": "api-request", "timestamp": "2026-11-17T09:47:59Z"}) + "\n")
    bridge._drain_transcript()
    progress = bridge._stall_progress_wall
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "queue-operation", "operation": "enqueue"}) + "\n")
        handle.write(json.dumps({"type": "cost-state"}) + "\n")
        handle.write(json.dumps({"type": "tools-used"}) + "\n")
    bridge._drain_transcript()
    assert bridge._stall_progress_wall == progress
    assert bridge._stall_diagnostic()[0] == "waiting for a model reply"


def test_queue_outstanding_messages_survive_progress_until_dequeued(tmp_path):
    bridge, path, _ = make_bridge(tmp_path)
    bridge._stall_messages.append({"text": "unanswered"})
    path.write_text(json.dumps({"type": "queue-operation", "operation": "enqueue"}) + "\n")
    bridge._drain_transcript()
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "assistant"}) + "\n")
    bridge._drain_transcript()
    assert bridge._stall_messages == [{"text": "unanswered"}]
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "queue-operation", "operation": "dequeue"}) + "\n")
    bridge._drain_transcript()
    assert bridge._stall_queue_outstanding == 0
    assert bridge._stall_messages == []


def test_stop_hook_end_clears_unmatched_queue_and_stall_messages(tmp_path):
    bridge, _, _ = make_bridge(tmp_path)
    bridge._stall_queue_outstanding = 1
    bridge._stall_messages.append({"text": "unanswered"})
    bridge._turn_end.touch()

    bridge.outbound_tick()

    assert bridge._stall_queue_outstanding == 0
    assert bridge._stall_messages == []


def test_media_caption_and_voice_updates_are_remembered_but_reactions_are_not(tmp_path):
    bridge, _, _ = make_bridge(tmp_path)
    bridge._stall_armed = True
    bridge._remember_stall_message(update(1, caption="photo caption", photo=[{"file_id": "p"}]))
    bridge._remember_stall_message(update(2, voice={"file_id": "v"}))
    bridge._remember_stall_message({"message_reaction": {"message_id": 3}})
    assert [item["message_id"] for item in bridge._stall_messages] == [1, 2]


def test_control_text_is_not_remembered_across_consecutive_recoveries(tmp_path, monkeypatch):
    bridge, _, _ = make_bridge(tmp_path)
    bridge._stall_armed = True
    controls = []
    monkeypatch.setattr(bridge, "restart_session", lambda: True)
    monkeypatch.setattr(bridge, "store_control_text", lambda text, sender_id, **kwargs: controls.append(text))
    bridge._stall_progress_wall -= bridge._stall_restart_s + 1
    bridge._stall_recovery_tick()
    first = {"_a2tt_control_send": True, "message": {"text": controls[-1]}}
    bridge._remember_stall_message(first)

    bridge._stall_armed = True
    bridge._stall_progress_wall -= bridge._stall_restart_s + 1
    bridge._stall_recovery_tick()
    second = {"_a2tt_control_send": True, "message": {"text": controls[-1]}}
    bridge._remember_stall_message(second)

    assert len(controls) == 2
    assert bridge._stall_messages == []


def test_notice_reports_remaining_threshold_minutes(tmp_path, monkeypatch):
    bridge, _, tg = make_bridge(tmp_path)
    now = time.time()
    bridge._stall_progress_wall = now - 601
    sent = []
    monkeypatch.setattr(bridge, "_send_final", lambda text, **kwargs: sent.append(text))
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    bridge._inactivity_stall_notice = ("idle", now - 601)
    bridge._finish_turn()
    notice = next(text for text in sent if "No progress" in text)
    assert "in 5 min" in notice


def test_diagnostic_replaces_invalid_utf8_and_catches_value_error(tmp_path, monkeypatch):
    bridge, path, _ = make_bridge(tmp_path)
    path.write_bytes(b'{"type":"api-request","timestamp":"2026-11-17T09:47:59Z"}\n\xff\n')
    assert bridge._stall_diagnostic()[0] == "waiting for a model reply"
    path.unlink()
    assert bridge._stall_diagnostic()[0] == "idle"

    class InvalidPath:
        def open(self, *args, **kwargs):
            raise ValueError("invalid path encoding")

    bridge._transcript = InvalidPath()
    assert bridge._stall_diagnostic()[0] == "idle"


def test_dangling_subagent_symlink_does_not_block_turn_end_notice(tmp_path):
    bridge, path, _ = make_bridge(tmp_path)
    subagents = path.parent / path.stem / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "missing.jsonl").symlink_to(subagents / "absent.jsonl")
    sent = []
    bridge._inactivity_stall_notice = ("idle", time.time() - 601)
    bridge._turn_active.set()
    bridge._turn_from_tg = True
    bridge._turn_text_sent = False
    bridge._turn_is_reaction = False
    bridge._owner_chat = bridge.key.chat_id
    bridge._send_final = lambda text, **kwargs: sent.append(text)

    bridge._finish_turn()

    assert not bridge._turn_active.is_set()
    assert sum("No progress" in text for text in sent) == 1

def test_subagent_growth_is_progress_without_activity_reset(tmp_path, monkeypatch):
    bridge, path, _ = make_bridge(tmp_path)
    subagents = path.parent / path.stem / "subagents"
    subagents.mkdir(parents=True)
    agent = subagents / "agent.jsonl"
    agent.write_text("{}\n")
    bridge._poll_subagent_progress()
    bridge._last_activity = 12.5
    bridge._stall_armed = True
    with agent.open("a") as handle:
        handle.write("{}\n")
    bridge._poll_subagent_progress()
    assert bridge._last_activity == 12.5
    assert not bridge._stall_armed

