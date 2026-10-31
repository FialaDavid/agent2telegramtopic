"""Archive records for topics closed with /close: ``index.jsonl`` plus one markdown file per day/topic."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .transcript import _json_lines

FIELDS = ("bot", "chat", "thread", "title", "agent", "session_id", "created_at",
          "closed_at", "transcript", "summary", "summary_error")


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def transcript_created_at(path: str | Path | None) -> str | None:
    """Earliest timestamp among the first records of a Claude or Codex transcript (None if none)."""
    if path is None:
        return None
    found = []
    for entry in _json_lines(path):
        raw = entry.get("timestamp") if isinstance(entry, dict) else None
        if not isinstance(raw, str):
            continue
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        found.append(moment)
        if len(found) >= 50:
            break
    return iso_utc(min(found)) if found else None


def _markdown_section(record: dict) -> str:
    lines = [f"## Closed {record['closed_at']}", ""]
    for field in FIELDS:
        if field != "summary":
            lines.append(f"- **{field}**: {record[field] if record[field] is not None else '-'}")
    lines += ["", "### Summary", "", record["summary"] if record["summary"] else "_(no summary)_", ""]
    return "\n".join(lines)


def write_archive(archive_dir: str | Path, record: dict) -> Path:
    """Append the record to ``index.jsonl`` and to the day's markdown file; never overwrite.

    Returns the markdown path. ``record`` must hold exactly the keys in FIELDS.
    """
    if set(record) != set(FIELDS):
        raise ValueError("archive record must have exactly the keys " + ", ".join(FIELDS))
    directory = Path(archive_dir)
    directory.mkdir(parents=True, exist_ok=True)
    line = (json.dumps({field: record[field] for field in FIELDS}, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(directory / "index.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    markdown = directory / f"{record['closed_at'][:10]}-{record['chat']}-{record['thread']}.md"
    title = f"# Archived topic {record['chat']}/{record['thread']}\n\n"
    fd = os.open(markdown, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        existing = os.fstat(fd).st_size > 0
        os.write(fd, (("\n" if existing else title) + _markdown_section(record)).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return markdown
