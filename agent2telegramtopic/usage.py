"""Model usage limits for ``/usage``, read from data the agents already produce locally.

Claude Code: the bridge's statusLine command (``statusline.py``) stores ``rate_limits`` from the
status-line JSON in a small file. Codex: its rollout log carries ``rate_limits`` in every
``token_count`` event. Neither needs credentials or network access.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

STALE_LIMIT = 3600  # seconds after which captured data is flagged as stale
NO_DATA = "No usage data yet — it appears after the agent's first reply in this bridge."
_TAIL_BYTES = 4 * 1024 * 1024


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _label(minutes) -> str:
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "7-day"
    return f"{int(minutes)}-minute" if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) else "usage"


def write_claude_limits(path: Path, rate_limits: dict, captured_at: float) -> None:
    """Atomically store the 5-hour and 7-day windows of a Claude Code status-line payload."""
    data = {"captured_at": captured_at,
            "five_hour": rate_limits.get("five_hour"), "seven_day": rate_limits.get("seven_day")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                     delete=False) as tmp:
        tmp.write(json.dumps(data))
    os.replace(tmp.name, path)


def read_claude_limits(path: Path):
    """(captured_at, [(title, used_percent, resets_at_epoch)]) or None."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    windows = []
    for key, title in (("five_hour", "5-hour"), ("seven_day", "7-day")):
        window = data.get(key)
        used = _percent(window.get("used_percentage")) if isinstance(window, dict) else None
        if used is not None:
            windows.append((title, used, window.get("resets_at")))
    return (data.get("captured_at"), windows) if windows else None


def _event_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_codex_limits(rollout: Path | None, sessions_root: Path | None = None):
    """Latest ``token_count`` rate limits from ``rollout`` (or the newest rollout under
    ``sessions_root``): (event time, [(title, used_percent, resets_at_epoch)]) or None."""
    if rollout is None and sessions_root is not None:
        try:
            rollouts = [p for p in sessions_root.rglob("rollout-*.jsonl")]
            rollout = max(rollouts, key=lambda p: p.stat().st_mtime, default=None)
        except OSError:
            return None
    if rollout is None:
        return None
    try:
        with open(rollout, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            record = json.loads(line)
            payload = record["payload"]
            limits = payload["rate_limits"]
        except (ValueError, KeyError, TypeError):
            continue
        if payload.get("type") != "token_count" or not isinstance(limits, dict):
            continue
        windows = []
        for key in ("primary", "secondary"):
            window = limits.get(key)
            used = _percent(window.get("used_percent")) if isinstance(window, dict) else None
            if used is not None:
                windows.append((_label(window.get("window_minutes")), used, window.get("resets_at")))
        if windows:
            return _event_time(record.get("timestamp")), windows
    return None


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def format_usage(agent_label: str, found, now: float) -> str:
    if found is None:
        return f"📊 {agent_label} usage\n• {NO_DATA}"
    captured_at, windows = found
    lines = [f"📊 {agent_label} usage"]
    for title, used, resets_at in windows:
        if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
            reset = "resetting now" if resets_at <= now else f"resets in {_duration(resets_at - now)}"
        else:
            reset = "reset time unknown"
        lines.append(f"• {title} window: {used:.0f}% used, {reset}")
    if isinstance(captured_at, (int, float)) and not isinstance(captured_at, bool):
        age = max(0.0, now - captured_at)
        lines.append(f"• captured {_duration(age)} ago" + (" ⚠️ stale data" if age > STALE_LIMIT else ""))
    else:
        lines.append("• captured at an unknown time ⚠️ stale data")
    return "\n".join(lines)
