import io
import json
from pathlib import Path
import pytest

from agent2telegramtopic import hook


def test_hook_writes_per_lane_markers_and_ignores_foreign_or_bad_input(monkeypatch, tmp_path):
    monkeypatch.setattr(hook.config, "lanes_root", lambda bot: tmp_path / bot)
    monkeypatch.setattr(hook.config, "lane_dir", lambda bot, chat, lane: tmp_path / bot / str(chat) / str(lane))
    monkeypatch.setattr(hook.config, "lane_state_dir", lambda bot, chat, lane, home=None: tmp_path / bot / "state" / str(chat) / str(lane))
    for chat, lane in ((123, 101), (456, 101)):
        payload = {"cwd": str(tmp_path / "bot" / str(chat) / str(lane)), "session_id": f"s{chat}", "transcript_path": f"/tmp/{chat}.jsonl"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main(["--bot", "bot"]) == 0
        marker = hook.config.lane_state_file("bot", chat, lane, "session")
        assert marker == tmp_path / "bot" / "state" / str(chat) / str(lane) / "lane_session.json"
        saved = json.loads(marker.read_text())
        assert saved == {"session_id": f"s{chat}", "transcript_path": f"/tmp/{chat}.jsonl"}
        config_marker = hook.config.lane_state_file("bot", chat, lane, "turn_end")
        assert config_marker.exists()
    marker_files = list((tmp_path / "bot" / "state").rglob("lane_session.json"))
    assert len(marker_files) == 2
    foreign = {"cwd": str(Path.home()), "session_id": "x", "transcript_path": "/x"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(foreign)))
    assert hook.main(["--bot", "bot"]) == 0
    another_bot = {"cwd": str(tmp_path / "another-bot" / "77"), "session_id": "y", "transcript_path": "/y"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(another_bot)))
    assert hook.main(["--bot", "bot"]) == 0
    for aliased_cwd in (tmp_path / "bot" / "123" / "007", tmp_path / "bot" / "123" / "٣"):
        payload = {"cwd": str(aliased_cwd), "session_id": "alias", "transcript_path": "/alias"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main(["--bot", "bot"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("{"))
    assert hook.main(["--bot", "bot"]) == 0
    for bad in ({"cwd": str(tmp_path / "bot" / "123" / "303"), "session_id": "missing-path"},
                {"cwd": str(tmp_path / "bot" / "123" / "404"), "session_id": "", "transcript_path": "/bad"}):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bad)))
        assert hook.main(["--bot", "bot"]) == 0
    assert sorted(str(p) for p in (tmp_path / "bot" / "state").rglob("lane_session.json")) == [
        str(tmp_path / "bot" / "state" / "123" / "101" / "lane_session.json"),
        str(tmp_path / "bot" / "state" / "456" / "101" / "lane_session.json")]


def test_hook_accepts_app_selected_lane_root(monkeypatch, tmp_path):
    custom_root = tmp_path / "custom"
    monkeypatch.setattr(hook.config, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / str(chat) / str(thread) / "lane_session.json")
    payload = {"cwd": str(custom_root / "123" / "42"), "session_id": "custom-session",
               "transcript_path": "/tmp/custom.jsonl"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main(["--bot", "bot", "--lanes-root", str(custom_root)]) == 0
    marker = hook.config.lane_state_file("bot", 123, 42, "session")
    assert json.loads(marker.read_text()) == {
        "session_id": "custom-session", "transcript_path": "/tmp/custom.jsonl"}


def test_compact_session_start_persists_identity_and_compact_done_without_stop(tmp_path, monkeypatch):
    lane_root = tmp_path / "bot"
    state_root = tmp_path / "state"
    from agent2telegramtopic import config
    monkeypatch.setattr(config, "lane_state_file", lambda bot, chat, thread, kind:
                   state_root / str(chat) / str(thread) /
                   ("lane_session.json" if kind == "session" else kind))
    payload = {"hook_event_name": "SessionStart", "source": "compact",
               "cwd": str(lane_root / "7" / "9"), "session_id": "sid",
               "transcript_path": "/tmp/session.jsonl"}
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main(["--bot", "bot", "--lanes-root", str(lane_root)]) == 0
    lane_state = state_root / "7" / "9"
    assert json.loads((lane_state / "lane_session.json").read_text()) == {
        "session_id": "sid", "transcript_path": "/tmp/session.jsonl"}
    assert (lane_state / "compact_done").exists()
    assert not (lane_state / "turn_end").exists()
