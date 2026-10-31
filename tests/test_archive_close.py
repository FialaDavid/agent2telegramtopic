"""/close summarises, archives and closes a topic; writing there again resumes the same session."""
import json
import threading
import time
from pathlib import Path

import pytest

from agent2telegramtopic import app as app_module
from agent2telegramtopic import lane as lane_module
from agent2telegramtopic import archive
from agent2telegramtopic.config import Config
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import Lane, LaneStore

KEY = LaneKey(123, 42)
CONFIRMATION = "🗄️ Archived. You can delete this topic; writing here again resumes the session."
SUMMARY = "Went over the boiler.\nOutcome: fixed.\nOpen: order a spare part."


class Client:
    def __init__(self, token=None):
        self.sent = []

    def send_message(self, key, text):
        self.sent.append((key, text))

    def send_chat_action(self, key, action):
        pass


class Session:
    """A tmux session that answers the summary prompt the way the real agent logs it."""

    def __init__(self, bridge, agent, answer=SUMMARY):
        self.bridge, self.agent, self.answer = bridge, agent, answer
        self.alive = True
        self.commands = []

    def send_command(self, text):
        self.commands.append(text)
        if self.answer is None:
            return
        stamp = "2026-11-01T10:00:00Z"
        if self.agent == "claude-code":
            records = [{"type": "user", "timestamp": stamp, "message": {"content": text}},
                       {"type": "assistant", "uuid": "s1", "timestamp": stamp,
                        "message": {"content": [{"type": "text", "text": self.answer}]}}]
        else:
            records = [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}},
                       {"type": "event_msg", "payload": {"type": "user_message", "message": text}},
                       {"type": "event_msg", "payload": {"type": "agent_message", "message": self.answer}},
                       {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t"}}]
        with self.bridge._transcript.open("a") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        if self.agent == "claude-code":
            self.bridge._turn_end.touch()

    def dialog_snapshot(self):
        return "", True

    def close(self):
        self.alive = False


@pytest.fixture
def world(monkeypatch, tmp_path):
    client = Client()
    monkeypatch.setattr(app_module, "TelegramClient", lambda token: client)
    monkeypatch.setattr(app_module, "state_db_path", lambda bot: tmp_path / "state" / f"{bot}.db")
    monkeypatch.setattr(app_module, "lane_state_file", lambda bot, chat, thread, kind:
                        tmp_path / "state" / bot / "lanes" / str(chat) / str(thread) / kind)
    monkeypatch.setattr(app_module, "lane_path", lambda root, chat, thread:
                        Path(root) / str(chat) / str(thread))
    monkeypatch.setattr(app_module, "status_file", lambda bot: tmp_path / "state" / f"{bot}.status.json")

    def build(agent, answer=SUMMARY):
        mux = app_module.build_app(Config("bot", agent, frozenset({8})), "token", str(tmp_path / "lanes"))
        bridge = mux.lane_factory(KEY)
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-10-30T08:00:00+02:00",
                                          "message": {"content": "boiler?"}}) + "\n")
        bridge._transcript = transcript
        bridge._tpos = transcript.stat().st_size
        bridge._last_resolve = float("inf")
        bridge._session = Session(bridge, agent, answer)
        bridge.session_id = "sess-1"
        mux.lane_store.upsert(Lane(KEY.chat_id, KEY.thread_id, "topic 42", agent, "sess-1", "live",
                                   False, time.time(), None, 0))
        mux.lane_store.set_topic_title(KEY, "Boiler")
        stop = threading.Event()

        def ticker():
            while not stop.is_set():
                bridge.outbound_tick()
                time.sleep(0.02)
        threading.Thread(target=ticker, daemon=True).start()
        world.stops.append(stop)
        return mux, bridge, client

    world.stops = []
    yield build
    for stop in world.stops:
        stop.set()


def close_and_wait(bridge, client, mux):
    bridge._on_close(KEY)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not client.sent:
        time.sleep(0.02)
    mux.status_writer.write()


def records(tmp_path):
    index = tmp_path / "state" / "bot" / "archive" / "index.jsonl"
    return [json.loads(line) for line in index.read_text().splitlines()]


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_close_archives_the_topic_with_the_agents_summary(world, tmp_path, agent):
    mux, bridge, client = world(agent)
    close_and_wait(bridge, client, mux)
    assert [text for _, text in client.sent] == [CONFIRMATION]
    status = json.loads((tmp_path / "state" / "bot.status.json").read_text())
    assert status["lanes"] == []
    (record,) = records(tmp_path)
    assert record["summary"] == SUMMARY and record["summary_error"] is None
    assert (record["bot"], record["chat"], record["thread"], record["title"]) == ("bot", 123, 42, "Boiler")
    assert record["agent"] == agent and record["session_id"] == "sess-1"
    assert record["created_at"] == "2026-10-30T06:00:00Z"
    assert record["closed_at"].endswith("Z")
    assert record["transcript"] == str((tmp_path / "transcript.jsonl").resolve())
    assert list(record) == list(archive.FIELDS)
    (markdown,) = (tmp_path / "state" / "bot" / "archive").glob("*-123-42.md")
    assert SUMMARY in markdown.read_text()
    assert mux.lane_store.get(KEY).status == "archived"
    assert not bridge._session.alive
    # The summary question and its answer stayed inside the session: nothing but the confirmation.
    assert len(client.sent) == 1


def test_summary_that_never_arrives_still_archives_with_null_summary(world, tmp_path, monkeypatch):
    monkeypatch.setattr(lane_module, "INTERNAL_COMMAND_TIMEOUT", 0.3)
    mux, bridge, client = world("claude-code", answer=None)
    close_and_wait(bridge, client, mux)
    assert [text for _, text in client.sent] == [CONFIRMATION]
    (record,) = records(tmp_path)
    assert record["summary"] is None and record["summary_error"]
    assert mux.lane_store.get(KEY).status == "archived"


def test_closing_twice_appends_instead_of_overwriting(world, tmp_path):
    mux, bridge, client = world("claude-code")
    close_and_wait(bridge, client, mux)
    mux, bridge, client = world("claude-code", answer="Second close.")
    close_and_wait(bridge, client, mux)
    assert [r["summary"] for r in records(tmp_path)] == [SUMMARY, "Second close."]
    (markdown,) = (tmp_path / "state" / "bot" / "archive").glob("*-123-42.md")
    text = markdown.read_text()
    assert SUMMARY in text and "Second close." in text and text.count("## Closed") == 2


def test_lane_without_a_live_session_is_archived_without_starting_one(world, tmp_path):
    mux, bridge, client = world("claude-code")
    bridge._session.alive = False
    close_and_wait(bridge, client, mux)
    (record,) = records(tmp_path)
    assert record["summary"] is None and record["summary_error"] is None
    assert bridge._session.commands == []
    assert mux.lane_store.get(KEY).status == "archived"


def test_archived_lane_is_not_listed_nor_counted_as_live(tmp_path):
    store = LaneStore(tmp_path / "x.db")
    store.upsert(Lane(1, 1, "a", "codex", "s-a", "archived", False, 0.0, None, 0))
    store.upsert(Lane(1, 2, "b", "codex", "s-b", "live", False, 0.0, None, 0))
    assert [lane.thread_id for lane in store.list_all()] == [2]
    assert [lane.thread_id for lane in store.list_live()] == [2]
    store.reconcile(set())
    assert store.get(LaneKey(1, 1)).status == "archived"


def test_old_database_is_migrated_and_keeps_its_lanes(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE lanes (chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
        name TEXT NOT NULL, agent TEXT NOT NULL, session_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('live', 'parked', 'waiting')),
        busy INTEGER NOT NULL, last_activity REAL NOT NULL, last_compact REAL,
        ctx_tokens INTEGER NOT NULL, PRIMARY KEY (chat_id, thread_id))""")
    db.execute("INSERT INTO lanes VALUES (1, 2, 'n', 'codex', 'sid', 'parked', 0, 1.0, NULL, 5)")
    db.commit()
    db.close()
    store = LaneStore(path)
    assert store.get(LaneKey(1, 2)).session_id == "sid"
    store.set_status(LaneKey(1, 2), "archived")
    assert store.get(LaneKey(1, 2)).status == "archived"


def test_message_in_an_archived_topic_resumes_the_same_session(world, tmp_path, monkeypatch):
    mux, bridge, client = world("claude-code")
    marker = tmp_path / "state" / "bot" / "lanes" / "123" / "42" / "session"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"session_id": "sess-1", "transcript_path": str(tmp_path / "transcript.jsonl")}))
    close_and_wait(bridge, client, mux)
    assert mux.lane_store.get(KEY).status == "archived"

    started = []

    class Resumed:
        def __init__(self, argv, **kwargs):
            started.append(argv)
            self.alive = True

        def inject(self, text):
            pass

        def dialog_snapshot(self):
            return "", True

        def close(self):
            self.alive = False
    monkeypatch.setattr(app_module, "TmuxSession", Resumed)
    mux._start_slot_manager()
    mux._handle_update_once({"update_id": 9, "message": {
        "chat": {"id": 123, "type": "private"}, "message_thread_id": 42, "from": {"id": 8},
        "text": "are we back?"}}, 0)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not started:
        time.sleep(0.02)
    mux.stop()
    assert started and "--resume" in started[0] and "sess-1" in started[0]
    assert mux.lane_store.get(KEY).status == "live"
    assert mux.lane_store.get(KEY).session_id == "sess-1"
    assert len(records(tmp_path)) == 1


def test_archived_lanes_do_not_consume_live_slots(tmp_path):
    store = LaneStore(tmp_path / "slots.db")
    for thread in range(1, 6):
        store.upsert(Lane(1, thread, "t", "codex", "s", "archived", False, 0.0, None, 0))
    from agent2telegramtopic.multiplexer import Multiplexer
    mux = Multiplexer(Client(), {8}, tmp_path / "bot", lambda key: None, lane_store=store, max_live=1)
    assert mux._live_count() == 0


def test_message_sent_while_closing_is_still_answered(world, tmp_path, monkeypatch):
    mux, bridge, client = world("claude-code")
    inbox = bridge._ensure_inbox()
    monkeypatch.setattr(inbox, "pending", lambda: ["queued while closing"])
    close_and_wait(bridge, client, mux)
    (record,) = records(tmp_path)
    assert record["summary"] == SUMMARY
    assert mux.lane_store.get(KEY).status == "waiting"
