"""Setup wizard flow with a mocked Telegram API: nothing is sent, nothing is installed."""
import builtins
import getpass
import json

import pytest

from agent2telegramtopic import setup as wizard
from agent2telegramtopic.config import config_file, hooks_settings_file, load_config
from agent2telegramtopic.telegram import TelegramError

TOKEN = "1234567:fake-token-for-tests-only-aaaaaaaaaaaa"
GOOD_BOT = {"id": 1, "username": "example_bot", "has_topics_enabled": True}
NO_TOPICS_BOT = {"id": 1, "username": "example_bot", "has_topics_enabled": False}


def dm(update_id, user_id=42, chat_type="private"):
    return {"update_id": update_id, "message": {
        "chat": {"id": user_id if chat_type == "private" else -1001, "type": chat_type},
        "from": {"id": user_id, "username": "alice"}, "text": "hi"}}


class FakeClient:
    """Scripted Bot API: get_me answers in order (last one repeats), get_updates yields batches."""
    instances = []

    def __init__(self, token, me=(GOOD_BOT,), updates=((),)):
        self.token = token
        self.me = list(me)
        self.batches = list(updates)
        self.offsets = []
        FakeClient.instances.append(self)

    def get_me(self):
        item = self.me.pop(0) if len(self.me) > 1 else self.me[0]
        if isinstance(item, Exception):
            raise item
        return item

    def get_updates(self, offset, timeout=0):
        self.offsets.append(offset)
        return list(self.batches.pop(0)) if self.batches else []


class Script:
    """Answers input()/getpass() by prompt text; records what was asked."""
    def __init__(self, monkeypatch, answers, secrets=(TOKEN,)):
        self.asked = []
        self.answers = answers
        self.secrets = list(secrets)
        monkeypatch.setattr(builtins, "input", self.input)
        monkeypatch.setattr(getpass, "getpass", self.getpass)
        monkeypatch.setattr(wizard.time, "sleep", lambda _s: None)

    def input(self, prompt=""):
        self.asked.append(prompt)
        for needle, answer in self.answers.items():
            if needle in prompt:
                return answer
        return ""

    def getpass(self, prompt=""):
        self.asked.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    FakeClient.instances.clear()
    monkeypatch.setattr(wizard, "find_binary", lambda b: f"/usr/bin/{b}" if b in ("claude", "codex") else None)
    installed = []
    monkeypatch.setattr(wizard.service, "install",
                        lambda platform, **kw: installed.append((platform, kw)) or f"/fake/{platform}/unit")
    return installed


@pytest.fixture(autouse=True)
def keychain_writes(monkeypatch):
    """No test may reach the real Keychain."""
    writes = []
    monkeypatch.setattr(wizard.keychain, "write",
                        lambda service, account, secret, **kw: writes.append((service, account, secret)))
    return writes


def factory(**kwargs):
    return lambda token: FakeClient(token, **kwargs)


def test_happy_path_writes_private_config_and_installs_service(tmp_path, monkeypatch, isolated, capsys):
    Script(monkeypatch, {"Which agent": "2", "Install and start": "y"})
    rc = wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="linux")
    assert rc == 0
    path = config_file(tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_config(tmp_path) == {"agent": "codex", "token": TOKEN, "allowed_user_ids": [42],
                                     "bot_username": "example_bot"}
    assert [platform for platform, _ in isolated] == ["linux"]
    assert TOKEN not in capsys.readouterr().out


def test_claude_choice_prepares_per_session_hooks_without_touching_global_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    Script(monkeypatch, {"Which agent": "1", "Keychain": "n", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="darwin") == 0
    hooks = json.loads(hooks_settings_file("example_bot", tmp_path).read_text())
    assert set(hooks["hooks"]) == {"Stop", "SessionStart"}
    assert load_config(tmp_path)["agent"] == "claude-code"
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_setup_installs_the_topic_naming_skill_for_the_chosen_agent(tmp_path, monkeypatch):
    Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="linux") == 0
    assert "topic-edit" in (tmp_path / ".codex/skills/telegram-topic/SKILL.md").read_text()
    assert not (tmp_path / ".claude").exists()


def test_elevenlabs_key_is_stored_in_the_config_file(tmp_path, monkeypatch):
    key = "sk_" + "z" * 24
    Script(monkeypatch, {"Which agent": "2", "via ElevenLabs": "y", "Install and start": "n"},
           secrets=(TOKEN, key))
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="linux") == 0
    assert load_config(tmp_path)["elevenlabs_api_key"] == key


def test_bot_without_topics_gets_botfather_instructions_and_setup_continues(tmp_path, monkeypatch, capsys):
    script = Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    rc = wizard.run(tmp_path, factory(me=[NO_TOPICS_BOT], updates=[[dm(7)]]), platform="linux")
    out = capsys.readouterr().out
    assert rc == 0
    assert "Threaded Mode" in out and "@BotFather" in out
    assert not any("check again" in p for p in script.asked)
    assert config_file(tmp_path).exists()


def test_invalid_token_is_reported_and_asked_again(tmp_path, monkeypatch, capsys):
    bad = "7654321:wrong-token-for-tests-only-bbbbbbbbbbbbbb"
    Script(monkeypatch, {"Which agent": "2", "Install and start": "n"}, secrets=(bad, TOKEN))
    clients = iter([FakeClient(bad, me=[TelegramError(401, "Unauthorized")]),
                    FakeClient(TOKEN, updates=[[dm(7)]])])
    assert wizard.run(tmp_path, lambda token: next(clients), platform="linux") == 0
    assert "Unauthorized" in capsys.readouterr().out
    assert load_config(tmp_path)["token"] == TOKEN


def test_message_from_a_group_does_not_authorize_anyone(tmp_path, monkeypatch, capsys):
    Script(monkeypatch, {"Which agent": "2"})
    rc = wizard.run(tmp_path, factory(updates=[[dm(7, chat_type="supergroup")]]), platform="linux")
    out = capsys.readouterr().out
    assert rc == 1
    assert "PRIVATE chat" in out
    assert not config_file(tmp_path).exists()


def test_user_id_can_be_entered_by_hand_when_detection_finds_nothing(tmp_path, monkeypatch):
    Script(monkeypatch, {"Which agent": "2", "numeric Telegram user id": "555", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(), platform="linux") == 0
    assert load_config(tmp_path)["allowed_user_ids"] == [555]


def test_updates_are_acknowledged_with_an_increasing_offset(tmp_path, monkeypatch):
    Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    wizard.run(tmp_path, factory(updates=[[dm(7, chat_type="supergroup")], [dm(9)]]), platform="linux")
    assert FakeClient.instances[0].offsets == [None, 8]


def test_no_installed_agent_stops_before_asking_for_a_token(tmp_path, monkeypatch, capsys):
    script = Script(monkeypatch, {})
    monkeypatch.setattr(wizard, "find_binary", lambda b: None)
    assert wizard.run(tmp_path, factory(), platform="linux") == 1
    assert "Neither `claude` nor `codex`" in capsys.readouterr().out
    assert not any("token" in q for q in script.asked)


def test_existing_config_is_kept_unless_the_user_confirms(tmp_path, monkeypatch):
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    Script(monkeypatch, {"Overwrite?": "n"})
    assert wizard.run(tmp_path, factory(), platform="linux") == 0
    assert path.read_text() == "{}"


def test_service_failure_is_reported_but_config_is_kept(tmp_path, monkeypatch, capsys):
    Script(monkeypatch, {"Which agent": "2", "Install and start": "y"})
    def boom(platform, **kw):
        raise RuntimeError("systemctl --user enable failed")
    monkeypatch.setattr(wizard.service, "install", boom)
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="linux") == 1
    assert "could not install the service" in capsys.readouterr().out
    assert config_file(tmp_path).exists()


def test_on_macos_the_token_goes_to_the_keychain_by_default(tmp_path, monkeypatch, keychain_writes, capsys):
    script = Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="darwin") == 0
    assert any("Store the bot token in the macOS Keychain instead of config.json?" in p for p in script.asked)
    assert keychain_writes == [("agent2telegramtopic.example_bot", None, TOKEN)]
    saved = load_config(tmp_path)
    assert "token" not in saved and saved["token_keychain"] == {"service": "agent2telegramtopic.example_bot"}
    assert TOKEN not in config_file(tmp_path).read_text() and TOKEN not in capsys.readouterr().out


def test_on_macos_the_elevenlabs_key_is_asked_about_separately(tmp_path, monkeypatch, keychain_writes):
    key = "sk_" + "z" * 24
    script = Script(monkeypatch, {"Which agent": "2", "via ElevenLabs": "y", "bot token in the macOS": "n",
                                  "Install and start": "n"}, secrets=(TOKEN, key))
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="darwin") == 0
    assert any("Store the ElevenLabs key in the macOS Keychain" in p for p in script.asked)
    assert keychain_writes == [("agent2telegramtopic.example_bot.elevenlabs", None, key)]
    saved = load_config(tmp_path)
    assert saved["token"] == TOKEN and "elevenlabs_api_key" not in saved
    assert saved["elevenlabs_api_key_keychain"] == {"service": "agent2telegramtopic.example_bot.elevenlabs"}


def test_declining_the_keychain_keeps_the_secrets_in_the_config_file(tmp_path, monkeypatch, keychain_writes):
    Script(monkeypatch, {"Which agent": "2", "Keychain": "n", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="darwin") == 0
    assert keychain_writes == [] and load_config(tmp_path)["token"] == TOKEN


def test_on_linux_there_is_no_keychain_question(tmp_path, monkeypatch, keychain_writes):
    script = Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="linux") == 0
    assert not any("Keychain" in p for p in script.asked)
    assert load_config(tmp_path)["token"] == TOKEN and keychain_writes == []


def test_a_failed_keychain_write_stops_setup_without_writing_the_secret_anywhere(tmp_path, monkeypatch, capsys):
    Script(monkeypatch, {"Which agent": "2", "Install and start": "n"})
    def refuse(*a, **k):
        raise ValueError("Keychain item 'agent2telegramtopic.example_bot' could not be written")
    monkeypatch.setattr(wizard.keychain, "write", refuse)
    assert wizard.run(tmp_path, factory(updates=[[dm(7)]]), platform="darwin") == 1
    assert "could not be written" in capsys.readouterr().out
    assert not config_file(tmp_path).exists()
