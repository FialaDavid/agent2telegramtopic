# Ported from agent2telegram tests/test_session_submit.py (MIT)
"""Outcome tests for tmux composer submission.

These say what the USER gets — the message arrives, or the bridge admits it did not — never
which mechanism produced that. The 2026-08-25 losses happened because the bridge decided "this
is not a dialog" by matching the text of dialogs it already knew about; the pane shapes below
are copies of real captures (Claude Code 2.1.243/2.1.245, Codex 0.147.0) so the tests keep
holding if the recognizer is rewritten again.
"""
import itertools
import subprocess
import textwrap
import unittest
from unittest.mock import patch

from agent2telegramtopic import session
from agent2telegramtopic.session import (
    CLEAR, HOLDS, UNKNOWN, BLOCKED_BY_OVERLAY, SAFE_INJECTION_CHUNK_CHARS, UNCONFIRMED_SUBMIT,
    PaneView, SessionError, TmuxSession,
)

MESSAGE = "[TG] check the deployment notes and repair the statistics"

# ---- real pane shapes ----------------------------------------------------------------------
# Claude Code: two full-width rules around the prompt line; the cursor sits on the prompt line.
CLAUDE_EMPTY = PaneView("Done\n────────────────────────\n❯ \n────────────────────────\n", 2, True)
CLAUDE_HOLDS = PaneView(f"Done\n────────────────────────\n❯ {MESSAGE}\n────────────────────────\n", 2, True)
# A long message becomes a chip; nothing has been submitted while the chip is still there.
CLAUDE_CHIP = PaneView("output\n────────────────────────\n❯ [Pasted text #1 +42 lines]\n────────────────────────\n", 2, True)
# The previous message echoed into the transcript above an empty composer is not pending text.
CLAUDE_ECHO = PaneView(f"❯ {MESSAGE}\nWorking…\n────────────────────────\n❯ \n────────────────────────\n", 3, True)

# An open AskUserQuestion, as captured on an isolated instance. Note the two rules with the
# dialog body between them: the old parser read exactly that as "an empty composer".
ASK_USER_QUESTION = (
    "❯ Ask me whether I prefer tea or coffee\n"
    "─────────────────────────────────────\n"
    " ☐ Tea or Coffee\n"
    "\n"
    "Do you prefer tea or coffee?\n"
    "\n"
    "❯ 1. Tea\n"
    "     You prefer tea\n"
    "  2. Coffee\n"
    "     You prefer coffee\n"
    "  3. Type something.\n"
    "─────────────────────────────────────\n"
    "  4. Chat about this\n"
    "\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)
DIALOG_CURSOR_HIDDEN = PaneView(ASK_USER_QUESTION, 6, False)
# Same dialog on a TUI that leaves the cursor showing — one layer of evidence gone, the answer
# must not change.
DIALOG_CURSOR_VISIBLE = PaneView(ASK_USER_QUESTION, 6, True)

# Codex: a bare prompt line, no box at all — and its dialogs use the SAME marker as its composer.
CODEX_EMPTY = PaneView("• You have 1 usage limit reset available.\n\n› Find and fix a bug in @filename\n\n  gpt-5.6-sol high · ~/work\n", 2, True)
CODEX_HOLDS = PaneView(f"• You have 1 usage limit reset available.\n\n› {MESSAGE}\n\n  gpt-5.6-sol high · ~/work\n", 2, True)
CODEX_DIALOG = PaneView("  ✨ Update available! 0.147.0 -> 0.149.1\n\n› 1. Update now\n  2. Skip\n\n  Press enter to continue\n", 2, False)

# Codex collapses a paste of more than 1000 characters into a chip and never puts the text on
# screen at all — captured verbatim on an isolated Codex 0.147.0 instance, 2026-08-26, at every
# pane width from 30 to 200 columns.
LONG_MESSAGE = "[TG] " + "the deployment notes are long and worth reading. " * 25

# The 2026-09-02 loss on Claude Code: a burst over ~1000-1300 chars can land in the composer
# with its HEAD silently dropped — no chip, no error, just the tail on screen. Reproduced live
# (Claude Code v2.1.251, isolated tmux session) via both `send-keys -l` and `paste-buffer`, so it
# is not specific to one injection primitive. Retained length was consistently
# `len(message) - ~1022`, i.e. everything up to that point vanished.
MULTIPARAGRAPH_MESSAGE = "[TG] " + "\n\n".join(
    f"Odstavec {i}: " + "slovo " * 15 for i in range(20)
)
_CORRUPTED_TAIL = " ".join(MULTIPARAGRAPH_MESSAGE.splitlines())[-181:]
CLAUDE_CORRUPTED_HOLDS = PaneView(
    f"Done\n────────────────────────\n❯ {_CORRUPTED_TAIL}\n────────────────────────\n", 2, True)
CLAUDE_MULTIPARAGRAPH_HOLDS = PaneView(
    f"Done\n────────────────────────\n❯ {' '.join(MULTIPARAGRAPH_MESSAGE.splitlines())}\n"
    "────────────────────────\n", 2, True)
CODEX_CHIP = PaneView(
    "• You have 1 usage limit reset available.\n\n"
    f"› [Pasted Content {len(LONG_MESSAGE)} chars]\n\n  gpt-5.6-sol high · ~/work\n", 2, True)
# The same paste when tmux gets the opening characters in before the TUI recognises a burst:
# those stay on screen beside the chip, and only the rest of the message is collapsed.
CODEX_CHIP_WITH_LEAD = PaneView(
    f"› {LONG_MESSAGE[:2]}[Pasted Content {len(LONG_MESSAGE) - 2} chars]\n\n"
    "  gpt-5.6-sol high · ~/work\n", 0, True)
# A chip that is NOT the message we just typed — a paste somebody left in the composer.
CODEX_FOREIGN_CHIP = PaneView(
    "› [Pasted Content 42 chars]\n\n  gpt-5.6-sol high · ~/work\n", 0, True)

SURVEY_OVERLAY = PaneView(
    "output above\n╭──────────────────────────────╮\n"
    "│ How is Claude doing this session? (optional) │\n"
    "╰──────────────────────────────╯\n", 2, False)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, seconds=0.0):
        self.now += float(seconds or 0)

    def monotonic(self):
        self.now += .001
        return self.now


class _Tmux:
    """A pane you can actually drive: reads are stable, keys change what is on screen.

    `on_keys` maps a keystroke ("type", "Enter", "Escape", "C-u") to the pane that follows it;
    `reads` lets the pane change on its own between snapshots, the way a live TUI does.
    """

    def __init__(self, view, on_keys=None, reads=None):
        self.calls = []
        self._view = view
        self._on_keys = on_keys or {}
        self._reads = list(reads or [])

    def __call__(self, *args, check=True, timeout=10):
        self.calls.append(args)
        head = args[0]
        if head == "display-message":
            if self._reads:
                self._view = self._reads.pop(0)
            view = self._view
            flag = "1" if view.cursor_visible else "0"
            return subprocess.CompletedProcess(args, 0, f"{view.cursor_row} {flag}\n{view.text}", "")
        if head == "capture-pane":
            return subprocess.CompletedProcess(args, 0, self._view.text, "")
        if head == "send-keys":
            key = "type" if "-l" in args else args[-1]
            if key in self._on_keys:
                self._view = self._on_keys[key]
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def keys(self):
        return [a[-1] for a in self.calls if a[0] == "send-keys"]

    @property
    def typed(self):
        return [a[-1] for a in self.calls if a[0] == "send-keys" and "-l" in a]


def _session():
    s = object.__new__(TmuxSession)
    s.name = "sess"
    s._origin = ""
    s._expected_agent_commands = ()
    s._exists = lambda: True
    s._pane_ok = lambda: (True, "ok")
    return s


class _Harness:
    """Runs one submission against a driveable pane and reports what reached it."""

    def __init__(self, view, on_keys=None, reads=None):
        self.tmux = _Tmux(view, on_keys, reads)
        self.clock = _Clock()

    def __enter__(self):
        self._patches = [
            patch.object(session, "_tmux", self.tmux),
            patch.object(session.time, "sleep", self.clock.sleep),
            patch.object(session.time, "monotonic", self.clock.monotonic),
        ]
        for p in self._patches:
            p.start()
        return self.tmux

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


DELIVERS = {"type": CLAUDE_HOLDS, "Enter": CLAUDE_EMPTY}


class ComposerRecognition(unittest.TestCase):
    """What counts as a composer, stated as: could the bridge type here?"""

    def test_claude_composer_is_recognised(self):
        self.assertEqual(_session()._composer_state(MESSAGE, CLAUDE_EMPTY), CLEAR)
        self.assertEqual(_session()._composer_state(MESSAGE, CLAUDE_HOLDS), HOLDS)

    def test_codex_composer_is_recognised(self):
        self.assertEqual(_session()._composer_state(MESSAGE, CODEX_EMPTY), CLEAR)
        self.assertEqual(_session()._composer_state(MESSAGE, CODEX_HOLDS), HOLDS)

    def test_a_placeholder_hint_is_not_a_pending_message(self):
        self.assertEqual(_session()._composer_state(MESSAGE, CODEX_EMPTY), CLEAR)

    def test_a_pasted_chip_means_the_message_is_still_sitting_there(self):
        self.assertEqual(_session()._composer_state(MESSAGE, CLAUDE_CHIP), HOLDS)

    def test_a_message_hidden_inside_a_chip_is_still_in_the_composer(self):
        """Codex shows a character count instead of the text, so nothing of it is on screen."""
        for view in (CODEX_CHIP, CODEX_CHIP_WITH_LEAD):
            with self.subTest(view=view.text.splitlines()[0]):
                self.assertEqual(_session()._composer_state(LONG_MESSAGE, view), HOLDS)

    def test_an_echoed_earlier_message_above_the_composer_is_not_pending(self):
        self.assertEqual(_session()._composer_state(MESSAGE, CLAUDE_ECHO), CLEAR)

    def test_an_open_question_dialog_is_never_an_empty_composer(self):
        """The exact 2026-08-25 pane. 'Empty composer' here means 'your message is delivered'."""
        for view in (DIALOG_CURSOR_HIDDEN, DIALOG_CURSOR_VISIBLE, CODEX_DIALOG, SURVEY_OVERLAY):
            with self.subTest(view=view.text.splitlines()[0]):
                self.assertEqual(_session()._composer_state(MESSAGE, view), UNKNOWN)

    def test_a_pane_nobody_can_parse_is_not_an_empty_composer(self):
        self.assertEqual(
            _session()._composer_state(MESSAGE, PaneView("no delimiters\njust output", 1, True)),
            UNKNOWN)


class NothingIsTypedWhereItCannotBeSeen(unittest.TestCase):
    """A dialog swallows keystrokes silently, and Enter picks its highlighted option.

    Verified on an isolated Claude Code instance on 2026-08-25: a 38-character message typed into
    an open AskUserQuestion left no trace on screen, and the Enter that followed answered the
    question in the user's name. So: no keys at all unless a composer is on screen.
    """

    def test_a_message_arriving_during_a_dialog_is_not_typed_and_not_reported_sent(self):
        for view in (DIALOG_CURSOR_HIDDEN, DIALOG_CURSOR_VISIBLE, CODEX_DIALOG):
            with self.subTest(view=view.text.splitlines()[0]):
                with _Harness(view) as tmux:
                    with self.assertRaises(SessionError):
                        _session()._type_and_submit(MESSAGE)
                self.assertEqual(tmux.typed, [], "the message was typed into a dialog")
                self.assertNotIn("Enter", tmux.keys, "Enter would answer the dialog for the user")

    def test_the_reason_given_names_the_dialog_so_the_user_can_act_on_it(self):
        with _Harness(DIALOG_CURSOR_HIDDEN):
            with self.assertRaisesRegex(SessionError, BLOCKED_BY_OVERLAY):
                _session()._type_and_submit(MESSAGE)

    def test_a_chip_holding_something_other_than_our_message_is_not_submitted(self):
        """Enter would send whatever that paste is, in the user's name, and lose ours."""
        with _Harness(CODEX_EMPTY, on_keys={"type": CODEX_FOREIGN_CHIP}) as tmux:
            with self.assertRaisesRegex(SessionError, UNCONFIRMED_SUBMIT):
                _session()._type_and_submit(LONG_MESSAGE)
        self.assertNotIn("Enter", tmux.keys)

    def test_text_that_never_appears_in_the_composer_is_not_submitted(self):
        """The composer looked right, the text did not land in it — Enter is the dangerous move."""
        with _Harness(CLAUDE_EMPTY) as tmux:      # nothing we type ever shows up
            with self.assertRaisesRegex(SessionError, UNCONFIRMED_SUBMIT):
                _session()._type_and_submit(MESSAGE)
        self.assertNotIn("Enter", tmux.keys)


class UncertaintyIsNeverDelivery(unittest.TestCase):
    def test_a_composer_that_cannot_be_read_after_the_submit_is_not_a_delivery(self):
        with _Harness(PaneView("nothing recognisable here", 0, True)):
            self.assertFalse(_session()._confirm_submitted(MESSAGE))

    def test_a_dialog_appearing_right_after_the_submit_is_not_a_delivery(self):
        with _Harness(CLAUDE_HOLDS, reads=[CLAUDE_HOLDS, DIALOG_CURSOR_HIDDEN]):
            self.assertFalse(_session()._confirm_submitted(MESSAGE, attempts=1))

    def test_a_composer_seen_empty_stays_delivered_even_if_a_dialog_covers_it_afterwards(self):
        """The opposite error: refusing to admit a delivery we positively witnessed."""
        with _Harness(CLAUDE_EMPTY, reads=[CLAUDE_EMPTY] * 8 + [DIALOG_CURSOR_HIDDEN]):
            self.assertTrue(_session()._confirm_submitted(MESSAGE))

    def test_a_composer_still_holding_the_message_gets_another_enter(self):
        with _Harness(CLAUDE_HOLDS, reads=[CLAUDE_HOLDS, CLAUDE_HOLDS] + [CLAUDE_EMPTY] * 8) as tmux:
            self.assertTrue(_session()._confirm_submitted(MESSAGE, attempts=5))
        self.assertEqual(tmux.keys.count("Enter"), 2)

    def test_a_chipped_message_that_will_not_leave_the_composer_is_not_a_delivery(self):
        with _Harness(CODEX_CHIP):
            self.assertFalse(_session()._confirm_submitted(LONG_MESSAGE, attempts=1))

    def test_an_empty_codex_composer_after_a_chipped_message_is_a_delivery(self):
        with _Harness(CODEX_EMPTY):
            self.assertTrue(_session()._confirm_submitted(LONG_MESSAGE))

    def test_a_message_that_will_not_leave_the_composer_is_reported_undelivered(self):
        with _Harness(CLAUDE_HOLDS, on_keys={"type": CLAUDE_HOLDS}):
            with self.assertRaisesRegex(SessionError, UNCONFIRMED_SUBMIT):
                _session()._type_and_submit(MESSAGE)


class OrdinaryMessagesStillGetThrough(unittest.TestCase):
    def test_a_claude_message_is_typed_once_and_submitted(self):
        with _Harness(CLAUDE_EMPTY, on_keys=DELIVERS) as tmux:
            _session()._type_and_submit(MESSAGE)
        self.assertEqual(tmux.typed, [MESSAGE])
        self.assertEqual(tmux.keys.count("Enter"), 1)

    def test_a_codex_message_is_typed_once_and_submitted(self):
        with _Harness(CODEX_EMPTY, on_keys={"type": CODEX_HOLDS, "Enter": CODEX_EMPTY}) as tmux:
            _session()._type_and_submit(MESSAGE)
        self.assertEqual(tmux.typed, [MESSAGE])
        self.assertEqual(tmux.keys.count("Enter"), 1)

    def test_a_long_message_the_tui_collapses_into_a_chip_still_arrives(self):
        """The 2026-08-26 outage: every message over ~1000 characters was retried 15 times and
        then dead-lettered, because the composer showed a count where the text used to be."""
        for view in (CODEX_CHIP, CODEX_CHIP_WITH_LEAD):
            with self.subTest(view=view.text.splitlines()[0]):
                with _Harness(CODEX_EMPTY, on_keys={"type": view, "Enter": CODEX_EMPTY}) as tmux:
                    _session()._type_and_submit(LONG_MESSAGE)
                # Outcome, not mechanism: the full message reaches the pane. It may now arrive as
                # several `-l` bursts (session.py chunks anything long — see
                # SAFE_INJECTION_CHUNK_CHARS — to stay under a length that corrupts on Claude
                # Code), so concatenating what was typed is the right check, not counting calls.
                self.assertEqual("".join(tmux.typed), LONG_MESSAGE)
                self.assertEqual(tmux.keys.count("Enter"), 1)

    def test_a_known_harmless_overlay_is_escaped_and_the_message_still_arrives(self):
        with _Harness(SURVEY_OVERLAY, on_keys={"Escape": CLAUDE_EMPTY, **DELIVERS}) as tmux:
            _session()._type_and_submit(MESSAGE)
        self.assertIn("Escape", tmux.keys)
        self.assertEqual(tmux.typed, [MESSAGE])

    def test_a_command_is_sent_without_the_telegram_origin_prefix(self):
        s = _session()
        s._origin = "[TG] "
        typed = PaneView("─────────────\n❯ /new\n─────────────\n", 1, True)
        with _Harness(CLAUDE_EMPTY, on_keys={"type": typed, "Enter": CLAUDE_EMPTY}) as tmux:
            s.send_command("/new")
        self.assertEqual(tmux.typed, ["/new"])

    def test_a_pane_that_is_not_running_the_agent_is_never_written_to(self):
        s = _session()
        s._pane_ok = lambda: (False, "tmux pane is at a shell prompt (zsh)")
        with _Harness(CLAUDE_EMPTY) as tmux:
            with self.assertRaises(SessionError):
                s.send_command("/new")
        self.assertEqual([c for c in tmux.calls if c[0] == "send-keys"], [])


class LongMultiParagraphMessages(unittest.TestCase):
    """2026-09-02: David sent a 2000+ char, multi-paragraph message and the agent only ever saw
    the tail — no error, nothing in the log to say a word was missing. Reproduced live: a single
    `send-keys -l` burst over ~1000-1300 chars can corrupt in Claude Code's own composer, keeping
    only the tail. These two tests cover both halves of the fix: the burst gets split so the
    corruption is not triggered, AND a composer that dropped the head is no longer accepted as
    "delivered" if it still happens."""

    def test_a_long_message_is_sent_in_bursts_under_the_corrupting_length(self):
        on_keys = {"type": CLAUDE_MULTIPARAGRAPH_HOLDS, "Enter": CLAUDE_EMPTY}
        with _Harness(CLAUDE_EMPTY, on_keys=on_keys) as tmux:
            _session()._type_and_submit(MULTIPARAGRAPH_MESSAGE)
        # _type_and_submit collapses newlines before typing (one Enter must submit the whole
        # message) — the outcome to check is that collapsed text, unabridged, reaches the pane.
        self.assertEqual("".join(tmux.typed), " ".join(MULTIPARAGRAPH_MESSAGE.splitlines()))
        self.assertTrue(all(len(chunk) <= SAFE_INJECTION_CHUNK_CHARS for chunk in tmux.typed))
        self.assertGreater(len(tmux.typed), 1, "a message this long must be split, not sent whole")
        self.assertEqual(tmux.keys.count("Enter"), 1)

    def test_a_composer_that_dropped_the_head_is_not_mistaken_for_delivered(self):
        """The exact 2026-09-02 shape: only the tail of the message is on screen. An either/or
        head-or-tail check calls this HOLDS (the tail alone was "proof enough") and Enter goes
        out on truncated text with nothing to say so. Requiring both anchors must reject it."""
        self.assertEqual(
            _session()._composer_state(MULTIPARAGRAPH_MESSAGE, CLAUDE_CORRUPTED_HOLDS), CLEAR)

    def test_a_message_that_arrives_with_only_its_tail_is_a_loud_failure_not_a_silent_one(self):
        with _Harness(CLAUDE_EMPTY, on_keys={"type": CLAUDE_CORRUPTED_HOLDS}) as tmux:
            with self.assertRaisesRegex(SessionError, UNCONFIRMED_SUBMIT):
                _session()._type_and_submit(MULTIPARAGRAPH_MESSAGE)
        self.assertNotIn("Enter", tmux.keys, "a corrupted composer must never be submitted")


# ---- 2026-10-03: a message taller than the composer's own rendered window --------------------
# Live finding (isolated tmux session, Claude Code v2.1.288, 80x24 — the same geometry as
# 'Claude - Master'): a long single-line message, typed via the existing safe chunked bursts
# (so none of the LongMultiParagraphMessages corruption above is in play), lands intact in the
# composer's actual buffer, but the composer's own render shows only a WINDOW of it ending at
# the cursor — there is no tmux scrollback for this (an alternate-screen TUI; `history_size`
# measured 0 throughout). Ctrl-A/Ctrl-E (verified live) move the cursor to the true start/end of
# the wrapped line and the rendered window follows: the true head reappears on Ctrl-A. These
# fixtures are built from that measurement, not guessed.
_WRAP_WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit zlutoucky kun upel "
               "dabelske ody prilis stoucky ceril vodu").split()
# Every word carries its own position so no 24-char window repeats anywhere in the message —
# otherwise a coincidental match of the probe's head inside the TAIL (this word list cycles)
# would pass the test for the wrong reason, independent of the Ctrl-A/Ctrl-E scroll proof.
LONG_SINGLE_LINE = " ".join(
    f"{w}{i:04d}" for i, w in enumerate(itertools.islice(itertools.cycle(_WRAP_WORDS), 620)))
_ALL_WRAPPED_ROWS = textwrap.wrap(LONG_SINGLE_LINE, width=76, break_long_words=False)
_VISIBLE_COMPOSER_ROWS = 7     # measured: how many rows fit between this fixture's borders


def _composer_pane(rows, cursor_row_index):
    """One fake 'Done / border / composer rows / border' pane, à la CLAUDE_HOLDS above."""
    lines = [f"❯ {rows[0]}"] + [f"  {r}" for r in rows[1:]] if rows else ["❯ "]
    header = "Done\n────────────────────────\n"
    text = header + "\n".join(lines) + "\n────────────────────────\n"
    return PaneView(text, header.count("\n") + cursor_row_index, True)


# The composer right after typing: only the last _VISIBLE_COMPOSER_ROWS rows are rendered, cursor
# on the last one — the real screen shape for a message too tall for the pane.
CLAUDE_TALL_TAIL_VIEW = _composer_pane(
    _ALL_WRAPPED_ROWS[-_VISIBLE_COMPOSER_ROWS:], _VISIBLE_COMPOSER_ROWS - 1)
# The same buffer after Ctrl-A: the window follows the cursor back to the true start, so the
# prompt marker now sits on the message's REAL first row, not a row from somewhere in the middle.
CLAUDE_TALL_HEAD_VIEW = _composer_pane(_ALL_WRAPPED_ROWS[:_VISIBLE_COMPOSER_ROWS], 0)
CLAUDE_TALL_EMPTY = _composer_pane([], 0)


class _BusyBannerTmux:
    """Like `_Tmux` (the composer changes only in response to keys, via `on_keys`), but the
    text ABOVE the composer changes on every single read regardless of keys — a spinner or an
    elapsed-turn timer redrawing while the agent works, which is the normal case when a message
    arrives mid-turn. `cursor_row` is shifted down by one line to account for the banner."""

    def __init__(self, view, on_keys=None):
        self.calls = []
        self._view = view
        self._on_keys = on_keys or {}
        self._tick = 0

    def _rendered(self):
        self._tick += 1
        return f"✻ Thinking… {self._tick}s (busy spinner — redraws on every read)\n" + self._view.text

    def __call__(self, *args, check=True, timeout=10):
        self.calls.append(args)
        head = args[0]
        if head == "display-message":
            flag = "1" if self._view.cursor_visible else "0"
            return subprocess.CompletedProcess(
                args, 0, f"{self._view.cursor_row + 1} {flag}\n{self._rendered()}", "")
        if head == "capture-pane":
            return subprocess.CompletedProcess(args, 0, self._rendered(), "")
        if head == "send-keys":
            key = "type" if "-l" in args else args[-1]
            if key in self._on_keys:
                self._view = self._on_keys[key]
        return subprocess.CompletedProcess(args, 0, "", "")

    @property
    def keys(self):
        return [a[-1] for a in self.calls if a[0] == "send-keys"]

    @property
    def typed(self):
        return [a[-1] for a in self.calls if a[0] == "send-keys" and "-l" in a]


class ABusyPaneDoesNotBlockClearingOrSubmission(unittest.TestCase):
    """2026-10-03 Master review: `_clear_composer` originally compared the WHOLE pane before and
    after each Ctrl-U. A spinner or an elapsed-turn timer redraws something outside the composer
    on every single frame while the agent is mid-turn — a very common moment for a Telegram
    message to arrive — so the whole pane never stabilised, every `C-u` attempt was burned, and
    an ordinary SHORT message was reported undelivered purely because the agent happened to be
    busy. Clearing must be judged from the composer's own text only."""

    def test_a_short_message_is_typed_and_submitted_once_while_the_pane_is_busy(self):
        tmux = _BusyBannerTmux(CLAUDE_EMPTY, on_keys={"type": CLAUDE_HOLDS, "Enter": CLAUDE_EMPTY})
        with patch.object(session, "_tmux", tmux), patch.object(session.time, "sleep"):
            _session()._type_and_submit(MESSAGE)
        self.assertEqual(tmux.typed, [MESSAGE])
        self.assertEqual(tmux.keys.count("Enter"), 1)
        # A short message into an already-empty composer must cost only a handful of C-u
        # presses, busy or not — not the full attempts cap.
        self.assertLessEqual(tmux.keys.count("C-u"), 5)

    def test_clearing_proof_ignores_noise_outside_the_composer(self):
        """The composer itself never changes here (no key is mapped) — only the banner above it
        does, on every read. _clear_composer must still prove it empty almost immediately."""
        tmux = _BusyBannerTmux(CLAUDE_EMPTY, on_keys={})
        s = _session()
        with patch.object(session, "_tmux", tmux), patch.object(session.time, "sleep"):
            self.assertTrue(s._clear_composer())
        self.assertLessEqual(tmux.keys.count("C-u"), 2)


class ATallMessageIsNotMistakenForOneThatVanished(unittest.TestCase):
    """The message is really there, just taller than the pane can render in one screen. This
    must still be confirmed as delivered — and still reach the agent exactly once — without the
    'both ends visible in one static screen' proof that LongMultiParagraphMessages relies on to
    catch genuine corruption, since a tall-but-intact message legitimately fails that proof too."""

    def test_a_message_taller_than_the_pane_still_gets_through_once(self):
        on_keys = {
            "type": CLAUDE_TALL_TAIL_VIEW,
            "C-a": CLAUDE_TALL_HEAD_VIEW,
            "C-e": CLAUDE_TALL_TAIL_VIEW,
            "Enter": CLAUDE_TALL_EMPTY,
        }
        with _Harness(CLAUDE_EMPTY, on_keys=on_keys) as tmux:
            _session()._type_and_submit(LONG_SINGLE_LINE)
        self.assertEqual("".join(tmux.typed), LONG_SINGLE_LINE)
        self.assertEqual(tmux.keys.count("Enter"), 1, "must submit exactly once, not retry")
        self.assertIn("C-a", tmux.keys)
        self.assertIn("C-e", tmux.keys)
        # Whatever the scroll probe did, the cursor-restoring Ctrl-E must be the last key sent
        # before Enter — Enter must never go out with the cursor left mid-buffer.
        enter_idx = tmux.keys.index("Enter")
        self.assertEqual(tmux.keys[enter_idx - 1], "C-e")

    def test_scrolling_to_the_true_start_and_end_confirms_a_tall_message(self):
        with _Harness(CLAUDE_TALL_TAIL_VIEW, on_keys={
                "C-a": CLAUDE_TALL_HEAD_VIEW, "C-e": CLAUDE_TALL_TAIL_VIEW}):
            self.assertTrue(_session()._confirm_long_text_by_scrolling(LONG_SINGLE_LINE))

    def test_a_tail_only_view_that_does_not_reveal_a_matching_head_is_not_confirmed(self):
        """Ctrl-A is sent (the probe is attempted) but the head it reveals is not ours — this
        must read as "not proven", the same as the 2026-09-02 real corruption case, not as a
        pass just because *some* text showed up."""
        foreign_head = _composer_pane(["a completely different message"], 0)
        with _Harness(CLAUDE_TALL_TAIL_VIEW, on_keys={
                "C-a": foreign_head, "C-e": CLAUDE_TALL_TAIL_VIEW}):
            self.assertFalse(_session()._confirm_long_text_by_scrolling(LONG_SINGLE_LINE))


class _WrappedLineClearTmux:
    """Models the 2026-10-03 live finding: Ctrl-U on a long WRAPPED single-line composer clears
    only the current on-screen row, not the whole logical line — so a long message needs one
    Ctrl-U per wrapped row, not one. Each Ctrl-U here pops the composer's current last row; once
    none are left, further Ctrl-Us are no-ops (the empty placeholder, which is what a real empty
    composer repaints identically every time)."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = []
        self.c_u_presses = 0

    def _view(self):
        return _composer_pane(self._rows, max(0, len(self._rows) - 1)) if self._rows \
            else CLAUDE_TALL_EMPTY

    def __call__(self, *args, check=True, timeout=10):
        self.calls.append(args)
        head = args[0]
        if head == "send-keys" and args[-1] == "C-u":
            self.c_u_presses += 1
            if self._rows:
                self._rows.pop()
        if head == "display-message":
            view = self._view()
            flag = "1" if view.cursor_visible else "0"
            return subprocess.CompletedProcess(args, 0, f"{view.cursor_row} {flag}\n{view.text}", "")
        if head == "capture-pane":
            return subprocess.CompletedProcess(args, 0, self._view().text, "")
        return subprocess.CompletedProcess(args, 0, "", "")


class ClearingAWrappedLongLineNeedsMoreThanOneCtrlU(unittest.TestCase):
    """2026-10-03: the root cause behind retries leaving a previous attempt's text behind. A
    single Ctrl-U only erases Claude Code's current wrapped ROW, so a message spanning several
    wrapped rows needs that many Ctrl-Us to actually go empty — sending exactly one and trusting
    it (the old code) left real characters sitting in the buffer for the next attempt to type
    on top of."""

    def test_clearing_a_several_row_wrapped_line_takes_more_than_one_ctrl_u(self):
        fake = _WrappedLineClearTmux(_ALL_WRAPPED_ROWS[-_VISIBLE_COMPOSER_ROWS:])
        s = _session()
        with patch.object(session, "_tmux", fake), patch.object(session.time, "sleep"):
            self.assertTrue(s._clear_composer())
        self.assertGreater(fake.c_u_presses, 1, "one Ctrl-U cannot clear several wrapped rows")
        self.assertEqual(fake._rows, [], "the composer must end up genuinely empty")

    def test_clearing_an_already_empty_composer_sends_exactly_one_ctrl_u(self):
        fake = _WrappedLineClearTmux([])
        s = _session()
        with patch.object(session, "_tmux", fake), patch.object(session.time, "sleep"):
            self.assertTrue(s._clear_composer())
        self.assertEqual(fake.c_u_presses, 1)

    def test_a_composer_that_never_stabilises_is_reported_not_cleared_not_retried_forever(self):
        """Bounded: if the COMPOSER's own text keeps changing forever (a stuck pane, or C-u
        genuinely not reaching it), _clear_composer must give up and say so rather than loop
        without end or silently call it clear. `_pane_view` issues ONE chained tmux call
        (`display-message ... ; capture-pane ...`), so the fake has to answer that one call,
        not a separate `capture-pane` — unlike the pane-wide noise in ABusyPaneDoesNotBlock..."""
        calls = {"n": 0}

        def ever_changing(*args, check=True, timeout=10):
            if args and args[0] == "display-message":
                calls["n"] += 1
                # A different composer line on every read: never stabilises.
                pane = f"────\n❯ frame {calls['n']}\n────\n"
                return subprocess.CompletedProcess(args, 0, f"1 1\n{pane}", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        s = _session()
        with patch.object(session, "_tmux", ever_changing), patch.object(session.time, "sleep"):
            self.assertFalse(s._clear_composer(attempts=5))
        # Bounded: a fixed, small number of tries, not an unbounded retry loop. One extra read
        # is the initial "prev" sample before the attempt loop starts sending C-u.
        self.assertLessEqual(calls["n"], 6)

    def test_a_retry_refuses_to_type_over_a_composer_it_could_not_prove_empty(self):
        """Criterion: before any re-type, the composer must be proven empty, or the attempt is
        refused outright — never typed over leftover text from a previous attempt."""

        def never_settles(*args, check=True, timeout=10):
            head = args[0]
            if head == "display-message":
                never_settles.n += 1
                # The COMPOSER's own row changes on every single read — this must look like a
                # pane that genuinely never goes empty, not just unrelated noise elsewhere.
                pane = f"Done\n────────────────────────\n❯ frame{never_settles.n}\n────────────────────────\n"
                return subprocess.CompletedProcess(args, 0, f"2 1\n{pane}", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        never_settles.n = 0
        s = _session()
        with patch.object(session, "_tmux", never_settles), patch.object(session.time, "sleep"):
            with self.assertRaisesRegex(SessionError, UNCONFIRMED_SUBMIT):
                s._type_and_submit(MESSAGE)


if __name__ == "__main__":
    unittest.main()
