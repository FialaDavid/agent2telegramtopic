"""Several bots side by side: each --config selects its own directory, service and lane environment."""
import json
import os
import plistlib
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent2telegramtopic import app, config, service, setup as wizard, topicedit, uninstall
from agent2telegramtopic.__main__ import main as cli_main
from agent2telegramtopic.config import (CONFIG_ENV, app_dir, config_file, hooks_settings_file, is_default_instance,
                                        lanes_root, set_config_path, state_dir)

PY = "/usr/bin/python3"
PATH = "/usr/bin:/bin"
SRC = Path("/src")


def other(tmp_path, name="second"):
    path = tmp_path / name / "config.json"
    set_config_path(path)
    return path


def test_default_install_keeps_its_paths_label_and_unit(tmp_path):
    assert config_file(tmp_path) == tmp_path / ".agent2telegramtopic/config.json"
    assert is_default_instance(tmp_path)
    data = plistlib.loads(service.launchd_plist(PY, tmp_path, PATH, SRC, bot="anybot").encode())
    assert data["Label"] == "com.agent2telegramtopic.bridge"
    assert data["ProgramArguments"] == [PY, "-m", "agent2telegramtopic", "run"]
    assert service.plist_path(tmp_path, "anybot").name == "com.agent2telegramtopic.bridge.plist"
    assert service.unit_path(tmp_path, "anybot").name == "agent2telegramtopic.service"
    assert f'ExecStart="{PY}" -m agent2telegramtopic run' in service.systemd_unit(PY, tmp_path, PATH, SRC).splitlines()
    hooks = tmp_path / "h.json"
    config.write_hooks_settings(hooks, PY, "anybot", tmp_path / "lanes", tmp_path)
    assert "--config" not in hooks.read_text()


def test_two_configs_get_distinct_directories(tmp_path):
    first = other(tmp_path, "one")
    second_dir = state_dir()
    second = other(tmp_path, "two")
    assert state_dir() != second_dir
    assert app_dir() == second.parent and second_dir == first.parent / "state"
    assert lanes_root("b") == second.parent / "topics" / "b"
    assert hooks_settings_file("b").parent == second.parent / "state"


def test_env_variable_is_the_fallback_and_flag_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "env/config.json"))
    assert app_dir() == tmp_path / "env"
    set_config_path(tmp_path / "flag/config.json")
    assert app_dir() == tmp_path / "flag"


def test_cli_flag_selects_the_instance_for_every_command(tmp_path, monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(topicedit, "main", lambda argv: seen.update(argv=argv, dir=app_dir()) or 0)
    target = tmp_path / "bot2" / "config.json"
    assert cli_main(["topic-edit", "--config", str(target), "name", "🔥"]) == 0
    assert seen == {"argv": ["name", "🔥"], "dir": target.parent}
    assert os.environ[CONFIG_ENV] == str(target)   # children (agent sessions) inherit it
    assert cli_main(["--config"]) == 2


def test_non_default_launchd_service_is_named_after_the_bot_and_passes_config(tmp_path):
    path = other(tmp_path)
    data = plistlib.loads(service.launchd_plist(PY, tmp_path, PATH, SRC, bot="second_bot").encode())
    assert data["Label"] == "com.agent2telegramtopic.bridge.second_bot"
    assert data["ProgramArguments"] == [PY, "-m", "agent2telegramtopic", "run", "--config", str(path)]
    assert data["StandardOutPath"] == str(path.parent / "logs/bridge.log")
    assert data["EnvironmentVariables"]["TMUX_TMPDIR"] == str(path.parent / "tmux")
    assert service.plist_path(tmp_path, "second_bot").name == "com.agent2telegramtopic.bridge.second_bot.plist"


def test_non_default_systemd_unit_is_named_after_the_bot_and_passes_config(tmp_path):
    path = other(tmp_path)
    unit = service.systemd_unit(PY, tmp_path, PATH, SRC)
    assert f'ExecStart="{PY}" -m agent2telegramtopic run "--config" "{path}"' in unit.splitlines()
    assert service.unit_path(tmp_path, "second_bot").name == "agent2telegramtopic-second_bot.service"


def test_a_non_default_service_needs_the_bot_name(tmp_path):
    other(tmp_path)
    with pytest.raises(ValueError, match="bot username"):
        service.plist_path(tmp_path)


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(returncode=0, stderr="")


def test_install_and_remove_act_on_the_instance_service_only(tmp_path):
    default = service.install("linux", python=PY, home=tmp_path, path=PATH, runner=Runner(), bot="first_bot")
    other(tmp_path)
    runner = Runner()
    second = service.install("linux", python=PY, home=tmp_path, path=PATH, runner=runner, bot="second_bot")
    assert Path(second).name == "agent2telegramtopic-second_bot.service"
    assert ["systemctl", "--user", "enable", "--now", "agent2telegramtopic-second_bot.service"] in runner.calls
    assert service.remove("linux", home=tmp_path, runner=runner, bot="second_bot")
    assert not Path(second).exists() and Path(default).exists()


def test_hooks_settings_and_lane_environment_point_back_at_the_same_instance(tmp_path):
    path = other(tmp_path)
    hooks = tmp_path / "h.json"
    config.write_hooks_settings(hooks, PY, "second_bot", tmp_path / "lanes", tmp_path)
    settings = json.loads(hooks.read_text())
    for event in ("Stop", "SessionStart"):
        command = settings["hooks"][event][0]["hooks"][0]["command"]
        assert shlex.split(command)[-2:] == ["--config", str(path)]
    assert app._lane_command(["claude", "--x"]) == ["env", f"{CONFIG_ENV}={path}", "claude", "--x"]


def test_default_lane_command_is_unchanged():
    assert app._lane_command(["claude", "--x"]) == ["claude", "--x"]


def test_the_hook_entrypoint_resolves_the_lane_under_its_instance(tmp_path):
    from agent2telegramtopic import hook
    path = tmp_path / "bot2" / "config.json"
    root = path.parent / "topics" / "b2"
    cwd = root / "1" / "2"
    cwd.mkdir(parents=True)
    payload = json.dumps({"cwd": str(cwd), "session_id": "s", "transcript_path": "/t", "hook_event_name": "Stop"})
    import io, sys
    old = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        assert hook.main(["--bot", "b2", "--lanes-root", str(root), "--config", str(path)]) == 0
    finally:
        sys.stdin = old
    assert (path.parent / "state/b2/lanes/1/2/turn_end").exists()


def test_topic_edit_in_a_lane_uses_the_token_of_its_own_bot(tmp_path, monkeypatch):
    path = other(tmp_path)
    config.save_config({"token": "111:second-token", "agent": "codex", "allowed_user_ids": [1],
                        "bot_username": "second_bot"})
    used = []

    class Client:
        def __init__(self, token):
            used.append(token)

        def _call(self, method, *a, **k):
            return [{"emoji": "🔥", "custom_emoji_id": "1"}] if method == "getForumTopicIconStickers" else {}

    monkeypatch.setattr(topicedit, "TelegramClient", Client)
    topic = tmp_path / "t" / "5" / "6"
    topic.mkdir(parents=True)
    assert topicedit.main(["n", "🔥"], cwd=topic) == 0
    assert used == ["111:second-token"] and path.exists()


def test_setup_with_config_writes_there_with_0600_and_installs_that_service(tmp_path, monkeypatch):
    from test_setup import FakeClient, Script, dm
    Script(monkeypatch, {"Which agent": "2", "Install and start": "y"})
    monkeypatch.setattr(wizard, "find_binary", lambda b: f"/usr/bin/{b}")
    installed = []
    monkeypatch.setattr(wizard.service, "install", lambda platform, **kw: installed.append(kw) or "/x")
    path = tmp_path / "bot2" / "config.json"
    set_config_path(path)
    assert wizard.run(tmp_path, lambda t: FakeClient(t, updates=[[dm(7)]]), platform="linux") == 0
    assert (path.stat().st_mode & 0o777) == 0o600
    assert json.loads(path.read_text())["bot_username"] == "example_bot"
    assert installed[0]["bot"] == "example_bot"
    assert not (tmp_path / ".agent2telegramtopic").exists()


def _install(tmp_path, name, bot):
    cfg = tmp_path / name / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"bot_username": bot}))
    (cfg.parent / "state").mkdir()
    set_config_path(cfg)
    runner = Runner()
    return cfg, Path(service.install("linux", python=PY, home=tmp_path, path=PATH, runner=runner, bot=bot))


def test_uninstalling_one_bot_leaves_the_other_and_the_shared_launcher(tmp_path, monkeypatch):
    launcher_dir = tmp_path / ".local/bin"
    launcher_dir.mkdir(parents=True)
    launcher = launcher_dir / "agent2telegramtopic"
    launcher.write_text("#!/bin/sh\nexec python3 -m agent2telegramtopic\n")
    monkeypatch.setenv("PATH", str(launcher_dir))
    monkeypatch.setattr(uninstall, "_stop_tmux", lambda home: None)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    from agent2telegramtopic.skills import install_skill
    install_skill("codex", tmp_path)
    cfg_a, unit_a = _install(tmp_path, "a", "bot_a")
    cfg_b, unit_b = _install(tmp_path, "b", "bot_b")

    assert uninstall.main(["--yes", "--config", str(cfg_b)], home=tmp_path, platform="linux") == 0
    assert not cfg_b.parent.exists() and not unit_b.exists()
    assert cfg_a.exists() and unit_a.exists()
    assert launcher.exists() and (tmp_path / ".codex/skills/telegram-topic/SKILL.md").exists()

    assert uninstall.main(["--yes", "--config", str(cfg_a)], home=tmp_path, platform="linux") == 0
    assert not unit_a.exists() and not launcher.exists()
    assert not (tmp_path / ".codex/skills/telegram-topic/SKILL.md").exists()


def test_uninstalling_the_default_keeps_a_second_bots_service(tmp_path, monkeypatch):
    monkeypatch.setattr(uninstall, "_stop_tmux", lambda home: None)
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setenv("PATH", "")
    cfg_b, unit_b = _install(tmp_path, "b", "bot_b")
    monkeypatch.delenv(CONFIG_ENV)
    default_unit = service.unit_path(tmp_path)
    default_unit.write_text("x")
    (tmp_path / ".agent2telegramtopic").mkdir()
    assert uninstall.main(["--yes"], home=tmp_path, platform="linux") == 0
    assert not default_unit.exists() and unit_b.exists() and cfg_b.exists()


def test_uninstall_refuses_to_delete_the_home_directory(tmp_path):
    set_config_path(tmp_path / "config.json")
    (tmp_path / "keep").write_text("x")
    assert uninstall.main(["--yes"], home=tmp_path, platform="linux") == 2
    assert (tmp_path / "keep").exists()
