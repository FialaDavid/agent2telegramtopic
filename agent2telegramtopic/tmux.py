"""Tmux lane driver for independent CLI agent sessions."""

import subprocess
import unicodedata


def session_name(
    agent_kind: str, topic_name: str | None, thread_id: int | str, chat_id: int | str,
) -> str:
    labels = {"claude-code": "Claude", "codex": "Codex"}
    if agent_kind not in labels:
        raise ValueError("agent_kind must be 'claude-code' or 'codex'")
    agent = labels[agent_kind]
    topic = topic_name if topic_name is not None else f"topic {thread_id}"
    safe = "".join(char for char in topic if unicodedata.category(char) != "Cc")
    return f"{agent} – {safe.replace(':', '-').replace('.', '-')} · {chat_id}"


def launch_argv(
    agent_kind: str, session_id: str, resume: bool, base_cmd: list[str] | tuple[str, ...],
    *, settings_path: str | None = None, cwd: str | None = None,
) -> list[str]:
    command = list(base_cmd)
    if agent_kind == "claude-code":
        if settings_path is not None:
            command += ["--settings", settings_path]
        return command + (["--resume", session_id] if resume else ["--session-id", session_id])
    if agent_kind == "codex":
        if resume:
            # Codex CLI 0.163.0 help lists --profile, bypass, and --cd on resume; order verified from help.
            command = [command[0], "resume", session_id, *command[1:]]
        if cwd is not None:
            command += ["--cd", cwd]
        return command
    raise ValueError("agent_kind must be 'claude-code' or 'codex'")
