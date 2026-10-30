"""Background progress (opt-in): work that runs outside a Telegram turn is visible in the chat.

Port of agent2telegram tests/test_background_progress.py, adapted to LaneBridge/LaneKey: every
send, edit and delete is asserted to carry this lane's topic (message_thread_id).

Two sources: Claude Code sub-agents that keep working after the main turn ended (one message,
edited in place, deleted when they finish), and main-agent turns not started from Telegram (e.g.
a background-task notification), which get the usual tool status bubble. Neither may show
anything for history the bridge replays at startup.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent2telegramtopic import background as bg
from agent2telegramtopic.background import SubagentWatcher
from agent2telegramtopic.config import Config
from agent2telegramtopic.lane import LaneBridge, LaneTelegram
from agent2telegramtopic.readers import ClaudeCodeReader
from agent2telegramtopic.routing import LaneKey

KEY = LaneKey(7, 21)


class _FakeTG:
    def __init__(self):
        self.sent = []       # (mid, text)
        self.edits = []      # (mid, text)
        self.deleted = []    # mid
        self.keys = []       # the LaneKey every call was addressed to
        self.fail = False
        self._next = 100

    def send_plain_id(self, key, text, parse_mode=None):
        self.keys.append(key)
        if self.fail:
            raise RuntimeError("network down")
        self._next += 1
        self.sent.append((self._next, text))
        return self._next

    def edit_plain(self, key, message_id, text, parse_mode=None):
        self.keys.append(key)
        if self.fail:
            raise RuntimeError("network down")
        self.edits.append((message_id, text))

    def delete_message(self, key, message_id):
        self.keys.append(key)
        self.deleted.append(message_id)

    def send_chat_action(self, key, action):
        pass

    def send_message(self, key, text, parse_mode=None):
        self.keys.append(key)
        self.sent.append((None, text))


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".000Z"


def _user(text, t):
    return {"type": "user", "timestamp": _iso(t), "message": {"role": "user", "content": text}}


def _tool(name, inp, t, tid):
    return {"type": "assistant", "timestamp": _iso(t), "uuid": "u" + tid,
            "message": {"stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _text(text, t, stop="end_turn"):
    return {"type": "assistant", "timestamp": _iso(t), "uuid": "t%f" % t,
            "message": {"stop_reason": stop, "content": [{"type": "text", "text": text}]}}


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.main = self.root / "sess.jsonl"
        self.main.write_text("")
        self.sub = self.root / "sess" / "subagents"
        self.sub.mkdir(parents=True)
        self.clock = _Clock(time.time())
        self.tg = _FakeTG()
        self.state = self.root / "state" / "background_progress"
        self.w = SubagentWatcher(LaneTelegram(self.tg, KEY), 7, transcript=lambda: self.main, state_path=self.state,
                                 language="cs", edit_interval=10, stale_after=300,
                                 clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, aid, desc, *recs):
        (self.sub / f"agent-{aid}.meta.json").write_text(
            json.dumps({"description": desc, "requestShape": "background"}))
        self._append(aid, *recs)

    def _append(self, aid, *recs):
        p = self.sub / f"agent-{aid}.jsonl"
        with open(p, "a") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        os.utime(p, (self.clock.t, self.clock.t))

    def _advance(self, s):
        self.clock.t += s

    def test_new_subagent_creates_one_message(self):
        t0 = self.clock.t - 200
        self._agent("a1", "Build the thing", _user("do it", t0),
                    _tool("Bash", {"description": "Run the tests", "command": "x"}, self.clock.t, "k1"))
        self.w.tick()
        self.assertEqual(len(self.tg.sent), 1)
        text = self.tg.sent[0][1]
        self.assertIn("Na pozadí: Build the thing · 3:20", text)
        self.assertIn("🛠️ Run the tests", text)
        self.assertEqual(self.state.read_text(), str(self.tg.sent[0][0]), "message id not persisted")

    def test_tool_record_edits_in_place_throttled(self):
        self._agent("a1", "Job", _user("go", self.clock.t),
                    _tool("Read", {"file_path": "/x/a.py"}, self.clock.t, "k1"))
        self.w.tick()
        mid = self.tg.sent[0][0]
        self._advance(3)
        self._append("a1", _tool("Grep", {"pattern": "needle"}, self.clock.t, "k2"))
        self.w.tick()
        self.assertEqual(self.tg.edits, [], "edited before the throttle interval passed")
        self._advance(8)
        self.w.tick()
        self.assertEqual(len(self.tg.edits), 1)
        self.assertEqual(self.tg.edits[0][0], mid)
        self.assertIn("Searching needle", self.tg.edits[0][1])
        self.assertEqual(len(self.tg.sent), 1, "a second message instead of an edit")

    def test_final_answer_deletes_message(self):
        self._agent("a1", "Job", _user("go", self.clock.t),
                    _tool("Bash", {"description": "work"}, self.clock.t, "k1"))
        self.w.tick()
        mid = self.tg.sent[0][0]
        self._advance(2)
        self._append("a1", _text("All done, here is the report.", self.clock.t))
        self.w.tick()
        self.assertEqual(self.tg.deleted, [mid])
        self.assertFalse(self.state.exists())
        self.w.tick()                                    # finished stays finished
        self.assertEqual(len(self.tg.sent), 1)

    def test_intro_text_without_end_turn_is_not_final_yet(self):
        self._agent("a1", "Job", _user("go", self.clock.t),
                    _tool("Bash", {"description": "work"}, self.clock.t, "k1"))
        self.w.tick()
        self._advance(2)
        self._append("a1", _text("Now I will edit the file.", self.clock.t, stop=None))
        self.w.tick()
        self.assertEqual(self.tg.deleted, [], "an intro before a tool call ended the progress")
        self._advance(bg.FINISH_GRACE + 1)
        self.w.tick()
        self.assertEqual(len(self.tg.deleted), 1, "a silent text-only tail must count as finished")

    def test_silent_subagent_goes_stale_and_is_deleted(self):
        self._agent("a1", "Job", _user("go", self.clock.t),
                    _tool("Bash", {"description": "long build"}, self.clock.t, "k1"))
        self.w.tick()
        self._advance(200)
        self.w.tick()
        self.assertEqual(self.tg.deleted, [])
        self._advance(120)
        self.w.tick()
        self.assertEqual(len(self.tg.deleted), 1)

    def test_old_and_finished_subagents_are_ignored(self):
        self._agent("old", "Yesterday", _user("go", self.clock.t - 9999),
                    _tool("Bash", {"description": "x"}, self.clock.t - 9999, "k1"))
        p = self.sub / "agent-old.jsonl"
        os.utime(p, (self.clock.t - 9999, self.clock.t - 9999))
        self._agent("done", "Just finished", _user("go", self.clock.t),
                    _text("result", self.clock.t))
        self.w.tick()
        self.assertEqual(self.tg.sent, [])

    def test_two_subagents_share_one_message(self):
        self._agent("a1", "First", _user("go", self.clock.t - 5),
                    _tool("Bash", {"description": "one"}, self.clock.t, "k1"))
        self._agent("a2", "Second", _user("go", self.clock.t),
                    _tool("Bash", {"description": "two"}, self.clock.t, "k2"))
        self.w.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("First", self.tg.sent[0][1])
        self.assertIn("Second", self.tg.sent[0][1])
        self._advance(11)
        self._append("a1", _text("done", self.clock.t))
        self._append("a2", _tool("Bash", {"description": "three"}, self.clock.t, "k3"))
        self.w.tick()
        self.assertEqual(self.tg.deleted, [])
        self.assertNotIn("First", self.tg.edits[-1][1])
        self.assertIn("three", self.tg.edits[-1][1])

    def test_restart_deletes_orphan_message(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("4242")
        self.w.cleanup_orphan()
        self.assertEqual(self.tg.deleted, [4242])
        self.assertFalse(self.state.exists())

    def test_telegram_failure_does_not_raise(self):
        self._agent("a1", "Job", _user("go", self.clock.t),
                    _tool("Bash", {"description": "work"}, self.clock.t, "k1"))
        self.tg.fail = True
        self.w.tick()                                    # must not raise
        self.assertIsNone(self.w._mid)
        self.tg.fail = False
        self._advance(11)
        self.w.tick()
        self.assertEqual(len(self.tg.sent), 1, "no retry after the failure")

    def test_resumed_subagent_is_tracked_again(self):
        self._agent("a1", "Job", _user("go", self.clock.t), _text("done", self.clock.t))
        self.w.tick()
        self.assertEqual(self.tg.sent, [])
        self._advance(5)
        self._append("a1", _user("one more thing", self.clock.t),
                     _tool("Bash", {"description": "again"}, self.clock.t, "k9"))
        self.w.tick()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("again", self.tg.sent[0][1])


def _bridge(tmpdir, *, enabled=True, agent="claude-code"):
    state = Path(tmpdir) / "state"
    config = Config("test-bot", agent, frozenset({7}), background_progress=enabled)
    tg = _FakeTG()
    b = LaneBridge(tg, KEY, state / "sent", state / "queue", agent=agent, config=config)
    b._turn_end = Path(tmpdir) / "turn_end"
    b._transcript = Path(tmpdir) / "main.jsonl"
    b._transcript.write_text("")
    b._tpos = 0
    b._turn_started = time.monotonic()
    b._stop.wait = lambda _: None
    b._retry_last_assistant_text = lambda: None
    b._test_tg = tg
    return b


def _write(path, *recs):
    with open(path, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


class BackgroundTurnBubbleTests(unittest.TestCase):
    def test_live_notification_turn_shows_bubble_and_turn_end_deletes_it(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_from_tg = True                       # left over from the last Telegram turn
            b._arm_background()
            now = time.time()
            _write(b._transcript, _user("run the build and report", now),
                   _tool("Bash", {"description": "Check the result"}, now, "k1"))
            b._drain_transcript()
            self.assertEqual(len(b._test_tg.sent), 1)
            self.assertIn("Check the result", b._test_tg.sent[0][1])
            mid = b._test_tg.sent[0][0]
            _write(b._transcript, _tool("Read", {"file_path": "/a/report.md"}, now, "k2"))
            b._drain_transcript()
            self.assertEqual(b._test_tg.edits[-1][0], mid)
            b._turn_end.write_text("1")                  # the Stop hook fired
            b._end_turn()
            self.assertEqual(b._test_tg.deleted, [mid])
            self.assertFalse(b._bg_status)

    def test_startup_replay_shows_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            now = time.time()                            # recent, but already in the file
            _write(b._transcript, _user("<task-notification>x</task-notification>", now),
                   _tool("Bash", {"description": "Old step"}, now, "k1"))
            b._arm_background()                          # bridge starts here
            b._tpos = 0                                  # startup rewind re-reads the turn
            b._drain_transcript()
            self.assertEqual(b._test_tg.sent, [])

    def test_old_records_after_arm_show_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._arm_background()
            old = time.time() - 3600
            _write(b._transcript, _tool("Bash", {"description": "Stale"}, old, "k1"))
            b._drain_transcript()
            self.assertEqual(b._test_tg.sent, [])

    def test_disabled_by_config_shows_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d, enabled=False)
            b._arm_background()
            now = time.time()
            _write(b._transcript, _tool("Bash", {"description": "Work"}, now, "k1"))
            b._drain_transcript()
            self.assertEqual(b._test_tg.sent, [])
            b._start_background()
            self.assertIsNone(b._bg_watcher, "watcher started although disabled")

    def test_silent_background_bubble_expires(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._arm_background()
            _write(b._transcript, _tool("Bash", {"description": "Work"}, time.time(), "k1"))
            b._drain_transcript()
            mid = b._test_tg.sent[0][0]
            b._expire_background_status()
            self.assertEqual(b._test_tg.deleted, [])
            b._bg_status_last -= 301
            b._expire_background_status()
            self.assertEqual(b._test_tg.deleted, [mid])

    def test_telegram_turn_bubble_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._turn_from_tg = False
            b._status_push("🛠️ x")                        # no turn, not background → refused
            self.assertEqual(b._test_tg.sent, [])

    def test_codex_never_uses_background_path(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b.cfg.agent = "codex"
            self.assertFalse(b._background_enabled())


class TopicTests(unittest.TestCase):
    def test_every_progress_call_is_addressed_to_the_lanes_topic(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            b._arm_background()
            _write(b._transcript, _tool("Bash", {"description": "Work"}, time.time(), "k1"))
            b._turn_from_tg = False
            b._drain_transcript()
            b._turn_end.write_text("1")
            b._end_turn()
            self.assertTrue(b._test_tg.keys)
            self.assertEqual(set(b._test_tg.keys), {KEY})

    def test_two_lanes_progress_goes_to_their_own_thread(self):
        from agent2telegramtopic.telegram import TelegramClient
        from telegram_fakes import TransportOpener
        calls = []

        def transport(method, params):
            calls.append((method, params))
            return {"ok": True, "result": {"message_id": 100 + len(calls)}}
        client = TelegramClient("secret", opener=TransportOpener(transport))
        with tempfile.TemporaryDirectory() as d:
            for thread in (21, 22):
                root = Path(d) / str(thread)
                main = root / "sess.jsonl"
                (root / "sess" / "subagents").mkdir(parents=True)
                main.write_text("")
                agent = root / "sess" / "subagents" / "agent-a1.jsonl"
                agent.write_text(json.dumps(_tool("Bash", {"description": "work"}, time.time(), "k1")) + "\n")
                watcher = SubagentWatcher(LaneTelegram(client, LaneKey(7, thread)), 7,
                                          transcript=lambda main=main: main, state_path=root / "state")
                watcher.tick()
                watcher.close()
        self.assertEqual([(m, p["message_thread_id"]) for m, p in calls],
                         [("sendMessage", 21), ("deleteMessage", 21),
                          ("sendMessage", 22), ("deleteMessage", 22)])

    def test_lane_start_deletes_an_orphan_progress_message_even_when_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d, enabled=False)
            b._bg_state_path.parent.mkdir(parents=True, exist_ok=True)
            b._bg_state_path.write_text("4242")
            b._prepare_start()
            self.assertEqual(b._test_tg.deleted, [4242])
            self.assertIsNone(b._bg_watcher)

    def test_lane_start_runs_the_watcher_when_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bridge(d)
            ticked = threading.Event()
            b._prepare_start()
            self.assertIsNotNone(b._bg_watcher)
            original = b._bg_watcher.tick
            b._bg_watcher.tick = lambda: (ticked.set(), original())[0]
            self.assertTrue(ticked.wait(2))
            b._stop.set()
            self.assertEqual(b._bg_state_path.name, "background_progress_21")


class ConfigTests(unittest.TestCase):
    def test_default_off_and_validated(self):
        c = Config("test-bot", "claude-code", frozenset({7}))
        self.assertFalse(c.background_progress)
        self.assertRaises(ValueError, Config, "test-bot", "claude-code", frozenset({7}),
                          background_progress="yes")
        self.assertTrue(Config("test-bot", "claude-code", frozenset({7}),
                               background_progress=True).background_progress)


if __name__ == "__main__":
    unittest.main()
