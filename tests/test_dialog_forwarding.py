"""A dialog that blocks delivery is offered in Telegram as ONE bubble; a tap answers it."""
import json

from agent2telegramtopic import lane as lane_mod
from agent2telegramtopic.lane import INBOUND_MAX_ATTEMPTS, LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.session import BLOCKED_BY_OVERLAY, SessionError
from test_dialog import NUDGE, TRUST, UNKNOWN

KEY = LaneKey(44, 12)


class FakeTelegram:
    def __init__(self):
        self.sent = []       # (text, buttons)
        self.edits = []      # (message_id, text)
        self.notices = []
        self.answers = []

    def send_inline_keyboard(self, key, text, rows):
        assert key == KEY
        self.sent.append((text, [b["text"] for r in rows for b in r], rows))
        return 100 + len(self.sent)

    def edit_clear_keyboard(self, key, message_id, text):
        self.edits.append((message_id, text))

    def send_message(self, key, text, **kw):
        self.notices.append(text)

    def send_chat_action(self, key, action):
        pass

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append((callback_query_id, text))


class ScreenSession:
    """A TUI stand-in: shows `pane`; keys act on it the way the real dialogs do."""

    def __init__(self, pane, *, options=2, highlighted=0):
        self.pane = pane
        self.focused = False
        self.keys = []
        self.injected = []
        self.n = options
        self.hl = highlighted
        self.after_enter = None     # pane to show after Enter (multi-step), else dialog closes

    def inject(self, text):
        if not self.focused:
            raise SessionError(f"{BLOCKED_BY_OVERLAY}: dialog on screen")
        self.injected.append(text)

    def dialog_snapshot(self):
        pane = self.pane
        if "Keep auto" in pane:
            pane = pane.replace("❯ ", "  ")
            marker = ["Keep auto", "Autocompact at"][self.hl]
            pane = pane.replace(f"  {marker}", f"❯ {marker}", 1)
        return pane, self.focused

    def press_dialog_key(self, key):
        self.keys.append(key)
        if key == "Down":
            self.hl = min(self.hl + 1, self.n - 1)
        elif key == "Up":
            self.hl = max(self.hl - 1, 0)
        elif self.after_enter and key == "Enter":
            self.pane = self.after_enter
        else:
            self.focused = True


def make_lane(tmp_path, session, tg=None):
    tg = tg or FakeTelegram()
    state = tmp_path / "12"
    lane = LaneBridge(tg, KEY, state / "sent", state / "queue")
    lane._session = session
    lane._stop.wait = lambda _: None
    return lane, tg


def deliver(lane, text="hello", uid=1):
    inbox = lane._ensure_inbox()
    rid = inbox.reserve({"update_id": uid, "message": {"text": text}})
    lane._submit_inbound_update(next(r.update for r in inbox.pending() if r.record_id == rid), rid)
    lane._inbound_queue.join()
    return rid


def tap(lane, bubble_id, label_or_index, tg, uid="u1"):
    rows = tg.sent[-1][2]
    buttons = [b for r in rows for b in r]
    button = buttons[label_or_index] if isinstance(label_or_index, int) else next(
        b for b in buttons if b["text"] == label_or_index)
    return lane.handle_dialog_callback({
        "id": uid, "from": {"id": 8}, "data": button["callback_data"],
        "message": {"message_id": bubble_id, "chat": {"id": 44, "type": "private"}, "message_thread_id": 12}})


def test_nudge_dialog_becomes_a_bubble_with_the_screens_options(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    assert len(tg.sent) == 1
    text, buttons, rows = tg.sent[0]
    assert buttons == ["Keep auto (1m tokens)", "Autocompact at 400k tokens", "Zrušit (Esc)"]
    assert text.startswith("⚠️ Claude Code čeká na tvou volbu")
    assert "We recommend compacting earlier" in text
    assert text.endswith("Tvoje zpráva počká, až dialog zavřeš.")
    assert all(len(b["callback_data"].encode()) <= 64 for r in rows for b in r)
    assert tg.notices == []


def test_codex_lane_says_agent(tmp_path):
    lane, tg = make_lane(tmp_path, ScreenSession(NUDGE))
    lane.agent = "codex"
    deliver(lane)
    assert tg.sent[0][0].startswith("⚠️ Agent čeká na tvou volbu")


def test_no_key_is_sent_without_a_click(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    lane._dialog_tick()
    assert sess.keys == [] and sess.injected == []


def test_numbered_dialog_click_sends_that_digit(tmp_path):
    sess = ScreenSession(TRUST)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    assert tg.sent[0][1] == ["Yes, I trust this folder", "No, exit", "Zrušit (Esc)"]
    tap(lane, 101, "No, exit", tg)
    assert sess.keys == ["2"]
    assert tg.edits[-1] == (101, "✓ Vybráno: No, exit")


def test_second_unnumbered_option_is_reached_and_verified_before_enter(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    tap(lane, 101, "Autocompact at 400k tokens", tg)
    assert sess.keys == ["Down", "Enter"]
    assert tg.edits == [(101, "✓ Vybráno: Autocompact at 400k tokens")]


def test_enter_is_withheld_when_the_highlight_does_not_land_on_the_target(tmp_path):
    sess = ScreenSession(NUDGE, options=1)       # Down cannot move: the highlight never arrives
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    tap(lane, 101, "Autocompact at 400k tokens", tg)
    assert "Enter" not in sess.keys
    assert tg.edits[0] == (101, "Dialog se mezitím změnil.")
    assert len(tg.sent) == 2                      # a fresh bubble is offered


def test_escape_button_cancels(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    tap(lane, 101, "Zrušit (Esc)", tg)
    assert sess.keys == ["Escape"]
    assert tg.edits[-1] == (101, "✓ Zrušeno")


def test_unrecognised_dialog_offers_only_escape(tmp_path):
    lane, tg = make_lane(tmp_path, ScreenSession(UNKNOWN))
    deliver(lane)
    assert tg.sent[0][1] == ["Zrušit (Esc)"]
    assert "Something entirely new" in tg.sent[0][0]


def test_pending_message_waits_without_dead_letter_and_is_delivered_after_close(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(lane_mod.time, "monotonic", lambda: now[0])
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    rid = deliver(lane, "my message")
    inbox = lane._ensure_inbox()
    for i in range(INBOUND_MAX_ATTEMPTS + 2):    # more tries than the retry limit
        deliver_again = next(r.update for r in inbox.pending())
        lane._submit_inbound_update(deliver_again, rid)
        lane._inbound_queue.join()
    assert [r.record_id for r in inbox.pending()] == [rid]
    assert inbox.dead_letters() == []
    assert sess.injected == []
    sess.focused = True                          # closed in the terminal
    now[0] += 6
    lane._dialog_tick()
    lane._inbound_queue.join()
    assert len(sess.injected) == 1 and "my message" in sess.injected[0]
    assert inbox.pending() == []
    assert tg.edits[-1] == (101, "Dialog byl zavřen v terminálu.")


def test_several_pending_messages_share_one_bubble(tmp_path):
    lane, tg = make_lane(tmp_path, ScreenSession(NUDGE))
    deliver(lane, "one", 1)
    deliver(lane, "two", 2)
    deliver(lane, "three", 3)
    assert len(tg.sent) == 1


def test_tap_then_pending_message_is_delivered(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane, "after the click")
    tap(lane, 101, 0, tg)
    lane._inbound_queue.join()
    assert sess.injected and "after the click" in sess.injected[0]


def test_stale_click_sends_nothing_and_says_so(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    sess.pane = NUDGE.replace("400k", "500k")    # a different dialog now has the keyboard
    tap(lane, 101, 1, tg)
    assert sess.keys == []
    assert tg.edits[0] == (101, "Dialog se mezitím změnil.")
    assert len(tg.sent) == 2 and "500k" in tg.sent[1][1][1]


def test_click_after_the_dialog_closed_in_the_terminal_sends_nothing(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    sess.focused = True
    tap(lane, 101, 1, tg)
    assert sess.keys == []
    assert tg.edits[-1] == (101, "Dialog byl zavřen v terminálu.")


def test_multi_step_dialog_gets_a_new_bubble(tmp_path):
    sess = ScreenSession(TRUST)
    sess.after_enter = None
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    # after the digit, a different dialog appears instead of the composer
    original = sess.press_dialog_key
    def press(key):
        original(key)
        sess.focused = False
        sess.pane = UNKNOWN
    sess.press_dialog_key = press
    tap(lane, 101, 0, tg)
    assert tg.edits[-1] == (101, "✓ Vybráno: Yes, I trust this folder")
    assert len(tg.sent) == 2 and tg.sent[1][1] == ["Zrušit (Esc)"]


def test_click_from_a_user_who_is_not_allowed_sends_nothing(tmp_path):
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    deliver(lane)
    token = tg.sent[0][2][1][0]["callback_data"]
    mux = Multiplexer(tg, {8}, tmp_path / "bot", lambda key: lane, lane_states_root=tmp_path / "bot" / "lanes")
    mux.tg = tg
    mux._lanes[KEY] = lane
    mux._started_lanes.add(KEY)

    def cb(uid, user):
        return {"update_id": uid, "callback_query": {
            "id": f"c{uid}", "from": {"id": user}, "data": token,
            "message": {"message_id": 101, "chat": {"id": 44, "type": "private"}, "message_thread_id": 12}}}

    mux._handle_update_once(cb(1, 999), 0)
    assert sess.keys == [] and tg.edits == []
    assert tg.answers == [("c1", None)]          # still answered, nothing done
    mux._handle_update_once(cb(2, 8), 2)
    assert sess.keys == ["Down", "Enter"]
    assert [a[0] for a in tg.answers] == ["c1", "c2"]


def test_client_sends_keyboard_clears_it_on_edit_and_answers_taps():
    from agent2telegramtopic.telegram import TelegramClient
    from telegram_fakes import TransportOpener
    calls = []

    def transport(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 7} if method == "sendMessage" else True}

    client = TelegramClient("secret", opener=TransportOpener(transport))
    rows = [[{"text": "A", "callback_data": "dlg:1"}]]
    assert client.send_inline_keyboard(KEY, "hi", rows) == 7
    client.edit_clear_keyboard(KEY, 7, "done")
    client.answer_callback_query("cb1")
    send, edit, answer = calls
    assert send[1]["message_thread_id"] == 12 and json.loads(send[1]["reply_markup"]) == {"inline_keyboard": rows}
    assert edit[0] == "editMessageText" and json.loads(edit[1]["reply_markup"]) == {"inline_keyboard": []}
    assert answer == ("answerCallbackQuery", {"callback_query_id": "cb1"})
