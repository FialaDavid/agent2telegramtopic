import json
from pathlib import Path

from agent2telegramtopic.control import write_close, write_restart, write_send


def test_control_request_is_atomic_json_with_required_fields(tmp_path):
    path = write_close('bot', 4, 9, home=tmp_path)
    data = json.loads(path.read_text())
    assert path.parent == tmp_path / '.agent2telegramtopic' / 'state' / 'bot' / 'control'
    assert data['action'] == 'close'
    assert (data['chat'], data['thread']) == (4, 9)
    assert isinstance(data['requested_at'], (int, float))
    assert not list(path.parent.glob('*.tmp'))


def test_send_control_request_is_atomic_and_contains_text(tmp_path):
    path = write_send('bot', 4, 9, 'hello', home=tmp_path)
    data = json.loads(path.read_text())
    assert data == {"action": "send", "chat": 4, "thread": 9, "text": "hello",
                    "requested_at": data["requested_at"]}
    assert not list(path.parent.glob('*.tmp'))


def test_restart_control_request_is_atomic(tmp_path):
    path = write_restart('bot', 4, 9, home=tmp_path)
    data = json.loads(path.read_text())
    assert data == {"action": "restart", "chat": 4, "thread": 9,
                    "requested_at": data["requested_at"]}
    assert not list(path.parent.glob('*.tmp'))


def test_close_cli_resolves_unique_lane_and_refuses_missing(tmp_path, monkeypatch, capsys):
    from agent2telegramtopic import control
    from agent2telegramtopic import config
    from agent2telegramtopic.config import Config
    from agent2telegramtopic.routing import LaneKey
    from agent2telegramtopic.state import Lane, LaneStore

    db = tmp_path / "bot.db"
    store = LaneStore(db)
    store.upsert(Lane(4, 9, "topic 9", "claude-code", "", "parked", False, 1, None, 0))
    store._db.close()
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "build_config", lambda raw: Config("bot", "claude-code", frozenset({8})))
    monkeypatch.setattr(config, "state_db_path", lambda bot: db)
    monkeypatch.setattr(control, "control_dir", lambda bot, home=None: tmp_path / "control")

    assert control.close_main(["--thread", "9"]) == 0
    item, = (tmp_path / "control").glob("*.json")
    request = json.loads(item.read_text())
    assert request["action"] == "close" and (request["chat"], request["thread"]) == (4, 9)
    assert control.close_main(["--thread", "10"]) == 2
    assert "missing" in capsys.readouterr().out


def test_send_cli_resolves_lane_and_rejects_missing_archived_and_invalid_text(tmp_path, monkeypatch, capsys):
    from agent2telegramtopic import control, config
    from agent2telegramtopic.config import Config
    from agent2telegramtopic.state import Lane, LaneStore

    db = tmp_path / "bot.db"
    store = LaneStore(db)
    store.upsert(Lane(4, 9, "topic 9", "claude-code", "", "parked", False, 1, None, 0))
    store.upsert(Lane(5, 9, "topic 9", "claude-code", "", "waiting", False, 1, None, 0))
    store.upsert(Lane(4, 10, "topic 10", "claude-code", "", "archived", False, 1, None, 0))
    store._db.close()
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "build_config", lambda raw: Config("bot", "claude-code", frozenset({8})))
    monkeypatch.setattr(config, "state_db_path", lambda bot: db)
    monkeypatch.setattr(control, "control_dir", lambda bot, home=None: tmp_path / "control")

    assert control.send_main(["--thread", "9", "--chat", "4", "--text", "hello"]) == 0
    item, = (tmp_path / "control").glob("*.json")
    request = json.loads(item.read_text())
    assert request["action"] == "send" and request["text"] == "hello"
    assert (request["chat"], request["thread"]) == (4, 9)
    assert control.send_main(["--thread", "11", "--text", "hello"]) == 2
    assert control.send_main(["--thread", "9", "--text", "hello"]) == 2
    assert control.send_main(["--thread", "10", "--text", "hello"]) == 2
    assert control.send_main(["--thread", "9", "--chat", "4", "--text", ""]) == 2
    assert control.send_main(["--thread", "9", "--chat", "4", "--text", "x" * 4001]) == 2
    too_long_file = tmp_path / "too-long.txt"
    too_long_file.write_text("x" * 4001)
    assert control.send_main(["--thread", "9", "--chat", "4", "--text-file", str(too_long_file)]) == 2


def test_restart_cli_accepts_only_unique_live_lane(tmp_path, monkeypatch, capsys):
    from agent2telegramtopic import control, config
    from agent2telegramtopic.config import Config
    from agent2telegramtopic.state import Lane, LaneStore

    db = tmp_path / "bot.db"
    store = LaneStore(db)
    store.upsert(Lane(4, 9, "topic 9", "claude-code", "", "live", True, 1, None, 0))
    store.upsert(Lane(5, 9, "topic 9", "claude-code", "", "waiting", False, 1, None, 0))
    store.upsert(Lane(4, 10, "topic 10", "claude-code", "", "archived", False, 1, None, 0))
    store._db.close()
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "build_config", lambda raw: Config("bot", "claude-code", frozenset({8})))
    monkeypatch.setattr(config, "state_db_path", lambda bot: db)
    monkeypatch.setattr(control, "control_dir", lambda bot, home=None: tmp_path / "control")

    assert control.restart_main(["--thread", "9", "--chat", "4"]) == 0
    item, = (tmp_path / "control").glob("*.json")
    request = json.loads(item.read_text())
    assert request["action"] == "restart" and (request["chat"], request["thread"]) == (4, 9)
    assert control.restart_main(["--thread", "11"]) == 2
    assert control.restart_main(["--thread", "9"]) == 2
    assert control.restart_main(["--thread", "9", "--chat", "5"]) == 2
    assert control.restart_main(["--thread", "10"]) == 2
    assert "not running" in capsys.readouterr().out
