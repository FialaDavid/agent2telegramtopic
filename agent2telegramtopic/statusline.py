"""``python3 -m agent2telegramtopic statusline <outfile>``: Claude Code statusLine command.

Claude Code pipes a JSON document to it after each reply. When that document carries
``rate_limits`` they are stored in ``<outfile>`` for ``/usage``; a one-line status is printed.
Nothing here may ever fail loudly: a broken status line must not disturb the session.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from .usage import write_claude_limits


def _write_ctx(path: Path, cwd: str, size: int, captured_at: float) -> None:
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[cwd] = {"ctx_window": size, "at": int(captured_at)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as tmp:
        json.dump(data, tmp)
    os.replace(tmp.name, path)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    model = ""
    try:
        data = json.loads(sys.stdin.read())
        if isinstance(data, dict):
            name = (data.get("model") or {}).get("display_name") if isinstance(data.get("model"), dict) else None
            model = name if isinstance(name, str) else ""
            limits = data.get("rate_limits")
            if argv and isinstance(limits, dict):
                write_claude_limits(Path(argv[0]), limits, time.time())
            context = data.get("context_window")
            workspace = data.get("workspace")
            cwd = workspace.get("current_dir") if isinstance(workspace, dict) else None
            size = context.get("context_window_size") if isinstance(context, dict) else None
            if (argv and isinstance(cwd, str) and cwd and isinstance(size, int)
                    and not isinstance(size, bool) and size > 0):
                limits_path = Path(argv[0])
                if limits_path.name.endswith("claude_rate_limits.json"):
                    ctx_path = limits_path.with_name(limits_path.name.replace(
                        "claude_rate_limits.json", "claude_ctx.json"))
                    _write_ctx(ctx_path, str(Path(cwd).resolve()), size, time.time())
    except Exception:
        pass
    print(model)
    return 0
