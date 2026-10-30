"""Sender display context never changes authorization or reply routing.

Port of agent2telegram tests/test_inbound_sender_label.py, adapted to LaneBridge/LaneKey.
agent2telegramtopic lanes only ever see private chats (routing rejects groups), so the group labels are
pinned on sender_label itself; the lane-level tests use private chats. test_notify_* is skipped:
agent2telegramtopic has no notify CLI."""
import unicodedata
from unittest.mock import patch, Mock

import pytest

from agent2telegramtopic.config import Config
from agent2telegramtopic.inbound import sender_label
from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.multiplexer import Multiplexer
from agent2telegramtopic.routing import LaneKey

KEY = LaneKey(22, 31)


class Telegram:
    def __init__(self):
        self.sent = []

    def send_message(self, key, text, **kwargs):
        self.sent.append((key, text))
        return []

    def send_chat_action(self, key, action):
        pass


class Session:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def make_lane(tmp_path, enabled=True):
    config = Config("test-bot", "claude-code", frozenset({22}), inbound_sender_label=enabled)
    lane = LaneBridge(Telegram(), KEY, tmp_path / "sent", tmp_path / "queue", config=config)
    lane._session = Session()
    lane._stop.wait = lambda _: None
    return lane


def incoming(name="Alex", **extra):
    message = {"message_id": 22, "message_thread_id": 31, "from": {"id": 22, "first_name": name},
               "chat": {"id": 22, "type": "private"}, "text": "Hello"}
    message.update(extra)
    return {"update_id": 22, "message": message}


def test_private_label_and_original_routing(tmp_path):
    lane = make_lane(tmp_path)
    assert lane._handle(incoming()) is True
    assert lane._session.injected == ["[From: Alex · DM] Hello"]
    assert lane._owner_chat == 22 and lane.key == KEY


def test_group_label():
    message = {"from": {"first_name": "Alex"}, "chat": {"type": "supergroup", "title": "Team"}}
    assert sender_label(message) == '[From: Alex · group "Team"]'


def test_label_sanitizes_brackets_controls_bidi_quotes_and_length():
    message = {"from": {"first_name": 'A]\n\x00‮["' + "x" * 1000},
               "chat": {"type": "group", "title": 'T]\r\t ["' + "y" * 1000}}
    label = sender_label(message) + " Hello"
    assert label.startswith("[From: ") and label.endswith("] Hello")
    assert label.count("[") == 1 and label.count("]") == 1
    assert not any(unicodedata.category(c).startswith("C") or c in "\n\r\t " for c in label)
    assert len(label) < 200
    assert label.count('"') == 2


def test_disabled_is_unchanged_and_config_is_a_strict_boolean(tmp_path):
    lane = make_lane(tmp_path, enabled=False)
    lane._handle(incoming())
    assert lane._session.injected == ["Hello"]
    assert Config("bot", "codex", frozenset({1})).inbound_sender_label is False
    with pytest.raises(ValueError):
        Config("bot", "codex", frozenset({1}), inbound_sender_label="yes")


def test_voice_and_attachment_and_reply_context_preserve_current_sender(tmp_path):
    lane = make_lane(tmp_path)
    lane._transcribe = Mock(return_value="Synthetic voice")
    update = incoming(text="", voice={"file_id": "fixture"})
    lane._handle(update)
    text = lane._session.injected[-1]
    assert text.startswith("[From: Alex · DM] ") and "voice transcript" in text

    lane._download_note = Mock(return_value="[file: fixture]")
    update = incoming(document={"file_id": "fixture"},
                      reply_to_message={"text": "Another speaker", "from": {"first_name": "Robin"}})
    lane._handle(update)
    text = lane._session.injected[-1]
    assert text.startswith("[From: Alex · DM] ") and "[file: fixture]" in text


def test_unauthorized_sender_cannot_inject_a_label(tmp_path):
    lane = make_lane(tmp_path)
    mux = Multiplexer(Telegram(), {22}, tmp_path / "bot", lambda key: lane,
                      lane_states_root=tmp_path / "lanes")
    update = incoming()
    update["message"]["from"]["id"] = 99
    mux._handle_update_once(update, 0)
    assert lane._session.injected == []


def test_reaction_uses_reacting_sender(tmp_path):
    lane = make_lane(tmp_path)
    lane._handle({"message_reaction": {
        "user": {"id": 22, "first_name": "Alex"}, "chat": {"id": 22, "type": "private"},
        "message_id": 7, "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
    assert lane._session.injected[0].startswith("[From: Alex · DM] ❤ reacted")


def test_prefix_precedes_label_in_the_actual_tmux_payload(tmp_path):
    from agent2telegramtopic import session
    from agent2telegramtopic.session import TmuxSession
    from test_session import _FocusedComposerTmux

    lane = make_lane(tmp_path)
    lane._handle(incoming())
    s = object.__new__(TmuxSession)
    s.name = "fixture"
    s._origin = "[TG] "
    s._expected_agent_commands = ()
    calls = []
    with patch.object(session, "_tmux", side_effect=_FocusedComposerTmux(calls)), \
            patch.object(session.time, "sleep"):
        s._send_keys(lane._session.injected[-1])
    assert ("send-keys", "-t", "fixture", "-l", "--", "[TG] [From: Alex · DM] Hello") in calls


def test_missing_display_fields_have_bounded_generic_fallback():
    assert sender_label({"chat": {"type": "group"}}) == '[From: user · group "untitled"]'


def test_english_default_and_explicit_czech_compatibility():
    private = {"from": {"first_name": "Alex"}, "chat": {"type": "private"}}
    assert sender_label(private) == "[From: Alex · DM]"
    assert sender_label(private, language="cs") == "[Od: Alex · DM]"
    assert (sender_label({"chat": {"type": "group"}}, language="cs")
            == '[Od: uživatel · skupina "bez názvu"]')


def test_czech_injection_preserves_prefix_for_all_inbound_paths(tmp_path):
    lane = make_lane(tmp_path)
    lane.language = "cs"
    for media in ({}, {"voice": {"file_id": "fixture"}}, {"document": {"file_id": "fixture"}}):
        lane._transcribe = Mock(return_value="Synthetic voice")
        lane._download_note = Mock(return_value="[file: fixture]")
        update = incoming(**media)
        if media.get("voice"):
            update["message"]["text"] = ""
        lane._handle(update)
        assert lane._session.injected[-1].startswith("[Od: Alex · DM] ")
    lane._handle({"message_reaction": {
        "user": {"id": 22, "first_name": "Alex"}, "chat": {"id": 22, "type": "private"},
        "message_id": 7, "new_reaction": [{"type": "emoji", "emoji": "❤"}]}})
    assert lane._session.injected[-1].startswith("[Od: Alex · DM] ❤ reacted")
