import pytest

from agent2telegramtopic.tmux import launch_argv, session_name


class FakeRunner:
    def __init__(self, outputs=()):
        self.calls = []
        self.outputs = deque(outputs)

    def __call__(self, argv):
        self.calls.append(argv)
        return self.outputs.popleft() if self.outputs else (0, "")


def test_session_name_sanitizes_and_uses_thread_fallback():
    assert session_name("claude-code", "a:b.c\n", 7, 123) == "Claude – a-b-c · 123"
    assert session_name("codex", None, 42, 456) == "Codex – topic 42 · 456"
    with pytest.raises(ValueError):
        session_name("claude", "x", 1, 123)


def test_session_names_distinguish_same_thread_in_different_chats():
    first = session_name("claude-code", "topic 42", 42, 123)
    second = session_name("claude-code", "topic 42", 42, 456)
    assert first == "Claude – topic 42 · 123"
    assert second == "Claude – topic 42 · 456"
    assert first != second


def test_launch_argv_for_both_agents_and_resume_states():
    base = ["claude", "--model", "opus"]
    assert launch_argv("claude-code", "sid", False, base) == base + ["--session-id", "sid"]
    assert launch_argv("claude-code", "sid", True, base) == base + ["--resume", "sid"]
    assert launch_argv("claude-code", "sid", False, base, settings_path="/hooks.json") == base + ["--settings", "/hooks.json", "--session-id", "sid"]
    assert launch_argv("claude-code", "sid", True, base, settings_path="/hooks.json") == base + ["--settings", "/hooks.json", "--resume", "sid"]
    codex = ["codex", "--profile", "work"]
    assert launch_argv("codex", "rid", False, codex, cwd="/lane") == codex + ["--cd", "/lane"]
    assert launch_argv("codex", "rid", True, codex, cwd="/lane") == ["codex", "resume", "rid", "--profile", "work", "--cd", "/lane"]
    assert launch_argv("claude-code", "sid", False, ("claude", "--model", "opus")) == [
        "claude", "--model", "opus", "--session-id", "sid"
    ]
    with pytest.raises(ValueError):
        launch_argv("claude", "sid", False, base)
