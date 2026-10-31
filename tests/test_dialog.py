from agent2telegramtopic import dialog

RULE = "─" * 80

NUDGE = f"""\
{RULE}
 We recommend compacting earlier

   Each message re-sends the whole conversation, so long conversations use up your plan's usage limits faster. With your current settings, Claude Code autocompacts (replaces older messages with a summary) at 1m tokens
   (default via auto setting). Compacting earlier can reduce usage, but it happens more often, and some details can be lost each time.

   Want to autocompact at 400k tokens instead? Your model doesn't change. Switch back any time with /autocompact.

   ❯ Keep auto (1m tokens)
     Autocompact at ‹ 400k › tokens

   ↑/↓ choose · ←/→ change size · enter confirm · esc not now
"""

TRUST = f"""\
some earlier output
{RULE}
 Do you trust the files in this folder?

 /Users/me/project

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

UNKNOWN = f"""\
{RULE}
 Something entirely new

 Type a value and press enter to continue
"""


def labels(pane):
    return [o.label for o in dialog.parse(pane).options]


def test_nudge_dialog_options_are_read_from_the_screen():
    dlg = dialog.parse(NUDGE)
    assert [o.label for o in dlg.options] == ["Keep auto (1m tokens)", "Autocompact at 400k tokens"]
    assert [o.key for o in dlg.options] == [None, None]
    assert dlg.highlighted == 0
    assert "We recommend compacting earlier" in dlg.body
    assert "Want to autocompact at 400k tokens instead?" in dlg.body
    assert "Keep auto" not in dlg.body and "esc not now" not in dlg.body


def test_numbered_dialog_has_one_option_per_row_with_its_digit():
    dlg = dialog.parse(TRUST)
    assert [(o.label, o.key) for o in dlg.options] == [("Yes, I trust this folder", "1"), ("No, exit", "2")]
    assert dlg.highlighted == 0
    assert "Do you trust the files" in dlg.body and "some earlier output" not in dlg.body


def test_unrecognised_dialog_has_no_options_but_keeps_its_text():
    dlg = dialog.parse(UNKNOWN)
    assert dlg.options == ()
    assert "Something entirely new" in dlg.body


def test_a_numbered_list_in_running_text_is_not_a_menu():
    pane = f"{RULE}\n Steps:\n 1. do this\n 2. do that\n"
    assert dialog.parse(pane).options == ()


def test_identity_ignores_the_moving_highlight_but_not_the_text():
    moved = NUDGE.replace("   ❯ Keep auto", "     Keep auto").replace("     Autocompact", "   ❯ Autocompact")
    assert dialog.parse(moved).digest == dialog.parse(NUDGE).digest
    assert dialog.parse(moved).highlighted == 1
    assert dialog.parse(NUDGE.replace("400k", "500k")).digest != dialog.parse(NUDGE).digest


MODEL_PICKER = f"""\
{RULE}
 Select model
 Switch between Claude models. Your pick becomes the default for new sessions. For other/previous model names, specify with --model.

     1.  Default (recommended)  Opus 5.5 · Best for everyday, complex tasks
   ❯ 2.  Opus 5.5 ✔             For complex work and everyday tasks
     3.  Sonnet 5.5             Most efficient for simpler tasks
     4.  Fable 5.1              For your toughest challenges
     5.  Haiku 5.5              Fastest for quick answers
     6.  Haiku 4.5              Fastest for quick answers
     7.  Sonnet 5               Efficient for routine tasks
     8.  Opus 5                 Best for everyday, complex tasks
     9.  Fable 5                Most capable for your hardest and longest-running tasks
   ↓ 10. Opus 4.8               Best for everyday, complex tasks
      … +3 models

   ◐ Medium effort (default) ←/→ to adjust

   Use /fast to turn on Fast mode (Opus 5.5).

   Enter to set as default · s to use this session only · Esc to cancel
"""


def test_model_picker_body_has_no_arrow_key_hints_and_keeps_the_numbered_models():
    dlg = dialog.parse(MODEL_PICKER)
    assert not any(a in dlg.body for a in "←→↑↓")
    assert "Select model" in dlg.body
    assert len(dlg.options) == 9
    assert [o.key for o in dlg.options] == [str(n) for n in range(1, 10)]
    assert dlg.highlighted == 1


def test_compact_dialog_body_has_no_arrow_key_hints():
    assert not any(a in dialog.parse(NUDGE).body for a in "←→↑↓")
