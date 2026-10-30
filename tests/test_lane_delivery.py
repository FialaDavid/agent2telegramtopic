from pathlib import Path

from agent2telegramtopic.durable import DurableOutbox
from agent2telegramtopic.config import Config
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey


class FakeTelegram:
    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []
        self.params = []

    def send_message(self, key, text):
        from agent2telegramtopic.routing import reply_params

        self.params.append(reply_params(key))
        if self.fail_at == len(self.calls) + 1:
            self.calls.append((key, text, "failed"))
            raise OSError("temporary failure")
        self.calls.append((key, text, "sent"))

    def send_file(self, key, path, **kwargs):
        self.calls.append((key, str(path), "file"))


def make_lane(tmp_path: Path, key: LaneKey, client):
    state = tmp_path / str(key.thread_id)
    return LaneBridge(client, key, state / "attach_sent.txt", state / "queue")


def test_failed_second_part_resumes_exactly_once_after_restart(tmp_path):
    key = LaneKey(55, 9)
    first_client = FakeTelegram(fail_at=2)
    first = make_lane(tmp_path, key, first_client)
    text = "A" * 4096 + "B" * 4096 + "C"
    first._send_final(text, "reply-1")
    first._flush_pending()
    assert [c[1][0] for c in first_client.calls] == ["A", "A"]

    resumed_client = FakeTelegram()
    resumed = make_lane(tmp_path, key, resumed_client)
    resumed._flush_pending()
    delivered = [c[1][0] for c in first_client.calls if c[2] == "sent"]
    delivered += [c[1][0] for c in resumed_client.calls]
    assert [len(c[1]) for c in first_client.calls if c[2] == "sent"] + [len(c[1]) for c in resumed_client.calls] == [4000, 4000, 193]
    assert "".join(c[1] for c in first_client.calls if c[2] == "sent") + "".join(c[1] for c in resumed_client.calls) == text
    assert not DurableOutbox(tmp_path / "9" / "queue").pending()


def test_sent_ledger_prevents_enqueue_after_restart(tmp_path):
    key = LaneKey(55, 4)
    client = FakeTelegram()
    bridge = make_lane(tmp_path, key, client)
    bridge._send_final("reply", "already")
    bridge._flush_pending()
    restarted = make_lane(tmp_path, key, client)
    restarted._send_final("reply", "already")
    restarted._flush_pending()
    assert [call[1] for call in client.calls] == ["reply"]


def test_sent_ledger_prevents_in_process_duplicate(tmp_path):
    key = LaneKey(55, 14)
    client = FakeTelegram()
    bridge = make_lane(tmp_path, key, client)
    bridge._send_final("reply", "same-key")
    bridge._flush_pending()
    bridge._send_final("reply again", "same-key")
    bridge._flush_pending()
    assert [call[1] for call in client.calls] == ["reply"]
    assert bridge._outbox.pending() == []


def test_ledger_failure_keeps_completed_parts_queued_without_resending(tmp_path, monkeypatch):
    import builtins

    key = LaneKey(55, 7)
    client = FakeTelegram()
    bridge = make_lane(tmp_path, key, client)
    bridge._send_final("one" * 1500, "ledger-fail")
    real_open = builtins.open

    fail_writes = 1

    def fail_ledger(path, *args, **kwargs):
        nonlocal fail_writes
        if Path(path) == bridge._sent_path:
            if fail_writes:
                fail_writes -= 1
                raise OSError("disk")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fail_ledger)
    bridge._flush_pending()
    assert len(client.calls) > 1
    sent_count = len(client.calls)
    pending = bridge._outbox.pending()
    assert len(pending) == 1
    assert len(pending[0].sent_chunks) == len(pending[0].chunks)
    ledger_contents = bridge._sent_path.read_text("utf-8") if bridge._sent_path.exists() else ""
    assert "ledger-fail" not in ledger_contents.split()

    # The ported a2t ordering retains the key in memory after a failed write. A retry
    # removes the completed record without resending its confirmed parts.
    bridge._flush_pending()
    assert len(client.calls) == sent_count
    assert bridge._outbox.pending() == []

    # A restart has no queued record to resend.
    monkeypatch.setattr("builtins.open", real_open)
    restarted = make_lane(tmp_path, key, client)
    restarted._flush_pending()
    assert len(client.calls) == sent_count
    assert restarted._outbox.pending() == []


def test_attachment_survives_a_failed_text_send(tmp_path):
    key = LaneKey(55, 31)
    client = FakeTelegram(fail_at=1)
    lane = make_lane(tmp_path, key, client)
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"report")
    config = Config("bot", "claude-code", frozenset({1}), outbox_dirs=[str(tmp_path)])
    lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "attach_sent.txt",
                      tmp_path / str(key.thread_id) / "queue", config=config)
    lane._send_final(f"answer\n[tg-file] {attachment}", "answer-1")
    lane._flush_pending()
    assert not any(call[2] == "file" for call in client.calls)
    assert len(lane._outbox.pending()) == 1

    resumed_client = FakeTelegram()
    resumed = LaneBridge(resumed_client, key, tmp_path / str(key.thread_id) / "attach_sent.txt",
                         tmp_path / str(key.thread_id) / "queue", config=config)
    resumed._flush_pending()
    assert [(call[1], call[2]) for call in resumed_client.calls] == [
        ("answer", "sent"), (str(attachment.resolve()), "file")]
    assert all(call[0] == key for call in resumed_client.calls)
    assert resumed._outbox.pending() == []


def test_lanes_are_isolated_and_general_omits_thread_parameter(tmp_path):
    client = FakeTelegram()
    regular = LaneKey(55, 23)
    general = LaneKey(55, 1)
    lane_a = make_lane(tmp_path, regular, client)
    lane_b = make_lane(tmp_path, general, client)
    lane_a._send_final("topic reply")
    lane_b._send_final("general reply")
    lane_a._flush_pending()
    lane_b._flush_pending()
    assert [(c[0].thread_id, c[1]) for c in client.calls] == [(23, "topic reply"), (1, "general reply")]
    assert client.params == [
        {"chat_id": 55, "message_thread_id": 23},
        {"chat_id": 55},
    ]


def test_foreign_chat_record_is_dead_lettered_without_send(tmp_path):
    key = LaneKey(55, 8)
    client = FakeTelegram()
    bridge = make_lane(tmp_path, key, client)
    bridge._outbox = DurableOutbox(tmp_path / "8" / "queue")
    bridge._outbox.enqueue(["wrong destination"], [], "foreign", chat=777)
    bridge._flush_pending()
    assert client.calls == []
    assert bridge._outbox.pending() == []
    assert [r.key for r in bridge._outbox.dead_letters()] == ["foreign"]
