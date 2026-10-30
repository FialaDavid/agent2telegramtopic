"""``python3 -m agent2telegramtopic statusline <outfile>``: Claude Code statusLine command.

Claude Code pipes a JSON document to it after each reply. When that document carries
``rate_limits`` they are stored in ``<outfile>`` for ``/usage``; a one-line status is printed.
Nothing here may ever fail loudly: a broken status line must not disturb the session.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .usage import write_claude_limits


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
    except Exception:
        pass
    print(model)
    return 0
