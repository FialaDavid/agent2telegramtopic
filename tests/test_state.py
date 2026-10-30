import sqlite3
from concurrent.futures import ThreadPoolExecutor

from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import Lane, LaneStore


def make_lane(thread_id=7, **changes):
    values = dict(chat_id=123, thread_id=thread_id, name="Build", agent="codex",
                  session_id="session-1", status="live", busy=False,
                  last_activity=10.0, last_compact=None, ctx_tokens=32000)
    values.update(changes)
    return Lane(**values)


def test_in_memory_roundtrip_and_upsert_overwrite():
    store = LaneStore(":memory:")
    lane = make_lane()
    store.upsert(lane)
    assert store.get(LaneKey(123, 7)) == lane

    replacement = make_lane(name="Renamed", session_id="session-2", ctx_tokens=12000)
    store.upsert(replacement)
    assert store.get(LaneKey(123, 7)) == replacement


def test_list_live_excludes_parked_lanes():
    store = LaneStore(":memory:")
    live, parked = make_lane(), make_lane(thread_id=8, status="parked")
    store.upsert(live)
    store.upsert(parked)
    assert store.list_live() == [live]


def test_file_store_creates_parent_and_persists_across_instances(tmp_path):
    db_path = tmp_path / "nested" / "state" / "bot.db"
    lane = make_lane()

    LaneStore(db_path).upsert(lane)

    reopened = LaneStore(db_path)
    assert db_path.is_file()
    assert reopened.get(LaneKey(123, 7)) == lane


def test_touch_compaction_busy_and_status_updates():
    store = LaneStore(":memory:")
    lane = make_lane()
    key = LaneKey(lane.chat_id, lane.thread_id)
    store.upsert(lane)

    store.touch(key, 20.5)
    assert store.get(key).last_activity == 20.5
    store.mark_compacted(key, 21.0, 15000)
    assert store.get(key).last_compact == 21.0
    assert store.get(key).ctx_tokens == 15000
    store.set_busy(key, True)
    assert store.get(key).busy is True
    store.set_status(key, "parked")
    assert store.get(key).status == "parked"


def test_set_ctx_tokens_updates_only_context_count():
    store = LaneStore(":memory:")
    lane = make_lane()
    key = LaneKey(lane.chat_id, lane.thread_id)
    store.upsert(lane)

    store.set_ctx_tokens(key, 18000)

    assert store.get(key) == make_lane(ctx_tokens=18000)
def test_old_status_constraint_migrates_existing_lanes(tmp_path):
    db_path = tmp_path / "old.db"
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE lanes (
        chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, name TEXT NOT NULL,
        agent TEXT NOT NULL, session_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('live', 'parked')),
        busy INTEGER NOT NULL, last_activity REAL NOT NULL, last_compact REAL,
        ctx_tokens INTEGER NOT NULL, PRIMARY KEY (chat_id, thread_id))""")
    db.execute("INSERT INTO lanes VALUES (123, 7, 'Build', 'codex', 'session-1', 'parked', 0, 10, NULL, 32000)")
    db.commit()
    db.close()

    store = LaneStore(db_path)
    assert store.get(LaneKey(123, 7)) == make_lane(status="parked")
    waiting = make_lane(status="waiting")
    store.upsert(waiting)
    assert store.get(LaneKey(123, 7)) == waiting


def test_waiting_status_persists_across_reopen(tmp_path):
    db_path = tmp_path / "waiting.db"
    lane = make_lane(status="waiting")
    LaneStore(db_path).upsert(lane)

    assert LaneStore(db_path).get(LaneKey(123, 7)) == lane


def test_concurrent_reads_and_writes_are_safe():
    store = LaneStore(":memory:")
    key = LaneKey(123, 7)
    store.upsert(make_lane())

    def update(index):
        store.touch(key, float(index))
        store.set_ctx_tokens(key, index)
        store.set_busy(key, bool(index % 2))
        return store.get(key)

    with ThreadPoolExecutor(max_workers=12) as pool:
        rows = list(pool.map(update, range(100)))

    final = store.get(key)
    assert final.last_activity in {float(i) for i in range(100)}
    assert final.ctx_tokens in range(100)
    assert len(rows) == 100
    assert all(row is not None for row in rows)
    assert all(row.chat_id == 123 and row.thread_id == 7 for row in rows)
    assert all(row.last_activity in {float(i) for i in range(100)} for row in rows)
    assert all(row.ctx_tokens in range(100) for row in rows)


def test_message_thread_mapping_roundtrips_and_persists(tmp_path):
    db_path = tmp_path / "bot.db"
    LaneStore(db_path).record_message(123, 900, 7)
    reopened = LaneStore(db_path)
    assert reopened.thread_for_message(123, 900) == 7
    assert reopened.thread_for_message(123, 901) is None
    assert reopened.thread_for_message(456, 900) is None
