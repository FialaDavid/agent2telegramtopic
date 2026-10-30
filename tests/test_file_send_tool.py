import pytest

from agent2telegramtopic.config import Config, outbox_path
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.readers import ClaudeCodeReader
from agent2telegramtopic.routing import LaneKey


@pytest.mark.parametrize("tool_input", [
    lambda path: {"files": [path]},
    lambda path: {"file": path},
    lambda path: {"paths": [path]},
    lambda path: {"path": path},
])
def test_send_user_file_reader_event_reaches_originating_topic(tmp_path, tool_input):
    allowed = outbox_path("bot", tmp_path)
    allowed.mkdir(parents=True)
    target = allowed / "result.pdf"
    target.write_bytes(b"result")
    record = {"type": "assistant", "uuid": "u1", "message": {"content": [{
        "type": "tool_use", "id": "tool-1", "name": "SendUserFile",
        "input": tool_input(str(target))}]}}
    events = list(ClaudeCodeReader().parse(record))
    assert len(events) == 1 and events[0].kind == "files"

    class Telegram:
        def __init__(self): self.sent = []; self.params = []
        def send_file(self, key, path, **kwargs):
            from agent2telegramtopic.routing import reply_params
            self.params.append(reply_params(key)); self.sent.append((key, path))
        def send_message(self, key, text, **kwargs): self.sent.append((key, text))
    tg = Telegram()
    state = tmp_path / "state"
    lane = LaneBridge(tg, LaneKey(50, 29), state / "sent", state / "queue",
                      config=Config("bot", "claude-code", frozenset({1}),
                                    outbox_dirs=[str(allowed)]))
    lane._turn_from_tg = True
    lane._handle_event(events[0])
    lane._flush_pending()
    assert [(key.thread_id, str(path)) for key, path in tg.sent] == [(29, str(target.resolve()))]
    assert tg.params == [{"chat_id": 50, "message_thread_id": 29}]


def test_ordinary_tool_does_not_become_a_files_event():
    record = {"type": "assistant", "uuid": "u2", "message": {"content": [{
        "type": "tool_use", "id": "read-1", "name": "Read",
        "input": {"file_path": "/tmp/a.pdf"}}]}}
    events = list(ClaudeCodeReader().parse(record))
    assert [event.kind for event in events] == ["tool"]


def test_send_user_file_without_paths_does_not_become_a_files_event():
    record = {"type": "assistant", "uuid": "u3", "message": {"content": [{
        "type": "tool_use", "id": "tool-1", "name": "SendUserFile",
        "input": {"caption": "no file supplied"}}]}}
    events = list(ClaudeCodeReader().parse(record))
    assert [event.kind for event in events] == ["tool"]
