from datetime import datetime

from agent2telegramtopic import lane as lane_mod
from test_dialog_forwarding import NUDGE, ScreenSession, make_lane


def setup_lane(tmp_path, monkeypatch):
    now = [1000.0]
    wall = [datetime(2026, 11, 2, 9, 12).timestamp()]
    monkeypatch.setattr(lane_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lane_mod.time, "time", lambda: wall[0])
    lane, tg = make_lane(tmp_path, ScreenSession(NUDGE))
    lane._turn_active.set()
    lane._turn_from_tg = True
    lane._last_activity = now[0]
    lane._last_activity_wall = wall[0]
    return lane, tg, now, wall


def test_running_turn_sends_one_notice_after_silence(tmp_path, monkeypatch):
    lane, tg, now, _ = setup_lane(tmp_path, monkeypatch)
    now[0] += lane_mod.SILENCE_NOTICE_S
    lane._silence_notice_tick()
    lane._silence_notice_tick()
    assert tg.notices == ["⏳ Agent je od 09:12 bez aktivity a tah pořád běží. Nejspíš na něčem visí."]


def test_activity_resets_notice_interval(tmp_path, monkeypatch):
    lane, tg, now, wall = setup_lane(tmp_path, monkeypatch)
    now[0] += lane_mod.SILENCE_NOTICE_S
    lane._silence_notice_tick()
    tg.notices.clear()
    now[0] += 30
    wall[0] += 30
    lane._note_activity()
    now[0] += lane_mod.SILENCE_NOTICE_S - 1
    lane._silence_notice_tick()
    assert tg.notices == []
    now[0] += 1
    lane._silence_notice_tick()
    assert len(tg.notices) == 1


def test_no_running_turn_sends_no_notice(tmp_path, monkeypatch):
    lane, tg, now, _ = setup_lane(tmp_path, monkeypatch)
    lane._turn_active.clear()
    now[0] += lane_mod.SILENCE_NOTICE_S + 1
    lane._silence_notice_tick()
    assert tg.notices == []


def test_open_dialog_bubble_suppresses_notice(tmp_path, monkeypatch):
    lane, tg, now, _ = setup_lane(tmp_path, monkeypatch)
    lane._dialog_bubble = object()
    now[0] += lane_mod.SILENCE_NOTICE_S + 1
    lane._silence_notice_tick()
    assert tg.notices == []
