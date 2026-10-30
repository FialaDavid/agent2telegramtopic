import json
import logging
import threading
import time
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent2telegramtopic.lane import BOT_COMMANDS, LaneBridge
from agent2telegramtopic.routing import LaneKey, reply_params
from agent2telegramtopic.session import BLOCKED_BY_OVERLAY
from agent2telegramtopic import app as app_module


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.deleted = []

    def send_message(self, key, text, **kwargs):
        self.messages.append((key, text, reply_params(key), kwargs))

    def send_chat_action(self, key, action):
        del action
        pass

    def delete_message(self, key, message_id):
        self.deleted.append((key, message_id))


class FakeSession:
    def __init__(self, error=None):
        self.error = error
        self.commands = []
        self.injected = []

    def send_command(self, text):
        if self.error:
            raise self.error
        self.commands.append(text)

    def inject(self, text):
        self.injected.append(text)


def make_lane(tmp_path, key=LaneKey(44, 12), agent="claude-code", on_close=None, client=None):
    client = client or FakeTelegram()
    state = tmp_path / str(key.thread_id)
    lane = LaneBridge(client, key, state / "sent", state / "queue", agent=agent,
                      on_close=on_close)
    lane._session = FakeSession()
    lane._stop.wait = lambda _: None
    return lane, client


def test_startup_registers_all_supported_topic_commands(monkeypatch, tmp_path):
    registered = []

    class FakeClient:
        def get_me(self):
            return {"has_topics_enabled": True}

        def set_my_commands(self, commands):
            registered.extend(commands)

    class FakeApp:
        client = FakeClient()

        def run_forever(self):
            pass

    monkeypatch.setattr(app_module.redaction, "install_log_redaction", lambda: None)
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    from agent2telegramtopic.config import save_config
    save_config({"agent": "claude-code", "token": "1234567:fake-token-for-tests-only-aaaaaaaaaaaa",
                 "allowed_user_ids": [8], "bot_username": "ExampleBot"}, tmp_path)
    monkeypatch.setattr(app_module, "build_app", lambda config, token, cwd: FakeApp())

    assert app_module.main([]) == 0
    assert [item["command"] for item in registered] == [
        "start", "help", "status", "new", "setkey", "voice", "id", "close"]
    assert all(item.get("description") for item in registered)


def submit(lane, update_id, text):
    inbox = lane._ensure_inbox()
    record_id = inbox.reserve({"update_id": update_id,
                               "message": {"text": text, "message_id": update_id}})
    lane._submit_inbound_update(inbox.pending()[0].update, record_id)
    lane._inbound_queue.join()
    return inbox, record_id


@pytest.mark.parametrize("agent,command", [("claude-code", "/clear"), ("codex", "/new")])
@pytest.mark.parametrize("text", ["/new", "/new@ExampleBot"])
def test_new_uses_agent_command_and_is_consumed(tmp_path, agent, command, text):
    lane, tg = make_lane(tmp_path, agent=agent)
    inbox, rid = submit(lane, 1, text)
    assert lane._session.commands == [command]
    assert lane._session.injected == []
    assert lane._turn_from_tg is False
    assert len(tg.messages) == 1
    assert tg.messages[0][1] == f"🧹 Started a fresh conversation — sent {command} to the session."
    assert inbox.pending() == []
    lane._stop.set()


@pytest.mark.parametrize("error,expected", [
    (RuntimeError("tmux unavailable"), "session may be busy or gone"),
    (RuntimeError(BLOCKED_BY_OVERLAY), "interactive dialog open"),
])
def test_new_failure_is_reported_once_consumed_and_record_completed(tmp_path, error, expected):
    lane, tg = make_lane(tmp_path)
    lane._session = FakeSession(error)
    inbox, rid = submit(lane, 2, "/new")
    assert lane._session.commands == []
    assert lane._session.injected == []
    assert lane._turn_from_tg is False
    assert len(tg.messages) == 1
    assert expected in tg.messages[0][1]
    assert inbox.pending() == []
    lane._stop.set()


def test_close_calls_lane_callback_once(tmp_path):
    keys = []
    lane, tg = make_lane(tmp_path, on_close=keys.append)
    inbox, _ = submit(lane, 3, "/close")
    assert keys == [lane.key]
    assert tg.messages == []
    assert lane._session.injected == []
    assert inbox.pending() == []
    lane._stop.set()


def test_close_leaves_messages_behind_it_durable_for_resume(tmp_path):
    lane, _ = make_lane(tmp_path, on_close=lambda _key: setattr(lane, "_closing", True))
    inbox = lane._ensure_inbox()
    close_id = inbox.reserve({"update_id": 30, "message": {"text": "/close", "message_id": 30}})
    later = {"update_id": 31, "message": {"text": "after close", "message_id": 31}}
    later_id = inbox.reserve(later)
    lane._submit_inbound_update({"update_id": 30,
                                 "message": {"text": "/close", "message_id": 30}}, close_id)
    lane._submit_inbound_update(later, later_id)
    lane._inbound_queue.join()
    assert lane._session.injected == []
    assert [record.update["update_id"] for record in inbox.pending()] == [31]
    lane._stop.set()


@pytest.mark.parametrize("callback", [None, "raises"])
def test_close_without_working_callback_notifies_once(tmp_path, callback, caplog):
    def raises(_key):
        raise ValueError("details are not logged")
    lane, tg = make_lane(tmp_path, on_close=raises if callback == "raises" else None)
    with caplog.at_level(logging.ERROR):
        inbox, _ = submit(lane, 4, "/close")
    assert len(tg.messages) == 1
    assert tg.messages[0][1] == "⚠️ Couldn't close this topic's session. Try again."
    assert lane._session.injected == []
    assert inbox.pending() == []
    if callback == "raises":
        assert "ValueError" in caplog.text
        assert "details are not logged" not in caplog.text
    lane._stop.set()


def test_unknown_command_is_forwarded_as_normal_text(tmp_path):
    lane, _ = make_lane(tmp_path)
    inbox, _ = submit(lane, 5, "/foo bar")
    assert lane._session.injected == ["/foo bar"]
    assert lane._turn_from_tg is True
    assert inbox.pending() == []
    lane._stop.set()


def test_new_in_one_lane_only_sends_and_notifies_in_that_lane(tmp_path):
    tg = FakeTelegram()
    key_a, key_b = LaneKey(44, 41), LaneKey(44, 42)
    lane_a, _ = make_lane(tmp_path, key_a, agent="claude-code", client=tg)
    lane_b, _ = make_lane(tmp_path, key_b, agent="codex", client=tg)
    submit(lane_a, 6, "/new")
    assert lane_a._session.commands == ["/clear"]
    assert lane_a._session.injected == []
    assert lane_b._session.commands == []
    assert lane_b._session.injected == []
    assert len(tg.messages) == 1
    assert tg.messages[0][0] == key_a
    assert tg.messages[0][1] == "🧹 Started a fresh conversation — sent /clear to the session."
    assert tg.messages[0][2] == {"chat_id": 44, "message_thread_id": 41}
    lane_a._stop.set()
    lane_b._stop.set()


def test_help_and_id_are_consumed_and_topic_targeted(tmp_path):
    lane, tg = make_lane(tmp_path)
    assert lane._handle_command("/help", 44)
    assert "/status" in tg.messages[-1][1] and "/usage" not in tg.messages[-1][1]
    assert tg.messages[-1][2] == {"chat_id": 44, "message_thread_id": 12}
    assert lane._handle_command("/id", 44)
    assert "44" in tg.messages[-1][1]
    lane._stop.set()


def test_status_reports_lane_agent_and_state(tmp_path):
    lane, tg = make_lane(tmp_path, agent="codex")
    class Inbox:
        def pending(self): return [1, 2]
        def dead_letters(self): return [1]
    class Outbox:
        def pending_chunks(self): return [(1, "reply")]
    lane._ensure_inbox = lambda: Inbox()
    lane._ensure_outbox = lambda: Outbox()
    lane._session.alive = True
    lane.tmux_session = "session-A"
    lane._handle_command("/status", 44)
    status = tg.messages[-1][1]
    assert "session-A" in status and "2 pending" in status and "dead-letter: 1" in status
    assert "Voice replies: off" in status
    assert tg.messages[-1][3] == {"parse_mode": None}
    assert tg.messages[-1][2] == {"chat_id": 44, "message_thread_id": 12}
    lane._stop.set()


def _config_home(tmp_path, monkeypatch):
    from agent2telegramtopic.config import save_config
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    save_config({"agent": "codex", "token": "1234567:fake-token-for-tests-only-aaaaaaaaaaaa",
                 "allowed_user_ids": [1], "bot_username": "bot-A"}, tmp_path)


def test_setkey_saves_to_config_deletes_user_message_and_does_not_log_key(tmp_path, monkeypatch, caplog):
    from agent2telegramtopic.config import config_file, load_config
    _config_home(tmp_path, monkeypatch)
    lane, tg = make_lane(tmp_path)
    lane.bot_name = "bot-A"
    key = "sk_secret-never-log"
    with caplog.at_level(logging.DEBUG):
        assert lane._handle_command(f"/setkey {key}", 44, 91)
    assert tg.deleted == [(lane.key, 91)]
    assert key not in caplog.text
    assert load_config(tmp_path)["elevenlabs_api_key"] == key
    assert (config_file(tmp_path).stat().st_mode & 0o777) == 0o600
    assert "key saved" in tg.messages[-1][1]
    lane._stop.set()


def test_setkey_updates_transcription_for_other_active_topics(tmp_path, monkeypatch):
    _config_home(tmp_path, monkeypatch)
    tg = FakeTelegram()
    key_a, key_b = LaneKey(44, 21), LaneKey(44, 22)
    a, _ = make_lane(tmp_path, key_a, client=tg)
    b, _ = make_lane(tmp_path, key_b, client=tg)
    a.bot_name = b.bot_name = "shared-test-bot"
    assert a._handle_command("/setkey sk_shared-key", 44, 99)
    assert b._status_text("Claude Code").endswith("transcription: on")
    assert b._elevenlabs_key() == "sk_shared-key"
    assert tg.deleted == [(key_a, 99)]
    a._stop.set(); b._stop.set()


def test_setkey_reports_failure_when_config_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)   # no config file there
    lane, tg = make_lane(tmp_path)
    lane.bot_name = "bot-nofile"
    assert lane._handle_command("/setkey sk_whatever-key", 44, 5)
    assert "Couldn't save the key" in tg.messages[-1][1]
    assert lane._elevenlabs_key() != "sk_whatever-key"
    lane._stop.set()


def test_voice_switch_is_persisted_per_lane(tmp_path):
    tg = FakeTelegram()
    a, _ = make_lane(tmp_path, LaneKey(44, 12), client=tg)
    b, _ = make_lane(tmp_path, LaneKey(44, 13), client=tg)
    assert a._handle_command("/voice", 44)
    assert a._voice_reply_on() and not b._voice_reply_on()
    assert a._voice_state_path.read_text() == "on"
    assert tg.messages[-1][2] == {"chat_id": 44, "message_thread_id": 12}
    restarted, _ = make_lane(tmp_path, LaneKey(44, 12), client=tg)
    restarted._handle_command("/status", 44)
    assert "Voice replies: on" in tg.messages[-1][1]
    assert tg.messages[-1][2] == {"chat_id": 44, "message_thread_id": 12}
    a._stop.set(); b._stop.set(); restarted._stop.set()


def test_status_queue_read_failure_reports_unavailable(tmp_path):
    lane, tg = make_lane(tmp_path)
    class Broken:
        def pending(self): raise OSError()
        def dead_letters(self): raise OSError()
        def pending_chunks(self): raise OSError()
    lane._ensure_inbox = lambda: Broken()
    lane._ensure_outbox = lambda: Broken()
    lane._handle_command("/status", 44)
    assert "unavailable pending" in tg.messages[-1][1]
    assert "unavailable chunks pending" in tg.messages[-1][1]
    lane._stop.set()


# ---- ported from a2t tests/test_attach_commands.py (names kept) ----------------------------

THREADED, GENERAL = LaneKey(44, 12), LaneKey(44, 1)


@pytest.fixture(params=[THREADED, GENERAL], ids=["topic", "general"])
def cmd_lane(request, tmp_path, monkeypatch):
    lane, tg = make_lane(tmp_path, request.param, agent="codex")
    yield lane, tg
    lane._stop.set()


def assert_reply_to_lane(tg, lane):
    key, _text, params, _kwargs = tg.messages[-1]
    assert key == lane.key
    assert params == reply_params(lane.key)
    if lane.key.thread_id == 1:
        assert "message_thread_id" not in params
    else:
        assert params["message_thread_id"] == lane.key.thread_id


def test_menu_help_and_dispatcher_include_new_but_not_usage_or_team(cmd_lane):
    lane, tg = cmd_lane
    commands = {item["command"] for item in BOT_COMMANDS}
    assert {"new", "close"} <= commands
    assert "usage" not in commands
    assert "team" not in commands
    lane._status_text = lambda agent: "status"
    assert lane._handle_command("/help", 7)
    assert "/usage" not in tg.messages[0][1]
    assert "/new" in tg.messages[0][1]
    assert_reply_to_lane(tg, lane)
    assert lane._handle_command("/new", 7)
    assert lane._session.commands == ["/new"]
    assert not lane._handle_command("/team", 7)
    assert all(m[0] == lane.key and m[2] == reply_params(lane.key) for m in tg.messages)


def test_status_uses_current_v2_queues_and_health_without_secrets(cmd_lane):
    lane, tg = cmd_lane

    class Queue:
        def pending(self): return [1, 2]
        def dead_letters(self): return [3]
        def pending_chunks(self): return [(0, "reply")]

    lane._session.alive = True
    lane._ensure_inbox = lambda: Queue()
    lane._ensure_outbox = lambda: Queue()
    lane._voice_reply_on = lambda: False
    assert lane._handle_command("/status", lane.key.chat_id)
    assert_reply_to_lane(tg, lane)
    out = tg.messages[-1][1]
    assert "alive" in out
    assert "2 pending" in out
    assert "dead-letter: 1" in out
    assert "1 chunks pending" in out
    assert "Heartbeat" in out
    assert "secret" not in out.lower()


def test_status_bounds_queue_failures_as_unavailable(cmd_lane):
    lane, tg = cmd_lane

    class Dead:
        alive = False

    def broken():
        raise OSError("bad queue")

    lane._session = Dead()
    lane._ensure_inbox = broken
    lane._ensure_outbox = broken
    lane._voice_reply_on = lambda: False
    assert lane._handle_command("/status", lane.key.chat_id)
    assert_reply_to_lane(tg, lane)
    out = tg.messages[-1][1]
    assert "GONE" in out
    assert "unavailable" in out
