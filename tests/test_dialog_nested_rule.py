from agent2telegramtopic.dialog import parse

PANE = """❯ [TG] Idealni by bylo pripravit ten navod ...
⏺ Opus 5.5's safeguards stopped the response above · continuing once with that noted
────────────────────────────────────────────────────────────────────────────────────────────
 ☐ Model switch
│ Opus 5.5's safeguards flagged this session. You may be seeing this for the first time: ...
│ these safeguards to reduce the amount of incorrectly flagged messages. Switch to Opus 4.8 and keep going whenever this happens? You can change this later in /config.
❯ 1. Switch automatically
     Continue on Opus 4.8 now, and switch without asking from now on
  2. Stay on Opus 5.5
     Stop here without switching, and ask me each time a message is flagged
  3. Type something.
────────────────────────────────────────────────────────────────────────────────────────────
  4. Chat about this
────────────────────────────────────────────────────────────────────────────────────────────
Enter to select · ↑/↓ to navigate
✻ Waiting for API response · will retry in 2m 40s · check your network"""


def test_nested_rule_recovers_full_menu_and_body():
    parsed = parse(PANE)
    assert [(o.key, o.label) for o in parsed.options] == [
        ("1", "Switch automatically"), ("2", "Stay on Opus 5.5"),
        ("3", "Type something."), ("4", "Chat about this")]
    assert parsed.highlighted == 0
    assert "Model switch" in parsed.body
    assert "Switch to Opus 4.8" in parsed.body
    assert "─" not in parsed.body
    moved = PANE.replace("❯ 1.", "  1.").replace("  2.", "❯ 2.")
    assert parse(moved).digest == parsed.digest


def test_valid_last_block_still_parses_as_before():
    pane = "Question?\n────────────────────────────────────────\n❯ 1. Yes\n  2. No\nEnter to select"
    parsed = parse(pane)
    assert [(o.key, o.label) for o in parsed.options] == [("1", "Yes"), ("2", "No")]
    assert parsed.highlighted == 0
    assert parsed.body == "Enter to select"
