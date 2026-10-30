import json
import logging
import threading
import time

from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.telegram import TelegramClient


def update(uid, thread=1, *, sender=8, chat_type="private", text="hello"):
    return {"update_id": uid, "message": {"chat": {"id": 123, "type": chat_type},
            "message_thread_id": thread, "from": {"id": sender}, "text": text}}


from telegram_fakes import TransportOpener


class Client:
    def __init__(self, batches=()):
        self.batches = list(batches)
        self.sent = []
        self.polls = []
        self.allowed_updates = []
        self.on_last_poll = lambda: None

    def _call(self, method, params, *, timeout=None):
        assert method == "getUpdates"
        self.polls.append((params["offset"], params["timeout"], timeout))
        self.allowed_updates.append(params["allowed_updates"])
        batch = self.batches.pop(0) if self.batches else []
        if not self.batches:
            self.on_last_poll()
        return batch

    def send_message(self, *args):
        self.sent.append(args)

    def send_chat_action(self, *args):
        pass

    def get_file_path(self, file_id):
        return f"photos/{file_id}.jpg"

    def download(self, file_path):
        return b"image"


class FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def make_mux(tmp_path, client, lanes=None):
    lanes = lanes if lanes is not None else {}

    def factory(key):
        root = tmp_path / "bot" / "lanes" / str(key.chat_id) / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        session = FakeSession()
        lane = LaneBridge(client, key, root / "sent", root / "queue")
        lane._session = session
        lane._start_outbound = lambda: None
        lane._outbound_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory,
                      lane_states_root=tmp_path / "bot" / "lanes")
    return mux, lanes


def test_crash_between_reserve_and_offset_save_replays_without_loss(tmp_path):
    upd = update(41, 10)
    key = LaneKey(123, 10)
    client = Client()
    lanes, delivered = {}, []
    lane_root = tmp_path / "bot" / "lanes" / "123" / "10"

    started, release = threading.Event(), threading.Event()
    restarting = [False]
    def factory(k):
        session = FakeSession()
        lane = LaneBridge(client, k, lane_root / "sent", lane_root / "queue")
        lane._session = session
        if restarting[0]:
            original_inject = session.inject
            def inject(text):
                started.set(); release.wait(2); original_inject(text); delivered.append(41)
            session.inject = inject
        else:
            session.inject = lambda text: delivered.append(41)
        lanes[k] = lane
        lane._outbound_loop = lambda: None
        return lane

    first = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_states_root=tmp_path / "bot" / "lanes")
    inbox = first._lane(key)._ensure_inbox()
    reserve = inbox.reserve
    def reserve_then_crash(update):
        reserve(update)
        raise KeyboardInterrupt("simulated process crash after durable reservation")
    inbox.reserve = reserve_then_crash
    try:
        first._handle_update_once(upd, 0)
    except KeyboardInterrupt:
        pass
    # The reservation survives, even though the bot-level commit did not.
    assert len(lanes[key]._ensure_inbox().pending()) == 1
    assert not (tmp_path / "bot" / "offset").exists()

    restarting[0] = True
    client = Client([[upd]])
    restarted = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_states_root=tmp_path / "bot" / "lanes")
    client.on_last_poll = restarted.stop
    restarted._inbound_loop()
    lane = restarted._lanes[key]
    assert started.wait(1)
    release.set()
    lane._inbound_queue.join()
    assert delivered == [41]
    assert json.loads((tmp_path / "bot" / "offset").read_text()) == {"offset": 42}


def test_reserve_before_offset_restart_replays_and_answers_once(tmp_path):
    upd = update(41, 10, text="replay me")
    key = LaneKey(123, 10)
    fake_http = {"updates": [], "sent": [], "mux": None}
    def transport(method, params):
        if method == "getUpdates":
            updates = fake_http["updates"]
            fake_http["updates"] = []
            if not updates:
                fake_http["mux"].stop()
            return {"ok": True, "result": updates}
        if method in ("sendMessage", "sendRichMessage"):
            sent = dict(params)
            if method == "sendRichMessage":
                sent["text"] = json.loads(params["rich_message"])["markdown"]
            fake_http["sent"].append(sent)
            return {"ok": True, "result": {"message_id": len(fake_http["sent"])} }
        if method == "sendChatAction":
            return {"ok": True, "result": True}
        raise AssertionError(method)
    client = TelegramClient("fake-token", opener=TransportOpener(transport))
    lanes = {}

    def factory(k):
        root = tmp_path / "bot" / "lanes" / str(k.chat_id) / str(k.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        transcript = root / "transcript.jsonl"
        marker = root / "session.json"
        marker.write_text(json.dumps({"transcript_path": str(transcript)}))
        lane = LaneBridge(client, k, root / "sent", root / "queue", agent="claude-code",
                          session_marker=marker)
        lane._transcript = transcript

        class AnswerSession(FakeSession):
            def inject(self, text):
                self.injected.append(text)
                with transcript.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "user", "message": {"content": "Telegram: " + text}}) + "\n")
                    f.write(json.dumps({"type": "assistant", "uuid": "replay-answer",
                        "message": {"content": [{"type": "text", "text": "answer: " + text}]}}) + "\n")
                (root / "turn_end").touch()

        lane._session = AnswerSession()
        lane._outbound_loop = lambda: None
        lanes[k] = lane
        return lane

    first = Multiplexer(client, {8}, tmp_path / "bot", factory,
                        lane_states_root=tmp_path / "bot" / "lanes")
    lane = first._lane(key)
    lane._ensure_inbox().reserve(upd)
    first.stop()  # simulated crash after reserve, before offset save or injection

    fake_http["updates"] = [upd]
    restarted = Multiplexer(client, {8}, tmp_path / "bot", factory,
                            lane_states_root=tmp_path / "bot" / "lanes")
    fake_http["mux"] = restarted
    restarted._inbound_loop()
    replayed = lanes[key]
    replayed._inbound_queue.join()
    replayed.outbound_tick()
    replayed.outbound_tick()
    assert replayed._session.injected == ["replay me"]
    assert [(item["text"], item["message_thread_id"]) for item in fake_http["sent"]] == [("answer: replay me", 10)]
    assert restarted._update_was_processed(41)


def test_duplicate_update_id_is_processed_once(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    upd = update(1, 10)
    mux._handle_update_once(upd, 0)
    lane = lanes[LaneKey(123, 10)]
    lane._inbound_queue.join()
    mux._handle_update_once(upd, 0)
    assert lanes[LaneKey(123, 10)]._session.injected == ["hello"]


def test_existing_parked_topic_message_is_injected_unchanged_after_restart(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore

    client = Client()
    lanes = {}
    store = LaneStore(tmp_path / "parked-lanes.db")
    key = LaneKey(123, 10)
    store.upsert(Lane(123, 10, "parked topic", "claude-code", "", "parked",
                      False, time.time(), None, 0))
    mux, _ = make_mux(tmp_path, client, lanes)
    mux.lane_store = store
    mux.max_live = 4
    mux._start_slot_manager()
    mux._start_lane(key)
    mux._handle_update_once(update(1, 10, text="resume existing"), 0)
    lane = lanes[key]
    lane._inbound_queue.join()
    assert lane._session.injected == ["resume existing"]


def test_general_topic_gets_no_name_hint(tmp_path):
    from agent2telegramtopic.state import LaneStore

    client = Client()
    lanes = {}
    mux, _ = make_mux(tmp_path, client, lanes)
    mux.lane_store = LaneStore(tmp_path / "general-lanes.db")
    mux.max_live = 4
    mux._start_slot_manager()
    key = LaneKey(123, 1)
    mux._start_lane(key)
    mux._handle_update_once(update(1, 1, text="general message"), 0)
    lane = lanes[key]
    lane._inbound_queue.join()
    assert lane._session.injected == ["general message"]


def test_topic_a_message_is_never_injected_into_topic_b_session(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    mux._handle_update_once(update(1, 10, text="topic A"), 0)
    mux._handle_update_once(update(2, 11, text="topic B"), 2)
    mux._handle_update_once(update(3, 1, text="general"), 3)
    for lane in lanes.values():
        lane._inbound_queue.join()
    assert lanes[LaneKey(123, 10)]._session.injected == ["topic A"]
    assert lanes[LaneKey(123, 11)]._session.injected == ["topic B"]
    assert lanes[LaneKey(123, 1)]._session.injected == ["general"]


def test_mid_inject_lane_does_not_block_other_topic_inbound(tmp_path):
    client = Client()
    lanes = {}
    mux, _ = make_mux(tmp_path, client, lanes)
    a_started, release_a, b_injected = threading.Event(), threading.Event(), threading.Event()
    def factory(key):
        root = tmp_path / "bot" / "lanes" / str(key.chat_id) / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        lane = LaneBridge(client, key, root / "sent", root / "queue")
        class Session(FakeSession):
            def inject(self, text):
                self.injected.append(text)
                if key.thread_id == 10 and text == "A first":
                    a_started.set()
                    assert release_a.wait(3)
                if key.thread_id == 11:
                    b_injected.set()
        lane._session = Session()
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane
    mux.lane_factory = factory
    mux._handle_update_once(update(1, 10, text="A first"), 0)
    lane_a = lanes[LaneKey(123, 10)]
    assert a_started.wait(2)
    def submit_followups():
        mux._handle_update_once(update(2, 10, text="A second"), 2)
        mux._handle_update_once(update(3, 11, text="B"), 3)
    followups = threading.Thread(target=submit_followups)
    followups.start()
    try:
        assert b_injected.wait(2)
        assert lane_a._session.injected == ["A first"]
    finally:
        release_a.set()
    followups.join(3)
    assert not followups.is_alive()
    lane_a._inbound_queue.join()
    lanes[LaneKey(123, 11)]._inbound_queue.join()
    assert lane_a._session.injected == ["A first", "A second"]
    mux.stop()


def test_fake_http_batch_answers_two_topics_concurrently_in_their_own_threads(tmp_path):
    """Exercise getUpdates through sendMessage with independent concurrent lane workers."""
    from agent2telegramtopic.durable import DurableInbox

    updates = [update(70, 10, text="topic A"), update(71, 11, text="topic B")]
    injection_barrier = threading.Barrier(2, timeout=3)
    sent = []
    lanes = {}
    mux_ref = {}
    batches = [updates, []]

    def transport(method, params):
        if method == "getUpdates":
            batch = batches.pop(0) if batches else []
            if not batch:
                mux_ref["mux"].stop()
            return {"ok": True, "result": batch}
        if method in ("sendMessage", "sendRichMessage"):
            item = dict(params)
            if method == "sendRichMessage":
                item["text"] = json.loads(params["rich_message"])["markdown"]
            sent.append(item)
            return {"ok": True, "result": {"message_id": len(sent)}}
        if method == "sendChatAction":
            return {"ok": True, "result": True}
        raise AssertionError(method)

    client = TelegramClient("fake-token", opener=TransportOpener(transport))

    def factory(key):
        root = tmp_path / "lanes" / str(key.thread_id)
        root.mkdir(parents=True)
        transcript = root / "transcript.jsonl"
        marker = root / "session.json"
        marker.write_text(json.dumps({"transcript_path": str(transcript)}))
        lane = LaneBridge(client, key, root / "sent", root / "queue", agent="claude-code",
                          session_marker=marker)
        lane._transcript = transcript

        class AnswerSession(FakeSession):
            def inject(self, text):
                self.injected.append(text)
                injection_barrier.wait()
                with transcript.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "user", "message": {
                        "content": "Telegram: " + text}}) + "\n")
                    stream.write(json.dumps({"type": "assistant", "uuid": "answer-" + text,
                        "message": {"content": [{"type": "text", "text": "answer: " + text}]}}) + "\n")
                (root / "turn_end").touch()

        lane._session = AnswerSession()
        lane._turn_end = root / "turn_end"
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory,
                      lane_states_root=tmp_path / "lanes")
    mux_ref["mux"] = mux
    mux._inbound_loop()
    for lane in lanes.values():
        lane._inbound_queue.join()
    ticks = [threading.Thread(target=lane.outbound_tick) for lane in lanes.values()]
    for tick in ticks:
        tick.start()
    for tick in ticks:
        tick.join(3)
    for lane in lanes.values():
        lane.outbound_tick()

    assert lanes[LaneKey(123, 10)]._session.injected == ["topic A"]
    assert lanes[LaneKey(123, 11)]._session.injected == ["topic B"]
    assert {(item["text"], item["message_thread_id"]) for item in sent} == {
        ("answer: topic A", 10), ("answer: topic B", 11),
    }
    assert len(sent) == 2


def test_blocked_injection_in_one_lane_does_not_delay_other_topic(tmp_path):
    entered_a, release_a, injected_b = threading.Event(), threading.Event(), threading.Event()
    client = Client()
    lanes = {}

    def factory(key):
        root = tmp_path / "lanes" / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        lane = LaneBridge(client, key, root / "sent", root / "queue")

        class Session(FakeSession):
            def inject(self, text):
                self.injected.append(text)
                if key.thread_id == 10 and text == "A first":
                    entered_a.set()
                    assert release_a.wait(3)
                if key.thread_id == 11:
                    injected_b.set()

        lane._session = Session()
        lane._start_outbound = lambda: None
        lane._outbound_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory,
                      lane_states_root=tmp_path / "lanes")
    mux._handle_update_once(update(80, 10, text="A first"), 0)
    lane_a = lanes[LaneKey(123, 10)]
    assert entered_a.wait(2)
    def poll_remaining_updates():
        mux._handle_update_once(update(81, 10, text="A second"), 81)
        mux._handle_update_once(update(82, 11, text="B"), 82)

    poller = threading.Thread(target=poll_remaining_updates)
    poller.start()
    assert injected_b.wait(2), "topic B was not injected while topic A remained blocked"
    assert not release_a.is_set()
    release_a.set()
    poller.join(2)
    assert not poller.is_alive()
    lane_a._inbound_queue.join()
    lanes[LaneKey(123, 11)]._inbound_queue.join()
    assert lane_a._session.injected == ["A first", "A second"]
    assert lanes[LaneKey(123, 11)]._session.injected == ["B"]


def test_topic_b_message_is_injected_during_topic_a_activation(tmp_path):
    activation_entered, release_activation, injected_b = (threading.Event(),
                                                          threading.Event(),
                                                          threading.Event())
    client = Client()
    lanes = {}

    def factory(key):
        root = tmp_path / "lanes" / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        lane = LaneBridge(client, key, root / "sent", root / "queue")
        if key.thread_id == 10:
            def activate():
                activation_entered.set()
                assert release_activation.wait(3)
            lane._activate_session = activate
        class Session(FakeSession):
            def inject(self, text):
                self.injected.append(text)
                if key.thread_id == 11:
                    injected_b.set()
        lane._session = Session()
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory)
    lane_a_start = threading.Thread(target=mux._start_lane, args=(LaneKey(123, 10),))
    lane_a_start.start()
    assert activation_entered.wait(2)
    try:
        mux._handle_update_once(update(90, 11, text="B"), 0)
        assert injected_b.wait(2), "topic B was not injected during topic A activation"
    finally:
        release_activation.set()
    lane_a_start.join(2)
    assert not lane_a_start.is_alive()
    lanes[LaneKey(123, 11)]._inbound_queue.join()
    assert lanes[LaneKey(123, 11)]._session.injected == ["B"]
    mux.stop()


def test_text_only_edited_message_reaches_its_topic_lane(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    edited = {"update_id": 1, "edited_message": update(1, 10, text="corrected")["message"]}
    mux._handle_update_once(edited, 0)
    lane = lanes[LaneKey(123, 10)]
    lane._inbound_queue.join()
    assert lane._session.injected == ["corrected"]


def test_disallowed_sender_and_group_create_no_lane_warn_safely_and_advance(tmp_path, caplog):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    updates = [update(1, sender=77, text="private secret"),
               update(2, chat_type="group", text="group secret")]
    with caplog.at_level(logging.WARNING):
        offset = mux._handle_update_once(updates[0], 0)
        offset = mux._handle_update_once(updates[1], offset)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert lanes == {}
    assert client.sent == []
    assert len(warnings) == 2
    assert "private secret" not in caplog.text and "group secret" not in caplog.text
    assert offset == 3
    assert json.loads((tmp_path / "bot" / "processed_updates").read_text()) == {"update_ids": [1, 2]}


def test_restart_replays_pending_record_with_committed_offset(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    upd = update(41, 10)
    lane = mux._lane(LaneKey(123, 10))
    lane._ensure_inbox().reserve(upd)
    mux._save_offset(42)
    mux.stop()
    client = Client([[]])
    restarted, restarted_lanes = make_mux(tmp_path, client)
    client.on_last_poll = restarted.stop
    restarted._inbound_loop()
    assert client.polls[0][0] == 42
    lane = restarted_lanes[LaneKey(123, 10)]
    lane._inbound_queue.join()
    assert lane._session.injected == ["hello"]


def test_forum_service_is_consumed_and_photo_routes_to_lane(tmp_path, caplog):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    service = {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"},
              "message_thread_id": 10, "from": {"id": 8}, "forum_topic_created": {"name": "A"}}}
    photo = {"update_id": 2, "message": {"chat": {"id": 123, "type": "private"},
             "message_thread_id": 11, "from": {"id": 8}, "photo": [{"file_id": "x"}]}}
    with caplog.at_level(logging.INFO):
        assert mux._handle_update_once(service, 0) == 2
        assert mux._handle_update_once(photo, 2) == 3
    assert LaneKey(123, 11) in lanes
    assert LaneKey(123, 10) not in lanes
    assert mux._update_was_processed(1) and mux._update_was_processed(2)
    assert not [record for record in caplog.records if record.levelno > logging.INFO]


def test_failed_reserve_stops_batch_before_later_lane(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    first, second = update(1, 10), update(2, 11)
    first_lane = mux._lane(LaneKey(123, 10))
    first_lane._ensure_inbox().reserve = lambda _upd: (_ for _ in ()).throw(OSError("disk"))
    client.batches = [[first, second], []]
    client.on_last_poll = mux.stop
    mux._inbound_loop()
    assert client.polls[1][0] == 0
    assert LaneKey(123, 11) not in mux._lanes


def test_reserved_update_is_delivered_and_offset_survives_restart(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    upd = update(3, 10, text="survives reservation")
    assert mux._handle_update_once(upd, 0) == 4
    lane = lanes[LaneKey(123, 10)]
    lane._inbound_queue.join()
    assert lane._session.injected == ["survives reservation"]
    assert json.loads((tmp_path / "bot" / "offset").read_text()) == {"offset": 4}
    assert mux._update_was_processed(3)


def test_each_lane_starts_once_across_replay_and_routed_update(tmp_path):
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    key = LaneKey(123, 10)
    inbox_root = tmp_path / "bot" / "lanes" / "123" / "10"
    inbox_root.mkdir(parents=True)
    from agent2telegramtopic.durable import DurableInbox
    DurableInbox(inbox_root / "queue").reserve(update(4, 10, text="pending"))
    starts = []
    def factory(k):
        lane_root = tmp_path / "bot" / "lanes" / str(k.chat_id) / str(k.thread_id)
        lane = LaneBridge(client, k, lane_root / "sent", lane_root / "queue")
        lane._session = FakeSession()
        lane._outbound_loop = lambda: starts.append("outbound")
        original_start = lane._start_inbound_worker
        def start():
            starts.append("start")
            original_start()
        lane._start_inbound_worker = start
        original_replay = lane._replay_pending_inbound
        def replay():
            starts.append("replay")
            return original_replay()
        lane._replay_pending_inbound = replay
        lanes[k] = lane
        return lane
    mux.lane_factory = factory
    mux._replay_startup_lanes()
    mux._handle_update_once(update(5, 10), 0)
    lane = lanes[key]
    lane._inbound_queue.join()
    assert lane._inbound_worker_started
    assert starts.count("replay") == 2
    assert starts.count("outbound") == 1
    assert lane._session.injected.count("pending") == 1
    assert lane._session.injected.count("hello") == 1
    assert len(mux._lanes) == 1


def test_default_startup_scan_uses_configured_bot_lane_state_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    bot_dir = home / ".agent2telegramtopic/state/bot"
    state_dir = bot_dir
    client = Client()
    lanes = {}
    def factory(key):
        root = bot_dir / "lanes" / str(key.chat_id) / str(key.thread_id)
        lane = LaneBridge(client, key, root / "sent", root / "queue")
        lane._session = FakeSession()
        lane._outbound_loop = lambda: None
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, state_dir, factory)
    inbox_dir = bot_dir / "lanes" / "123" / "10"
    inbox_dir.mkdir(parents=True)
    from agent2telegramtopic.durable import DurableInbox
    DurableInbox(inbox_dir / "queue").reserve(update(6, 10))
    (bot_dir / "lanes" / "not-a-thread").mkdir()
    mux._replay_startup_lanes()
    key_for = LaneKey(123, 10)
    assert key_for in lanes
    lanes[key_for]._inbound_queue.join()
    assert lanes[key_for]._session.injected == ["hello"]


def test_startup_delivers_pending_outbox_without_starting_agent(tmp_path, monkeypatch):
    client = Client()
    key = LaneKey(123, 10)
    activations = []
    lanes = {}

    def factory(lane_key):
        root = tmp_path / "bot" / "lanes" / str(lane_key.chat_id) / str(lane_key.thread_id)
        lane = LaneBridge(client, lane_key, root / "sent", root / "queue")
        lane._activate_session = lambda: activations.append(lane_key)
        lane._outbound_loop = lane.outbound_tick
        lanes[lane_key] = lane
        return lane

    first = Multiplexer(client, {8}, tmp_path / "bot", factory,
                        lane_states_root=tmp_path / "bot" / "lanes")
    originating_lane = first._lane(key)
    originating_lane._send_final("reply from the stopped agent", key="reply-before-restart")
    first.stop()

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("agent2telegramtopic.multiplexer.threading.Thread", ImmediateThread)
    restarted = Multiplexer(client, {8}, tmp_path / "bot", factory,
                            lane_states_root=tmp_path / "bot" / "lanes")
    restarted._replay_startup_lanes()
    # Reconciliation may be retried in-process; the durable reply must remain
    # topic-bound and must not be delivered twice.
    restarted._replay_startup_lanes()

    assert client.sent == [(key, "reply from the stopped agent")]
    assert activations == []
    assert key not in restarted._started_lanes
    assert lanes[key]._outbox.pending() == []

    # A message can arrive after the startup scan while this lane remains parked.
    # Outbound polling must leave that inbox record durable until activation.
    inbox = lanes[key]._ensure_inbox()
    inbox.reserve(update(91, 10, text="arrived while parked"))
    lanes[key].outbound_tick()
    assert lanes[key]._session is None
    assert [record.update["update_id"] for record in inbox.pending()] == [91]
    assert client.sent == [(key, "reply from the stopped agent")]


def test_startup_reconciliation_delivers_pending_inbox_to_live_lane(tmp_path):
    from agent2telegramtopic.durable import DurableInbox
    from agent2telegramtopic.state import Lane, LaneStore

    key = LaneKey(123, 10)
    root = tmp_path / "lanes"
    store = LaneStore(tmp_path / "live-retry.db")
    store.upsert(Lane(123, 10, "live", "codex", "session", "live", False, 1, None, 0))
    DurableInbox(root / "123" / "10" / "queue").reserve(update(42, 10, text="retry me"))
    client = Client([[]])
    lanes = {}
    injected = threading.Event()

    def factory(lane_key):
        lane_root = root / str(lane_key.chat_id) / str(lane_key.thread_id)
        lane = LaneBridge(client, lane_key, lane_root / "sent", lane_root / "queue")
        lane._session = FakeSession()
        original_inject = lane._session.inject

        def inject(text):
            original_inject(text)
            injected.set()

        lane._session.inject = inject
        lane._session.alive = True
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[lane_key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      lane_states_root=root)
    mux._replay_startup_lanes()
    lane = lanes[key]
    assert injected.wait(2), "startup did not inject the durable Telegram message"
    assert lane._session.injected == ["retry me"]
    assert lane._ensure_inbox().pending() == []
    mux.stop()


def test_restart_replays_same_thread_separately_for_two_private_chats(tmp_path):
    client = Client([[]])
    lanes = {}
    root = tmp_path / "bot" / "lanes"
    from agent2telegramtopic.durable import DurableInbox

    for chat_id, message in ((123, "first chat"), (456, "second chat")):
        upd = update(chat_id, 10, text=message)
        upd["message"]["chat"]["id"] = chat_id
        DurableInbox(root / str(chat_id) / "10" / "queue").reserve(upd)

    def factory(key):
        lane_root = root / str(key.chat_id) / str(key.thread_id)
        from agent2telegramtopic.config import lane_path
        lane_cwd = lane_path(tmp_path / "bot" / "topics", key.chat_id, key.thread_id)
        lane = LaneBridge(client, key, lane_root / "sent", lane_root / "queue", lane_cwd=lane_cwd)
        lane._session = FakeSession()
        lane._outbound_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_states_root=root)
    mux._replay_startup_lanes()
    for lane in lanes.values():
        lane._inbound_queue.join()
    assert lanes[LaneKey(123, 10)]._session.injected == ["first chat"]
    assert lanes[LaneKey(456, 10)]._session.injected == ["second chat"]
    first, second = lanes[LaneKey(123, 10)], lanes[LaneKey(456, 10)]
    assert first._queue_root != second._queue_root
    assert first._sent_path != second._sent_path
    assert first.lane_cwd != second.lane_cwd
    assert first._ensure_inbox().directory != second._ensure_inbox().directory
    assert first._turn_end.parent != second._turn_end.parent
    first._send_final("reply to first", "first-reply")
    second._send_final("reply to second", "second-reply")
    first._flush_pending()
    second._flush_pending()
    assert client.sent == [(LaneKey(123, 10), "reply to first"),
                           (LaneKey(456, 10), "reply to second")]
    assert first._ensure_outbox().head() is None
    assert second._ensure_outbox().head() is None
    assert "first-reply" in first._sent_keys and "second-reply" in second._sent_keys
    assert first._sent_path.read_text().splitlines() == ["first-reply"]
    assert second._sent_path.read_text().splitlines() == ["second-reply"]
    assert first._queue_root != second._queue_root
    assert first._ensure_inbox().directory.exists() and second._ensure_inbox().directory.exists()


def test_startup_does_not_start_parked_lane_without_pending_inbox(tmp_path):
    client = Client([[]])
    lanes = {}
    root = tmp_path / "bot" / "lanes"
    parked = root / "123" / "10"
    parked.mkdir(parents=True)
    starts = []

    def factory(key):
        lane_root = root / str(key.chat_id) / str(key.thread_id)
        lane = LaneBridge(client, key, lane_root / "sent", lane_root / "queue")
        lane._session = FakeSession()
        lane._outbound_loop = lambda: starts.append(key)
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_states_root=root)
    mux._replay_startup_lanes()
    assert lanes == {}
    assert starts == []


def test_new_topic_with_free_slot_is_queued_silently(tmp_path):
    from agent2telegramtopic.state import LaneStore
    sent = []
    def transport(method, params):
        if method == "getUpdates":
            return {"ok": True, "result": []}
        if method in ("sendMessage", "sendRichMessage"):
            item = dict(params)
            if method == "sendRichMessage":
                item["text"] = json.loads(params["rich_message"])["markdown"]
            sent.append(item)
            return {"ok": True, "result": {"message_id": len(sent)}}
        if method == "sendChatAction":
            return {"ok": True, "result": True}
        raise AssertionError(method)
    client = TelegramClient("fake-token", opener=TransportOpener(transport))
    store = LaneStore(tmp_path / "free-slot.db")
    lanes = {}

    class Session(FakeSession):
        def __init__(self, transcript):
            super().__init__()
            self.transcript = transcript
        @property
        def alive(self):
            return True
        def inject(self, text):
            self.injected.append(text)
            with self.transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "user", "message": {"content": "Telegram: " + text}}) + "\n")
                stream.write(json.dumps({"type": "assistant", "uuid": "reply",
                    "message": {"content": [{"type": "text", "text": "answer: " + text}]}}) + "\n")

    def factory(key):
        root = tmp_path / "lanes" / str(key.chat_id) / str(key.thread_id)
        root.mkdir(parents=True)
        transcript = root / "transcript.jsonl"
        transcript.touch()
        marker = root / "session.json"
        marker.write_text(json.dumps({"transcript_path": str(transcript)}))
        lane = LaneBridge(client, key, root / "sent", root / "queue", agent="claude-code",
                          session_marker=marker)
        lane._session = Session(transcript)
        lane._transcript = transcript
        lane._tpos = 0
        lane._activate_session = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      max_live=4, lane_states_root=tmp_path / "lanes")
    mux._start_slot_manager()
    mux._handle_update_once(update(19, 14, text="free slot"), 0)
    key = LaneKey(123, 14)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(item.get("text", "").startswith("answer: free slot") for item in sent):
        time.sleep(.01)
    assert not any(item.get("text") == "This topic is queued and will resume when a session is available." for item in sent)
    assert any(item.get("text", "").startswith("answer: free slot") and item.get("message_thread_id") == 14 for item in sent)
    assert store.get(key).status == "live"
    mux.stop()


def test_slot_manager_never_starts_a_fifth_live_lane_and_delivers_it_later(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "slots.db")
    http_sent = []
    def transport(method, params):
        if method == "getUpdates":
            return {"ok": True, "result": []}
        if method in ("sendMessage", "sendRichMessage"):
            item = dict(params)
            if method == "sendRichMessage":
                item["text"] = json.loads(params["rich_message"])["markdown"]
            http_sent.append(item)
            return {"ok": True, "result": {"message_id": len(http_sent)} }
        if method == "sendChatAction":
            return {"ok": True, "result": True}
        raise AssertionError(method)
    client = TelegramClient("fake-token", opener=TransportOpener(transport))
    lanes = {}
    live_tmux = set()
    max_seen = [0]
    transcript_written = threading.Event()

    class Session(FakeSession):
        def __init__(self, key, transcript_path):
            super().__init__()
            self.key = key
            self.transcript_path = transcript_path
        @property
        def alive(self):
            return self.key in live_tmux
        def inject(self, text):
            self.injected.append(text)
            self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript_path.open("a", encoding="utf-8") as transcript:
                transcript.write(json.dumps({"type": "user", "message": {"content": "Telegram: " + text}}) + "\n")
                transcript.write(json.dumps({"type": "assistant", "uuid": "reply-" + str(self.key.thread_id),
                    "message": {"content": [{"type": "text", "text": "answer: " + text}]}}) + "\n")
            transcript_written.set()
        def close(self):
            live_tmux.discard(self.key)

    def factory(key):
        root = tmp_path / "lanes" / str(key.chat_id) / str(key.thread_id)
        transcript_path = root / "transcript.jsonl"
        marker = root / "session.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"transcript_path": str(transcript_path)}))
        lane = LaneBridge(client, key, root / "sent", root / "queue", agent="claude-code",
                          session_marker=marker)
        lane._session = Session(key, transcript_path)
        lane._transcript = transcript_path
        lane._tpos = 0
        lane._activate_session = lambda: (live_tmux.add(key), max_seen.__setitem__(0, max(max_seen[0], len(live_tmux))))
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store, max_live=4,
                      lane_states_root=tmp_path / "lanes")
    mux._start_slot_manager()
    for n in range(10, 14):
        key = LaneKey(123, n)
        lane = mux._lane(key)
        mux._start_lane(key)
        store.set_status(key, "live")
        lane._turn_active.set()  # Busy lanes cannot be selected for eviction.
    assert len(live_tmux) == 4
    mux._handle_update_once(update(20, 14, text="fifth topic"), 0)
    assert len(live_tmux) == 4
    assert store.get(LaneKey(123, 14)).status == "waiting"
    queued_notice = "This topic is queued and will resume when a session is available."
    fifth_key = LaneKey(123, 14)
    assert sum(item["text"] == queued_notice and item.get("message_thread_id") == 14 for item in http_sent) == 1
    lanes[LaneKey(123, 10)]._turn_active.clear()
    store.set_busy(LaneKey(123, 10), False)
    mux._slot_wakeup.set()
    deadline = time.monotonic() + 2
    fifth_key = LaneKey(123, 14)
    while time.monotonic() < deadline and fifth_key not in live_tmux:
        time.sleep(.01)
    assert len(live_tmux) == 4
    assert max_seen[0] <= 4
    assert fifth_key in live_tmux
    assert store.get(fifth_key).status == "live"
    fifth = lanes[fifth_key]
    # Activation precedes replay; wait for the real inbound worker to finish it.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not fifth._session.injected:
        time.sleep(.01)
    assert fifth._session.injected[0].startswith("fifth topic\n\n(New topic:")
    assert transcript_written.wait(2)
    # Model the agent's turn-end hook arriving in a later step, after inject has
    # closed the transcript file and its complete answer is visible to the reader.
    (fifth._transcript.parent / "turn_end").touch()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(item["text"].startswith("answer: fifth topic") and item.get("message_thread_id") == 14 for item in http_sent):
        time.sleep(.01)
    # Keep the real outbound loop running for several ticks after the first
    # delivery so a duplicate on a later tick cannot pass unnoticed.
    time.sleep(.5)
    assert sum(item["text"] == queued_notice and item.get("message_thread_id") == 14 for item in http_sent) == 1
    assert sum(item["text"].startswith("answer: fifth topic") and item.get("message_thread_id") == 14 for item in http_sent) == 1
    assert max_seen[0] <= 4
    mux.stop()


def test_startup_reconciles_stale_busy_rows_and_queues_pending_lane(tmp_path):
    from agent2telegramtopic.durable import DurableInbox
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "reconcile.db")
    stale, pending, alive_pending = LaneKey(123, 10), LaneKey(123, 11), LaneKey(123, 12)
    store.upsert(Lane(123, 10, "stale", "codex", "s", "live", True, 1, None, 0))
    store.upsert(Lane(123, 11, "pending", "codex", "p", "live", True, 2, None, 0))
    store.upsert(Lane(123, 12, "alive", "codex", "a", "parked", True, 3, None, 0))
    root = tmp_path / "lanes"
    DurableInbox(root / "123" / "11" / "queue").reserve(update(31, 11, text="restart"))
    DurableInbox(root / "123" / "12" / "queue").reserve(update(32, 12, text="already alive"))
    client, lanes = Client([[]]), {}
    def factory(key):
        lane_root = root / str(key.chat_id) / str(key.thread_id)
        lane = LaneBridge(client, key, lane_root / "sent", lane_root / "queue")
        lane._session = FakeSession()
        lane._session.alive = key.thread_id == 12
        lane._start_inbound_worker = lambda: None
        lane._replay_pending_inbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store, max_live=4,
                      lane_states_root=root)
    mux._replay_startup_lanes()
    assert store.get(stale).status == "parked" and not store.get(stale).busy
    assert store.get(pending).status == "waiting" and not store.get(pending).busy
    assert store.get(alive_pending).status == "live" and not store.get(alive_pending).busy
    mux.stop()


def test_startup_reconciliation_continues_when_one_live_lane_fails_to_start(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "start-failure.db")
    first, second = LaneKey(123, 10), LaneKey(123, 11)
    for key in (first, second):
        store.upsert(Lane(key.chat_id, key.thread_id, "topic", "codex", "s", "live", False,
                           1, None, 0))
    started = []
    client = Client()
    def factory(key):
        lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "sent",
                          tmp_path / str(key.thread_id) / "queue")
        lane._session_alive = lambda: True
        lane._activate_session = lambda: (_ for _ in ()).throw(RuntimeError("worker failed")) if key == first else None
        lane._start_inbound_worker = lambda: started.append(key)
        lane._replay_pending_inbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      lane_states_root=tmp_path / "lanes")
    mux._replay_startup_lanes()
    assert started == [second]
    mux.stop()


def test_failed_lane_start_closes_activated_session_before_releasing_slot(tmp_path):
    from agent2telegramtopic.state import LaneStore
    store = LaneStore(tmp_path / "failed-start.db")
    key = LaneKey(123, 9)
    client = Client()
    class Session:
        alive = False
        def close(self):
            self.alive = False
    lane = LaneBridge(client, key, tmp_path / "sent", tmp_path / "queue")
    lane._session = Session()
    def activate():
        lane._session.alive = True
    lane._activate_session = activate
    lane._start_inbound_worker = lambda: (_ for _ in ()).throw(RuntimeError("worker failed"))
    mux = Multiplexer(client, {8}, tmp_path / "bot", lambda _key: lane, lane_store=store)
    try:
        mux._start_lane(key)
    except RuntimeError:
        pass
    else:
        assert False, "worker failure should propagate"
    assert not lane._session.alive
    assert store.get(key).status == "parked"


def test_eviction_skips_lane_with_accepted_pending_inbox_work(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "eviction-replay.db")
    victim, waiting = LaneKey(123, 1), LaneKey(123, 2)
    store.upsert(Lane(123, 1, "victim", "codex", "s", "live", False, 1, None, 30000))
    store.upsert(Lane(123, 2, "waiting", "codex", "w", "waiting", False, 2, None, 0))
    client, lanes = Client(), {}
    class Session:
        alive = True
        def close(self):
            pass
    def factory(key):
        lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "sent",
                          tmp_path / str(key.thread_id) / "queue")
        lane._session = Session()
        lane._activate_session = lambda: None
        lane._replay_pending_inbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lane.compact_internal = lambda: None
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      max_live=1, compact_min_tokens=1)
    victim_lane = mux._lane(victim)
    pending_record = victim_lane._ensure_inbox().reserve(update(91, 1, text="accepted"))
    victim_lane._inbound_queue.put((update(91, 1, text="accepted"), pending_record))
    mux._slot_wakeup.set()
    original_wait = mux._slot_wakeup.wait
    waits = [0]
    def one_pass(_timeout):
        waits[0] += 1
        if waits[0] > 1:
            mux._stop.set()
        return True
    mux._slot_wakeup.wait = one_pass
    mux._slot_loop()
    mux._slot_wakeup.wait = original_wait
    assert victim_lane._closing is False
    assert victim_lane._inbound_queue.qsize() == 1
    assert [record.record_id for record in victim_lane._ensure_inbox().pending()] == [pending_record]
    assert store.get(victim).status == "live"
    assert store.get(waiting).status == "waiting"
    assert waiting not in lanes


def test_update_during_compaction_stays_live_and_is_queued_silently(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "compacting.db")
    key = LaneKey(123, 10)
    store.upsert(Lane(123, 10, "topic 10", "codex", "s", "live", True, 1, None, 0))
    client, submitted = Client(), []
    root = tmp_path / "lanes" / "123" / "10"
    def factory(lane_key):
        lane = LaneBridge(client, lane_key, root / "sent", root / "queue")
        lane._internal_turn = True
        lane._submit_inbound_update = lambda *args: submitted.append(args)
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      lane_states_root=tmp_path / "lanes")
    mux._started_lanes.add(key)
    mux._handle_update_once(update(71, 10, text="during compact"), 0)
    assert store.get(key).status == "live"
    assert len(mux._lanes[key]._ensure_inbox().pending()) == 1
    assert client.sent == []
    assert submitted == []
    mux.stop()


def test_lane_is_persisted_live_before_inbox_replay_and_keeps_busy_state(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "start-order.db")
    key = LaneKey(123, 10)
    store.upsert(Lane(123, 10, "topic", "codex", "s", "waiting", True, 1, None, 0))
    client = Client()
    observed = []
    def factory(lane_key):
        lane = LaneBridge(client, lane_key, tmp_path / "sent", tmp_path / "queue")
        lane._activate_session = lambda: None
        lane._start_inbound_worker = lambda: observed.append(("worker", store.get(key).status))
        lane._replay_pending_inbound = lambda: observed.append(("replay", store.get(key).status))
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store)
    mux._start_lane(key)
    row = store.get(key)
    assert observed == [("worker", "live"), ("replay", "live"), ("replay", "live")]
    assert row.status == "live"
    assert row.busy is True
    mux.stop()


def test_update_reserved_during_activation_replays_after_older_inbox_once(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "replay-race.db")
    key = LaneKey(123, 10)
    store.upsert(Lane(123, 10, "topic", "codex", "s", "waiting", False, 1, None, 0))
    client = Client()
    activation_entered, release_activation = threading.Event(), threading.Event()
    injected = []
    lane_ref = {}
    def factory(lane_key):
        lane = LaneBridge(client, lane_key, tmp_path / "sent", tmp_path / "queue")
        def activate():
            activation_entered.set()
            assert release_activation.wait(3)
        lane._activate_session = activate
        class Session(FakeSession):
            def inject(self, text):
                injected.append(text)
        lane._session = Session()
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lane_ref["lane"] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store)
    lane = mux._lane(key)
    older = {"update_id": 80, "message": {"text": "older"}}
    lane._ensure_inbox().reserve(older)
    starter = threading.Thread(target=mux._start_lane, args=(key,))
    starter.start()
    assert activation_entered.wait(2)
    mux._handle_update_once(update(81, 10, text="reserved while starting"), 0)
    assert store.get(key).status == "live"
    assert client.sent == []
    assert lane._ensure_inbox().pending()
    release_activation.set()
    starter.join(3)
    assert not starter.is_alive()
    lane._inbound_queue.join()
    assert store.get(key).status == "live"
    assert injected == ["older", "reserved while starting"]
    assert lane._ensure_inbox().pending() == []
    mux.stop()


def test_eviction_rechecks_turn_after_idle_candidate_snapshot(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "eviction-race.db")
    victim, waiting = LaneKey(123, 1), LaneKey(123, 2)
    store.upsert(Lane(123, 1, "victim", "codex", "s", "live", False, 1, None, 0))
    store.upsert(Lane(123, 2, "waiting", "codex", "w", "waiting", False, 2, None, 0))
    client, lanes = Client(), {}
    class Session:
        alive = True
        def close(self):
            self.alive = False
    def factory(key):
        lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "sent",
                          tmp_path / str(key.thread_id) / "queue")
        lane._session = Session()
        lane._activate_session = lambda: None
        lane._replay_pending_inbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store, max_live=1)
    victim_lane = mux._lane(victim)
    original_list_live = store.list_live
    calls = [0]
    def list_live_then_turn_starts():
        rows = original_list_live()
        calls[0] += 1
        if calls[0] == 2:
            victim_lane._turn_active.set()
            store.set_busy(victim, True)
        return rows
    store.list_live = list_live_then_turn_starts
    mux._slot_wakeup.set()
    waits = [0]
    def one_pass(_timeout):
        waits[0] += 1
        if waits[0] > 1:
            mux._stop.set()
        return True
    mux._slot_wakeup.wait = one_pass
    mux._slot_loop()
    assert victim_lane._session.alive
    assert store.get(victim).status == "live"
    assert waiting not in lanes
    mux.stop()


def test_eviction_skips_lane_with_dequeued_message_mid_inject(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "eviction-inject.db")
    victim, waiting = LaneKey(123, 1), LaneKey(123, 2)
    store.upsert(Lane(123, 1, "victim", "codex", "s", "live", False, 1, None, 0))
    store.upsert(Lane(123, 2, "waiting", "codex", "w", "waiting", False, 2, None, 0))
    client, lanes = Client(), {}
    entered, release = threading.Event(), threading.Event()
    class Session(FakeSession):
        alive = True
        def inject(self, text):
            self.injected.append(text)
            entered.set()
            assert release.wait(3)
        def close(self):
            self.alive = False
    def factory(key):
        lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "sent",
                          tmp_path / str(key.thread_id) / "queue")
        lane._session = Session()
        lane._activate_session = lambda: None
        lane._replay_pending_inbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store,
                      max_live=1, compact_min_tokens=1)
    mux._start_lane(victim)
    lane = lanes[victim]
    inbox = lane._ensure_inbox()
    record_id = inbox.reserve(update(32, 1, text="in flight"))
    lane._submit_inbound_update(update(32, 1, text="in flight"), record_id)
    assert entered.wait(2)
    compactions = []
    lane.compact_internal = lambda: compactions.append(True)
    mux._slot_wakeup.set()
    waits = [0]
    def one_pass(_timeout):
        waits[0] += 1
        if waits[0] > 1:
            mux._stop.set()
        return True
    mux._slot_wakeup.wait = one_pass
    try:
        mux._slot_loop()
        assert lane._closing is False
        assert lane._session.alive
        assert compactions == []
    finally:
        release.set()
    lane._inbound_queue.join()
    assert lane._session.injected == ["in flight"]
    assert inbox.pending() == []
    mux.stop()


def test_reservation_after_victim_snapshot_replays_after_restart(tmp_path):
    from agent2telegramtopic.state import Lane, LaneStore
    store = LaneStore(tmp_path / "eviction-reserve-race.db")
    victim, waiting = LaneKey(123, 1), LaneKey(123, 2)
    store.upsert(Lane(123, 1, "victim", "codex", "s", "live", False, 1, None, 0))
    store.upsert(Lane(123, 2, "waiting", "codex", "w", "waiting", False, 2, None, 0))
    client, lanes, injected = Client(), {}, []
    class Session(FakeSession):
        alive = True
        def inject(self, text):
            self.injected.append(text)
            injected.append((self.key.thread_id, text))
        def close(self):
            self.alive = False
    def factory(key):
        lane = LaneBridge(client, key, tmp_path / str(key.thread_id) / "sent",
                          tmp_path / str(key.thread_id) / "queue")
        lane._session = Session()
        lane._session.key = key
        lane._activate_session = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        # Mirrors the production timeout callback's inbox read and status write.
        # The eviction path must not run this stale second status decision.
        def parked_callback(_key):
            pending_now = lane._ensure_inbox().pending()
            store.set_status(key, "waiting" if pending_now else "parked")
        lane._on_parked = parked_callback
        lanes[key] = lane
        return lane
    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store, max_live=1)
    lane = mux._lane(victim)
    inbox = lane._ensure_inbox()
    original_pending = inbox.pending
    record = update(92, 1, text="arrived at eviction")
    calls = [0]
    def reserve_after_snapshot():
        pending = original_pending()
        calls[0] += 1
        if calls[0] == 1:
            inbox.reserve(record)
        return pending
    inbox.pending = reserve_after_snapshot
    mux._slot_wakeup.set()
    waits = [0]
    def one_pass(_timeout):
        waits[0] += 1
        if waits[0] > 2:
            mux._stop.set()
        return True
    mux._slot_wakeup.wait = one_pass
    mux._slot_loop()
    assert waits[0] == 3
    assert victim in mux._started_lanes
    assert store.get(victim).status == "live"
    lanes[victim]._inbound_queue.join()
    assert injected == [(1, "arrived at eviction")]
    assert original_pending() == []
    mux.stop()


def test_injected_lane_root_does_not_depend_on_state_dir_ancestors(tmp_path):
    client = Client()
    root = tmp_path / "lanes"
    mux = Multiplexer(client, {8}, "state/bot", lambda _key: None,
                      lane_states_root=root)
    assert mux.lane_states_root == root


def test_allowed_updates_covers_messages_edits_and_reactions(tmp_path):
    client = Client([[]])
    mux, _ = make_mux(tmp_path, client)
    client.on_last_poll = mux.stop
    mux._inbound_loop()
    assert json.loads(client.allowed_updates[0]) == ["message", "edited_message", "message_reaction"]


def test_polling_uses_telegram_api_result_and_json_array_allowed_updates(tmp_path):
    upd = update(9, 10, text="from Telegram")
    calls = []
    class FakeTransport:
        def __call__(self, method, params):
            calls.append((method, params))
            return {"ok": True, "result": [upd]}

    client = TelegramClient("token", opener=TransportOpener(FakeTransport()))
    mux, lanes = make_mux(tmp_path, client)
    polls = [0]
    def stop_after_poll():
        polls[0] += 1
        if polls[0] == 1:
            mux.stop()
    client.send_chat_action = lambda *_args: None
    client.send_message = lambda *_args, **_kwargs: None
    mux._replay_startup_lanes = lambda: None
    mux._load_offset = lambda: 0
    mux._stop.wait = lambda _seconds: False
    # Stop from the opener after returning its single response.
    opener = client._opener
    class StoppingOpener:
        def open(self, req, timeout=None):
            response = opener.open(req, timeout=timeout)
            stop_after_poll()
            return response
    client._opener = StoppingOpener()
    mux._inbound_loop()
    key = LaneKey(123, 10)
    lanes[key]._inbound_queue.join()
    assert lanes[key]._session.injected == ["from Telegram"]
    assert json.loads(calls[0][1]["allowed_updates"]) == ["message", "edited_message", "message_reaction"]


def test_exhausted_poll_network_retries_use_outage_backoff(tmp_path, caplog, monkeypatch):
    from urllib.error import URLError
    monkeypatch.setattr("agent2telegramtopic.telegram.time.sleep", lambda _delay: None)

    attempts = []
    class BrokenTransport:
        def __call__(self, method, params):
            attempts.append(method)
            raise URLError("network down")

    client = TelegramClient("token", opener=TransportOpener(BrokenTransport()), max_retries=1)
    mux, _ = make_mux(tmp_path, client)
    waits = []
    def stop_after_backoff(seconds):
        waits.append(seconds)
        mux.stop()
        return True
    mux._stop.wait = stop_after_backoff
    mux._replay_startup_lanes = lambda: None
    mux._load_offset = lambda: 0
    with caplog.at_level(logging.WARNING):
        mux._inbound_loop()
    assert attempts == ["getUpdates", "getUpdates"]
    assert waits == [3]
    assert "transient network error (1)" in caplog.text
    assert "getUpdates failed" not in caplog.text


def test_one_update_batch_answers_two_topics_concurrently_and_once(tmp_path):
    client = Client([[update(51, 10, text="alpha"), update(52, 11, text="beta")]])
    lanes = {}

    class AnsweringSession(FakeSession):
        def __init__(self, key, transcript):
            super().__init__()
            self.key = key
            self.transcript = transcript

        def inject(self, text):
            self.injected.append(text)
            self.transcript.parent.mkdir(parents=True, exist_ok=True)
            with self.transcript.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {"content": "Telegram: " + text}}) + "\n")
                f.write(json.dumps({"type": "assistant", "uuid": "reply-" + str(self.key.thread_id),
                    "message": {"content": [{"type": "text", "text": "answer: " + text}]}}) + "\n")
            (self.transcript.parent / "turn_end").touch()

    def factory(key):
        root = tmp_path / "bot" / "lanes" / str(key.chat_id) / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        transcript = root / "transcript.jsonl"
        marker = root / "session.json"
        marker.write_text(json.dumps({"transcript_path": str(transcript)}))
        lane = LaneBridge(client, key, root / "sent", root / "queue", agent="claude-code",
                          session_marker=marker)
        lane._session = AnsweringSession(key, transcript)
        lane._transcript = transcript
        lane._start_outbound = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory,
                      lane_states_root=tmp_path / "bot" / "lanes")
    client.on_last_poll = mux.stop
    mux._inbound_loop()
    for lane in lanes.values():
        lane._inbound_queue.join()
        lane.outbound_tick()
        lane.outbound_tick()

    assert lanes[LaneKey(123, 10)]._session.injected == ["alpha"]
    assert lanes[LaneKey(123, 11)]._session.injected == ["beta"]
    assert client.sent.count((LaneKey(123, 10), "answer: alpha")) == 1
    assert client.sent.count((LaneKey(123, 11), "answer: beta")) == 1
    assert client.sent == [(LaneKey(123, 10), "answer: alpha"),
                           (LaneKey(123, 11), "answer: beta")]


def test_media_updates_create_lanes_in_their_topic_and_sticker_is_consumed(tmp_path):
    cases = [({"voice": {"file_id": "v"}}, 21), ({"audio": {"file_id": "a"}}, 26),
             ({"photo": [{"file_id": "p"}]}, 22),
             ({"document": {"file_id": "d"}}, 23),
             ({"photo": [{"file_id": "p"}], "caption": "caption"}, 24)]
    client = Client()
    mux, lanes = make_mux(tmp_path, client)
    for index, (payload, thread) in enumerate(cases, 1):
        message = {"chat": {"id": 123, "type": "private"}, "from": {"id": 8},
                   "message_thread_id": thread, **payload}
        mux._handle_update_once({"update_id": index, "message": message}, index - 1)
        assert LaneKey(123, thread) in lanes
    before = set(lanes)
    mux._handle_update_once({"update_id": 6, "message": {
        "chat": {"id": 123, "type": "private"}, "from": {"id": 8},
        "message_thread_id": 25, "sticker": {"file_id": "s"}}}, 5)
    assert set(lanes) == before
    mux.stop()
