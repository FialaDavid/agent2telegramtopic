# Ported from agent2telegram agent2telegram/session.py (MIT)
"""Persistent agent sessions over **tmux** — the same approach as a hand-rolled bridge that
drives a live TUI, generalized into a product.

Why tmux: it keeps the agent's *interactive* session alive, so full context, loaded tools
and working state persist across messages — exactly like talking to it in a terminal. It
works for any agent that has an interactive CLI (Claude Code, Codex, …).

Inbound (proven send-keys sequence): clear the prompt line (``C-u``), type the message
literally (``send-keys -l --``), then submit (``Enter``). Newlines are collapsed so a single
Enter submits the whole message.

Completion + response — two strategies:
  * **Hook** (robust, used for Claude Code): the agent runs a Stop hook at end of turn that
    writes the final answer to a per-session signal file; the bridge waits for it. This is
    authoritative (reads the transcript, not the screen). Set up by the installer.
  * **Idle** (universal fallback): poll ``capture-pane`` and treat the turn as done once the
    output has been stable for ``idle`` seconds. Good enough for agents without a hook.
"""
from __future__ import annotations

import logging
import re
import shlex
import shutil
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("agent2telegramtopic.session")

# TUI chrome filters for the idle (screen-scraping) path. capture-pane gives plain text
# (no ANSI), so we only strip recognizable decoration lines and leading bullet markers.
_SEP_RE = re.compile(r"^[\s─━—–_=·.\-]{6,}$")
_STATUS_RE = re.compile(r"^[✻✶✳✢✺✷*]\s")           # spinner/status, e.g. "✻ Worked for 1s"
_BULLET_RE = re.compile(r"^\s*[⏺●○•]\s?")           # assistant output bullet
UNCONFIRMED_SUBMIT = "message not submitted"
# The composer could not be found — a dialog or menu has the keyboard. Reported as a delivery
# FAILURE, never as a send: the bridge may only type where it can see the composer.
BLOCKED_BY_OVERLAY = "interactive overlay blocks the input"
DIALOG_KEYS = frozenset({"Up", "Down", "Enter", "Escape", *"123456789"})
HOLDS, CLEAR, UNKNOWN = "holds", "clear", "unknown"
_PROMPT_RE = re.compile(r"^[❯›>]\s?")
# "❯ 1. Yes, I trust this folder" — the highlighted row of a selection dialog carries the SAME
# marker as the composer prompt, so a prompt marker alone never proves this is a text input.
_MENU_ITEM_RE = re.compile(r"^[❯›>]\s*\d+[.)]\s")
# A TUI that collapses a long paste into a chip shows only a count, never the text — so the
# substring probe below can never find the message that was just typed, and the bridge would
# report "not submitted" for every long message. Codex 0.147.0 renders "[Pasted Content 1288
# chars]" for anything over 1000 characters; Claude Code's chip counts lines instead.
_PASTE_CHIP_RE = re.compile(r"\[pasted\b[^\]]*\]")
_PASTE_CHIP_CHARS_RE = re.compile(r"\[pasted\b[^\]]*?(\d+)\s*chars?\]")
# tmux hands the first keystrokes to the TUI before it recognises the burst as a paste, so a
# few leading characters can stay on screen beside the chip instead of inside it. Measured on
# an isolated Codex 0.147.0: 0 normally, 1-2 when the message opens with a non-ASCII character.
MAX_UNCHIPPED_LEAD = 8
_BORDER_CHARS = "│┃|"
MAX_COMPOSER_LINES = 14
# Dialogs known to be harmless to cancel, which Escape reliably closes. This list is NOT the
# safety mechanism and adding entries to it protects nobody: whether the bridge may type is
# decided by _composer_text() positively finding a focused composer, never by this list failing
# to match. It exists so one known nuisance dialog does not need a human to clear it by hand —
# delete it and the only consequence is that the survey modal blocks delivery until dismissed.
_ESCAPABLE_OVERLAY_MARKERS = ("how is claude doing this session",)


def _pane_has_escapable_overlay(pane_text: str) -> bool:
    low = (pane_text or "").lower()
    return any(marker in low for marker in _ESCAPABLE_OVERLAY_MARKERS)


class PaneView(NamedTuple):
    """One coherent snapshot of a pane: its text and where the terminal cursor is.

    cursor_visible is the pane's DECTCEM state. A TUI hides the cursor when a dialog takes the
    keyboard and shows it while a text input has focus — measured 2026-08-25 on isolated
    instances of Claude Code 2.1.243 and Codex 0.147.0: hidden for the trust dialog, the /model
    picker and an open AskUserQuestion; visible throughout a full streaming turn (70 consecutive
    samples), so "busy" is never mistaken for "blocked". None means tmux could not report it.
    """
    text: str
    cursor_row: int
    cursor_visible: bool | None


def _strip_borders(line: str) -> str:
    return line.strip().strip(_BORDER_CHARS).strip()


def _is_prompt_line(line: str) -> bool:
    return bool(_PROMPT_RE.match(_strip_borders(line)))


def _is_menu_item(line: str) -> bool:
    return bool(_MENU_ITEM_RE.match(_strip_borders(line)))

def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()

MAX_TMUX_INJECTION_CHARS = 8000
# `send-keys -l -- <text>` handing the whole message to the TUI in one burst silently corrupts
# it once the burst crosses roughly 1000-1300 characters on Claude Code (measured 2026-09-02,
# isolated tmux session, Claude Code v2.1.251, tens of trials at each length): the composer ends
# up holding only the TAIL of the message — the head is gone, with no chip, no error, nothing on
# screen to say so. It reproduces identically via `paste-buffer`, so it is not specific to `-l`;
# it lives in Claude Code's own input handling, not in how tmux hands the bytes over. Splitting
# the burst into pieces safely under the failure threshold and letting the composer settle
# between them avoids ever crossing it — verified corruption-free up to 5000 chars over 10+
# trials per length. 400 gives comfortable margin below the ~1000-1300 char boundary.
SAFE_INJECTION_CHUNK_CHARS = 400
_INJECTION_CHUNK_PAUSE = 0.35
_SHELL_COMMANDS = {
    "ash", "bash", "csh", "dash", "fish", "ksh", "mksh", "pwsh", "sh", "tcsh", "zsh",
}
_AGENT_WRAPPERS = {
    "bun", "deno", "node", "nodejs", "npm", "npx", "pnpm", "python", "python3", "uv",
    "uvx", "yarn",
}


def _clean_tui(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if _SEP_RE.match(s):
            continue
        if s.lstrip().startswith("❯") or s.lstrip().startswith(">"):   # prompt / input echo
            continue
        if _STATUS_RE.match(s.lstrip()):
            continue
        out.append(_BULLET_RE.sub("", s))
    return "\n".join(out).strip()


def sanitize_for_tmux(text: str) -> str:
    """Drop terminal control bytes that tmux would otherwise pass to the live TUI."""
    out = []
    for ch in text:
        codepoint = ord(ch)
        if (
            ch not in "\n\t"
            and (codepoint < 0x20 or codepoint == 0x7f or 0x80 <= codepoint <= 0x9f)
        ):
            continue
        out.append(ch)
        if len(out) >= MAX_TMUX_INJECTION_CHARS:
            break
    return "".join(out)


def _command_name(command: str) -> str:
    return Path(str(command).strip()).name.lower().lstrip("-")


def _argv0(args: str) -> str:
    if not args:
        return ""
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = str(args).split()
    return parts[0] if parts else ""


def _command_names(commands: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for command in commands:
        name = _command_name(command)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _mentions_expected_agent(args: str, expected: tuple[str, ...]) -> bool:
    low = (args or "").lower()
    for name in expected:
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", low):
            return True
    return False


def _process_command_names(command: str, args: str) -> tuple[str, ...]:
    out: list[str] = []
    seen = set()
    for value in (command, _argv0(args)):
        name = _command_name(value)
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def _process_matches_agent(command: str, args: str, expected: tuple[str, ...]) -> bool:
    names = _process_command_names(command, args)
    if any(name in expected for name in names):
        return True
    return any(name in _AGENT_WRAPPERS for name in names) and _mentions_expected_agent(args, expected)


def _process_shell_name(command: str, args: str) -> str:
    for name in _process_command_names(command, args):
        if name in _SHELL_COMMANDS:
            return name
    return ""


def _pane_value(target: str, fmt: str) -> str:
    res = _tmux("display-message", "-p", "-t", target, fmt, check=False, timeout=3)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _pane_processes(target: str) -> list[tuple[int, int, str, str]]:
    pid_s = _pane_value(target, "#{pane_pid}")
    if not pid_s.isdigit():
        return []
    root_pid = int(pid_s)
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm=,args="],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []

    rows: dict[int, tuple[int, int, str, str]] = {}
    children: dict[int, list[int]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        args = parts[3] if len(parts) > 3 else command
        rows[pid] = (pid, ppid, command, args)
        children.setdefault(ppid, []).append(pid)

    found: list[tuple[int, int, str, str]] = []
    stack = [root_pid]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = rows.get(pid)
        if row:
            found.append(row)
        stack.extend(children.get(pid, ()))
    return found


def _agent_alive(target: str, expected_commands: list[str] | tuple[str, ...] | set[str]) -> tuple[bool, str]:
    expected = _command_names(expected_commands)
    if not expected:
        return True, "no expected agent command configured"

    current = _pane_value(target, "#{pane_current_command}")
    current_name = _command_name(current)
    expected_s = ", ".join(expected)
    if current_name in expected:
        return True, f"tmux pane command is {current_name}"

    # Security guard: send-keys must only target a live agent TUI. tmux may report a
    # stale title or a login shell as the pane command, so first search the pane's full
    # process subtree for the configured agent binary by basename.
    processes = _pane_processes(target)
    for _pid, _ppid, command, args in processes:
        if _process_matches_agent(command, args, expected):
            return True, f"tmux pane process is {_command_name(command)}"

    shell_name = current_name if current_name in _SHELL_COMMANDS else ""
    if not shell_name and len(processes) == 1:
        _pid, _ppid, command, args = processes[0]
        shell_name = _process_shell_name(command, args)
    if shell_name:
        return False, f"tmux pane is at a shell prompt ({shell_name}); expected {expected_s}"

    detail = current_name or "unknown"
    return False, f"tmux pane command is {detail}; expected {expected_s}"


class SessionError(Exception):
    pass


def _tmux(*args: str, check: bool = True, timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check, timeout=timeout)


class TmuxSession:
    """A live agent running in a detached tmux session, fed via send-keys."""

    def __init__(self, agent_argv: list[str], *, cwd: Path, name: str | None = None,
                 timeout: int = 600, idle: float = 1.5, settle: float = 0.4,
                 origin_prefix: str = "", signal_file: Path | None = None,
                 boot_wait: float = 2.0,
                 expected_agent_commands: list[str] | tuple[str, ...] | None = None) -> None:
        if shutil.which("tmux") is None:
            raise SessionError("tmux is not installed. Install it first: `sudo apt install tmux` (Debian/Ubuntu) or `brew install tmux` (macOS) — "
                               "or `apt install tmux`) — the persistent session needs it.")
        self.name = name or ("a2tt_" + uuid.uuid4().hex[:10])
        self._timeout = timeout
        self._idle = idle
        self._settle = settle
        self._origin = origin_prefix
        self._signal = signal_file
        self._expected_agent_commands = tuple(expected_agent_commands or (agent_argv[:1] if agent_argv else ()))
        cwd.mkdir(parents=True, exist_ok=True)
        if not self._exists():
            if not agent_argv:
                # Attach mode: we only drive an existing session, never create one.
                raise SessionError(f"tmux session '{self.name}' does not exist (attach mode).")
            _tmux("new-session", "-d", "-s", self.name, "-c", str(cwd), "-x", "220", "-y", "50", *agent_argv,
                  timeout=15)
            time.sleep(boot_wait)   # let the TUI come up before the first message

    # ---- lifecycle ---------------------------------------------------------
    def _exists(self) -> bool:
        return subprocess.run(["tmux", "has-session", "-t", self.name],
                              capture_output=True).returncode == 0

    @property
    def alive(self) -> bool:
        return self._exists()

    def close(self) -> None:
        _tmux("kill-session", "-t", self.name, check=False)

    # ---- messaging ---------------------------------------------------------
    def _pane_ok(self) -> tuple[bool, str]:
        return _agent_alive(self.name, getattr(self, "_expected_agent_commands", ()))

    def _dismiss_escapable_overlay(self, attempts: int = 4) -> bool:
        """Escape a known-harmless dialog out of the way. Convenience, NOT the safety check.

        Nothing here decides whether the bridge may type — _focused_composer() does. Escape is the
        only key this module ever sends at a dialog, and only when a dialog we know is safe to
        cancel is on screen: Escape into a permission prompt would deny it, and into a streaming
        turn it would interrupt the agent. Never "improve" this by escaping unknown dialogs, and
        never by guessing a second key.
        """
        for _ in range(max(1, attempts)):
            if not _pane_has_escapable_overlay(self._capture()):
                return True
            log.info("dismissing known overlay in tmux session '%s' with Escape", self.name)
            _tmux("send-keys", "-t", self.name, "Escape", check=False)
            # Ink/React TUIs need a render tick to redraw after Escape; 0.15s proved to be
            # under that, and a two-step questionnaire needs more than a single Escape anyway.
            time.sleep(0.4)
        return not _pane_has_escapable_overlay(self._capture())

    def _send_keys(self, text: str, origin_prefix: bool = True) -> None:
        ok, detail = self._pane_ok()
        if not ok:
            # Fail closed: if the agent has crashed back to a shell, Enter would execute the
            # Telegram message as a shell command instead of submitting it to the agent TUI.
            log.warning("refusing tmux injection into '%s': %s", self.name, detail)
            raise SessionError(f"refusing to inject into tmux session '{self.name}': {detail}")
        text = sanitize_for_tmux(text)
        text = " ".join(text.splitlines())                 # one Enter submits everything
        if origin_prefix and self._origin:
            text = f"{self._origin}{text}"
        self._type_and_submit(text)

    def _focused_composer(self) -> PaneView:
        """The pane, proven to have the composer focused. Raises when it does not.

        This is the gate the 2026-08-25 loss went through: the bridge decided "this is not a
        dialog" by matching the text of dialogs it knew about, so every new Claude Code modal was
        a fresh silent loss. It now has to see the composer instead.
        """
        view = self._pane_view()
        if self._composer_text(view) is not None:
            return view
        if _pane_has_escapable_overlay(view.text) and self._dismiss_escapable_overlay():
            view = self._pane_view()
            if self._composer_text(view) is not None:
                return view
        raise SessionError(
            f"{BLOCKED_BY_OVERLAY}: the composer in tmux session '{self.name}' does not have the "
            "keyboard — an interactive dialog or menu is on screen")

    def dialog_snapshot(self) -> tuple[str, bool]:
        """The pane text and whether the composer has the keyboard (False = a dialog is up)."""
        view = self._pane_view()
        return view.text, self._composer_text(view) is not None

    def press_dialog_key(self, key: str) -> None:
        """Send ONE navigation key to a dialog the user chose to answer from Telegram."""
        if key not in DIALOG_KEYS:
            raise SessionError(f"refusing to send key {key!r} to a dialog")
        ok, detail = self._pane_ok()
        if not ok:
            raise SessionError(f"refusing to press {key} in tmux session '{self.name}': {detail}")
        _tmux("send-keys", "-t", self.name, key, check=False)

    def _clear_composer(self, attempts: int = 200, pause: float = 0.08) -> bool:
        """Clear whatever is in the composer and PROVE it is empty before returning True.

        2026-10-03 finding: `C-u` on Claude Code's composer is "kill to the start of the
        current ON-SCREEN (wrapped) row", not "kill the whole logical line". A long single-line
        message (no real newlines, see `_send_keys`) wraps into many rows, so one `C-u` only
        erases the last visual row and leaves the rest sitting in the buffer — reproduced live
        (isolated tmux session, Claude Code v2.1.288, 80x24): a single `C-u` after typing a
        1131-char line left 3 full rows of the message behind, and the EARLIER (not later)
        rows surface next because the viewport re-centres on the cursor as it moves back through
        the still-wrapped text. That leftover is exactly the mechanism behind the "retries
        accumulate fragments" symptom: a caller that sends one `C-u` and retypes is typing on
        top of real leftover characters, not a clean composer.

        There is no portable way to ask "is the logical buffer empty" directly, so this proves
        it operationally: keep sending `C-u` until a send stops changing the composer's OWN
        text. An already-empty composer repaints its (dim) placeholder identically every read,
        so it exits after the first, harmless send; one still holding text keeps changing (as
        many presses as it has wrapped rows, not a fixed guess) until nothing is left to delete.

        2026-10-03 Master review fix: this used to compare the WHOLE pane (`_capture()`), not
        just the composer. The agent is very often mid-turn when a message arrives — spinner,
        an elapsed-turn timer, streaming tool output all redraw outside the composer on every
        single frame — so the whole pane never stabilised and an ordinary short message sent
        while the agent was busy burned all `attempts` and was reported undelivered, even though
        the composer itself had been empty since the very first press. Only the composer's own
        text (via `_composer_text`) is compared now; unrelated activity elsewhere on screen can
        no longer stall this. Returns False only if the COMPOSER itself is still changing (or
        unreadable) after `attempts` tries — the caller must not retype in that case (see
        `_type_and_submit`), or old and new text will concatenate instead of the new message
        replacing the old.
        """
        def composer_now() -> str | None:
            return self._composer_text(self._pane_view())

        prev = composer_now()
        for _ in range(max(1, attempts)):
            _tmux("send-keys", "-t", self.name, "C-u", check=False)
            time.sleep(pause)
            cur = composer_now()
            # Both reads must be an actual composer (never None == None — that would also match
            # two readings where the composer itself is genuinely unreadable, e.g. an overlay).
            if cur is not None and cur == prev:
                return True
            prev = cur
        return False

    def _confirm_long_text_by_scrolling(self, text: str) -> bool:
        """Fallback proof for a message taller than the pane can render at once.

        2026-10-03 finding, live (isolated tmux session, Claude Code v2.1.288, 80x24 pane —
        the same geometry as 'Claude – Master'): typing a genuinely long single-line message
        (measured up to 4000 chars) via the existing chunked bursts (SAFE_INJECTION_CHUNK_CHARS)
        lands the FULL text in the composer's buffer, but the composer only ever RENDERS a
        window of it ending at the cursor — there is no tmux scrollback to fall back on (this is
        an alternate-screen TUI; `#{history_size}` stayed 0 throughout), so when the logical
        content needs more rows than the pane has to give it, the rows above the visible window
        are not drawn anywhere on screen, not lost from the buffer. `_composer_state`'s
        both-ends substring check (see its docstring) then sees only the tail and — correctly,
        by its own contract — refuses to call that HOLDS, because that is also the exact shape
        of the 2026-09-02 real corruption (head genuinely dropped). The two cases are
        indistinguishable from one static screen; they are NOT indistinguishable if the cursor
        moves: `Ctrl-A`/`Ctrl-E` (verified live) move the cursor to the true start/end of the
        wrapped logical line and the composer's viewport follows it, so a legitimately-clipped
        head reappears on `Ctrl-A` while a genuinely corrupted one (the data was never there)
        does not. This is only ever called after a composer was positively identified at the
        cursor (never at a dialog/overlay — `_await_composer_holds` gates that), and it always
        ends on `Ctrl-E` so the cursor is back at the true end before any Enter is sent.
        """
        probe = _norm(text)
        if not probe:
            return False
        _tmux("send-keys", "-t", self.name, "C-a", check=False)
        time.sleep(0.15)
        head = self._composer_text(self._pane_view())
        head_ok = head is not None and probe[:24] in _norm(head)
        _tmux("send-keys", "-t", self.name, "C-e", check=False)
        time.sleep(0.15)
        tail = self._composer_text(self._pane_view())
        tail_ok = tail is not None and probe[-24:] in _norm(tail)
        return head_ok and tail_ok

    def _type_and_submit(self, text: str) -> None:
        """Type one already-authorized message into the composer — and prove it got there.

        Nothing is typed before a composer is positively identified, and Enter is not pressed
        before the typed text is visible IN that composer. Both halves matter: with a selection
        dialog on screen the keystrokes are swallowed without a trace and Enter picks the
        highlighted option. Reproduced on an isolated instance 2026-08-25 — a 38-character message
        vanished into an open AskUserQuestion and the following Enter answered the question in the
        user's name ("User answered Claude's questions: → Tea"), while the bridge logged the
        message as delivered.
        """
        text = sanitize_for_tmux(text)
        text = " ".join(text.splitlines())                 # one Enter submits everything
        self._focused_composer()
        if not self._clear_composer():
            # Do not type on top of a composer we could not prove empty — that is exactly how a
            # retry's text ends up concatenated with a previous failed attempt's leftovers.
            raise SessionError(
                f"{UNCONFIRMED_SUBMIT}: could not clear the composer of tmux session "
                f"'{self.name}' before typing, so the message was not sent")
        # A single `-l --` burst above ~1000-1300 chars corrupts on Claude Code (see
        # SAFE_INJECTION_CHUNK_CHARS) — split it and let the composer settle between pieces so
        # no individual burst ever crosses the threshold that triggers it.
        for i in range(0, len(text), SAFE_INJECTION_CHUNK_CHARS):
            _tmux("send-keys", "-t", self.name, "-l", "--", text[i:i + SAFE_INJECTION_CHUNK_CHARS])
            if i + SAFE_INJECTION_CHUNK_CHARS < len(text):
                time.sleep(_INJECTION_CHUNK_PAUSE)
        self._await_composer_settled()
        if not self._await_composer_holds(text):
            # Our own text is not where we can see it, so we do not know what has the keyboard.
            # Pressing Enter here is the dangerous move, not the cautious one. This is also the
            # backstop if a burst still gets corrupted despite chunking (different TUI, different
            # version, different threshold): _composer_state below requires BOTH the head and the
            # tail of the text to be visible, so a corrupted composer that kept only the tail (the
            # 2026-09-02 failure mode) fails this proof instead of passing it — the loss becomes a
            # loud SessionError here instead of a silently truncated submit.
            self._clear_composer()   # best-effort take-back of whatever we typed
            raise SessionError(
                f"{UNCONFIRMED_SUBMIT}: the text never appeared in the composer of tmux session "
                f"'{self.name}', so it was not submitted")
        _tmux("send-keys", "-t", self.name, "Enter")
        if not self._confirm_submitted(text):
            raise SessionError(f"{UNCONFIRMED_SUBMIT}: message remains in composer")

    def _await_composer_settled(self, quiet: float = 0.12, limit: float = 1.5) -> None:
        deadline = time.monotonic() + limit
        previous = None
        while time.monotonic() < deadline:
            time.sleep(quiet)
            current = self._capture()
            if current == previous:
                return
            previous = current

    def _await_composer_holds(self, text: str, limit: float = 3.0) -> bool:
        """Wait until the composer visibly holds `text`. False = we never saw it arrive.

        If the deadline passes without that, but a composer WAS positively identified at the
        cursor at some point (as opposed to never — an overlay, or a pane we cannot parse at
        all), the message may simply be taller than the pane can render at once rather than
        actually missing: fall back to `_confirm_long_text_by_scrolling`, which moves the cursor
        instead of trusting one static screen. Never attempted when no composer was ever seen —
        that path must stay exactly as cautious as before (see `NothingIsTypedWhereItCannotBeSeen`).
        """
        deadline = time.monotonic() + limit
        saw_composer = False
        while True:
            view = self._pane_view()
            if self._composer_text(view) is not None:
                saw_composer = True
            if self._composer_state(text, view) == HOLDS:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if saw_composer:
            return self._confirm_long_text_by_scrolling(text)
        return False

    def _pane_view(self) -> PaneView:
        """Pane text plus cursor, in ONE tmux round trip so the two cannot disagree."""
        res = _tmux("display-message", "-p", "-t", self.name, "#{cursor_y} #{cursor_flag}",
                    ";", "capture-pane", "-p", "-t", self.name, check=False, timeout=5)
        out = res.stdout or ""
        head, _, pane = out.partition("\n")
        parts = head.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1] in ("0", "1"):
            return PaneView(pane, int(parts[0]), parts[1] == "1")
        # tmux too old to report the cursor: keep the whole output as pane text and fall back to
        # structural identification. Weaker, but the proof that the text actually landed in the
        # composer (_type_and_submit) still stands, so a modal still cannot be mistaken for a send.
        return PaneView(out, -1, None)

    @staticmethod
    def _composer_text(view: PaneView) -> str | None:
        """The text sitting in the composer, or None when the composer has not got the keyboard.

        Positive identification, in this order:
          * the pane cursor is visible at all — a dialog that grabs the keyboard hides it;
          * the cursor sits on (or, for a wrapped message, below) a line carrying the agent's
            prompt marker;
          * that line is not the highlighted row of a numbered selection dialog, which uses the
            very same marker.
        Anything else is None, meaning "no composer here" — never "an empty composer". The old
        parser took the last two box-drawing lines in the pane, which is why a modal's own border
        read as an empty composer.
        """
        if view.cursor_visible is False:
            return None
        lines = view.text.splitlines()
        if not lines:
            return None
        row = view.cursor_row
        if row < 0:      # no cursor from tmux: fall back to the last prompt line on screen
            row = max((i for i, ln in enumerate(lines) if _is_prompt_line(ln)), default=-1)
        if row < 0 or row >= len(lines):
            return None
        start = next((i for i in range(row, max(-1, row - MAX_COMPOSER_LINES), -1)
                      if _is_prompt_line(lines[i])), None)
        if start is None or _is_menu_item(lines[start]):
            return None
        body = [_PROMPT_RE.sub("", _strip_borders(lines[start]), count=1)]
        body += [_strip_borders(ln) for ln in lines[start + 1:row + 1]]
        return " ".join(s for s in body if s).strip()

    def _composer_state(self, text: str, view: PaneView | None = None) -> str:
        composer = self._composer_text(view if view is not None else self._pane_view())
        if composer is None:
            return UNKNOWN
        composer = _norm(composer)
        chipped = [int(n) for n in _PASTE_CHIP_CHARS_RE.findall(composer)]
        if chipped:
            # The chip hides the text, so its character count is the only evidence on screen —
            # and a stronger one than a substring: it has to add up to the message just typed.
            # A chip that does not add up is not proof of anything, so the probe below decides.
            if 0 <= len(text) - sum(chipped) <= MAX_UNCHIPPED_LEAD:
                return HOLDS
        elif _PASTE_CHIP_RE.search(composer):
            # A chip counting something other than characters (Claude Code counts lines), or a
            # wording no one has seen yet. Not proof the text is ours, but "the composer is not
            # empty" is the safe half: before the submit it blocks nothing, and after it, Enter
            # is exactly what still needs sending.
            return HOLDS
        probe = _norm(text)
        # Both ends, not either: measured 2026-09-02 on Claude Code v2.1.251, a burst above
        # ~1000-1300 chars can corrupt in the composer and keep ONLY the tail — no chip, no
        # error. An either/or check calls that HOLDS (the tail alone was "proof enough"), so the
        # corrupted composer looked submitted and Enter went out on truncated text. Requiring
        # both anchors means a composer that dropped the head fails this proof instead of passing
        # it, and _type_and_submit turns that into a loud SessionError before Enter is ever sent.
        return HOLDS if probe and (probe[:24] in composer and probe[-24:] in composer) else CLEAR

    def _confirm_submitted(self, text: str, attempts: int = 6, limit: float = 15.0,
                           settle: float = 2.2) -> bool:
        """True only on positive evidence that the text left the composer.

        "I cannot see a composer" (UNKNOWN) is not evidence of delivery and never returns True on
        its own — treating it as success is what made a lost message look delivered. Once the
        composer HAS been seen empty after the submit, a dialog appearing on top of it afterwards
        does not retract that evidence.
        """
        deadline = time.monotonic() + limit
        watch_until = time.monotonic() + settle
        seen_clear = False
        sent = 0
        while time.monotonic() < deadline:
            time.sleep(0.4)
            state = self._composer_state(text)
            if state == HOLDS:
                seen_clear = False
                if sent >= attempts:
                    break
                sent += 1
                _tmux("send-keys", "-t", self.name, "Enter", check=False)
                time.sleep(min(0.4 * sent, 1.5))
                watch_until = time.monotonic() + settle
                continue
            if state == CLEAR:
                seen_clear = True
            if seen_clear and time.monotonic() >= watch_until:
                return True
        return seen_clear and self._composer_state(text) != HOLDS

    def send_command(self, text: str) -> None:
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        ok, detail = self._pane_ok()
        if not ok:
            raise SessionError(f"refusing to command tmux session '{self.name}': {detail}")
        self._type_and_submit(sanitize_for_tmux(text))

    def _capture(self) -> str:
        return _tmux("capture-pane", "-p", "-t", self.name, check=False).stdout

    def inject(self, text: str, origin_prefix: bool = True) -> None:
        """Fire-and-forget: type the message into the session, don't wait for a reply.
        Used by the async attach bridge (outbound is handled separately)."""
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        self._send_keys(text, origin_prefix=origin_prefix)

    def send(self, text: str) -> str:
        if not self.alive:
            raise SessionError(f"agent session '{self.name}' is gone")
        if self._signal is not None:
            return self._send_with_hook(text)
        return self._send_with_idle(text)

    def _send_with_hook(self, text: str) -> str:
        """Authoritative completion: a Stop hook writes the final answer to the signal file."""
        try:
            self._signal.unlink()                          # clear any stale answer
        except FileNotFoundError:
            pass
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._signal.exists():
                answer = self._signal.read_text("utf-8")
                try:
                    self._signal.unlink()
                except OSError:
                    pass
                return answer.strip()
            time.sleep(0.3)
        raise SessionError(f"agent timed out after {self._timeout}s")

    def _send_with_idle(self, text: str) -> str:
        before = self._capture()
        self._send_keys(text)
        deadline = time.monotonic() + self._timeout
        last, stable_since = "", 0.0
        while time.monotonic() < deadline:
            time.sleep(self._settle)
            cur = self._capture()
            if cur != last:
                last, stable_since = cur, time.monotonic()
                continue
            if stable_since and (time.monotonic() - stable_since) >= self._idle and cur != before:
                return self._delta(before, cur)
        raise SessionError(f"agent timed out after {self._timeout}s")

    @staticmethod
    def _delta(before: str, after: str) -> str:
        b, a = before.splitlines(), after.splitlines()
        i = 0
        while i < len(b) and i < len(a) and b[i] == a[i]:
            i += 1
        return _clean_tui("\n".join(a[i:]))
