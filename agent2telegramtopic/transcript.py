"""File-based readers for Claude and Codex lane transcripts."""

import json
from pathlib import Path
from typing import Any


def claude_transcript_path(
    session_id: str,
    cwd: str | Path,
    projects_root: Path = Path("~/.claude/projects").expanduser(),
) -> Path:
    """Return the Claude transcript path for a working directory and session."""
    project_dir = str(cwd).replace("/", "-").replace(".", "-")
    return projects_root / project_dir / f"{session_id}.jsonl"


def _json_lines(path: str | Path):
    try:
        with open(path, "rb") as transcript:
            for raw_line in transcript:
                try:
                    yield json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except FileNotFoundError:
        return


def claude_ctx_tokens(path: str | Path) -> int | None:
    """Return token usage from the latest Claude assistant entry with usage."""
    result = None
    for entry in _json_lines(path):
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        usage = message["usage"]
        result = sum(
            value if isinstance(value, (int, float)) else 0
            for value in (
                usage.get("input_tokens", 0),
                usage.get("cache_read_input_tokens", 0),
                usage.get("cache_creation_input_tokens", 0),
            )
        )
    return result


def claude_model(path: str | Path) -> str | None:
    """Return the latest real model id on a Claude assistant entry."""
    result = None
    for entry in _json_lines(path):
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != "<synthetic>":
            result = model
    return result


def codex_ctx_tokens(path: str | Path) -> int | None:
    """Return input token usage from the latest Codex token_count event."""
    result = None
    for entry in _json_lines(path):
        if not isinstance(entry, dict) or entry.get("type") != "event_msg":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        info = payload.get("info")
        if not isinstance(info, dict):
            continue
        usage = info.get("last_token_usage")
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if not isinstance(input_tokens, int):
            usage = info.get("total_token_usage")
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if isinstance(input_tokens, int):
            result = input_tokens
    return result


def codex_model(path: str | Path) -> str | None:
    """Return the latest model id in a Codex rollout turn_context entry."""
    result = None
    for entry in _json_lines(path):
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model:
            result = model
    return result


def _first_json_line(path: str | Path) -> Any | None:
    """Read and decode only the first line of a JSONL file."""
    try:
        with open(path, "rb") as transcript:
            line = transcript.readline()
        return json.loads(line)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def find_codex_rollout(
    sessions_root: Path,
    lane_cwd: str | Path,
    started_after: float,
) -> Path | None:
    """Find the newest recent Codex rollout belonging to a lane cwd."""
    try:
        expected_cwd = str(Path(lane_cwd).resolve())
        candidates = sessions_root.rglob("rollout-*.jsonl")
        matches: list[tuple[float, Path]] = []
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
                if mtime < started_after:
                    continue
                entry = _first_json_line(path)
            except OSError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "session_meta":
                continue
            payload = entry.get("payload")
            if (
                isinstance(payload, dict)
                and isinstance(payload.get("cwd"), str)
                and isinstance(payload.get("id"), str)
                and str(Path(payload["cwd"]).resolve()) == expected_cwd
            ):
                matches.append((mtime, path))
        return max(matches, key=lambda match: match[0])[1] if matches else None
    except OSError:
        return None


def codex_rollout_id(path: str | Path) -> str | None:
    """Return the rollout session id, falling back to its filename UUID."""
    entry = _first_json_line(path)
    if isinstance(entry, dict) and entry.get("type") == "session_meta":
        payload = entry.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("id"), str):
            return payload["id"]
    stem = Path(path).name
    if not stem.startswith("rollout-") or not stem.endswith(".jsonl"):
        return None
    groups = stem[:-6].split("-")[-5:]
    if len(groups) != 5 or not all(groups):
        return None
    return "-".join(groups)


def codex_rollout_path(sessions_root: Path, rollout_id: str) -> Path | None:
    """Find the newest rollout whose filename ends in the given id."""
    try:
        matches: list[tuple[float, Path]] = []
        for path in sessions_root.rglob(f"rollout-*-{rollout_id}.jsonl"):
            try:
                if path.is_file():
                    matches.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError:
        return None
    return max(matches, key=lambda match: match[0])[1] if matches else None
