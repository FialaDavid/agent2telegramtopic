# Ported from agent2telegram agent2telegram/readers.py (MIT)
"""Transcript readers: turn an agent's session log into one normalized event stream.

``AttachBridge`` is agent-agnostic — it does Telegram I/O (progress messages, the live tool
status bubble, the typing indicator) the same way for every agent. Each reader here knows the
on-disk transcript format of one agent and maps its records to a common set of events:

  ``turn_start`` — a new turn began. Codex writes ``task_started``; Claude Code has no such
                   record, so for it the bridge starts the turn from the inbound message.
  ``user``       — a user message (``Ev.text``). Used to detect whether the turn came from
                   Telegram (origin prefix) so only those turns are forwarded.
  ``text``       — assistant text to forward as a kept progress/final message. ``Ev.key`` is a
                   stable dedup id; ``Ev.final`` hints this is the final answer.
  ``tool``       — a tool/command call, summarized for the one-line status bubble. ``Ev.key`` is
                   the call id (so the same call isn't pushed twice).
  ``turn_end``   — the turn finished. Codex writes ``task_complete`` (so no Stop hook is needed!);
                   Claude Code has none, so the bridge ends the turn via its Stop-hook marker / idle.
  ``error``      — the AGENT ITSELF reported that it could not answer (usage limit reached, not
                   logged in, upstream 5xx). ``Ev.text`` is the agent's own wording, ``Ev.code`` a
                   normalized reason both TUIs share. This is agent STATE, read from the record the
                   agent wrote about its own failure — never a guess from the wording of a reply.

Why an abstraction instead of an ``if agent == ...`` in the bridge: the two formats differ a lot
(Claude = one assistant record with text+tool_use blocks and a uuid; Codex = separate event_msg /
response_item lines, no uuid, but an explicit task_complete). Keeping that knowledge in small
readers makes the bridge identical for both and easy to extend to a third agent later.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
import os
import urllib.parse
from dataclasses import dataclass
from .redaction import redact


@dataclass
class Ev:
    kind: str               # turn_start | user | text | tool | files | turn_end | error
    text: str = ""          # message / tool-summary text
    key: str = ""           # stable dedup id (text uuid/hash, tool call id)
    final: bool = False      # for 'text': hint that this is the final answer
    files: tuple = ()        # for 'files': paths the agent wants delivered to the user
    code: str = ""          # for 'error': normalized reason (see USAGE_LIMIT)


def _short(s: str, n: int = 58) -> str:
    s = " ".join(redact(s).split()).replace("**", "").replace("`", "")
    return s if len(s) <= n else s[:n - 1] + "…"


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


#: Normalized ``Ev.code`` for "the account ran out of quota, so the turn was refused". Both TUIs
#: record this as machine-readable state next to the human sentence — Codex as
#: ``task_complete.error.codex_error_info``, Claude Code as ``error``/``apiErrorStatus`` on a
#: synthetic assistant record. Readers map those onto this one value so the bridge never has to
#: look at the *wording* of a message to tell "the agent is out of quota" from "the agent is
#: talking about quota".
USAGE_LIMIT = "usage_limit"


# --------------------------------------------------------------------------- Claude Code

def _claude_tool_summary(name: str, inp: dict) -> str:
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "🛠️ " + _short(inp.get("description") or inp.get("command", "command"))
    if name == "Read":
        return "📄 Reading " + _short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Edit", "Write", "NotebookEdit"):
        return "✏️ Editing " + _short(os.path.basename(inp.get("file_path", "")) or "file")
    if name in ("Grep", "Glob"):
        return "🔎 Searching " + _short(inp.get("pattern", ""))
    if name == "WebFetch":
        try:
            host = urllib.parse.urlparse(inp.get("url", "")).hostname or inp.get("url", "")
        except Exception:
            host = inp.get("url", "")
        return "🌐 Web " + _short(host)
    if name == "WebSearch":
        return "🔎 Web search: " + _short(inp.get("query", ""))
    if name in ("Agent", "Task"):
        return "🤖 " + _short(inp.get("description") or "subagent")
    if name.startswith("mcp__"):
        return "🔌 " + _short(name.replace("mcp__", "").replace("__", " "))
    return "🛠️ " + _short(name or "tool")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""



#: Tools by which an agent hands a FILE to the user. The harness runs them itself, so the bridge
#: never sees a call — only its record in the transcript. Without this the file silently vanishes:
#: the reply arrives, the attachment does not, and nothing anywhere says so (2026-08-24).
#: Recognising the tool by name is deliberate — it beats guessing from prose, which would be
#: language-dependent and full of false positives.
FILE_SEND_TOOLS = {"senduserfile"}


def _file_tool_paths(name: str, inp) -> tuple:
    """Paths a file-sending tool call asks to deliver, or () when it is not such a call."""
    if str(name or "").strip().lower() not in FILE_SEND_TOOLS:
        return ()
    if not isinstance(inp, dict):
        return ()
    raw = inp.get("files") or inp.get("file") or inp.get("paths") or inp.get("path")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(p for p in raw if isinstance(p, str) and p.strip())


def _is_synthetic_user(rec: dict) -> bool:
    """Synthetic harness records must never decide a conversation's origin."""
    if rec.get("isSidechain") or rec.get("isCompactSummary") or rec.get("isMeta"):
        return True
    return _text_of((rec.get("message") or {}).get("content")).lstrip().startswith(
        ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION", "<command-name>",
         "<local-command-"))


def _queued_command_prompt(rec: dict) -> str | None:
    """Return a human Claude queued-command attachment exactly once.

    Claude 2.1.240 records a mid-turn steering prompt as ``attachment/queued_command``;
    the surrounding queue-operation entries are bookkeeping duplicates, not user turns.
    """
    if rec.get("type") != "attachment" or rec.get("isSidechain"):
        return None
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    prompt = att.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return None if prompt.lstrip().startswith(
        ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION")) else prompt


class ClaudeCodeReader:
    """Claude Code transcript (one JSONL record per message; assistant records carry text and
    tool_use blocks; user records may be real messages or tool results). No turn boundaries in
    the file — the bridge handles those via the Stop-hook marker and idle fallback."""

    name = "claude-code"
    emits_turn_end = False

    @staticmethod
    def _is_tool_result(rec: dict) -> bool:
        """A ``user`` record that is really a TOOL RESULT, not something the person typed.

        Claude Code files tool results under ``type: "user"``. They are indistinguishable from a
        real prompt by type alone, and treating one as a prompt is not cosmetic: the bridge
        decides from the last user text whether the turn came from Telegram, so a tool result
        silently reclassified a live Telegram turn as terminal-originated and the answer was
        never forwarded — no error, no backstop, nothing in the log (2026-08-23, an image the
        agent read mid-turn).
        """
        if "toolUseResult" in rec:
            return True
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return False

    @staticmethod
    def _is_harness_record(rec: dict) -> bool:
        """A ``user`` record the HARNESS wrote, not the person: the compaction summary
        (``isCompactSummary``), transcript-only notes (``isVisibleInTranscriptOnly``) and meta
        records (``isMeta``). Treating one as a prompt is the same trap as a tool result: it has
        no origin prefix, so it reclassified a live Telegram turn as terminal-originated and the
        real answer that followed was dropped without a log line (2026-09-02 22:25, right after
        a context compaction)."""
        return any(rec.get(k) for k in ("isCompactSummary", "isVisibleInTranscriptOnly", "isMeta"))

    def user_text(self, rec: dict) -> str | None:
        if rec.get("type") != "user":
            return _queued_command_prompt(rec)
        if self._is_tool_result(rec) or self._is_harness_record(rec) or _is_synthetic_user(rec):
            return None
        return _text_of(rec.get("message", {}).get("content"))

    def parse(self, rec: dict):
        typ = rec.get("type")
        if typ == "user":
            if self._is_tool_result(rec) or self._is_harness_record(rec) or _is_synthetic_user(rec):
                return                      # a tool result / harness note is not something the person typed
            t = _text_of(rec.get("message", {}).get("content"))
            if t.strip():
                yield Ev("user", text=t)
            return
        if typ != "assistant":
            return
        if rec.get("isApiErrorMessage"):
            # Claude Code files "I could not run this turn" as a SYNTHETIC assistant record
            # (model "<synthetic>") carrying the reason in `error` / `apiErrorStatus`. Forwarding
            # its text as an ordinary reply hid what it was; the bridge needs the state, so it can
            # say the turn was refused instead of pretending the agent answered.
            reason = str(rec.get("error") or "")
            code = USAGE_LIMIT if (reason == "rate_limit" or rec.get("apiErrorStatus") == 429) else reason
            yield Ev("error", text=_text_of(rec.get("message", {}).get("content")).strip(),
                     key=rec.get("uuid", "") or _hash(reason + str(rec.get("timestamp", ""))),
                     code=code)
            return
        blocks = rec.get("message", {}).get("content")
        blocks = blocks if isinstance(blocks, list) else []
        # Text first, then tool calls — so a progress message clears the previous bubble before
        # the next call re-creates it below (the bridge relies on this order).
        text = "\n".join(b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        if text:
            yield Ev("text", text=text, key=rec.get("uuid", "") or _hash(text))
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tid = b.get("id")
                if not tid:
                    continue
                paths = _file_tool_paths(b.get("name", ""), b.get("input"))
                if paths:
                    yield Ev("files", key=tid, files=paths)
                    continue                      # not a tool bubble — it is an attachment
                yield Ev("tool", text=_claude_tool_summary(b.get("name", ""), b.get("input")), key=tid)


# --------------------------------------------------------------------------- Codex

def _codex_tool_summary(payload: dict) -> str:
    pt = payload.get("type")
    if pt == "function_call":
        name = payload.get("name", "")
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except Exception:
            args = {}
        if name in ("exec_command", "shell", "local_shell", "container.exec"):
            cmd = args.get("cmd") or args.get("command") or ""
            if isinstance(cmd, list):
                cmd = " ".join(str(c) for c in cmd)
            return "🛠️ " + _short(cmd or name)
        if name in ("read_file", "view"):
            return "📄 Reading " + _short(os.path.basename(args.get("path", "")) or "file")
        if name.startswith("mcp"):
            return "🔌 " + _short(name)
        return "🛠️ " + _short(name or "tool")
    if pt == "custom_tool_call":
        name = payload.get("name", "tool")
        if name == "apply_patch":
            return "✏️ " + _short("apply_patch")
        return "🛠️ " + _short(name)
    if pt == "web_search_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        q = action.get("query") or (action.get("queries") or [""])[0] or ""
        return "🔎 Web search: " + _short(q) if q else "🔎 Searching the web"
    return "🛠️ tool"


def _codex_message_text(payload: dict, block_type: str) -> str:
    """Join the ``block_type`` parts of a Codex ``response_item/message`` payload.

    Codex >= 0.149 records user prompts (``input_text``) and assistant replies (``output_text``)
    only in this form; the legacy ``event_msg`` records are gone.
    """
    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""
    return "".join(
        part.get("text") or "" for part in parts
        if isinstance(part, dict) and part.get("type") == block_type
    ).strip()


def _codex_user_message_text(payload: dict) -> str:
    """Extract a genuine Codex response_item/user message using trusted block metadata.

    The interactive TUI also writes an injected instruction preamble as a user-role
    response_item.  ``content_item_kinds`` is parallel to ``content`` and distinguishes
    that preamble from the actual ``user.text`` input block.
    """
    if not isinstance(payload, dict) or payload.get("type") != "message" \
            or payload.get("role") != "user":
        return ""
    parts = payload.get("content")
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(parts, list) or not isinstance(kinds, list) or len(parts) != len(kinds):
        return ""
    texts = []
    for part, kind in zip(parts, kinds):
        if kind != "user.text" or not isinstance(part, dict) or part.get("type") != "input_text":
            continue
        value = part.get("text")
        if isinstance(value, str):
            texts.append(value)
    return "".join(texts).strip()


class CodexReader:
    """Codex CLI rollout transcript (``~/.codex/sessions/.../rollout-*.jsonl``). Each line is an
    ``event_msg`` or ``response_item`` with a ``payload.type``. Crucially it records explicit
    ``task_started`` / ``task_complete`` events, so turn boundaries (and thus the typing
    indicator and bubble cleanup) need no external Stop hook."""

    name = "codex"
    emits_turn_end = True

    def __init__(self) -> None:
        # Codex <= 0.144 logs an agent reply TWICE: first as ``event_msg/agent_message``, then as
        # ``response_item/message`` (role=assistant). Newer Codex (>= 0.149) logs ONLY the second
        # form. We therefore read both and remember what we already emitted, so old versions do not
        # double-send and new versions do not go silent. Silence is the dangerous failure here: the
        # bridge keeps showing "typing" while the reply never arrives (reported 2026-08-23).
        # A bounded window, not a session-wide set: the two records of one reply sit next to each
        # other in the log, while an agent legitimately repeating the same sentence later in the
        # session must still be delivered.
        # The legacy record is followed immediately by the response_item copy of the *same*
        # reply.  Keep only that pending adjacent duplicate, rather than a broad content window:
        # a final answer may legitimately repeat a prior commentary sentence and must still be
        # delivered as the task's final response.
        self._legacy_message_duplicate: str | None = None
        self._recent_users: deque = deque(maxlen=8)

    def user_text(self, rec: dict) -> str | None:
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        # Interactive Codex TUI (unlike `codex exec`) records real user turns as an
        # item completion.  Do not use response_item/user as the primary signal: it
        # also contains the injected instruction preamble at session start.
        item = rec.get("item") if isinstance(rec.get("item"), dict) else {}
        if (rec.get("type") == "event_msg" and p.get("type") == "item_completed"
                and item.get("type") == "UserMessage"):
            return item.get("text") or None
        if rec.get("type") == "event_msg" and p.get("type") == "user_message":
            return p.get("message", "")
        msg = _codex_user_message_text(p) if rec.get("type") == "response_item" else ""
        return msg or None

    def parse(self, rec: dict):
        t = rec.get("type")
        p = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        pt = p.get("type")
        if t == "event_msg" and pt == "task_started":
            self._legacy_message_duplicate = None
            yield Ev("turn_start")
        elif (t == "event_msg" and pt == "item_completed"
              and isinstance(rec.get("item"), dict)
              and rec["item"].get("type") == "UserMessage"):
            msg = rec["item"].get("text") or ""
            if msg.strip() and _hash(msg) not in self._recent_users:
                self._legacy_message_duplicate = None
                self._recent_users.append(_hash(msg))
                yield Ev("user", text=msg)
        elif t == "event_msg" and pt == "user_message":
            msg = p.get("message", "")
            if msg.strip():
                h = _hash(msg.strip())
                if h in self._recent_users:
                    return
                self._legacy_message_duplicate = None
                self._recent_users.append(h)
                yield Ev("user", text=msg)
        elif t == "response_item" and pt == "message" and p.get("role") == "user":
            msg = _codex_user_message_text(p)
            if msg.strip() and _hash(msg.strip()) not in self._recent_users:
                self._recent_users.append(_hash(msg.strip()))
                yield Ev("user", text=msg)
        elif t == "event_msg" and pt == "agent_message":
            msg = (p.get("message") or "").strip()
            if msg:
                ts = rec.get("timestamp", "")
                self._legacy_message_duplicate = _hash(msg)
                yield Ev("text", text=msg, key=f"{ts}:{_hash(msg)}",
                         final=(p.get("phase") == "final_answer"))
        elif t == "response_item" and pt == "message" and p.get("role") == "assistant":
            # Fallback for Codex >= 0.149, which no longer emits event_msg/agent_message.
            msg = _codex_message_text(p, "output_text")
            if msg and _hash(msg) == self._legacy_message_duplicate:
                self._legacy_message_duplicate = None
                return
            # Any response_item that is not the immediate legacy twin begins a fresh duplicate
            # relation.  Content equality with an older commentary is not dedup evidence.
            self._legacy_message_duplicate = None
            if msg:
                ts = rec.get("timestamp", "")
                yield Ev("text", text=msg, key=f"{ts}:{_hash(msg)}",
                         final=(p.get("phase") == "final_answer" or p.get("phase") is None))
        elif t == "response_item" and pt in ("function_call", "custom_tool_call", "web_search_call"):
            if pt == "web_search_call":
                action = p.get("action") if isinstance(p.get("action"), dict) else {}
                key = "web:" + (action.get("query") or "search")     # stable across status updates
            else:
                key = p.get("call_id") or _hash(json.dumps(p, sort_keys=True)[:200])
            yield Ev("tool", text=_codex_tool_summary(p), key=key)
        elif t == "event_msg" and pt == "task_complete":
            self._legacy_message_duplicate = None
            # Codex ends EVERY turn with task_complete — including one it refused. A refused turn
            # has `last_agent_message: null` and an `error` object; the reason lives in
            # `codex_error_info` (machine) next to `message` (human). Discarding it is what let a
            # quota-refused turn look like a silent turn, so the backstop forwarded an unrelated
            # old answer instead (live, 2026-08-26, four times in a row).
            err = p.get("error") if isinstance(p.get("error"), dict) else None
            if err:
                info = str(err.get("codex_error_info") or "")
                yield Ev("error", text=str(err.get("message") or "").strip(),
                         key="error:" + str(p.get("turn_id")
                                            or rec.get("timestamp") or _hash(str(err))),
                         code=USAGE_LIMIT if info == "usage_limit_exceeded" else info)
            yield Ev("turn_end")


def for_agent(agent: str):
    """Return the reader for the configured agent (defaults to Claude Code)."""
    return CodexReader() if (agent or "").lower() == "codex" else ClaudeCodeReader()
