"""Topic naming (new-topic hint, topic-edit, skill) and /usage without credentials."""
import io
import json
import time

import pytest

from agent2telegramtopic import skills, statusline, topicedit, uninstall, usage
from agent2telegramtopic.config import claude_rate_limits_file, save_config, write_hooks_settings
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.state import LaneStore
from telegram_fakes import TransportOpener
from test_lane_commands import make_lane
from test_multiplexer import Client, make_mux, update

TOKEN = "1234567:fake-token-for-tests-only-aaaaaaaaaaaa"
HINT = "(New topic: name it after the subject of this message"


def run_mux(tmp_path, texts_by_thread):
    client, lanes = Client(), {}
    store = LaneStore(tmp_path / "lanes.db")
    mux, _ = make_mux(tmp_path, client, lanes)
    mux.lane_store, mux.max_live = store, 4
    build = mux.lane_factory

    def factory(key):  # production (app.py) hands every lane the store
        lane = build(key)
        lane._lane_store = store
        return lane

    mux.lane_factory = factory
    mux._start_slot_manager()
    return mux, lanes, store


def injected(lane, count):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(lane._session.injected) < count:
        time.sleep(.01)
    lane._inbound_queue.join()
    return lane._session.injected


def created(uid, thread, *, via_thread_field=True):
    """The service update Telegram delivers when the user creates a topic."""
    msg = {"chat": {"id": 123, "type": "private"}, "message_id": thread, "from": {"id": 8},
           "forum_topic_created": {"name": "New topic"}}
    if via_thread_field:
        msg["message_thread_id"] = thread
    return {"update_id": uid, "message": msg}


def test_first_message_after_topic_creation_gets_the_naming_hint_and_later_ones_do_not(tmp_path):
    mux, lanes, _ = run_mux(tmp_path, None)
    mux._handle_update_once(created(1, 10), 0)
    assert LaneKey(123, 10) not in lanes  # the service message is not content: no turn, no lane
    mux._handle_update_once(update(2, 10, text="first message"), 2)
    mux._handle_update_once(update(3, 10, text="second message"), 3)
    texts = injected(lanes[LaneKey(123, 10)], 2)
    assert texts[0].startswith("first message") and HINT in texts[0]
    assert "topic-edit" in texts[0]
    assert texts[1] == "second message"


def test_creation_without_thread_field_still_names_the_topic_by_its_service_message_id(tmp_path):
    mux, lanes, _ = run_mux(tmp_path, None)
    mux._handle_update_once(created(1, 10, via_thread_field=False), 0)
    mux._handle_update_once(update(2, 10, text="first message"), 2)
    assert HINT in injected(lanes[LaneKey(123, 10)], 1)[0]


def test_creation_survives_a_bridge_restart_before_the_first_message(tmp_path):
    mux, _, _ = run_mux(tmp_path, None)
    mux._handle_update_once(created(1, 10), 0)
    mux2, lanes2, _ = run_mux(tmp_path, None)  # a new process opens the same lane database
    mux2.lane_store = LaneStore(tmp_path / "lanes.db")
    mux2._handle_update_once(update(2, 10, text="first message"), 2)
    assert HINT in injected(lanes2[LaneKey(123, 10)], 1)[0]


def test_message_in_preexisting_topic_without_creation_event_gets_no_hint(tmp_path):
    """After a fresh install every old topic has no lane row; that is not a new topic."""
    mux, lanes, _ = run_mux(tmp_path, None)
    mux._handle_update_once(update(1, 10, text="old topic message"), 0)
    assert injected(lanes[LaneKey(123, 10)], 1) == ["old topic message"]


def test_new_in_a_created_topic_does_not_trigger_the_naming_hint_again(tmp_path):
    """Regression 2026-10-30: an emptied session after /new is not a new topic."""
    mux, lanes, store = run_mux(tmp_path, None)
    mux._handle_update_once(created(1, 10), 0)
    mux._handle_update_once(update(2, 10, text="first message"), 1)
    lane = lanes[LaneKey(123, 10)]
    assert HINT in injected(lane, 1)[0]
    lane._turn_active.clear()  # the first turn has finished
    store.set_busy(LaneKey(123, 10), False)
    lane._session.send_command = lambda text: None
    mux._handle_update_once(update(3, 10, text="/new"), 3)
    mux._handle_update_once(update(4, 10, text="after the reset"), 4)
    texts = injected(lane, 2)
    assert texts[1] == "after the reset"
    assert all(HINT not in t for t in texts[1:])


def test_general_topic_gets_no_hint_even_with_a_creation_event(tmp_path):
    mux, lanes, _ = run_mux(tmp_path, None)
    mux._handle_update_once(created(1, 1), 0)
    mux._handle_update_once(update(2, 1, text="general"), 2)
    assert injected(lanes[LaneKey(123, 1)], 1) == ["general"]


# ---- topic-edit ------------------------------------------------------------------------------

STICKERS = [{"emoji": "💡", "custom_emoji_id": "111"}, {"emoji": "✈️", "custom_emoji_id": "222"}]


class Api:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "getForumTopicIconStickers":
            return {"ok": True, "result": STICKERS}
        return {"ok": True, "result": True}


def topic_dir(tmp_path):
    d = tmp_path / "topics" / "bot" / "555" / "77"
    d.mkdir(parents=True)
    return d


def edit(tmp_path, args, api, cwd=None):
    from agent2telegramtopic.telegram import TelegramClient
    save_config({"agent": "codex", "token": TOKEN, "allowed_user_ids": [1], "bot_username": "b"}, tmp_path)
    client = TelegramClient(TOKEN, opener=TransportOpener(api))
    return topicedit.main(args, home=tmp_path, cwd=cwd or topic_dir(tmp_path), client=client)


def test_topic_edit_renames_the_topic_of_the_current_directory(tmp_path, capsys):
    api = Api()
    assert edit(tmp_path, ["Garden", "💡"], api) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "ok" and TOKEN not in out.out + out.err
    method, params = api.calls[-1]
    assert method == "editForumTopic"
    assert (params["chat_id"], params["message_thread_id"], params["name"],
            params["icon_custom_emoji_id"]) == (555, 77, "Garden", "111")
    store = LaneStore(tmp_path / ".agent2telegramtopic/state/b.db")
    assert store.topic_titles()[LaneKey(555, 77)] == "Garden"


def test_topic_edit_records_title_when_telegram_says_not_modified(tmp_path, capsys):
    class NotModified:
        def _call(self, method, params=None, **kwargs):
            if method == "getForumTopicIconStickers":
                return STICKERS
            from agent2telegramtopic.telegram import TelegramError
            raise TelegramError(400, "TOPIC_NOT_MODIFIED")
    save_config({"agent": "codex", "token": TOKEN, "allowed_user_ids": [1], "bot_username": "b"}, tmp_path)
    assert topicedit.main(["Same", "💡"], home=tmp_path, cwd=topic_dir(tmp_path), client=NotModified()) == 0
    store = LaneStore(tmp_path / ".agent2telegramtopic/state/b.db")
    assert store.topic_titles()[LaneKey(555, 77)] == "Same"


def test_failed_topic_edit_does_not_record_title(tmp_path):
    class Failed:
        def _call(self, method, params=None, **kwargs):
            if method == "getForumTopicIconStickers":
                return STICKERS
            from agent2telegramtopic.telegram import TelegramError
            raise TelegramError(400, "FORUM_TOPIC_CLOSED")
    save_config({"agent": "codex", "token": TOKEN, "allowed_user_ids": [1], "bot_username": "b"}, tmp_path)
    assert topicedit.main(["Nope", "💡"], home=tmp_path, cwd=topic_dir(tmp_path), client=Failed()) == 1
    store = LaneStore(tmp_path / ".agent2telegramtopic/state/b.db")
    assert store.topic_titles() == {}


def test_topic_edit_accepts_emoji_with_variation_selector(tmp_path):
    api = Api()
    assert edit(tmp_path, ["Trip", "✈"], api) == 0
    assert api.calls[-1][1]["icon_custom_emoji_id"] == "222"


def test_topic_edit_unknown_emoji_fails_and_lists_the_allowed_ones(tmp_path, capsys):
    api = Api()
    assert edit(tmp_path, ["Garden", "🦄"], api) != 0
    err = capsys.readouterr().err
    assert "💡" in err and "✈" in err
    assert all(m != "editForumTopic" for m, _ in api.calls)


def test_topic_edit_outside_a_topic_directory_fails(tmp_path, capsys):
    plain = tmp_path / "somewhere"
    plain.mkdir()
    assert edit(tmp_path, ["x", "💡"], Api(), cwd=plain) != 0
    assert "topic" in capsys.readouterr().err


# ---- skill -----------------------------------------------------------------------------------

def test_setup_installs_skill_for_the_chosen_agent_and_uninstall_removes_it(tmp_path):
    assert "installed" in skills.install_skill("claude-code", tmp_path)
    assert "installed" in skills.install_skill("codex", tmp_path)
    claude = tmp_path / ".claude/skills/telegram-topic/SKILL.md"
    codex = tmp_path / ".codex/skills/telegram-topic/SKILL.md"
    assert claude.read_text() == codex.read_text() and "topic-edit" in claude.read_text()
    assert set(skills.remove_skills(tmp_path)) == {claude, codex}
    assert not claude.exists() and not codex.exists()


def test_skill_install_never_overwrites_a_different_existing_skill(tmp_path):
    target = tmp_path / ".claude/skills/telegram-topic/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own skill")
    assert "different" in skills.install_skill("claude-code", tmp_path)
    assert target.read_text() == "my own skill"
    assert skills.remove_skills(tmp_path) == [] and target.read_text() == "my own skill"


def test_uninstall_command_removes_only_our_skill(tmp_path, monkeypatch):
    skills.install_skill("claude-code", tmp_path)
    mine = tmp_path / ".codex/skills/telegram-topic/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("custom")
    monkeypatch.setattr(uninstall.service, "remove", lambda *a, **k: False)
    monkeypatch.setattr(uninstall, "_stop_tmux", lambda home: None)
    assert uninstall.main(["--yes"], home=tmp_path, platform="linux") == 0
    assert not (tmp_path / ".claude/skills/telegram-topic/SKILL.md").exists()
    assert mine.read_text() == "custom"


# ---- statusline and /usage -------------------------------------------------------------------

SAMPLE = {"model": {"display_name": "Opus 5.5"},
          "rate_limits": {"five_hour": {"used_percentage": 19, "resets_at": 1793394000},
                          "seven_day": {"used_percentage": 16, "resets_at": 1793862000}}}


def run_statusline(monkeypatch, stdin, out):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return statusline.main([str(out)])


def test_statusline_records_rate_limits_and_prints_a_status(monkeypatch, tmp_path, capsys):
    out = tmp_path / "limits.json"
    assert run_statusline(monkeypatch, json.dumps(SAMPLE), out) == 0
    assert capsys.readouterr().out.strip() == "Opus 5.5"
    saved = json.loads(out.read_text())
    assert saved["five_hour"]["used_percentage"] == 19 and saved["seven_day"]["used_percentage"] == 16
    assert saved["captured_at"] > 0


def test_statusline_records_context_window_by_resolved_cwd(monkeypatch, tmp_path):
    out = tmp_path / "bot.claude_rate_limits.json"
    payload = {"workspace": {"current_dir": str(tmp_path / "work" / "..")},
               "context_window": {"context_window_size": 200000}}
    assert run_statusline(monkeypatch, json.dumps(payload), out) == 0
    saved = json.loads((tmp_path / "bot.claude_ctx.json").read_text())
    assert saved[str(tmp_path.resolve())] == {"ctx_window": 200000, "at": saved[str(tmp_path.resolve())]["at"]}


@pytest.mark.parametrize("stdin", ["", "{", "[]", "null", json.dumps({"model": 3, "rate_limits": "x"}),
                                   json.dumps({"model": {"display_name": "M"}})])
def test_statusline_survives_garbage_and_keeps_the_previous_file(monkeypatch, tmp_path, stdin):
    out = tmp_path / "limits.json"
    out.write_text('{"keep": 1}')
    assert run_statusline(monkeypatch, stdin, out) == 0
    assert out.read_text() == '{"keep": 1}'


def test_claude_settings_register_the_statusline_next_to_the_hooks(tmp_path):
    path = tmp_path / "state" / "bot.hooks.json"
    write_hooks_settings(path, "/usr/bin/python3", "bot", tmp_path)
    settings = json.loads(path.read_text())
    command = settings["statusLine"]["command"]
    assert settings["statusLine"]["type"] == "command"
    assert "statusline" in command and str(path.with_name("bot.claude_rate_limits.json")) in command


def usage_lane(tmp_path, monkeypatch, agent, **kw):
    monkeypatch.setenv("HOME", str(tmp_path))
    lane, tg = make_lane(tmp_path, agent=agent)
    lane.bot_name = "bot"
    lane.codex_sessions_root = tmp_path / "sessions"
    return lane, tg


def ask_usage(lane, tg):
    lane._handle_command("/usage", 44)
    return tg.messages[-1][1]


def test_usage_without_data_explains_when_it_appears(tmp_path, monkeypatch):
    for agent in ("claude-code", "codex"):
        lane, tg = usage_lane(tmp_path, monkeypatch, agent)
        assert usage.NO_DATA in ask_usage(lane, tg)


def test_claude_usage_shows_windows_and_is_not_stale_when_fresh(tmp_path, monkeypatch):
    lane, tg = usage_lane(tmp_path, monkeypatch, "claude-code")
    now = time.time()
    usage.write_claude_limits(claude_rate_limits_file("bot"), {
        "five_hour": {"used_percentage": 19, "resets_at": now + 7200},
        "seven_day": {"used_percentage": 16.4, "resets_at": now + 3 * 86400}}, now - 120)
    text = ask_usage(lane, tg)
    assert "5-hour window: 19% used, resets in 1h 59m" in text or "5-hour window: 19% used, resets in 2h 0m" in text
    assert "7-day window: 16% used, resets in 2d" in text
    assert "captured 2m ago" in text and "stale" not in text


def test_claude_usage_flags_old_data_as_stale(tmp_path, monkeypatch):
    lane, tg = usage_lane(tmp_path, monkeypatch, "claude-code")
    now = time.time()
    usage.write_claude_limits(claude_rate_limits_file("bot"),
                              {"five_hour": {"used_percentage": 50, "resets_at": now - 5}}, now - 7200)
    text = ask_usage(lane, tg)
    assert "50% used, resetting now" in text and "captured 2h 0m ago" in text and "stale data" in text


def rollout(path, windows_by_event):
    lines = [json.dumps({"timestamp": "2026-10-30T08:00:00.000Z", "type": "session_meta", "payload": {}})]
    for ts, limits in windows_by_event:
        lines.append(json.dumps({"timestamp": ts, "type": "event_msg",
                                 "payload": {"type": "token_count", "info": None, "rate_limits": limits}}))
    lines.append("not json at all")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def test_codex_usage_reads_latest_token_count_with_null_secondary(tmp_path, monkeypatch):
    lane, tg = usage_lane(tmp_path, monkeypatch, "codex")
    rollout(tmp_path / "sessions/2026/10/30/rollout-a.jsonl", [
        ("2026-10-30T08:30:00.000Z", {"primary": {"used_percent": 3.0, "window_minutes": 300, "resets_at": 1}, "secondary": None}),
        ("2026-10-30T08:36:40.610Z", {"primary": {"used_percent": 8.0, "window_minutes": 10080, "resets_at": 1793775636},
                                      "secondary": None}),
        ("2026-10-30T08:40:00.000Z", None)])
    text = ask_usage(lane, tg)
    assert "7-day window: 8% used" in text and "5-hour" not in text
    assert "stale data" in text  # the event is from the past


def test_codex_two_windows_use_window_minutes_for_labels(tmp_path):
    path = tmp_path / "rollout-x.jsonl"
    rollout(path, [("2026-10-30T08:36:40Z", {
        "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 5},
        "secondary": {"used_percent": 20, "window_minutes": 10080, "resets_at": 6}}),
        ("2026-10-30T08:37:00Z", {
            "primary": {"used_percent": 11, "window_minutes": 60, "resets_at": 5},
            "secondary": {"used_percent": 21, "window_minutes": 10080, "resets_at": 6}})])
    _, windows = usage.read_codex_limits(path)
    assert [(t, u) for t, u, _ in windows] == [("60-minute", 11.0), ("7-day", 21.0)]
    path2 = tmp_path / "rollout-y.jsonl"
    rollout(path2, [("2026-10-30T08:36:40Z", {
        "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 5}, "secondary": None})])
    assert [t for t, _, _ in usage.read_codex_limits(path2)[1]] == ["5-hour"]
