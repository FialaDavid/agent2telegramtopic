import pytest

import json
import os
import subprocess
import sys
from pathlib import Path

import agent2telegramtopic

from agent2telegramtopic.config import (Config, app_dir, build_config, config_file, load_config, save_config,
                                        save_elevenlabs_key, hooks_settings_file, lane_dir, lane_state_dir, lane_state_file, lanes_root, state_db_path, write_hooks_settings)


def test_config_defaults():
    config = Config(bot_name="bot", agent_kind="codex", allowed_users=frozenset({9}))
    assert config.max_live_lanes == 4
    assert config.idle_compact_minutes == 45
    assert config.compact_min_tokens == 30000
    assert config.claude_cmd == ("claude", "--dangerously-skip-permissions")
    assert config.codex_cmd == ("codex", "-c", "check_for_update_on_startup=false", "--dangerously-bypass-approvals-and-sandbox")
    assert config.hooks_settings_path is None
    assert config.elevenlabs_language == ""
    assert config.tts_speed == 1.25
    assert config.file_marker == "[tg-file]"
    assert config.outbox_dirs == []
    assert config.background_progress is False
    assert config.inbound_sender_label is False


FAKE_TOKEN = "1234567:fake-token-for-tests-only-aaaaaaaaaaaa"


def minimal(**extra):
    return {"agent": "claude-code", "token": FAKE_TOKEN, "allowed_user_ids": [42],
            "bot_username": "example_bot", **extra}


def test_missing_config_file_tells_the_user_to_run_setup(tmp_path):
    with pytest.raises(ValueError, match="setup"):
        load_config(tmp_path)
    assert config_file(tmp_path) == tmp_path / ".agent2telegramtopic/config.json"


def test_saved_config_is_private_and_round_trips(tmp_path):
    path = save_config(minimal(elevenlabs_api_key="sk_" + "a" * 24), tmp_path)
    assert path == config_file(tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_config(tmp_path) == minimal(elevenlabs_api_key="sk_" + "a" * 24)
    assert not list(path.parent.glob("*.tmp"))


def test_overwriting_a_config_keeps_it_private(tmp_path):
    path = save_config(minimal(), tmp_path)
    path.chmod(0o644)
    save_config(minimal(), tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_setkey_updates_only_the_key_and_keeps_mode(tmp_path):
    save_config(minimal(), tmp_path)
    save_elevenlabs_key("sk_" + "b" * 24, tmp_path)
    saved = load_config(tmp_path)
    assert saved["elevenlabs_api_key"] == "sk_" + "b" * 24
    assert saved["token"] == FAKE_TOKEN
    assert (config_file(tmp_path).stat().st_mode & 0o777) == 0o600


def test_build_config_maps_file_to_config():
    cfg = build_config(minimal(max_live_lanes=2, claude_cmd="claude --model sonnet", tts_speed=1.0))
    assert (cfg.bot_name, cfg.agent_kind, cfg.allowed_users) == ("example_bot", "claude-code", frozenset({42}))
    assert cfg.max_live_lanes == 2 and cfg.tts_speed == 1.0
    assert cfg.claude_cmd == ("claude", "--model", "sonnet")


def test_elevenlabs_key_comes_from_environment_when_not_in_file(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "e" * 24)
    assert build_config(minimal()).elevenlabs_api_key == "sk_" + "e" * 24
    assert build_config(minimal(elevenlabs_api_key="sk_" + "f" * 24)).elevenlabs_api_key == "sk_" + "f" * 24


@pytest.mark.parametrize("values", [
    {"tts_speed": 0.49}, {"tts_speed": 2.01},
    {"tts_speed": True},
    {"background_progress": "yes"}, {"background_progress": 1},
    {"inbound_sender_label": "yes"}, {"inbound_sender_label": 0},
    {"agent": "other"}, {"allowed_user_ids": []}, {"allowed_user_ids": ["7"]}, {"token": ""},
    {"claude_cmd": ""},
])
def test_invalid_values_are_rejected(tmp_path, values):
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**minimal(), **values}))
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_config_rejects_unknown_and_missing_keys(tmp_path):
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**minimal(), "tts_provider": "macos"}))
    with pytest.raises(ValueError, match="Unknown config keys"):
        load_config(tmp_path)
    path.write_text(json.dumps({"agent": "codex"}))
    with pytest.raises(ValueError, match="Missing config keys"):
        load_config(tmp_path)


def test_config_accepts_custom_agent_commands():
    config = Config(bot_name="bot", agent_kind="codex", allowed_users=frozenset({9}), claude_cmd=("claude", "--model", "opus"), codex_cmd=("codex", "--profile", "work"))
    assert config.claude_cmd == ("claude", "--model", "opus")
    assert config.codex_cmd == ("codex", "--profile", "work")


def test_invalid_agent_kind_raises_value_error():
    with pytest.raises(ValueError):
        Config(bot_name="bot", agent_kind="other", allowed_users=frozenset({9}))


def test_config_rejects_empty_allowlist():
    with pytest.raises(ValueError, match="allowed_users"):
        Config(bot_name="bot", agent_kind="codex", allowed_users=frozenset())


def test_state_db_path_ends_with_bot_database_name(tmp_path):
    assert state_db_path("ExampleBot", tmp_path) == tmp_path / ".agent2telegramtopic/state/ExampleBot.db"


def test_home_paths_are_scoped_under_supplied_home(tmp_path):
    assert hooks_settings_file("bot", tmp_path) == tmp_path / ".agent2telegramtopic/state/bot.hooks.json"
    assert lanes_root("bot", tmp_path) == tmp_path / ".agent2telegramtopic/topics/bot"


def test_lane_paths_and_state_files_are_scoped_per_chat_and_thread(tmp_path):
    assert lane_dir("bot", 8, 42, tmp_path) == tmp_path / ".agent2telegramtopic/topics/bot/8/42"
    assert lane_state_dir("bot", 8, 42, tmp_path) == tmp_path / ".agent2telegramtopic/state/bot/lanes/8/42"
    expected = {"sent": "attach_sent.txt", "outbox": "queue", "inbox": None,
                "turn_end": "turn_end", "status": "status_bubble", "session": "lane_session.json"}
    for kind, filename in expected.items():
        expected_path = lane_state_dir("bot", 8, 42, tmp_path) if filename is None else lane_state_dir("bot", 8, 42, tmp_path) / filename
        assert lane_state_file("bot", 8, 42, kind, tmp_path) == expected_path
    with pytest.raises(ValueError, match="unknown lane state"):
        lane_state_file("bot", 8, 42, "unknown", tmp_path)
    assert lane_dir("bot", 9, 42, tmp_path) != lane_dir("bot", 8, 42, tmp_path)
    assert lane_state_dir("bot", 9, 42, tmp_path) != lane_state_dir("bot", 8, 42, tmp_path)


def test_write_hooks_settings_registers_lane_scoped_stop_and_session_start(tmp_path):
    path = tmp_path / "bot.hooks.json"
    write_hooks_settings(path, "/path/python with spaces", "test-bot")
    settings = json.loads(path.read_text())
    assert list(settings) == ["hooks", "statusLine"]
    assert set(settings["hooks"]) == {"Stop", "SessionStart"}
    repo_root = Path(agent2telegramtopic.__file__).resolve().parent.parent
    expected = {"hooks": [{"type": "command", "command": f"PYTHONPATH={repo_root} '/path/python with spaces' -m agent2telegramtopic.hook --bot test-bot"}]}
    assert settings["hooks"]["Stop"] == [expected]
    assert settings["hooks"]["SessionStart"] == [expected]


def test_hook_command_signals_turn_end_from_lane_cwd_without_pythonpath(tmp_path):
    home = tmp_path / "home"
    lanes = tmp_path / "lanes"
    lane = lanes / "123" / "42"
    lane.mkdir(parents=True)
    path = tmp_path / "hooks.json"
    write_hooks_settings(path, sys.executable, "test-bot", lanes)
    command = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    subprocess.run(command, shell=True, cwd=lane,
                   env={"PATH": os.environ["PATH"], "HOME": str(home)},
                   input=json.dumps({"cwd": str(lane), "session_id": "session",
                                     "transcript_path": str(lane / "transcript.jsonl"),
                                     "hook_event_name": "Stop"}),
                   text=True, check=True)
    assert (home / ".agent2telegramtopic/state/test-bot/lanes/123/42/turn_end").exists()


def test_bot_config_file_can_enable_background_progress_and_sender_label(tmp_path):
    save_config(minimal(background_progress=True, inbound_sender_label=True), tmp_path)
    cfg = build_config(load_config(tmp_path))
    assert cfg.background_progress is True and cfg.inbound_sender_label is True


def test_shipped_example_config_is_valid_apart_from_its_placeholders():
    example = json.loads((Path(__file__).resolve().parent.parent / "config.example.json").read_text())
    cfg = build_config(example)
    assert cfg.agent_kind == "claude-code"
    assert cfg.claude_cmd == Config("b", "codex", frozenset({1})).claude_cmd
    assert cfg.codex_cmd == Config("b", "codex", frozenset({1})).codex_cmd
