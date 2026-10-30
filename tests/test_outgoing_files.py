from pathlib import Path
import os
from unittest import mock

from agent2telegramtopic.config import Config, outbox_path
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.readers import Ev
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.telegram import TelegramClient


class Telegram:
    def __init__(self):
        self.messages = []
        self.files = []
        self.message_params = []
        self.file_params = []
    def send_message(self, key, text):
        from agent2telegramtopic.routing import reply_params
        self.message_params.append(reply_params(key))
        self.messages.append((key, text))
    def send_file(self, key, path, **kwargs):
        from agent2telegramtopic.routing import reply_params
        self.file_params.append(reply_params(key))
        self.files.append((key, Path(path)))


def bridge(tmp_path, client, *, key=LaneKey(42, 17), extra=()):
    state = tmp_path / "state"
    dirs = [str(outbox_path("bot", tmp_path)), *[str(x) for x in extra]]
    cfg = Config("bot", "claude-code", frozenset({1}), outbox_dirs=dirs)
    return LaneBridge(client, key, state / "sent", state / "queue", config=cfg)


def test_marker_is_stripped_and_text_then_file_go_to_lane_thread(tmp_path):
    client = Telegram()
    lane = bridge(tmp_path, client)
    lane._turn_from_tg = True
    allowed = outbox_path("bot", tmp_path)
    allowed.mkdir(parents=True)
    item = allowed / "report.pdf"
    item.write_bytes(b"data")
    lane._handle_event(Ev("text", text=f"Here\n[tg-file] {item}", key="reply"))
    lane._flush_pending()
    assert [text for _, text in client.messages] == ["Here"]
    assert [key.thread_id for key, _ in client.files] == [17]
    assert client.files[0][1] == item.resolve()
    assert client.file_params == [{"chat_id": 42, "message_thread_id": 17}]
    assert client.message_params == [{"chat_id": 42, "message_thread_id": 17}]


def test_outside_and_symlink_files_are_refused_and_reported(tmp_path):
    client = Telegram()
    lane = bridge(tmp_path, client)
    allowed = outbox_path("bot", tmp_path)
    allowed.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("private")
    link = allowed / "link"
    os.symlink(secret, link)
    assert lane._safe_outbox_path(str(secret))[0] is None
    assert lane._safe_outbox_path(str(link))[0] is None
    lane._send_files([str(secret), str(link)])
    lane._flush_pending()
    assert len(client.messages) == 2
    assert all("Couldn't send" in text for _, text in client.messages)
    assert client.files == []


def test_empty_file_refused_and_custom_folder_allowed(tmp_path):
    client = Telegram()
    extra = tmp_path / "extra"
    extra.mkdir()
    lane = bridge(tmp_path, client, extra=[extra])
    empty = extra / "empty"
    empty.touch()
    good = extra / "good.bin"
    good.write_bytes(b"x")
    assert lane._safe_outbox_path(str(empty))[0] is None
    assert lane._safe_outbox_path(str(good))[0] == good.resolve()
    lane._send_files([str(empty)])
    lane._flush_pending()
    assert any("Couldn't send empty" in text for _, text in client.messages)


def test_failed_upload_is_reported_in_the_originating_topic(tmp_path):
    class FailingTelegram(Telegram):
        def send_file(self, key, path, **kwargs):
            super().send_file(key, path, **kwargs)
            raise OSError("upload failed")

    client = FailingTelegram()
    lane = bridge(tmp_path, client)
    allowed = outbox_path("bot", tmp_path)
    allowed.mkdir(parents=True)
    item = allowed / "report.pdf"
    item.write_bytes(b"data")
    lane._send_files([str(item)])
    lane._flush_pending()
    assert any("Couldn't send report.pdf: upload failed" in text for _, text in client.messages)
    assert client.message_params == [{"chat_id": 42, "message_thread_id": 17}]


def test_file_type_selects_telegram_method():
    assert TelegramClient.kind_for("a.mp4") == ("sendVideo", "video")
    assert TelegramClient.kind_for("a.MOV") == ("sendVideo", "video")
    assert TelegramClient.kind_for("a.opus") == ("sendAudio", "audio")
    assert TelegramClient.kind_for("a.png") == ("sendPhoto", "photo")
    assert TelegramClient.kind_for("a.zip") == ("sendDocument", "document")


def test_oversized_file_fails_before_upload(tmp_path):
    file_path = tmp_path / "large.mp4"
    file_path.write_bytes(b"x")
    client = TelegramClient("1:x", opener=mock.Mock())
    with mock.patch("os.path.getsize", return_value=TelegramClient.MAX_UPLOAD + 1):
        try:
            client.send_file(LaneKey(42, 17), str(file_path))
        except Exception as exc:
            assert "at most" in str(exc)
        else:
            raise AssertionError("oversized upload should fail")


def test_bot_default_outbox_is_always_allowed(tmp_path):
    lane = bridge(tmp_path, Telegram())
    assert outbox_path("bot") in lane._allowed_outbox_dirs()


def test_files_event_delivers_once_and_only_for_telegram_turn(tmp_path):
    client = Telegram()
    lane = bridge(tmp_path, client)
    allowed = outbox_path("bot", tmp_path)
    allowed.mkdir(parents=True)
    item = allowed / "a.bin"
    item.write_bytes(b"x")
    event = Ev("files", key="tool-1", files=(str(item),))
    lane._turn_from_tg = True
    lane._handle_event(event)
    lane._handle_event(event)
    lane._flush_pending()
    assert len(client.files) == 1
    assert client.files[0][0].thread_id == 17

    other = bridge(tmp_path / "other", Telegram())
    other._handle_event(event)
    assert other._outbox is None
