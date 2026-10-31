import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent2telegramtopic import app as app_module
from agent2telegramtopic.config import Config
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey


def msg(update_id, thread_id, text="hello", *, chat_type="private"):
    return {"update_id": update_id, "message": {
        "chat": {"id": 123, "type": chat_type}, "message_thread_id": thread_id,
        "from": {"id": 8}, "text": text,
    }}


class Client:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.sent = []
        self.polls = []

    def get_updates(self, offset, timeout):
        self.polls.append((offset, timeout))
        return self.batches.pop(0) if self.batches else []

    def send_message(self, key, text):
        self.sent.append((key, text))

    def send_chat_action(self, key, action):
        del action
        pass


def test_bridge_creation_persists_lane_row_before_compaction_updates(tmp_path, monkeypatch):
    class Session:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(app_module, "TelegramClient", lambda token: Client())
    monkeypatch.setattr(app_module, "TmuxSession", Session)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / f"{bot}.db")
    monkeypatch.setattr(app_module, "lane_path", lambda root, chat, thread: tmp_path / "cwd" / str(chat) / str(thread))
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) /
                        {"sent": "sent", "outbox": "outbox", "session": "session",
                         "turn_end": "turn_end", "compact_done": "compact_done"}[kind])
    monkeypatch.setattr(app_module, "lanes_root", lambda *args: tmp_path / "cwd")
    mux = app_module.build_app(Config("test", "codex", frozenset({8})), "fake-token", str(tmp_path))
    key = LaneKey(123, 10)
    bridge = mux.lane_factory(key)
    row = bridge._lane_store.get(key)
    assert row is not None
    assert row.status == "parked"
    assert row.session_id == ""
    bridge._lane_store.mark_compacted(key, 12.0, 345)
    assert bridge._lane_store.get(key).last_compact == 12.0


def test_restart_attach_keeps_launch_argv_and_uses_agent_executable_guard(tmp_path, monkeypatch):
    captured = []
    class Session:
        def __init__(self, argv, **kwargs):
            captured.append((argv, kwargs["expected_agent_commands"]))

    monkeypatch.setattr(app_module, "TelegramClient", lambda token: Client())
    monkeypatch.setattr(app_module, "TmuxSession", Session)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / f"{bot}.db")
    monkeypatch.setattr(app_module, "lane_path", lambda root, chat, thread: tmp_path / "cwd")
    paths = {"sent": "sent", "outbox": "outbox", "session": "session",
             "turn_end": "turn_end", "compact_done": "compact_done"}
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / paths[kind])
    monkeypatch.setattr(app_module, "lanes_root", lambda *args: tmp_path / "cwd")
    monkeypatch.setattr(app_module.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(app_module.subprocess, "run", lambda *args, **kwargs:
                        type("Result", (), {"returncode": 0})())
    config = replace(Config("test", "codex", frozenset({8})),
                     codex_cmd=("/usr/bin/codex", "codex", "--profile", "gpt-master"))
    mux = app_module.build_app(config, "fake-token", str(tmp_path))
    marker = tmp_path / "state" / "session"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"session_id": "recorded-rollout"}))
    bridge = mux.lane_factory(LaneKey(123, 10))
    bridge._activate_session()
    argv, expected = captured[0]
    from agent2telegramtopic.tmux import launch_argv
    launched = launch_argv("codex", "recorded-rollout", True, config.codex_cmd,
                           settings_path=config.hooks_settings_path, cwd=str(tmp_path / "cwd"))
    assert argv == launched
    assert expected == ["codex"]
    assert expected != launched
    claude_config = replace(Config("test", "claude-code", frozenset({8})),
                            claude_cmd=("/opt/claude/bin/claude", "--resume"))
    assert app_module._expected_agent_commands(claude_config) == ["claude"]


def test_park_callback_queues_lane_when_inbox_has_pending_records(tmp_path, monkeypatch):
    class Session:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(app_module, "TelegramClient", lambda token: Client())
    monkeypatch.setattr(app_module, "TmuxSession", Session)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / f"{bot}.db")
    monkeypatch.setattr(app_module, "lane_path", lambda root, chat, thread: tmp_path / "cwd")
    paths = {"sent": "sent", "outbox": "outbox", "session": "session",
             "turn_end": "turn_end", "compact_done": "compact_done"}
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) / paths[kind])
    monkeypatch.setattr(app_module, "lanes_root", lambda *args: tmp_path / "cwd")
    mux = app_module.build_app(Config("test", "codex", frozenset({8})), "fake-token", str(tmp_path))
    key = LaneKey(123, 10)
    bridge = mux.lane_factory(key)
    bridge._ensure_inbox().reserve(msg(9, 10, "waiting update"))
    bridge._on_parked(key)
    row = bridge._lane_store.get(key)
    assert row.status == "waiting"
    assert not row.busy
    assert mux._slot_wakeup.is_set()








def test_build_app_multiplexer_delivers_reply_once_through_lane_bridge(monkeypatch, tmp_path):
    class FakeClient(Client):
        def __init__(self, token):
            super().__init__()

    class FakeSession:
        def __init__(self, *args, **kwargs): pass

    class CapturedThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            pass

    from agent2telegramtopic import multiplexer as multiplexer_module
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state" / "state.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(bot) / "lanes" / str(chat) / str(thread) / kind)
    monkeypatch.setattr(app_module, "lane_path", lambda root, chat, thread:
                        Path(root) / str(chat) / str(thread))
    monkeypatch.setattr(multiplexer_module.threading, "Thread", CapturedThread)
    mux = app_module.build_app(Config("test", "claude-code", frozenset({8})), "token", str(tmp_path / "lanes"))
    client = mux.client
    lane = mux._lane(LaneKey(123, 10))
    lane._send_final("reply from running lane", key="reply-1")
    lane.outbound_tick()
    lane.outbound_tick()
    assert client.sent == [(LaneKey(123, 10), "reply from running lane")]


FAKE_TOKEN = "1234567:fake-token-for-tests-only-aaaaaaaaaaaa"


def write_config(home, **extra):
    from agent2telegramtopic.config import save_config
    save_config({"agent": "claude-code", "token": FAKE_TOKEN, "allowed_user_ids": [8],
                 "bot_username": "ExampleBot", **extra}, home)


def test_main_without_config_fails_with_a_hint(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    assert app_module.main([]) == 2
    assert "setup" in caplog.text


def test_main_starts_bridge_when_topics_are_reported_disabled(monkeypatch, tmp_path, caplog):
    class FakeClient:
        def get_me(self):
            return {"has_topics_enabled": False}

        def set_my_commands(self, commands):
            del commands

    class FakeApp:
        client = FakeClient()
        ran = False

        def run_forever(self):
            self.ran = True
    fake = FakeApp()
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    write_config(tmp_path)
    monkeypatch.setattr(app_module, "build_app", lambda config, token, cwd: fake)
    assert app_module.main([]) == 0
    assert fake.ran
    assert caplog.text.count("topics are not enabled") == 1
    assert FAKE_TOKEN not in caplog.text


def test_main_resolves_workdir_before_building_app(monkeypatch, tmp_path):
    target = tmp_path / "real-project"
    target.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(target, target_is_directory=True)

    class FakeClient:
        def get_me(self):
            return {"has_topics_enabled": True}

        def set_my_commands(self, commands):
            del commands

    class FakeApp:
        client = FakeClient()

        def run_forever(self):
            pass

    seen = []
    installed = []
    monkeypatch.setattr(app_module.redaction, "install_log_redaction", lambda: installed.append(True))
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    write_config(tmp_path, workdir=str(link))
    monkeypatch.setattr(app_module, "build_app", lambda config, token, cwd: seen.append(cwd) or FakeApp())
    assert app_module.main([]) == 0
    assert seen == [str(target.resolve())]
    assert installed == [True]


def test_main_applies_config_file_and_creates_scoped_startup_files(monkeypatch, tmp_path):
    class FakeClient:
        def get_me(self):
            return {"has_topics_enabled": True}

        def set_my_commands(self, commands):
            del commands

    class FakeApp:
        client = FakeClient()

        def run_forever(self):
            pass

    seen = []
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    write_config(tmp_path, claude_cmd="claude --model 'opus 5' --flag", codex_cmd="codex --profile 'gpt master'")
    monkeypatch.setattr(app_module, "build_app", lambda config, token, cwd: seen.append((config, token, cwd)) or FakeApp())
    assert app_module.main([]) == 0
    config, token, cwd = seen[0]
    assert token == FAKE_TOKEN
    assert config.claude_cmd == ("claude", "--model", "opus 5", "--flag")
    assert config.codex_cmd == ("codex", "--profile", "gpt master")
    hooks = tmp_path / ".agent2telegramtopic/state/ExampleBot.hooks.json"
    assert config.hooks_settings_path == str(hooks)
    assert hooks.exists()
    assert cwd == str(tmp_path / ".agent2telegramtopic/topics/ExampleBot")
    assert (tmp_path / ".agent2telegramtopic/topics/ExampleBot").is_dir()


def test_build_app_constructs_chat_scoped_lane_paths(monkeypatch, tmp_path):
    sessions = []
    class FakeSession:
        def __init__(self, argv, *, cwd, name, signal_file, origin_prefix, **kwargs):
            self.argv, self.cwd, self.name, self.signal_file = argv, cwd, name, signal_file
            self.origin_prefix = origin_prefix
            self.messages = []
            sessions.append(self)

        def inject(self, text):
            self.messages.append(text)

    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    class FakeClient:
        def __init__(self, token):
            pass

        send_message = lambda *args: None
        send_chat_action = lambda *args: None

    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    names = {"sent": "attach_sent.txt", "outbox": "queue", "inbox": None,
             "turn_end": "turn_end", "status": "status_bubble", "session": "lane_session.json"}
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) /
                        (names[kind] if names[kind] else ""))
    root = tmp_path / "custom-root"
    app = app_module.build_app(Config("bot", "claude-code", frozenset({8})), "token", str(root))
    from agent2telegramtopic.routing import LaneKey
    first = app.lane_factory(LaneKey(123, 42))
    second = app.lane_factory(LaneKey(456, 42))
    first._activate_session(); second._activate_session()
    assert first.lane_cwd == root / "123" / "42"
    assert second.lane_cwd == root / "456" / "42"
    assert first._sent_path != second._sent_path
    assert first._queue_root != second._queue_root
    assert first.session_marker != second.session_marker
    assert sessions[0].origin_prefix == sessions[1].origin_prefix == "[TG] "
    from agent2telegramtopic.tmux import session_name
    assert sessions[0].name == session_name("claude-code", "topic 42", 42, 123)
    assert first.session_id == sessions[0].argv[sessions[0].argv.index("--session-id") + 1]
    assert first._ensure_inbox().directory != second._ensure_inbox().directory
    first._session.inject("hello from chat 123")
    second._session.inject("hello from chat 456")
    assert sessions[0].messages == ["hello from chat 123"]
    assert sessions[1].messages == ["hello from chat 456"]
    assert sessions[0].cwd != sessions[1].cwd
    assert sessions[1].name == session_name("claude-code", "topic 42", 42, 456)
    assert "--session-id" in sessions[0].argv and "--session-id" in sessions[1].argv
    assert first._outbox is None and second._outbox is None
    assert first._turn_end != second._turn_end
    assert first.cfg.tts_voice_id == Config("b", "codex", frozenset({1})).tts_voice_id
    assert first._sent_path != second._sent_path
    assert first.session_marker != second.session_marker


def test_main_loads_optional_settings_and_keeps_secrets_out_of_the_log(monkeypatch, tmp_path, caplog):
    class FakeClient:
        def get_me(self):
            return {"has_topics_enabled": True}

        def set_my_commands(self, commands):
            del commands
    class FakeApp:
        client = FakeClient()
        def run_forever(self): pass
    seen = []
    monkeypatch.setattr(app_module.Path, "home", classmethod(lambda cls: tmp_path))
    write_config(tmp_path, elevenlabs_language="cs", tts_speed=1.1, elevenlabs_api_key="private-eleven-key")
    monkeypatch.setattr(app_module, "build_app", lambda config, token, cwd: seen.append(config) or FakeApp())
    assert app_module.main([]) == 0
    cfg = seen[0]
    assert cfg.elevenlabs_language == "cs"
    assert cfg.tts_speed == 1.1
    assert cfg.elevenlabs_api_key == "private-eleven-key"
    assert "private-eleven-key" not in caplog.text
    assert FAKE_TOKEN not in caplog.text


def test_build_app_resumes_claude_from_lane_marker_and_scopes_tmux_names(monkeypatch, tmp_path):
    sessions = []
    class FakeSession:
        def __init__(self, argv, *, cwd, name, signal_file, origin_prefix, **kwargs):
            self.argv, self.cwd, self.name = argv, cwd, name
            self.origin_prefix = origin_prefix
            sessions.append(self)
    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    class FakeClient:
        def __init__(self, token): pass
        send_message = lambda *args: None
        send_chat_action = lambda *args: None
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) /
                        ("lane_session.json" if kind == "session" else kind))
    path = tmp_path / "state" / "456" / "42" / "lane_session.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"session_id":"known-session","transcript_path":"/tmp/transcript"}')
    app = app_module.build_app(Config("bot", "claude-code", frozenset({8})), "token", str(tmp_path / "lanes"))
    first = app.lane_factory(LaneKey(123, 42))
    second = app.lane_factory(LaneKey(456, 42))
    first._activate_session(); second._activate_session()
    assert "--session-id" in sessions[0].argv
    assert "known-session" not in sessions[0].argv
    assert first.session_id == sessions[0].argv[sessions[0].argv.index("--session-id") + 1]
    assert second.session_id == "known-session"
    assert first.session_id in sessions[0].argv
    assert sessions[0].origin_prefix == sessions[1].origin_prefix == "[TG] "
    assert "--resume" in sessions[1].argv and "known-session" in sessions[1].argv
    from agent2telegramtopic.tmux import session_name
    assert sessions[0].name == session_name("claude-code", "topic 42", 42, 123)
    assert sessions[1].name == session_name("claude-code", "topic 42", 42, 456)
    assert sessions[0].name != sessions[1].name


def test_build_app_starts_new_codex_lane_without_resume_id(monkeypatch, tmp_path):
    sessions = []

    class FakeSession:
        def __init__(self, argv, *, cwd, name, signal_file, origin_prefix, **kwargs):
            self.argv, self.name = argv, name
            self.origin_prefix = origin_prefix
            sessions.append(self)

    class FakeClient:
        def __init__(self, token): pass
        send_message = lambda *args: None
        send_chat_action = lambda *args: None

    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) / kind)
    app = app_module.build_app(Config("bot", "codex", frozenset({8})), "token", str(tmp_path / "lanes"))
    lane = app.lane_factory(LaneKey(123, 42))
    lane._activate_session()
    assert "resume" not in sessions[0].argv
    assert lane.session_id == ""


def test_build_app_does_not_resume_unmarked_codex_rollout(monkeypatch, tmp_path):
    sessions = []

    class FakeSession:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            sessions.append(self)

    class FakeClient:
        def __init__(self, token): pass
        send_message = lambda *args: None
        send_chat_action = lambda *args: None

    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    marker = tmp_path / "state" / "123" / "42" / "lane_session.json"
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        marker if kind == "session" else tmp_path / "state" / str(chat) / str(thread) / kind)
    lane = app_module.build_app(Config("bot", "codex", frozenset({8})), "token", str(tmp_path / "lanes")) \
        .lane_factory(LaneKey(123, 42))
    lane._activate_session()
    assert "resume" not in sessions[0].argv
    assert not marker.exists()


def test_build_app_resumes_codex_only_from_lane_marker(monkeypatch, tmp_path):
    sessions = []

    class FakeSession:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            sessions.append(self)

    class FakeClient:
        def __init__(self, token): pass
        send_message = lambda *args: None
        send_chat_action = lambda *args: None

    marker = tmp_path / "state" / "123" / "42" / "lane_session.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"session_id": "recorded-rollout"}))
    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        marker if kind == "session" else tmp_path / "state" / str(chat) / str(thread) / kind)
    lane = app_module.build_app(Config("bot", "codex", frozenset({8})), "token", str(tmp_path / "lanes")) \
        .lane_factory(LaneKey(123, 42))
    lane._activate_session()
    assert sessions[0].argv[:2] == ["codex", "resume"]
    assert "recorded-rollout" in sessions[0].argv


def test_adopted_codex_rollout_is_recorded_and_used_for_next_launch(monkeypatch, tmp_path):
    sessions = []

    class FakeSession:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            sessions.append(self)

    class FakeClient:
        def __init__(self, token): pass
        send_message = lambda *args: None
        send_chat_action = lambda *args: None

    marker = tmp_path / "state" / "123" / "42" / "lane_session.json"
    lane_root = tmp_path / "lanes"
    monkeypatch.setattr(app_module, "TmuxSession", FakeSession)
    monkeypatch.setattr(app_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        marker if kind == "session" else tmp_path / "state" / str(chat) / str(thread) / kind)
    app = app_module.build_app(Config("bot", "codex", frozenset({8})), "token", str(lane_root))
    lane = app.lane_factory(LaneKey(123, 42))
    lane._activate_session()
    assert "resume" not in sessions[0].argv

    sessions_root = tmp_path / "codex-sessions"
    rollout = sessions_root / "2026" / "rollout-fake-id.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "fake-id", "cwd": str(lane.lane_cwd.resolve()),
    }}) + "\n")
    lane.codex_sessions_root = sessions_root
    lane._last_resolve = -100

    # A failed marker write must leave adoption retryable; otherwise unchanged transcripts
    # short-circuit the next poll and a restarted lane loses the rollout identity.
    from agent2telegramtopic import lane as lane_module
    real_replace = lane_module.os.replace
    replace_calls = 0

    def fail_first_replace(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("temporary marker write failure")
        return real_replace(source, destination)

    monkeypatch.setattr(lane_module.os, "replace", fail_first_replace)
    lane._maybe_reresolve()
    assert not marker.exists()
    lane._last_resolve = -100
    lane._maybe_reresolve()

    assert json.loads(marker.read_text("utf-8"))["session_id"] == "fake-id"
    next_lane = app.lane_factory(LaneKey(123, 42))
    next_lane._activate_session()
    assert sessions[1].argv[:3] == ["codex", "resume", "fake-id"]


def test_main_rejects_unknown_arguments():
    with pytest.raises(SystemExit) as error:
        app_module.main(["--bot", "ExampleBot"])
    assert error.value.code == 2
