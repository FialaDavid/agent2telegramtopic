import json

from agent2telegramtopic.config import Config
from agent2telegramtopic.durable import DurableOutbox
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail_keyboard = False
        self.edits = []
        self.answers = []

    def edit_reply_markup(self, key, message_id, rows):
        self.edits.append((message_id, rows))

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append((callback_query_id, text))

    def send_message(self, key, text):
        self.sent.append((text, None))
        return [{"message_id": len(self.sent)}]

    def send_message_with_keyboard(self, key, text, rows):
        self.sent.append((text, rows))
        if self.fail_keyboard:
            self.fail_keyboard = False
            raise OSError("temporary")
        return len(self.sent)


def lane_for(tmp_path, client=None):
    client = client or FakeTelegram()
    state = tmp_path / "state"
    lane = LaneBridge(client, LaneKey(42, 17), state / "sent", state / "queue",
                      config=Config("bot", "codex", frozenset({1})))
    return lane, client


def test_button_markers_parse_strip_and_last_marker_wins(tmp_path):
    lane, _ = lane_for(tmp_path)
    text, buttons = lane._extract_buttons("Before\n [TG-BUTTONS] a | | b \nMiddle\n[tg-buttons] c | d\nAfter")
    assert text == "Before\nMiddle\nAfter"
    assert buttons == ["c", "d"]
    assert lane._extract_buttons("[tg-buttons] | \nvisible") == ("visible", [])


def test_keyboard_only_on_last_chunk_and_empty_marker_sends_prompt(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._send_final("x" * 5000 + "\n[tg-buttons] yes | no")
    lane._flush_pending()
    assert client.sent[-1][1] is not None
    assert all(rows is None for _, rows in client.sent[:-1])
    assert lane._answer_buttons[1] == ("yes", "no")
    other, client2 = lane_for(tmp_path / "empty")
    other._send_final("[tg-buttons] A | B")
    other._flush_pending()
    assert client2.sent == [("Vyberte:", client2.sent[0][1])]


def test_failed_first_send_retries_with_buttons(tmp_path):
    lane, client = lane_for(tmp_path)
    client.fail_keyboard = True
    lane._send_final("answer\n[tg-buttons] yes | no")
    lane._flush_pending()
    lane._flush_pending()
    assert client.sent[0][1] is not None
    assert client.sent[1][1] is not None
    assert len(lane._answer_buttons[2]) == 2


def test_old_outbox_without_buttons_loads_and_sends(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._send_final("legacy")
    record = lane._outbox.head()
    path = lane._outbox._pending_path(record.id)
    data = json.loads(path.read_text())
    data.pop("buttons")
    path.write_text(json.dumps(data))
    assert lane._outbox.head().buttons == ()
    lane._flush_pending()
    assert client.sent == [("legacy", None)]


def test_buttons_skip_voice_and_split_rows(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._set_voice_state(True)
    lane._try_send_voice = lambda text: (_ for _ in ()).throw(AssertionError("voice attempted"))
    lane._send_final("pick\n[tg-buttons] " + " | ".join(f"option{i}" for i in range(12)))
    lane._flush_pending()
    rows = client.sent[-1][1]
    assert [len(row) for row in rows] == [4, 4, 3, 1]
    assert all(sum(len(item["text"]) for item in row) <= 28 for row in rows)
    assert all(len(item["text"]) <= 48 for row in rows for item in row)


def test_valid_tap_routes_label_and_tapper_to_inbound(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._answer_buttons = (99, ("good", "bad"), ("ab:1", "ab:2"))
    events = []
    lane.store_control_text = lambda text, sender: ({"message": {"text": text, "from": {"id": sender}}}, "stored")
    lane._submit_inbound_update = lambda update, record_id: events.append(
        ("submit", update["message"]["text"], update["message"]["from"]["id"]))
    client.edit_reply_markup = lambda key, message_id, rows: events.append(("confirm", message_id, rows))
    mux = Multiplexer.__new__(Multiplexer)
    mux._lane_lock = __import__("threading").RLock()
    mux._lanes = {lane.key: lane}
    mux._started_lanes = {lane.key}
    mux.allowed = frozenset({1, 7})
    mux.tg = client
    mux.lane_store = None
    mux._slot_wakeup = __import__("threading").Event()
    mux.log = __import__("logging").getLogger(__name__)
    mux._handle_callback_query({"callback_query": {"id": "cb", "data": "ab:1",
        "from": {"id": 7}, "message": {"message_id": 99, "chat": {"id": 42, "type": "private"},
        "message_thread_id": 17}}})
    assert events == [
        ("submit", "good", 7),
        ("confirm", 99, [[{"text": "✓ good", "callback_data": "ab:done"}]]),
    ]
    assert lane._answer_buttons is None


def test_store_failure_keeps_keyboard_and_does_not_submit(tmp_path):
    for failure in (None, OSError("disk")):
        lane, client = lane_for(tmp_path / str(failure))
        lane._answer_buttons = (99, ("yes",), ("ab:1",))
        lane.store_control_text = lambda *args, failure=failure: (_ for _ in ()).throw(failure) if isinstance(failure, Exception) else failure
        submitted = []
        lane._submit_inbound_update = lambda *args: submitted.append(args)
        mux = Multiplexer.__new__(Multiplexer)
        mux._lane_lock = __import__("threading").RLock()
        mux._lanes = {lane.key: lane}
        mux._started_lanes = {lane.key}
        mux.allowed = frozenset({7})
        mux.tg = client
        mux.lane_store = None
        mux._slot_wakeup = __import__("threading").Event()
        mux.log = __import__("logging").getLogger(__name__)
        mux._handle_callback_query({"callback_query": {"id": "cb", "data": "ab:1",
            "from": {"id": 7}, "message": {"message_id": 99, "chat": {"id": 42, "type": "private"},
            "message_thread_id": 17}}})
        assert lane._answer_buttons is not None
        assert client.edits == []
        assert submitted == []
        assert client.answers[-1][1] == "Odpověď se nepodařilo předat, napište ji prosím."


def test_done_tap_is_silently_acknowledged(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._answer_buttons = (99, ("yes",), ("ab:1",))
    mux = Multiplexer.__new__(Multiplexer)
    mux._lane_lock = __import__("threading").RLock()
    mux._lanes = {lane.key: lane}
    mux._started_lanes = {lane.key}
    mux.allowed = frozenset({7})
    mux.tg = client
    mux.lane_store = None
    mux._slot_wakeup = __import__("threading").Event()
    mux.log = __import__("logging").getLogger(__name__)
    mux._handle_callback_query({"callback_query": {"id": "cb", "data": "ab:done",
        "from": {"id": 7}, "message": {"message_id": 99, "chat": {"id": 42}}}})
    assert client.answers == [("cb", None)]
    assert client.edits == []
    assert lane._answer_buttons is not None


def test_invalidation_does_not_clear_state_replaced_during_edit(tmp_path):
    lane, client = lane_for(tmp_path)
    old = (88, ("old",), ("ab:old",))
    new = (89, ("new",), ("ab:new",))
    lane._answer_buttons = old
    original = client.edit_reply_markup
    def replace_state(key, message_id, rows):
        original(key, message_id, rows)
        with lane._answer_buttons_lock:
            lane._answer_buttons = new
    client.edit_reply_markup = replace_state
    lane._invalidate_answer_buttons()
    assert lane._answer_buttons is new


def test_invalid_taps_toast_without_edit(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._answer_buttons = (99, ("yes",), ("ab:1",))
    for message_id, data in ((98, "ab:1"), (99, "ab:unknown")):
        toast, label = lane.handle_answer_callback({"data": data,
            "message": {"message_id": message_id, "chat": {"id": 42}}})
        assert toast == "Tahle volba už neplatí."
        assert label is None
    lane._answer_buttons = None
    toast, label = lane.handle_answer_callback({"data": "ab:1"})
    assert (toast, label) == ("Tahle volba už neplatí.", None)
    assert client.edits == []


def test_bridge_command_keeps_keyboard(tmp_path):
    from test_multiplexer import Client, make_mux, update
    client = Client()
    client.edits = []
    client.edit_reply_markup = lambda key, message_id, rows: client.edits.append((key, message_id, rows))
    mux, _ = make_mux(tmp_path, client)
    mux._start_lane = lambda key: None
    key = LaneKey(123, 17)
    mux._started_lanes.add(key)
    lane = mux._lane(key)
    lane._answer_buttons = (90, ("old",), ("ab:old",))
    mux._handle_update_once(update(1, 17, sender=8, text="/status"), 0)
    assert lane._answer_buttons is not None
    assert client.edits == []


def test_new_button_message_invalidates_previous(tmp_path):
    lane, client = lane_for(tmp_path)
    lane._answer_buttons = (88, ("old",), ("ab:old",))
    lane._send_final("new\n[tg-buttons] next")
    lane._flush_pending()
    assert client.edits == [(88, [])]
    assert lane._answer_buttons[1] == ("next",)


def test_typed_message_invalidates_answer_buttons(tmp_path):
    from test_multiplexer import Client, make_mux, update

    client = Client()
    client.edits = []
    client.edit_reply_markup = lambda key, message_id, rows: client.edits.append((key, message_id, rows))
    mux, lanes = make_mux(tmp_path, client)
    mux._start_lane = lambda key: None
    mux._started_lanes.add(LaneKey(123, 17))
    lane = mux._lane(LaneKey(123, 17))
    lane._answer_buttons = (90, ("old",), ("ab:old",))
    mux._handle_update_once(update(1, 17, sender=8, text="typed"), 0)
    assert lane._answer_buttons is None
    assert client.edits == [(lane.key, 90, [])]
