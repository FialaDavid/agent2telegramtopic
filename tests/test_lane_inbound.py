from pathlib import Path
import json
import threading
import time

import pytest

from agent2telegramtopic.durable import DurableInbox
from agent2telegramtopic.lane import INBOUND_MAX_ATTEMPTS, INJECT_ATTEMPTS, LaneBridge
from agent2telegramtopic.lane import MAX_INBOUND_PROMPT_CHARS
from agent2telegramtopic.routing import LaneKey, reply_params
from agent2telegramtopic.session import BLOCKED_BY_OVERLAY, SessionError
from agent2telegramtopic import stt


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.file_paths = []
        self.downloads = []

    def send_message(self, key, text):
        self.calls.append((key, text, reply_params(key)))

    def send_chat_action(self, key, action):
        del action
        pass

    def get_file_path(self, file_id):
        self.file_paths.append(file_id)
        return f"files/{file_id}.jpg"

    def download(self, file_path):
        self.downloads.append(file_path)
        return b"FILE-BYTES"


class FakeSession:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls = []

    def inject(self, text):
        self.calls.append(text)
        if self.errors:
            raise self.errors.pop(0)


def make_lane(tmp_path: Path, key=LaneKey(44, 12), client=None):
    state = tmp_path / str(key.thread_id)
    lane = LaneBridge(client or FakeTelegram(), key, state / "sent", state / "queue")
    lane._session = FakeSession()
    lane._stop.wait = lambda _: None
    return lane


def test_injection_retries_and_notifies_once_in_own_topic(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    lane._session = FakeSession([OSError("busy"), OSError("busy"), OSError("busy")])
    assert lane._inject("hello") is False
    assert len(lane._session.calls) == INJECT_ATTEMPTS
    assert len(tg.calls) == 1
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 12}
    assert "failed after 3 attempts" in tg.calls[0][1]

    lane._session = FakeSession([SessionError(BLOCKED_BY_OVERLAY)] * INJECT_ATTEMPTS)
    assert lane._inject("again") is False
    assert "interactive dialog" in tg.calls[-1][1]


def test_failed_inbound_record_remains_then_dead_letters(tmp_path):
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    record_id = inbox.reserve({"update_id": 1, "message": {"text": "hello"}})
    entered = threading.Event()
    def not_delivered(_upd):
        entered.set()
        return False
    lane._handle = not_delivered
    for attempt in range(INBOUND_MAX_ATTEMPTS):
        entered.clear()
        lane._submit_inbound_update(inbox.pending()[0].update, record_id)
        assert entered.wait(2)
        lane._inbound_queue.join()
        if attempt + 1 < INBOUND_MAX_ATTEMPTS:
            assert [r.record_id for r in inbox.pending()] == [record_id]
    assert inbox.pending() == []
    assert [r.record_id for r in inbox.dead_letters()] == [record_id]


def test_failed_inbound_retries_from_outbound_tick_until_dead_letter(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("agent2telegramtopic.lane.time.monotonic", lambda: now[0])
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    # Establish the retry timestamp before there is a pending delivery.
    lane._maybe_reresolve = lambda: None
    lane._flush_pending = lambda: None
    lane._drain_transcript = lambda: None
    lane._drain_signal = lambda: None
    lane._beat = lambda: None
    lane.outbound_tick()
    record_id = inbox.reserve({"update_id": 101, "message": {"text": "retry me"}})
    # Every injection attempt fails. Retry scheduling is driven by this fake clock.
    lane._session = FakeSession([OSError("busy")] * INJECT_ATTEMPTS * INBOUND_MAX_ATTEMPTS)
    lane._maybe_reresolve = lambda: None
    lane._flush_pending = lambda: None
    lane._drain_transcript = lambda: None
    lane._drain_signal = lambda: None
    lane._beat = lambda: None

    lane._submit_inbound_update(inbox.pending()[0].update, record_id)
    lane._inbound_queue.join()
    assert len(lane._session.calls) == INJECT_ATTEMPTS
    assert [record.record_id for record in inbox.pending()] == [record_id]

    now[0] += 29.0
    lane.outbound_tick()
    lane._inbound_queue.join()
    assert len(lane._session.calls) == INJECT_ATTEMPTS
    assert [record.record_id for record in inbox.pending()] == [record_id]

    now[0] += 2.0
    lane.outbound_tick()
    lane._inbound_queue.join()
    assert len(lane._session.calls) == INJECT_ATTEMPTS * 2
    assert [record.record_id for record in inbox.pending()] == [record_id]

    for attempt in range(1, INBOUND_MAX_ATTEMPTS - 1):
        now[0] += 31.0
        lane.outbound_tick()
        lane._inbound_queue.join()
        assert len(lane._session.calls) == INJECT_ATTEMPTS * (attempt + 2)
        if attempt + 2 < INBOUND_MAX_ATTEMPTS:
            assert [record.record_id for record in inbox.pending()] == [record_id]

    assert inbox.pending() == []
    assert [record.record_id for record in inbox.dead_letters()] == [record_id]
    now[0] += 31.0
    lane.outbound_tick()
    lane._inbound_queue.join()
    assert len(lane._session.calls) == INJECT_ATTEMPTS * INBOUND_MAX_ATTEMPTS
    lane._stop.set()

def _transcribe(lane, monkeypatch, transcriber):
    lane.cfg.elevenlabs_api_key = "fake-key"
    monkeypatch.setattr(lane.tg, "get_file_path", lambda _file_id: "voice.ogg")
    monkeypatch.setattr(lane.tg, "download", lambda _path: b"VOICE-BYTES")
    monkeypatch.setattr(stt, "transcribe", transcriber)
    return lane._transcribe({"file_id": "v1"}, lane.key.chat_id)


def test_voice_note_is_transcribed_with_the_downloaded_audio(tmp_path, monkeypatch):
    lane = make_lane(tmp_path)
    audio = []
    def transcriber(data, **_kwargs):
        audio.append(data)
        return "ahoj 123"
    assert _transcribe(lane, monkeypatch, transcriber) == "ahoj 123"
    assert audio == [b"VOICE-BYTES"]


def test_transcription_failure_is_reported_in_the_topic(tmp_path, monkeypatch):
    lane = make_lane(tmp_path)
    def fail(*_a, **_k):
        raise stt.STTError("boom")
    assert _transcribe(lane, monkeypatch, fail) is None
    assert "Voice transcription failed" in lane._outbox.pending()[0].chunks[0]


def test_durable_inject_failure_notifies_once_when_dead_lettered(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    lane._session = FakeSession([OSError("busy")] * INJECT_ATTEMPTS * INBOUND_MAX_ATTEMPTS)
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": 70, "message": {"text": "retry me"}})
    for attempt in range(INBOUND_MAX_ATTEMPTS):
        lane._submit_inbound_update(inbox.pending()[0].update, rid)
        lane._inbound_queue.join()
        assert len(tg.calls) == (1 if attempt + 1 == INBOUND_MAX_ATTEMPTS else 0)
    assert "Couldn't deliver your message" in tg.calls[0][1]
    assert inbox.dead_letters()[0].record_id == rid
    lane._stop.set()


def test_restarted_inbound_failure_notifies_from_durable_record(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": 74, "message": {"text": "survives restart"}})
    for _ in range(INBOUND_MAX_ATTEMPTS - 1):
        inbox.fail(rid, "busy")

    # A fresh bridge models restart: notice state is process-local and starts empty.
    restarted = make_lane(tmp_path, client=tg)
    restarted._inbox = restarted._ensure_inbox()
    restarted._inbound_failed(rid, "busy")

    assert len(tg.calls) == 1
    assert "Couldn't deliver your message" in tg.calls[0][1]
    assert "survives restart" in tg.calls[0][1]
    assert restarted._inbox.pending() == []
    assert restarted._inbox.dead_letters()[0].record_id == rid
    restarted._stop.set()


def test_durable_overlay_failure_notifies_once_when_dead_lettered(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    lane._session = FakeSession([SessionError(f"refusing to inject: {BLOCKED_BY_OVERLAY}")]
                                * INJECT_ATTEMPTS * INBOUND_MAX_ATTEMPTS)
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": 72, "message": {"text": "overlay"}})
    for attempt in range(INBOUND_MAX_ATTEMPTS):
        lane._submit_inbound_update(inbox.pending()[0].update, rid)
        lane._inbound_queue.join()
        assert len(tg.calls) == (1 if attempt + 1 == INBOUND_MAX_ATTEMPTS else 0)
    assert "blocked a Telegram message" in tg.calls[0][1]
    assert inbox.dead_letters()[0].record_id == rid
    lane._stop.set()


def test_durable_inject_failure_then_success_sends_no_notice(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    lane._session = FakeSession([OSError("busy")] * INJECT_ATTEMPTS * 2)
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": 73, "message": {"text": "eventual success"}})
    for _ in range(2):
        lane._submit_inbound_update(inbox.pending()[0].update, rid)
        lane._inbound_queue.join()
        assert tg.calls == []
    lane._submit_inbound_update(inbox.pending()[0].update, rid)
    lane._inbound_queue.join()
    assert tg.calls == []
    assert inbox.pending() == []
    lane._stop.set()


def test_worker_text_path_strips_begins_turn_and_injects_control_chars(tmp_path):
    lane = make_lane(tmp_path)
    text = "look\x00\nnow\tplease\x85!"
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": 71, "message": {"text": f"  {text}  "}})
    lane._submit_inbound_update(inbox.pending()[0].update, rid)
    lane._inbound_queue.join()
    assert lane._session.calls == [text]
    assert lane._turn_from_tg is True
    assert inbox.pending() == []
    lane._stop.set()


def test_text_prompt_limit_and_whitespace_are_applied_before_injection(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    assert lane._handle({"message": {"text": "   "}}) is True
    assert lane._session.calls == []
    oversized = "x" * (MAX_INBOUND_PROMPT_CHARS + 1)
    assert lane._handle({"message": {"text": oversized}}) is True
    assert lane._session.calls == []
    assert len(tg.calls) == 1
    assert str(MAX_INBOUND_PROMPT_CHARS + 1) in tg.calls[0][1]


def test_voice_transcript_is_marked_and_reply_context_is_included(tmp_path, monkeypatch):
    tg = FakeTelegram()
    tg.get_file_path = lambda _file_id: "voice.ogg"
    tg.download = lambda _path: b"audio"
    lane = make_lane(tmp_path, client=tg)
    lane.cfg.elevenlabs_api_key = "fake-key"
    monkeypatch.setattr("agent2telegramtopic.stt.transcribe", lambda *_a, **_kw: "recognized words")
    assert lane._handle({"message": {"voice": {"file_id": "v"},
                                      "reply_to_message": {"text": "Original note"}}})
    assert lane._session.calls == ["[replying to: Original note]\n[voice transcript – may contain recognition errors]\nrecognized words"]


def test_attachment_only_reply_injects_note_without_quoted_text(tmp_path):
    lane = make_lane(tmp_path)
    for attachment in ({"photo": [{"file_id": "p"}]}, {"document": {"file_id": "d"}}):
        message = {**attachment, "reply_to_message": {"text": "Quoted message"}}
        assert lane._handle({"message": message}) is True
    assert len(lane._session.calls) == 2
    assert all("[replying to: Quoted message]" in text and "attached a file saved at:" in text
               for text in lane._session.calls)


def test_photo_uses_largest_size_and_saves_in_lane_state(tmp_path):
    tg = FakeTelegram()
    lane_a = make_lane(tmp_path, key=LaneKey(44, 12), client=tg)
    lane_b = make_lane(tmp_path, key=LaneKey(44, 13), client=tg)
    message = {"message": {"photo": [{"file_id": "small", "file_size": 10},
                                       {"file_id": "large"}]}}
    lane_a._handle(message)
    lane_b._handle(message)
    path_a = lane_a._queue_root.parent / "attachments" / "image.jpg"
    path_b = lane_b._queue_root.parent / "attachments" / "image.jpg"
    assert tg.file_paths == ["large", "large"]
    assert path_a.read_bytes() == path_b.read_bytes() == b"FILE-BYTES"
    assert path_a != path_b
    assert str(path_a) in lane_a._session.calls[0]
    assert str(path_b) in lane_b._session.calls[0]


def test_document_caption_note_filename_sanitizing_and_collision(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    msg = {"caption": "Please inspect", "document": {"file_id": "d", "file_name": "../bad name?.pdf"}}
    lane._handle({"message": msg})
    lane._handle({"message": msg})
    first = lane._queue_root.parent / "attachments" / "bad_name_.pdf"
    second = lane._queue_root.parent / "attachments" / "bad_name_-1.pdf"
    assert first.read_bytes() == second.read_bytes() == b"FILE-BYTES"
    assert lane._session.calls[0].startswith("Please inspect\n[The user attached a file saved at:")
    assert str(second) in lane._session.calls[1]


def test_document_over_bot_api_limit_is_reported_without_download_or_injection(tmp_path):
    tg = FakeTelegram()
    lane = make_lane(tmp_path, client=tg)
    lane._handle({"message": {"document": {"file_id": "large", "file_name": "large.pdf",
                                            "file_size": 21 * 1024 * 1024}}})
    assert tg.file_paths == tg.downloads == []
    assert lane._session.calls == []
    assert len(tg.calls) == 1 and "20 MB" in tg.calls[0][1]
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 12}


def test_attachment_download_failure_reports_same_topic_without_injection(tmp_path):
    tg = FakeTelegram()
    tg.download = lambda _path: (_ for _ in ()).throw(OSError("offline"))
    lane = make_lane(tmp_path, client=tg)
    lane._handle({"message": {"caption": "caption", "document": {"file_id": "d"}}})
    assert lane._session.calls == []
    assert len(tg.calls) == 1 and "Couldn't download the attachment" in tg.calls[0][1]
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 12}


def test_voice_failure_and_silent_note_are_reported_to_same_topic(tmp_path, monkeypatch):
    tg = FakeTelegram()
    tg.get_file_path = lambda _file_id: "voice.ogg"
    tg.download = lambda _path: b"audio"
    lane = make_lane(tmp_path, client=tg)
    lane.cfg.elevenlabs_api_key = "fake-key"
    from agent2telegramtopic import stt
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_kw: (_ for _ in ()).throw(stt.STTError("offline")))
    lane._handle({"message": {"voice": {"file_id": "v"}}})
    lane._flush_pending()
    assert "transcription failed" in tg.calls[-1][1].lower()
    assert tg.calls[-1][2] == {"chat_id": 44, "message_thread_id": 12}
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_kw: (_ for _ in ()).throw(stt.EmptyTranscript()))
    lane._handle({"message": {"audio": {"file_id": "a"}}})
    lane._flush_pending()
    assert "couldn't hear any speech" in tg.calls[-1][1].lower()
    assert len(tg.calls) == 2 and lane._session.calls == []


def test_audio_over_limit_is_rejected_before_download(tmp_path):
    tg = FakeTelegram()
    tg.get_file_path = lambda _file_id: (_ for _ in ()).throw(AssertionError("download lookup"))
    lane = make_lane(tmp_path, client=tg)
    lane.cfg.elevenlabs_api_key = "fake-key"
    lane._handle({"message": {"voice": {"file_id": "v", "file_size": 25 * 1024 * 1024 + 1}}})
    assert len(tg.calls) == 1 and "limit is 25 MB" in tg.calls[0][1]
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 12}


def test_downloaded_audio_over_limit_is_rejected_before_stt(tmp_path, monkeypatch):
    tg = FakeTelegram()
    tg.get_file_path = lambda _file_id: "voice.ogg"
    tg.download = lambda _path: b"x" * (25 * 1024 * 1024 + 1)
    lane = make_lane(tmp_path, client=tg)
    lane.cfg.elevenlabs_api_key = "fake-key"
    from agent2telegramtopic import stt
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("STT called")))
    lane._handle({"message": {"voice": {"file_id": "v"}}})
    assert len(tg.calls) == 1 and "limit is 25 MB" in tg.calls[0][1]
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 12}


def test_stt_prompt_limit_applies_after_transcript_marker_and_context(tmp_path, monkeypatch):
    tg = FakeTelegram()
    tg.get_file_path = lambda _file_id: "voice.ogg"
    tg.download = lambda _path: b"audio"
    lane = make_lane(tmp_path, client=tg)
    lane.cfg.elevenlabs_api_key = "fake-key"
    from agent2telegramtopic import stt
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_kw: "x" * MAX_INBOUND_PROMPT_CHARS)
    lane._handle({"message": {"voice": {"file_id": "v"}, "reply_to_message": {"text": "context"}}})
    assert lane._session.calls == []
    assert "too long after processing" in tg.calls[-1][1]


def test_active_turn_text_injects_without_replacing_turn(tmp_path):
    lane = make_lane(tmp_path)
    lane._turn_active.set()
    lane._turn_started = 123.0
    assert lane._handle({"message": {"text": " second turn "}})
    assert lane._session.calls == ["second turn"]
    assert lane._turn_started == 123.0


def test_failed_injection_clears_memory_and_persisted_busy_state(tmp_path):
    from agent2telegramtopic.state import LaneStore, Lane
    lane = make_lane(tmp_path)
    store = LaneStore(tmp_path / "busy.db")
    store.upsert(Lane(44, 12, "topic", "claude-code", "session", "live", False, 1, None, 0))
    lane._lane_store = store
    lane._inject = lambda _text: (_ for _ in ()).throw(OSError("injection failed"))
    try:
        lane._handle({"message": {"text": "fail"}})
    except OSError:
        pass
    else:
        assert False, "injection exception should propagate"
    assert not lane._turn_active.is_set()
    assert store.get(lane.key).busy is False


def test_unconfirmed_injection_clears_memory_and_persisted_busy_state(tmp_path):
    from agent2telegramtopic.state import LaneStore, Lane
    lane = make_lane(tmp_path)
    store = LaneStore(tmp_path / "busy-false.db")
    store.upsert(Lane(44, 12, "topic", "claude-code", "session", "live", False, 1, None, 0))
    lane._lane_store = store
    lane._inject = lambda _text: False
    assert lane._handle({"message": {"text": "not injected"}}) is False
    assert not lane._turn_active.is_set()
    assert store.get(lane.key).busy is False


def test_failed_worker_record_in_lane_a_only_notifies_a_and_inboxes_isolate(tmp_path):
    tg = FakeTelegram()
    lane_a = make_lane(tmp_path, LaneKey(44, 41), tg)
    lane_b = make_lane(tmp_path, LaneKey(44, 42), tg)
    inbox_a, inbox_b = lane_a._ensure_inbox(), lane_b._ensure_inbox()
    assert inbox_a.directory != inbox_b.directory
    lane_a._session = FakeSession([OSError("busy")] * INJECT_ATTEMPTS * INBOUND_MAX_ATTEMPTS)
    rid = inbox_a.reserve({"update_id": 72, "message": {"text": "A only"}})
    lane_a._submit_inbound_update(inbox_a.pending()[0].update, rid)
    lane_a._inbound_queue.join()
    assert [r.record_id for r in inbox_a.pending()] == [rid]
    assert inbox_b.pending() == []
    assert tg.calls == []
    for _ in range(INBOUND_MAX_ATTEMPTS - 1):
        lane_a._submit_inbound_update(inbox_a.pending()[0].update, rid)
        lane_a._inbound_queue.join()
    assert len(tg.calls) == 1 and tg.calls[0][0] == lane_a.key
    assert tg.calls[0][2]["message_thread_id"] == 41
    lane_a._stop.set()


def test_replay_submits_pending_record_only_once_while_inflight(tmp_path):
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    record = {"update_id": 2, "message": {"text": "once"}}
    inbox.reserve(record)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def hold(upd):
        calls.append(upd)
        entered.set()
        release.wait(2)
        return True
    lane._handle = hold
    assert lane._replay_pending_inbound() == 1
    assert entered.wait(2)
    assert lane._replay_pending_inbound() == 0
    assert len(calls) == 1
    release.set()
    lane._inbound_queue.join()
    assert inbox.pending() == []
    lane._stop.set()


def test_replay_drops_record_removed_after_its_pending_snapshot(tmp_path):
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    record = {"update_id": 3, "message": {"text": "once after stale snapshot"}}
    rid = inbox.reserve(record)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handle(upd):
        calls.append(upd)
        entered.set()
        assert release.wait(2)
        return True
    lane._handle = handle
    original_pending = inbox.pending
    snapshot_taken, continue_replay = threading.Event(), threading.Event()
    first_snapshot = [True]
    def pending_with_stale_snapshot():
        snapshot = original_pending()
        if first_snapshot[0]:
            first_snapshot[0] = False
            snapshot_taken.set()
            assert continue_replay.wait(2)
        return snapshot
    inbox.pending = pending_with_stale_snapshot
    replayed = []
    replay = threading.Thread(target=lambda: replayed.append(lane._replay_pending_inbound()))
    replay.start()
    assert snapshot_taken.wait(2)
    # The poller submits the record after replay has snapshotted it. The worker
    # finishes it before replay attempts to enqueue its stale snapshot.
    lane._submit_inbound_update(record, rid)
    assert entered.wait(2)
    release.set()
    lane._inbound_queue.join()
    continue_replay.set()
    replay.join(2)
    assert not replay.is_alive()
    assert replayed == [1]
    assert calls == [record]
    assert original_pending() == []
    lane._stop.set()


def test_restart_replays_leftover_exactly_once(tmp_path):
    first = make_lane(tmp_path)
    record = {"update_id": 22, "message": {"text": "survives restart"}}
    rid = first._ensure_inbox().reserve(record)
    restarted = make_lane(tmp_path)
    calls = []
    restarted._handle = lambda upd: (calls.append(upd) or True)
    assert restarted._replay_pending_inbound() == 1
    restarted._inbound_queue.join()
    assert calls == [record]
    assert restarted._ensure_inbox().pending() == []
    restarted._stop.set()


def test_completion_deletes_before_releasing_inflight_claim(tmp_path, monkeypatch):
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    record_id = inbox.reserve({"update_id": 3, "message": {"text": "race"}})
    lane._inbound_inflight().add(record_id)
    real_done = inbox.done

    def observe_done(record):
        assert record in lane._inflight_ids
        real_done(record)

    monkeypatch.setattr(inbox, "done", observe_done)
    lane._inbound_done(record_id)
    assert record_id not in lane._inflight_ids
    assert lane._replay_pending_inbound() == 0


def test_failed_durable_completion_can_be_replayed_by_same_bridge(tmp_path, monkeypatch):
    lane = make_lane(tmp_path)
    inbox = lane._ensure_inbox()
    record_id = inbox.reserve({"update_id": 4, "message": {"text": "retry cleanup"}})
    monkeypatch.setattr(inbox, "done", lambda _record_id: (_ for _ in ()).throw(OSError("disk")))

    lane._complete_inbound_record(record_id)

    assert [record.record_id for record in inbox.pending()] == [record_id]
    monkeypatch.undo()
    assert lane._replay_pending_inbound() == 1
    lane._inbound_queue.join()
    assert inbox.pending() == []
    lane._stop.set()


def test_unsafe_pane_notice_throttled_and_two_lanes_isolated(tmp_path):
    tg = FakeTelegram()
    lane_a = make_lane(tmp_path, LaneKey(44, 21), tg)
    lane_b = make_lane(tmp_path, LaneKey(44, 22), tg)
    assert lane_a._queue_root != lane_b._queue_root
    lane_a._session = FakeSession([SessionError("refusing to inject into unsafe pane")])
    lane_a._inject("private to A")
    lane_a._notify_unsafe_pane("repeat")
    assert len(tg.calls) == 1
    assert tg.calls[0][0] == lane_a.key
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 21}
    assert "agent2telegramtopic" in tg.calls[0][1]
    lane_b._ensure_inbox()
    lane_a._ensure_inbox()
    assert lane_a._inbox.directory != lane_b._inbox.directory


def test_failed_injection_only_notifies_its_lane(tmp_path):
    tg = FakeTelegram()
    lane_a = make_lane(tmp_path, LaneKey(44, 31), tg)
    lane_b = make_lane(tmp_path, LaneKey(44, 32), tg)
    lane_a._session = FakeSession([OSError("busy")] * INJECT_ATTEMPTS)
    assert lane_a._inject("A only") is False
    assert len(tg.calls) == 1
    assert tg.calls[0][0] == lane_a.key
    assert tg.calls[0][2] == {"chat_id": 44, "message_thread_id": 31}
    assert not any(call[0] == lane_b.key for call in tg.calls)
    assert lane_a._queue_root != lane_b._queue_root
