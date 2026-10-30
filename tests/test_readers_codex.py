"""CodexReader must survive the Codex log-format change (0.144 -> 0.149).

Old Codex logged an agent reply twice (event_msg/agent_message + response_item/message).
New Codex logs only the response_item form. Reading only the old form means the bridge
goes SILENT — typing indicator on, reply never delivered.
"""
import unittest

from agent2telegramtopic.readers import CodexReader


def _texts(reader, records):
    out = []
    for rec in records:
        for ev in reader.parse(rec):
            if ev.kind == "text":
                out.append(ev.text)
    return out


OLD = [
    {"type": "event_msg", "timestamp": "t1",
     "payload": {"type": "agent_message", "message": "Hello there."}},
    {"type": "response_item", "timestamp": "t1",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Hello there."}]}},
]

NEW = [
    {"type": "response_item", "timestamp": "t1",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Hello there."}]}},
]


class CodexReaderFormatTests(unittest.TestCase):
    def test_old_format_is_not_double_sent(self):
        self.assertEqual(_texts(CodexReader(), OLD), ["Hello there."])

    def test_new_format_still_delivers(self):
        self.assertEqual(_texts(CodexReader(), NEW), ["Hello there."])

    def test_user_and_developer_messages_are_not_treated_as_replies(self):
        recs = [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "output_text", "text": "a question"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                                  "content": [{"type": "output_text", "text": "system"}]}},
        ]
        self.assertEqual(_texts(CodexReader(), recs), [])

    def test_multipart_content_is_joined(self):
        recs = [{"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "part 1 "},
                        {"type": "output_text", "text": "part 2"}]}}]
        self.assertEqual(_texts(CodexReader(), recs), ["part 1 part 2"])

    def test_empty_or_malformed_payload_is_ignored(self):
        recs = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": "nope"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "   "}]}},
        ]
        self.assertEqual(_texts(CodexReader(), recs), [])

    def test_tool_calls_still_parsed(self):
        rec = {"type": "response_item", "payload": {"type": "function_call", "call_id": "c1",
                                                    "name": "shell", "arguments": "{}"}}
        kinds = [ev.kind for ev in CodexReader().parse(rec)]
        self.assertEqual(kinds, ["tool"])

    def test_final_repeating_earlier_commentary_is_not_content_deduplicated(self):
        reader = CodexReader()
        records = [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Still working."}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                "phase": "final_answer", "content": [{"type": "output_text", "text": "Still working."}]}},
        ]
        self.assertEqual(_texts(reader, records), ["Still working.", "Still working."])


if __name__ == "__main__":
    unittest.main()


class CodexReaderUserTurnTests(unittest.TestCase):
    """Recognising the Telegram prompt. Without it the bridge reads the reply and then discards it."""

    def _kinds(self, reader, records):
        return [ev.kind for rec in records for ev in reader.parse(rec)]

    def test_response_item_user_is_not_an_ownership_signal(self):
        rec = {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "AGENTS.md instructions"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["agents_md.instructions"]}}}
        self.assertIsNone(CodexReader().user_text(rec))

    def test_response_item_user_with_user_text_metadata_is_emitted(self):
        rec = {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "[TG] reply with PONG"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"]}}}
        reader = CodexReader()
        self.assertEqual(reader.user_text(rec), "[TG] reply with PONG")
        self.assertEqual([ev.text for ev in reader.parse(rec)], ["[TG] reply with PONG"])

    def test_mixed_instruction_and_user_blocks_only_extracts_user_text(self):
        rec = {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "hidden preamble"},
                        {"type": "input_text", "text": "[TG] actual"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["environments.environment_context", "user.text"]}}}
        self.assertEqual(CodexReader().user_text(rec), "[TG] actual")

    def test_malformed_user_metadata_and_content_are_ignored(self):
        base = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
        malformed = [
            {**base, "payload": {**base["payload"], "content": [],
                                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": "user.text"}}},
            {**base, "payload": {**base["payload"], "content": [{"type": "input_text", "text": "[TG] x"}],
                                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": []}}},
            {**base, "payload": {**base["payload"], "content": [{"type": "input_text", "text": 7}],
                                 "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]}}},
        ]
        for rec in malformed:
            self.assertIsNone(CodexReader().user_text(rec))

    def test_legacy_event_user_prompt_is_emitted(self):
        recs = [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "[TG] hi"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "[TG] hi"}},
        ]
        self.assertEqual(self._kinds(CodexReader(), recs), ["user"])

    def test_assistant_content_blocks_of_other_type_are_ignored(self):
        rec = {"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "input_text", "text": "this is not a reply"}]}}
        self.assertEqual(list(CodexReader().parse(rec)), [])

    def test_interactive_tui_user_item_marks_the_turn(self):
        rec = {"type": "event_msg", "payload": {"type": "item_completed"},
               "item": {"type": "UserMessage", "text": "[TG] ping"}}
        reader = CodexReader()
        self.assertEqual(reader.user_text(rec), "[TG] ping")
        self.assertEqual([ev.kind for ev in reader.parse(rec)], ["user"])

    def test_interactive_tui_preamble_is_not_a_user_turn(self):
        # Untyped response_item/user records retain safe behavior.
        rec = {"type": "response_item", "payload": {"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "AGENTS.md instructions"}]}}
        self.assertIsNone(CodexReader().user_text(rec))
