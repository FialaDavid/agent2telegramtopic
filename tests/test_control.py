import json
from pathlib import Path

from agent2telegramtopic.control import write_close


def test_control_request_is_atomic_json_with_required_fields(tmp_path):
    path = write_close('bot', 4, 9, home=tmp_path)
    data = json.loads(path.read_text())
    assert path.parent == tmp_path / '.agent2telegramtopic' / 'state' / 'bot' / 'control'
    assert data['action'] == 'close'
    assert (data['chat'], data['thread']) == (4, 9)
    assert isinstance(data['requested_at'], (int, float))
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
