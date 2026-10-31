"""Reading an interactive TUI dialog off the screen — pure functions, no tmux, no Telegram.

The bridge never decides anything for the user: it only turns the dialog on screen into labelled
choices, and every key it later sends is the result of one click. Whatever cannot be recognised
with certainty is left unrecognised (no options), never guessed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_RULE_RE = re.compile(r"^\s*─{20,}\s*$")
_MARKER_CHARS = "❯›"
_NUMBERED_RE = re.compile(r"^(\s*)([❯›]\s*)?(\d+)[.)]\s+(\S.*?)\s*$")
_MARKED_RE = re.compile(r"^(\s*[❯›]\s*)\S")
_HINT_RE = re.compile(r"(·.*\b(esc|enter)\b)|(^\s*press (enter|esc))", re.IGNORECASE)
_DECOR_RE = re.compile(r"[‹›]")
FALLBACK_TAIL_LINES = 20
MAX_BODY_CHARS = 3000


@dataclass(frozen=True)
class Option:
    label: str
    key: str | None      # digit key for numbered options; None = reach it with Up/Down + Enter


@dataclass(frozen=True)
class Dialog:
    digest: str                     # identity of the dialog, stable while the highlight moves
    body: str                       # question text for the bubble
    options: tuple[Option, ...]     # empty = not recognised
    highlighted: int | None         # index into options of the row carrying the marker


def _clean(label: str) -> str:
    return " ".join(_DECOR_RE.sub(" ", label).split())


def block_of(pane: str) -> list[str]:
    """The dialog block: everything after the last full-width rule, else the pane's tail."""
    lines = (pane or "").rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if _RULE_RE.match(lines[i]):
            return lines[i + 1:]
    return lines[-FALLBACK_TAIL_LINES:]


def _digest(lines: list[str]) -> str:
    norm = "\n".join(" ".join(_DECOR_RE.sub(" ", ln.replace("❯", " ").replace("›", " ")).split())
                     for ln in lines if ln.strip())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def _numbered(lines: list[str]) -> tuple[list[int], list[Option], int | None] | None:
    rows, options, marked = [], [], []
    for i, ln in enumerate(lines):
        m = _NUMBERED_RE.match(ln)
        if m:
            rows.append(i)
            if m.group(2):
                marked.append(len(options))
            options.append((int(m.group(3)), _clean(m.group(4))))
    # A numbered list in running text is not a menu: it needs a marker on exactly one row and
    # the numbers 1..n in order, each a single key press.
    if (len(options) < 2 or len(marked) != 1 or len(options) > 9
            or [n for n, _ in options] != list(range(1, len(options) + 1))):
        return None
    return rows, [Option(label, str(n)) for n, label in options], marked[0]


def _unnumbered(lines: list[str]) -> tuple[list[int], list[Option], int | None] | None:
    marked = [i for i, ln in enumerate(lines) if _MARKED_RE.match(ln) and not _HINT_RE.search(ln)]
    if len(marked) != 1:
        return None
    at = marked[0]
    col = len(_MARKED_RE.match(lines[at]).group(1))

    def same_column(ln: str) -> bool:
        return bool(ln.strip()) and not _HINT_RE.search(ln) and len(ln) - len(ln.lstrip()) == col

    first = at
    while first - 1 >= 0 and same_column(lines[first - 1]):
        first -= 1
    last = at
    while last + 1 < len(lines) and same_column(lines[last + 1]):
        last += 1
    rows = list(range(first, last + 1))
    if len(rows) < 2:
        return None
    labels = [_clean(lines[i].lstrip(" " + _MARKER_CHARS)) for i in rows]
    return rows, [Option(label, None) for label in labels], rows.index(at)


def parse(pane: str) -> Dialog:
    lines = block_of(pane)
    found = _numbered(lines) or _unnumbered(lines)
    rows, options, highlighted = found if found else ([], [], None)
    skip = set(rows)
    if found:
        skip |= {i for i, ln in enumerate(lines) if _HINT_RE.search(ln)}
    body: list[str] = []
    for i, ln in enumerate(lines):
        if i in skip:
            continue
        text = ln.strip()
        if text or (body and body[-1]):
            body.append(text)
    text = "\n".join(body).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS].rstrip() + "…"
    return Dialog(_digest(lines), text, tuple(options), highlighted)
