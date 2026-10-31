import json
import os
import stat
import time

import pytest

from agent2telegramtopic import status as status_module
from agent2telegramtopic.config import CONFIG_ENV, Config, lane_state_file
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.state import Lane, LaneStore
from agent2telegramtopic.status import StatusWriter

TOKEN = "7654321:status-test-token-must-never-leak"
LANE_KEYS = {"chat", "thread", "title", "agent", "cwd", "tmux_session", "alive", "status", "busy",
             "last_activity", "last_compact", "ctx_tokens", "ctx_window"}
TOP_KEYS = {"schema", "bot", "version", "pid", "config", "tmux_tmpdir", "started_at", "updated_at",
            "heartbeat_seconds", "lanes"}


@pytest.fixture
def instance(tmp_path, monkeypatch):
    cfg = tmp_path / "inst" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text("{}")
    monkeypatch.setenv(CONFIG_ENV, str(cfg))
    return cfg


def lane(thread=7, **changes):
    values = dict(chat_id=123, thread_id=thread, name=f"topic {thread}", agent="claude-code",
                  session_id="s", status="live", busy=True, last_activity=1000.9,
                  last_compact=None, ctx_tokens=84213)
    values.update(changes)
    return Lane(**values)


def make_writer(instance, tmp_path, store, sessions=lambda: set(), **kw):
    config = Config("StatusBot", "claude-code", frozenset({8}))
    return StatusWriter(tmp_path / "inst" / "state" / "StatusBot.status.json", config, instance,
                        store, str(tmp_path / "lanes"), sessions=sessions, **kw)


def read(writer):
    return json.loads(writer.path.read_text("utf-8"))


def test_writes_valid_schema_1_file_with_every_key(instance, tmp_path):
    store = LaneStore(":memory:")
    store.upsert(lane())
    name = "Claude – topic 7 · 123"
    writer = make_writer(instance, tmp_path, store, sessions=lambda: {name}, heartbeat_seconds=15)
    writer.write()
    data = read(writer)
    assert set(data) == TOP_KEYS
    assert data["schema"] == 1 and data["bot"] == "StatusBot" and data["heartbeat_seconds"] == 15
    assert data["pid"] == os.getpid() and data["config"] == str(instance)
    assert isinstance(data["started_at"], int) and isinstance(data["updated_at"], int)
    (entry,) = data["lanes"]
    assert set(entry) == LANE_KEYS
    assert entry["chat"] == 123 and entry["thread"] == 7 and entry["agent"] == "claude-code"
    assert entry["cwd"] == str(tmp_path / "lanes" / "123" / "7")
    assert entry["tmux_session"] == name and entry["alive"] is True
    assert entry["status"] == "live" and entry["busy"] is True
    assert entry["last_activity"] == 1000 and entry["ctx_tokens"] == 84213
    assert stat.S_IMODE(writer.path.stat().st_mode) == 0o600


def test_unknown_values_are_null_not_missing(instance, tmp_path):
    store = LaneStore(":memory:")
    store.upsert(lane(status="parked", busy=False))
    writer = make_writer(instance, tmp_path, store)
    writer.write()
    (entry,) = read(writer)["lanes"]
    assert entry["title"] is None and entry["last_compact"] is None and entry["ctx_window"] is None
    assert entry["alive"] is False and entry["status"] == "parked"


def test_context_window_is_matched_by_resolved_cwd_and_codex_stays_null(instance, tmp_path):
    store = LaneStore(":memory:")
    store.upsert(lane())
    store.upsert(lane(thread=8))
    writer = make_writer(instance, tmp_path, store)
    sidecar = writer.path.with_name("StatusBot.claude_ctx.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({str((tmp_path / "lanes/123/7").resolve()):
                                   {"ctx_window": 200000, "at": 1},
                                   str(tmp_path / "other"): {"ctx_window": 123, "at": 1}}))
    writer.write()
    entries = read(writer)["lanes"]
    assert entries[0]["ctx_window"] == 200000
    assert entries[1]["ctx_window"] is None
    writer.config = Config("StatusBot", "codex", frozenset({8}))
    writer.write()
    assert read(writer)["lanes"][0]["ctx_window"] is None


def test_last_compact_is_the_later_of_bridge_record_and_hook_marker(instance, tmp_path):
    store = LaneStore(":memory:")
    store.upsert(lane(last_compact=5000.0))
    writer = make_writer(instance, tmp_path, store)
    writer.write()
    assert read(writer)["lanes"][0]["last_compact"] == 5000
    marker = lane_state_file("StatusBot", 123, 7, "compact_done")
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    os.utime(marker, (9000, 9000))
    writer.write()
    assert read(writer)["lanes"][0]["last_compact"] == 9000
    store.upsert(lane(last_compact=12000.0))
    writer.write()
    assert read(writer)["lanes"][0]["last_compact"] == 12000


def test_failed_write_leaves_the_previous_file_whole(instance, tmp_path, monkeypatch):
    store = LaneStore(":memory:")
    store.upsert(lane())
    writer = make_writer(instance, tmp_path, store)
    writer.write()
    before = writer.path.read_text("utf-8")
    store.upsert(lane(ctx_tokens=1))

    def broken(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(status_module.os, "replace", broken)
    writer.write()  # logs, does not raise
    assert writer.path.read_text("utf-8") == before
    json.loads(before)


def test_title_is_captured_from_topic_service_messages_including_the_bots_own_edit(tmp_path):
    store = LaneStore(":memory:")
    mux = Multiplexer(object(), {8}, tmp_path / "bot", lambda key: None, lane_store=store,
                      lane_states_root=tmp_path / "bot" / "lanes")
    mux.bot_id = 555

    def service(uid, sender, field, payload, thread=10):
        msg = {"chat": {"id": 123, "type": "private"}, "from": sender, field: payload}
        if thread is not None:
            msg["message_thread_id"] = thread
        else:
            msg["message_id"] = 10
        return {"update_id": uid, "message": msg}

    mux._handle_update_once(service(1, {"id": 8}, "forum_topic_created", {"name": "Build"}, thread=None), 0)
    titles = lambda: {(k.chat_id, k.thread_id): t for k, t in store.topic_titles().items()}
    assert titles() == {(123, 10): "Build"}
    mux._handle_update_once(service(2, {"id": 8}, "forum_topic_edited", {"name": "Deploy"}), 2)
    assert titles() == {(123, 10): "Deploy"}
    mux._handle_update_once(service(3, {"id": 555, "is_bot": True}, "forum_topic_edited", {"name": "Own name"}), 3)
    assert titles() == {(123, 10): "Own name"}
    mux._handle_update_once(service(4, {"id": 555, "is_bot": True}, "forum_topic_edited", {"icon_custom_emoji_id": "1"}), 4)
    assert titles() == {(123, 10): "Own name"}
    mux._handle_update_once(service(5, {"id": 999, "is_bot": True}, "forum_topic_edited", {"name": "Foreign"}), 5)
    assert titles() == {(123, 10): "Own name"}


def test_title_appears_in_file_for_its_lane(instance, tmp_path):
    from agent2telegramtopic.routing import LaneKey
    store = LaneStore(":memory:")
    store.set_topic_title(LaneKey(123, 7), "agentsmon")
    store.upsert(lane())
    writer = make_writer(instance, tmp_path, store)
    writer.write()
    assert read(writer)["lanes"][0]["title"] == "agentsmon"


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    return False


def test_lane_change_rewrites_the_file_without_waiting_for_the_heartbeat(instance, tmp_path):
    from agent2telegramtopic.routing import LaneKey
    store = LaneStore(":memory:")
    store.upsert(lane(busy=False))
    writer = make_writer(instance, tmp_path, store, heartbeat_seconds=3600)
    writer.start()
    try:
        assert wait_for(lambda: read(writer)["lanes"][0]["busy"] is False)
        store.set_busy(LaneKey(123, 7), True)
        assert wait_for(lambda: read(writer)["lanes"][0]["busy"] is True)
        store.set_status(LaneKey(123, 7), "parked")
        assert wait_for(lambda: read(writer)["lanes"][0]["status"] == "parked")
        store.mark_compacted(LaneKey(123, 7), 777.0, 10)
        assert wait_for(lambda: read(writer)["lanes"][0]["last_compact"] == 777)
        store.upsert(lane(thread=8))
        assert wait_for(lambda: len(read(writer)["lanes"]) == 2)
        store.set_topic_title(LaneKey(123, 8), "Renamed")
        assert wait_for(lambda: read(writer)["lanes"][1]["title"] == "Renamed")
    finally:
        writer.stop()


def test_heartbeat_refreshes_updated_at_when_nothing_changes(instance, tmp_path):
    store = LaneStore(":memory:")
    ticks = iter(range(1000, 2000))
    writer = make_writer(instance, tmp_path, store, heartbeat_seconds=0.05, clock=lambda: next(ticks))
    writer.start()
    try:
        assert wait_for(lambda: read(writer)["updated_at"] > read(writer)["started_at"] + 2)
    finally:
        writer.stop()


def test_file_carries_no_token_and_no_message_text(instance, tmp_path):
    store = LaneStore(":memory:")
    store.upsert(lane(name=f"private chat text {TOKEN}", session_id=TOKEN))
    config = Config("StatusBot", "claude-code", frozenset({8}), )
    writer = StatusWriter(tmp_path / "inst" / "state" / "StatusBot.status.json", config, instance,
                          store, str(tmp_path / "lanes"), sessions=lambda: set())
    writer.write()
    raw = writer.path.read_text("utf-8")
    assert TOKEN not in raw and "private chat text" not in raw
    assert set(json.loads(raw)["lanes"][0]) == LANE_KEYS


def test_running_bridge_writes_the_file_on_start(instance, tmp_path):
    class Poller:
        def _call(self, method, params, *, timeout=None):
            mux.stop()
            return []

    store = LaneStore(":memory:")
    store.upsert(lane())
    mux = Multiplexer(Poller(), {8}, tmp_path / "bot", lambda key: None, lane_store=store,
                      lane_states_root=tmp_path / "bot" / "lanes")
    mux.status_writer = make_writer(instance, tmp_path, store)
    mux.run_forever()
    assert wait_for(lambda: read(mux.status_writer)["schema"] == 1)
