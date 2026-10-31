"""Proactive dialog notices are delayed until a recognised screen is stable."""
from agent2telegramtopic import lane as lane_mod
from test_dialog_forwarding import NUDGE, UNKNOWN, ScreenSession, make_lane, deliver, tap


def test_stable_dialog_sends_one_proactive_bubble_and_later_inbound_reuses_it(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(lane_mod.time, "monotonic", lambda: now[0])
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    lane.outbound_tick()
    assert tg.sent == []
    now[0] += 5
    lane.outbound_tick()
    assert tg.sent == []
    now[0] += 5
    lane.outbound_tick()
    assert len(tg.sent) == 1
    assert tg.sent[0][1] == ["Keep auto (1m tokens)", "Autocompact at 400k tokens", "Zrušit (Esc)"]
    assert tg.sent[0][0].endswith("Agent čeká, dokud nevybereš.")
    deliver(lane, "after choice")
    assert len(tg.sent) == 1
    tap(lane, 101, 0, tg)
    lane._inbound_queue.join()
    assert sess.injected and "after choice" in sess.injected[0]


def test_dialog_gone_before_ten_seconds_produces_no_bubble(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(lane_mod.time, "monotonic", lambda: now[0])
    sess = ScreenSession(NUDGE)
    lane, tg = make_lane(tmp_path, sess)
    lane._dialog_tick()
    now[0] += 5
    sess.focused = True
    lane._dialog_tick()
    now[0] += 10
    lane._dialog_tick()
    assert tg.sent == []


def test_unrecognised_screen_does_not_start_proactive_timer(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(lane_mod.time, "monotonic", lambda: now[0])
    lane, tg = make_lane(tmp_path, ScreenSession(UNKNOWN))
    lane._dialog_tick()
    now[0] += 10
    lane._dialog_tick()
    assert tg.sent == []


def test_dead_or_parked_lane_is_not_captured(tmp_path):
    sess = ScreenSession(NUDGE)
    sess.alive = False
    lane, tg = make_lane(tmp_path, sess)
    lane._dialog_tick()
    assert tg.sent == []
    assert getattr(lane, "_dialog_proactive_seen", None) is None
    sess.alive = True
    lane._defer_inbound_replay = True
    lane._dialog_tick()
    assert tg.sent == []
