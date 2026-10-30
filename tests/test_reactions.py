"""Reactions (Phase B2): port of agent2telegram tests/test_attach_queue.py ReactionDedupTests and
tests/test_attach_backstop.py reaction tests, adapted to LaneBridge/LaneKey, plus the topic mapping
(Telegram reaction updates carry no message_thread_id)."""

import json
import logging
import time

from agent2telegramtopic import lane as lane_mod
from agent2telegramtopic.lane import LaneBridge, LaneTelegram, REACTION_DEDUP_S
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import Lane, LaneStore

CHAT = 44


class Telegram:
    def __init__(self):
        self.messages, self.next_id = [], 1000

    def send_message(self, key, text, **kwargs):
        self.messages.append((key, text))
        self.next_id += 1
        return [self.next_id]

    def send_chat_action(self, key, action):
        pass

    def send_file(self, key, path, **kwargs):
        self.next_id += 1
        return {"message_id": self.next_id}

    def send_voice(self, key, path, **kwargs):
        self.next_id += 1
        return {"message_id": self.next_id}


class FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def make_lane(tmp_path, key=LaneKey(CHAT, 12), client=None, store=None):
    state = tmp_path / str(key.thread_id)
    lane = LaneBridge(client or Telegram(), key, state / "sent", state / "queue", lane_store=store)
    lane._session = FakeSession()
    lane._stop.wait = lambda _: None
    lane._retry_last_assistant_text = lambda: None
    return lane


def reaction(uid=1, emoji="❤️", message_id=20800, chat=CHAT):
    return {"update_id": uid, "message_reaction": {
        "user": {"id": 8}, "chat": {"id": chat, "type": "private"}, "message_id": message_id,
        "new_reaction": [{"type": "emoji", "emoji": emoji}]}}


# ---- ReactionDedupTests (a2t test_attach_queue.py) --------------------------------------

def build_dedup_lane(tmp_path):
    lane = make_lane(tmp_path)
    lane._injected = []
    lane._inject = lambda text: (lane._injected.append(text), True)[1]
    lane._begin_turn = lambda: None
    return lane


def test_six_identical_reactions_produce_one_reply(tmp_path):
    lane = build_dedup_lane(tmp_path)
    for uid in range(100, 106):
        lane._handle(reaction(uid))
    assert len(lane._injected) == 1, f"agent received {len(lane._injected)} prompts instead of one"


def test_reaction_to_another_message_passes(tmp_path):
    lane = build_dedup_lane(tmp_path)
    lane._handle(reaction(1, message_id=100))
    lane._handle(reaction(2, message_id=101))
    assert len(lane._injected) == 2


def test_another_emoji_passes(tmp_path):
    lane = build_dedup_lane(tmp_path)
    lane._handle(reaction(1, emoji="❤️"))
    lane._handle(reaction(2, emoji="👍"))
    assert len(lane._injected) == 2


def test_reaction_passes_after_dedup_window(tmp_path):
    lane = build_dedup_lane(tmp_path)
    lane._handle(reaction(1))
    key = list(lane._reaction_seen)[0]
    lane._reaction_seen[key] -= REACTION_DEDUP_S + 1
    lane._handle(reaction(2))
    assert len(lane._injected) == 2


def test_reaction_memory_does_not_grow_unbounded(tmp_path):
    lane = build_dedup_lane(tmp_path)
    for i in range(700):
        lane._handle(reaction(i, message_id=i))
    assert len(lane._reaction_seen) <= 512


# ---- reaction backstop tests (a2t test_attach_backstop.py) -----------------------------

def flush(lane):
    lane._flush_pending()


def test_reaction_without_a_reply_forwards_nothing(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._retry_last_assistant_text = lambda: "No response requested."
    lane._drain_transcript = lambda: None

    lane._handle(reaction())
    assert lane._session.injected, "the reaction never reached the session"
    lane._finish_turn()
    flush(lane)

    assert tg.messages == [], "the agent's internal note leaked to the user after a reaction"
    assert not lane._turn_active.is_set()


def test_reaction_turn_still_delivers_an_explicit_reply_to_its_own_topic(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._retry_last_assistant_text = lambda: "No response requested."
    lane._drain_transcript = lambda: None

    lane._handle(reaction())
    lane._send_final("thanks!")                # the agent decided to answer anyway
    lane._finish_turn()
    flush(lane)

    assert tg.messages == [(LaneKey(CHAT, 12), "thanks!")]


def test_reaction_prompt_asks_for_a_short_answer(tmp_path):
    lane = make_lane(tmp_path)
    lane._handle(reaction())
    prompt = lane._session.injected[0].lower()
    assert "always answer" in prompt
    assert "short" in prompt
    assert "no need to reply" not in prompt


def test_reaction_during_a_running_turn_keeps_the_backstop_armed(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._turn_active.set()                     # a question is running
    lane._turn_from_tg = True
    lane._retry_last_assistant_text = lambda: "[TG] the real answer"
    lane._drain_transcript = lambda: None

    lane._handle(reaction())
    lane._finish_turn()
    flush(lane)

    assert tg.messages == [(LaneKey(CHAT, 12), "the real answer")]


def test_turn_end_log_reports_a_reaction_turn(tmp_path, caplog):
    lane = make_lane(tmp_path)
    lane._drain_transcript = lambda: None
    lane._handle(reaction())
    with caplog.at_level(logging.INFO, logger="agent2telegramtopic.lane"):
        lane._finish_turn()
    assert "reaction=True" in caplog.text
    assert "reaction turn without a reply" in caplog.text


def test_turn_end_log_reports_a_text_turn_as_not_a_reaction(tmp_path, caplog):
    lane = make_lane(tmp_path)
    lane._drain_transcript = lambda: None
    lane._begin_turn()
    lane._turn_text_sent = True
    with caplog.at_level(logging.INFO, logger="agent2telegramtopic.lane"):
        lane._finish_turn()
    assert "reaction=False" in caplog.text


# ---- topic mapping ----------------------------------------------------------------------

def test_lane_records_every_message_it_sends_in_its_topic(tmp_path):
    store = LaneStore(":memory:")
    tg = Telegram()
    wrapper = LaneTelegram(tg, LaneKey(CHAT, 12), store)
    wrapper.send_message(CHAT, "text")
    wrapper.send_file(CHAT, "x.png")
    wrapper.send_voice(CHAT, "v.ogg")
    assert [store.thread_for_message(CHAT, i) for i in (1001, 1002, 1003)] == [12, 12, 12]
    assert store.thread_for_message(CHAT, 1004) is None


class Client(Telegram):
    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.on_last_poll = lambda: None

    def _call(self, method, params, *, timeout=None):
        batch = self.batches.pop(0) if self.batches else []
        if not self.batches:
            self.on_last_poll()
        return batch


def text_update(uid, thread, message_id, text="hello"):
    return {"update_id": uid, "message": {"message_id": message_id,
            "chat": {"id": CHAT, "type": "private"}, "message_thread_id": thread,
            "from": {"id": 8}, "text": text}}


def make_mux(tmp_path, client):
    store = LaneStore(":memory:")
    lanes = {}

    def factory(key):
        root = tmp_path / "bot" / "lanes" / str(key.chat_id) / str(key.thread_id)
        root.mkdir(parents=True, exist_ok=True)
        lane = LaneBridge(client, key, root / "sent", root / "queue", lane_store=store)
        lane._session = FakeSession()
        lane._start_outbound = lambda: None
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        lanes[key] = lane
        return lane

    mux = Multiplexer(client, {8}, tmp_path / "bot", factory, lane_store=store, max_live=4,
                      lane_states_root=tmp_path / "bot" / "lanes")
    for thread in (10, 11):
        key = LaneKey(CHAT, thread)
        store.upsert(Lane(CHAT, thread, "t", "codex", "s", "live", False, 1.0, None, 0))
        mux._start_lane(key)
    return mux, lanes, store


def drain(lanes):
    for lane in lanes.values():
        lane._inbound_queue.join()


def test_inbound_messages_are_recorded_in_their_topic(tmp_path):
    client = Client([])
    mux, lanes, store = make_mux(tmp_path, client)
    mux._handle_update_once(text_update(1, 10, 500), 0)
    mux._handle_update_once(text_update(2, 11, 501), 2)
    drain(lanes)
    assert store.thread_for_message(CHAT, 500) == 10
    assert store.thread_for_message(CHAT, 501) == 11


def test_reaction_on_a_message_in_thread_x_reaches_lane_x_and_only_lane_x(tmp_path):
    client = Client([])
    mux, lanes, store = make_mux(tmp_path, client)
    store.record_message(CHAT, 700, 10)
    store.record_message(CHAT, 701, 11)
    mux._handle_update_once(reaction(1, message_id=700), 0)
    drain(lanes)
    assert len(lanes[LaneKey(CHAT, 10)]._session.injected) == 1
    assert "#700" in lanes[LaneKey(CHAT, 10)]._session.injected[0]
    assert lanes[LaneKey(CHAT, 11)]._session.injected == []
    mux._handle_update_once(reaction(2, message_id=701), 2)
    drain(lanes)
    assert len(lanes[LaneKey(CHAT, 11)]._session.injected) == 1
    assert len(lanes[LaneKey(CHAT, 10)]._session.injected) == 1


def test_reaction_on_an_unmapped_message_is_logged_and_dropped(tmp_path, caplog):
    client = Client([])
    mux, lanes, store = make_mux(tmp_path, client)
    with caplog.at_level(logging.INFO, logger="agent2telegramtopic.multiplexer"):
        offset = mux._handle_update_once(reaction(1, message_id=999), 0)
    drain(lanes)
    assert offset == 2
    assert "dropping reaction on unmapped message" in caplog.text
    assert all(lane._session.injected == [] for lane in lanes.values())


def test_reaction_from_a_user_who_is_not_allowed_is_ignored(tmp_path):
    client = Client([])
    mux, lanes, store = make_mux(tmp_path, client)
    store.record_message(CHAT, 700, 10)
    stranger = reaction(1, message_id=700)
    stranger["message_reaction"]["user"]["id"] = 99
    mux._handle_update_once(stranger, 0)
    drain(lanes)
    assert lanes[LaneKey(CHAT, 10)]._session.injected == []


def test_polling_asks_telegram_for_message_reaction_updates(tmp_path):
    seen = []
    client = Client([[]])
    original = client._call
    client._call = lambda method, params, *, timeout=None: (seen.append(params["allowed_updates"]), original(method, params))[1]
    mux, _lanes, _store = make_mux(tmp_path, client)
    client.on_last_poll = mux.stop
    mux._inbound_loop()
    assert "message_reaction" in json.loads(seen[0])
