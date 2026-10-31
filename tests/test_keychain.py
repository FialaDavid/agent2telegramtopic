"""Secrets from the macOS Keychain: `security` is replaced by a fake runner, the real Keychain is never reached."""
import logging
from types import SimpleNamespace

import pytest

from agent2telegramtopic import config, keychain, redaction
from agent2telegramtopic.config import bot_token, build_config, load_config, save_config, save_elevenlabs_key

TOKEN = "1234567:keychain-token-for-tests-only-aaaaaaaa"
KEY = "sk_" + "k" * 24


def base(**extra):
    return {"agent": "codex", "allowed_user_ids": [1], "bot_username": "kc_bot", **extra}


class Security:
    """Fake `security`: a dict of (service, account) -> secret."""
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.calls = []
        self.stdin = []

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["security", "find-generic-password"]:
            service = argv[argv.index("-s") + 1]
            account = argv[argv.index("-a") + 1] if "-a" in argv else None
            found = [v for (s, a), v in self.items.items() if s == service and account in (None, a)]
            if not found:
                return SimpleNamespace(returncode=44, stdout="", stderr="could not be found")
            return SimpleNamespace(returncode=0, stdout=found[0] + "\n", stderr="")
        assert argv == ["security", "-i"]
        self.stdin.append(input)
        parts = __import__("shlex").split(input)
        service = parts[parts.index("-s") + 1]
        account = parts[parts.index("-a") + 1]   # the real `security` requires one
        self.items[(service, account)] = parts[parts.index("-w") + 1]
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_token_is_read_from_the_keychain_item_the_config_names():
    runner = Security({("my.service", "me"): TOKEN})
    assert keychain.read("my.service", "me", platform="darwin", runner=runner) == TOKEN
    assert runner.calls == [["security", "find-generic-password", "-s", "my.service", "-a", "me", "-w"]]
    runner = Security({("my.service", "x"): TOKEN})
    keychain.read("my.service", platform="darwin", runner=runner)
    assert runner.calls == [["security", "find-generic-password", "-s", "my.service", "-w"]]


def test_a_missing_item_error_names_the_service_and_never_a_secret():
    with pytest.raises(ValueError) as err:
        keychain.read("agent2telegramtopic.kc_bot", platform="darwin", runner=Security({("other", None): TOKEN}))
    assert "agent2telegramtopic.kc_bot" in str(err.value) and TOKEN not in str(err.value)


def test_off_macos_reading_is_a_clear_error():
    with pytest.raises(ValueError, match=r"agent2telegramtopic\.kc_bot.*only available on macOS"):
        keychain.read("agent2telegramtopic.kc_bot", platform="linux", runner=Security())


def test_bot_token_prefers_nothing_but_the_configured_source(monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", Security({("svc", "x"): TOKEN}))
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    assert bot_token(base(token_keychain={"service": "svc"})) == TOKEN
    assert bot_token(base(token="plain:token")) == "plain:token"


def test_exactly_one_token_source_is_required():
    with pytest.raises(ValueError, match="exactly one"):
        build_config(base())
    with pytest.raises(ValueError, match="exactly one"):
        build_config(base(token=TOKEN, token_keychain={"service": "s"}))
    build_config(base(token_keychain={"service": "s", "account": "a"}), keychain_secrets=False)


@pytest.mark.parametrize("bad", ["svc", {}, {"service": ""}, {"service": "s", "account": 3}, {"service": "s", "x": 1}])
def test_keychain_references_are_validated(bad):
    with pytest.raises(ValueError, match="token_keychain"):
        build_config(base(token_keychain=bad))
    with pytest.raises(ValueError, match="elevenlabs_api_key_keychain"):
        build_config(base(token=TOKEN, elevenlabs_api_key_keychain=bad))


def test_elevenlabs_key_has_one_source_and_env_still_works(monkeypatch):
    with pytest.raises(ValueError, match="only one"):
        build_config(base(token=TOKEN, elevenlabs_api_key=KEY, elevenlabs_api_key_keychain={"service": "e"}))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_" + "e" * 24)
    assert build_config(base(token=TOKEN)).elevenlabs_api_key == "sk_" + "e" * 24


def test_elevenlabs_key_is_read_from_the_keychain_at_startup(monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", Security({("el.svc", keychain.DEFAULT_ACCOUNT): KEY}))
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    assert build_config(base(token=TOKEN, elevenlabs_api_key_keychain={"service": "el.svc"})).elevenlabs_api_key == KEY


def test_elevenlabs_item_not_yet_created_means_no_key_so_setkey_can_create_it(monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", Security())
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert build_config(base(token=TOKEN, elevenlabs_api_key_keychain={"service": "el.svc"})).elevenlabs_api_key == ""


def test_loading_the_config_file_does_not_read_the_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(keychain.subprocess, "run", lambda *a, **k: pytest.fail("Keychain read while loading"))
    save_config(base(token_keychain={"service": "svc"}), tmp_path)
    assert load_config(tmp_path)["token_keychain"] == {"service": "svc"}


def test_setkey_stores_in_the_keychain_and_leaves_config_json_alone(tmp_path, monkeypatch):
    security = Security()
    monkeypatch.setattr(keychain.subprocess, "run", security)
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    save_config(base(token=TOKEN, elevenlabs_api_key_keychain={"service": "el.svc", "account": "me"}), tmp_path)
    before = config.config_file(tmp_path).read_text()
    save_elevenlabs_key(KEY, tmp_path)
    assert security.items == {("el.svc", "me"): KEY}
    assert config.config_file(tmp_path).read_text() == before and KEY not in before
    assert not any(KEY in " ".join(call) for call in security.calls)   # the secret is not in any argv


def test_a_failed_keychain_write_is_an_error_without_the_secret():
    def failing(argv, input=None, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="")
    with pytest.raises(ValueError, match="svc") as err:
        keychain.write("svc", None, KEY, platform="darwin", runner=failing)
    assert KEY not in str(err.value)


def test_a_service_without_account_is_written_under_a_default_account():
    security = Security()
    keychain.write("svc", None, KEY, platform="darwin", runner=security)
    assert security.items == {("svc", keychain.DEFAULT_ACCOUNT): KEY}


def test_setkey_command_writes_the_keychain_when_configured(tmp_path, monkeypatch):
    from test_lane_commands import make_lane
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    security = Security()
    monkeypatch.setattr(keychain.subprocess, "run", security)
    monkeypatch.setattr(keychain.sys, "platform", "darwin")
    save_config(base(token=TOKEN, bot_username="bot-A", elevenlabs_api_key_keychain={"service": "el.svc"}), tmp_path)
    lane, tg = make_lane(tmp_path)
    lane.bot_name = "bot-A"
    assert lane._handle_command(f"/setkey {KEY}", 44, 91)
    assert security.items == {("el.svc", keychain.DEFAULT_ACCOUNT): KEY}
    assert "elevenlabs_api_key" not in load_config(tmp_path)
    assert "key saved" in tg.messages[-1][1]
    lane._stop.set()


def test_values_read_from_the_keychain_are_masked_in_logs(monkeypatch):
    odd = "unrecognisable-secret-without-any-known-shape"
    monkeypatch.setattr(redaction, "_SECRETS", set())
    keychain.read("svc", platform="darwin", runner=Security({("svc", "x"): odd}))
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "request failed for %s", (odd,), None)
    redaction.RedactingFilter().filter(record)
    assert odd not in record.getMessage()
