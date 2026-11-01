# Ported from agent2telegram tests/test_session.py (MIT)
"""Tests for tmux session input handling."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from agent2telegramtopic import session
from agent2telegramtopic.session import (MAX_TMUX_INJECTION_CHARS, SessionError, TmuxSession,
                                    sanitize_for_tmux)


class SanitizeForTmuxTests(unittest.TestCase):
    def test_regular_text_is_unchanged(self):
        text = "Hello, world 123. Regular punctuation: []{};:,./?\n\tDone."
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_empty_text_stays_empty(self):
        self.assertEqual(sanitize_for_tmux(""), "")

    def test_unicode_is_preserved(self):
        text = "Grüße ✅ Привет こんにちは 🚀"
        self.assertEqual(sanitize_for_tmux(text), text)

    def test_control_characters_are_stripped_except_newline_and_tab(self):
        control_chars = "".join(chr(i) for i in [*range(0x20), 0x7f, *range(0x80, 0xa0)])
        self.assertEqual(sanitize_for_tmux(f"a{control_chars}b"), "a\t\nb")

    def test_text_is_truncated(self):
        text = "x" * (MAX_TMUX_INJECTION_CHARS + 1)
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)

    def test_truncation_counts_only_surviving_characters(self):
        text = ("\x00" * 100) + ("x" * (MAX_TMUX_INJECTION_CHARS + 50))
        self.assertEqual(sanitize_for_tmux(text), "x" * MAX_TMUX_INJECTION_CHARS)


class _FocusedComposerTmux:
    """A tmux stub whose pane shows a live, focused composer.

    The tests in this file are about ONE thing: may the bridge write into this pane, given what
    is running in it. They still have to hand the session a pane it can recognise as a composer,
    otherwise they pass or fail for the wrong reason — refusing to type into a screen with no
    input on it is correct behaviour, not the guard under test.
    """

    def __init__(self, calls, pane_values=None):
        self.calls = calls
        self.pending = ""
        self.pane_values = pane_values or {}     # pane identity queries, e.g. #{pane_current_command}

    def _pane(self):
        return f"─────────────\n❯ {self.pending}\n─────────────\n"

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        head = args[0]
        if head == "send-keys":
            if "-l" in args:
                self.pending = args[-1]
            elif args[-1] == "Enter":
                self.pending = ""
        if head == "display-message":
            if args[-1] in self.pane_values:
                return subprocess.CompletedProcess(args, 0, stdout=self.pane_values[args[-1]] + "\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=f"1 1\n{self._pane()}", stderr="")
        if head == "capture-pane":
            return subprocess.CompletedProcess(args, 0, stdout=self._pane(), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


class TmuxSessionSendKeysTests(unittest.TestCase):
    def _bare(self, expected=("codex",)):
        s = object.__new__(TmuxSession)
        s.name = "a2t-test"
        s._origin = ""
        s._expected_agent_commands = tuple(expected)
        return s

    @staticmethod
    def _cp(args, *, stdout="", returncode=0):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    def test_send_keys_passes_sanitized_literal_argument_to_tmux(self):
        calls = []
        tmux = _FocusedComposerTmux(calls)
        with patch.object(session, "_tmux", side_effect=tmux), patch.object(session.time, "sleep"):
            s = object.__new__(TmuxSession)
            s.name = "a2t-test"
            s._origin = "[TG] "
            s._expected_agent_commands = ()
            s._send_keys("hello\x0bthere\x03\x1b[31m\x7fworld\x85!")

        literal_calls = [c for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        self.assertEqual(len(literal_calls), 1)
        self.assertEqual(literal_calls[0][-1], "[TG] hellothere[31mworld!")

    def test_send_command_does_not_prepend_origin_prefix(self):
        s = self._bare()
        with (patch.object(TmuxSession, "alive", new_callable=PropertyMock, return_value=True),
              patch.object(TmuxSession, "_pane_ok", return_value=(True, "")),
              patch.object(TmuxSession, "_type_and_submit") as submit):
            s._origin = "[TG] "
            s.send_command("/compact")
        submit.assert_called_once_with("/compact")

    def test_inject_can_submit_without_origin_prefix(self):
        s = self._bare()
        with (patch.object(TmuxSession, "alive", new_callable=PropertyMock, return_value=True),
              patch.object(TmuxSession, "_pane_ok", return_value=(True, "")),
              patch.object(TmuxSession, "_type_and_submit") as submit):
            s._origin = "[TG] "
            s.inject("/compact", origin_prefix=False)
        submit.assert_called_once_with("/compact")

    def test_send_keys_collapses_newlines_to_one_literal_submit(self):
        calls = []
        tmux = _FocusedComposerTmux(calls)
        with patch.object(session, "_tmux", side_effect=tmux), patch.object(session.time, "sleep"):
            s = self._bare(())
            s._send_keys("first line\nsecond line\r\nthird\tline")

        literal = [c[-1] for c in calls if c[:5] == ("send-keys", "-t", "a2t-test", "-l", "--")]
        submits = [c for c in calls if c == ("send-keys", "-t", "a2t-test", "Enter")]
        self.assertEqual(literal, ["first line second line third\tline"])
        self.assertEqual(len(submits), 1)

    def test_allowed_agent_pane_allows_injection(self):
        calls = []

        tmux = _FocusedComposerTmux(calls, {"#{pane_current_command}": "codex"})

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            self._bare(("codex",)).inject("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_node_wrapper_for_expected_agent_allows_injection(self):
        calls = []

        tmux = _FocusedComposerTmux(calls, {"#{pane_current_command}": "node", "#{pane_pid}": "100"})

        ps_out = "\n".join([
            "100 1 /bin/zsh zsh",
            "101 100 /usr/local/bin/node node /opt/tools/codex/bin/codex.js",
        ])
        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.subprocess, "run", return_value=self._cp(["ps"], stdout=ps_out)), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_full_path_expected_agent_allows_basename_match(self):
        with patch.object(session, "_pane_value", return_value="/opt/homebrew/bin/codex"):
            ok, detail = session._agent_alive("a2t-test", ("/home/me/.local/bin/codex",))

        self.assertTrue(ok)
        self.assertIn("codex", detail)

    def test_python_wrapper_that_mentions_expected_agent_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "/bin/zsh", "zsh"),
            (101, 100, "/usr/bin/python3", "python3 /opt/wrappers/run_agent.py --agent codex"),
        ]

        tmux = _FocusedComposerTmux(calls)

        with patch.object(session, "_pane_value", return_value="python3"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("codex",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_pane_without_agent_child_blocks_injection_even_when_not_shell(self):
        calls = []
        processes = [(100, 1, "/usr/bin/vim", "vim README.md")]

        tmux = _FocusedComposerTmux(calls)

        with patch.object(session, "_pane_value", return_value="vim"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("codex",))._send_keys("echo pwned")

    def test_agent_exit_back_to_shell_refuses_injection(self):
        calls = []
        processes = [(100, 1, "/bin/zsh", "zsh")]
        tmux = _FocusedComposerTmux(calls, {"#{pane_current_command}": "zsh",
                                           "#{pane_pid}": "100"})

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            with self.assertRaises(SessionError):
                self._bare(("codex",)).inject("echo pwned")

        self.assertFalse(any(call[:3] == ("send-keys", "-t", "a2t-test") for call in calls))

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_leader_with_full_path_claude_child_allows_injection(self):
        calls = []
        processes = [
            (100, 1, "-sh", "-sh"),
            (
                101,
                100,
                "/home/you/.local/bin/claude",
                "/home/you/.local/bin/claude --resume abc123 --dangerously-skip-permissions",
            ),
        ]

        tmux = _FocusedComposerTmux(calls)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            self._bare(("claude",))._send_keys("hello")

        self.assertIn(("send-keys", "-t", "a2t-test", "Enter"), calls)

    def test_shell_leader_without_agent_child_blocks_injection(self):
        calls = []
        processes = [(100, 1, "-sh", "-sh")]

        tmux = _FocusedComposerTmux(calls)

        with patch.object(session, "_pane_value", return_value="2.1.185"), \
             patch.object(session, "_pane_processes", return_value=processes), \
             patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"):
            with self.assertRaises(SessionError):
                self._bare(("claude",))._send_keys("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])

    def test_shell_prompt_blocks_injection(self):
        calls = []

        def tmux(*args, **kwargs):
            calls.append(args)
            if args[:4] == ("display-message", "-p", "-t", "a2t-test"):
                return self._cp(args, stdout="zsh\n")
            return self._cp(args)

        with patch.object(session, "_tmux", side_effect=tmux), \
             patch.object(session.time, "sleep"), \
             patch.object(TmuxSession, "_exists", return_value=True):
            with self.assertRaises(SessionError):
                self._bare(("codex",)).inject("echo pwned")

        send_calls = [c for c in calls if c and c[0] == "send-keys"]
        self.assertEqual(send_calls, [])


class TmuxSessionCwdTests(unittest.TestCase):
    def test_new_session_starts_in_the_given_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "lane"
            with patch.object(session.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(TmuxSession, "_exists", return_value=False), \
                 patch.object(session, "_tmux") as tmux, \
                 patch.object(session.time, "sleep"):
                TmuxSession(["codex"], cwd=cwd, name="lane-test", boot_wait=0)

        tmux.assert_called_once_with(
            "new-session", "-d", "-s", "lane-test", "-c", str(cwd),
            "-x", "220", "-y", "50", "codex", timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
