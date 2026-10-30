import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic import readers


def make_lane(tmp_path, agent, **kwargs):
    session_id = kwargs.pop("session_id", None)
    lane = LaneBridge(object(), LaneKey(1, 9), tmp_path / "sent", tmp_path / "queue",
                      agent=agent, lane_cwd=tmp_path / "lanes" / "9", **kwargs)
    lane.session_id = session_id
    lane._last_resolve = -100
    return lane


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def claude_file(path, before, reply):
    path.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": stamp(before), "message": {"content": "old"}},
        {"type": "assistant", "timestamp": stamp(before), "message": {"content": [{"type": "text", "text": "old reply"}]}},
        {"type": "user", "timestamp": stamp(reply), "message": {"content": "new"}},
        {"type": "assistant", "timestamp": stamp(reply), "message": {"content": [{"type": "text", "text": "new reply"}]}},
    ]) + "\n")


def test_claude_new_session_reply_is_positioned_once(tmp_path, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("agent2telegramtopic.lane.time.time", lambda: now)
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text('{"type":"user"}\n')
    claude_file(b, now - 30, now + 1)
    marker = tmp_path / "marker.json"
    marker.write_text(json.dumps({"session_id": "B", "transcript_path": str(b)}))
    lane = make_lane(tmp_path, "claude-code", session_marker=marker, session_id="A")
    lane._transcript = a
    lane._turn_active.set()
    lane._turn_started_wall = now
    lane._maybe_reresolve()
    assert lane._transcript == b
    assert lane.session_id == "B"
    # Reading from the byte cursor yields the new turn's reply exactly once.
    with b.open("rb") as f:
        f.seek(lane._tpos)
        delivered = [event.text for line in f if line.strip() for event in readers.ClaudeCodeReader().parse(json.loads(line)) if event.kind == "text"]
    assert delivered == ["new reply"]
    prior = lane._tpos
    lane._last_resolve = -100
    lane._maybe_reresolve()
    assert lane._tpos == prior


def test_codex_adopts_only_matching_new_rollout_and_positions_reply(tmp_path, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr("agent2telegramtopic.lane.time.time", lambda: now)
    cwd = tmp_path / "lanes" / "9"
    cwd.mkdir(parents=True)
    root = tmp_path / "sessions"
    root.mkdir()
    def rollout(path, ident, rollout_cwd, before, after):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(x) for x in [
            {"type": "session_meta", "payload": {"id": ident, "cwd": str(rollout_cwd)}},
            {"type": "event_msg", "timestamp": stamp(before), "payload": {"type": "user_message", "message": "old"}},
            {"type": "response_item", "timestamp": stamp(before), "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "old reply"}]}},
            {"type": "event_msg", "timestamp": stamp(after), "payload": {"type": "user_message", "message": "new"}},
            {"type": "response_item", "timestamp": stamp(after), "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "new reply"}]}},
        ]) + "\n")
    old = root / "old" / "rollout-old.jsonl"
    rollout(old, "old", cwd, now - 30, now - 20)
    lane = make_lane(tmp_path, "codex", codex_sessions_root=root)
    lane._transcript = old
    lane._turn_active.set()
    lane._turn_started_wall = now
    new = root / "new" / "rollout-new.jsonl"
    rollout(new, "new", cwd, now - 30, now + 1)
    new.touch()
    lane._maybe_reresolve()
    assert lane._transcript == new and lane.session_id == "new"
    with new.open("rb") as f:
        f.seek(lane._tpos)
        delivered = [event.text for line in f if line.strip() for event in readers.CodexReader().parse(json.loads(line)) if event.kind == "text"]
    assert delivered == ["new reply"]
    foreign = root / "foreign" / "rollout-foreign.jsonl"
    rollout(foreign, "foreign", tmp_path / "another-lane", now, now + 4)
    foreign.touch()
    foreign_home = root / "home" / "rollout-home.jsonl"
    rollout(foreign_home, "home", Path.home(), now, now + 5)
    foreign_home.touch()
    lane._last_resolve = -100
    lane._maybe_reresolve()
    assert lane._transcript == new and lane.session_id == "new"


def test_codex_missing_matching_rollout_clears_identity_and_history_starts_at_end(tmp_path, caplog):
    lane = make_lane(tmp_path, "codex", codex_sessions_root=tmp_path / "none")
    old = tmp_path / "old.jsonl"
    old.write_text("{}\n")
    lane._transcript = old
    with caplog.at_level(logging.ERROR):
        lane._maybe_reresolve()
    assert lane._transcript is None
    assert "no rollout is proven" in caplog.text
    path = tmp_path / "history.jsonl"
    path.write_text("{}\n")
    lane.agent = "claude-code"
    lane.session_marker = tmp_path / "marker.json"
    lane.session_marker.write_text(json.dumps({"session_id": "history", "transcript_path": str(path)}))
    lane.session_id = "history"
    lane._transcript = None
    lane._turn_active.clear()
    lane._last_resolve = -100
    lane._maybe_reresolve()
    assert lane._transcript == path
    assert lane._tpos == path.stat().st_size
