"""A tool result must never be mistaken for something the user typed.

Claude Code files tool results under `type: "user"`. The bridge decides from the last user text
whether the turn came from Telegram, so a tool result arriving mid-turn silently reclassified a
live Telegram turn as terminal-originated: the answer was never forwarded, the backstop never
ran, and NOTHING appeared in the log. Reproduced from the real transcript record on 2026-08-23,
where the agent read an image during the turn.
"""
import unittest
from pathlib import Path

from agent2telegramtopic.lane import LaneBridge
from agent2telegramtopic.readers import ClaudeCodeReader
from agent2telegramtopic.routing import LaneKey

# Shape taken verbatim from a real transcript, trimmed to the fields that matter.
TOOL_RESULT = {
    "type": "user",
    "toolUseResult": {"type": "image"},
    "message": {"content": [{"tool_use_id": "toolu_011EtqadNnd41qkrYeR45kvJ",
                             "type": "tool_result",
                             "content": "[Image: original 3300x1880, displayed at 2000x1139.]"}]},
}
# A tool result that does not carry the toolUseResult key — only the block type gives it away.
TOOL_RESULT_BLOCK_ONLY = {
    "type": "user",
    "message": {"content": [{"tool_use_id": "x", "type": "tool_result", "content": "output"}]},
}
REAL_PROMPT = {"type": "user", "message": {"content": "[TG] what is the status?"}}


class ToolResultIsNotAPromptTests(unittest.TestCase):
    def test_tool_result_is_not_user_text(self):
        self.assertIsNone(ClaudeCodeReader().user_text(TOOL_RESULT))
        self.assertIsNone(ClaudeCodeReader().user_text(TOOL_RESULT_BLOCK_ONLY))

    def test_tool_result_emits_no_user_event(self):
        self.assertEqual(list(ClaudeCodeReader().parse(TOOL_RESULT)), [])
        self.assertEqual(list(ClaudeCodeReader().parse(TOOL_RESULT_BLOCK_ONLY)), [])

    def test_a_real_prompt_still_counts(self):
        self.assertEqual(ClaudeCodeReader().user_text(REAL_PROMPT), "[TG] what is the status?")
        self.assertEqual([e.kind for e in ClaudeCodeReader().parse(REAL_PROMPT)], ["user"])


class TurnOriginTests(unittest.TestCase):
    """Second layer: even if some other synthetic record slips through, a running turn that is
    already known to come from Telegram must not be downgraded to local."""

    def _bridge(self):
        class Telegram:
            def __init__(self):
                self.sent = []

            def send_message(self, key, text):
                self.sent.append((key, text))

        self.telegram = Telegram()
        return LaneBridge(self.telegram, LaneKey(1, 2), self.tmp_path / "sent",
                          self.tmp_path / "queue")

    def _user(self, text):
        from agent2telegramtopic.readers import Ev
        return Ev("user", text=text)

    def setUp(self):
        import tempfile
        self.tmp_path = Path(tempfile.mkdtemp())

    def _text(self, text, key="reply-1"):
        from agent2telegramtopic.readers import Ev
        return Ev("text", text=text, key=key)

    def test_a_prefixed_message_marks_the_turn(self):
        b = self._bridge()
        b._handle_event(self._user("[TG] hello"))
        b._handle_event(self._text("answer"))
        b._flush_pending()
        self.assertEqual([text for _, text in self.telegram.sent], ["answer"])

    def test_an_unprefixed_record_mid_turn_does_not_downgrade_it(self):
        b = self._bridge()
        b._handle_event(self._user("[TG] hello"))
        b._turn_active.set()                       # the turn is running
        b._handle_event(self._user("[Image: 3300x1880 …]"))
        b._handle_event(self._text("answer"))
        b._flush_pending()
        self.assertEqual([text for _, text in self.telegram.sent], ["answer"],
                         "a mid-turn record reclassified a Telegram turn as local")

    def test_a_terminal_turn_is_still_recognised_as_local(self):
        b = self._bridge()
        b._handle_event(self._user("[TG] previous turn"))
        b._handle_event(self._user("just typed in the terminal"))
        b._handle_event(self._text("local answer"))
        b._flush_pending()
        self.assertEqual(self.telegram.sent, [])


if __name__ == "__main__":
    unittest.main()
