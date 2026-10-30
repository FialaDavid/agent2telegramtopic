# Ported from agent2telegram agent2telegram/telegram.py (MIT)
"""Small Telegram Bot API client with bounded transient-error handling."""

import html as _html
import json
import mimetypes
import logging
import os
import re
import sys
import time
import secrets
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

log = logging.getLogger(__name__)
MAX_MESSAGE_LEN = 4000
RICH_MESSAGE_MAX_CHARS = 32768
SEND_TIMEOUT = 15

from . import redaction
from .routing import LaneKey, reply_params


def _is_word_char(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or ch == "_")


def _parse_emphasis(text: str) -> str:
    """Stack-based inline-emphasis parser: turns ``**bold**`` and ``*italic*``/``_italic_``
    (including combined ``***bold italic***``) into properly nested ``<b>``/``<i>`` tags.

    This replaces three independent ``re.sub`` passes (bold, then star-italic, then
    underscore-italic) that had no notion of nesting: on input like
    ``**bold *and*more***`` they crossed tags (``<b>...<i>...</b></i>``), which Telegram
    rejects outright with HTTP 400 and drops ALL formatting as a result (see the formatting
    brief, vada 1). A single pass with a stack has no such ambiguity: a delimiter run closes
    the top of the stack if it matches, otherwise it opens a new marker, and anything left
    open at the end reverts to the literal characters it came from — never a dangling tag.

    Boundary rule preserved from the old regexes: a *single* ``*``/``_`` is not a marker
    when touching a word character on the side that would make it one (mid-word
    ``snake_case_name`` stays plain). ``**``/``***`` (bold, and bold+italic) carry no such
    restriction, matching the old bold regex.
    """
    out: list[str] = []
    stack: list[tuple[str, int, int]] = []  # (delim char, size 1|2, index into `out` of its opening tag)
    OPEN = {1: "<i>", 2: "<b>"}
    CLOSE = {1: "</i>", 2: "</b>"}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch not in "*_":
            j = i
            while j < n and text[j] not in "*_":
                j += 1
            out.append(text[i:j])
            i = j
            continue
        j = i
        while j < n and text[j] == ch:
            j += 1
        run = j - i
        usable = min(run, 3) if ch == "*" else (1 if run == 1 else 0)
        if usable == 0:
            out.append(text[i:j])
            i = j
            continue
        before = text[i - 1] if i > 0 else ""
        after = text[j] if j < n else ""
        if usable == 1:
            would_close = bool(stack) and stack[-1][0] == ch and stack[-1][1] == 1
            if would_close and not _is_word_char(after):
                _, size, _idx = stack.pop()
                out.append(CLOSE[size])
            elif not would_close and not _is_word_char(before):
                stack.append((ch, 1, len(out)))
                out.append(OPEN[1])
            else:
                out.append(ch)  # boundary rejects it; stays literal (run==1 so just `ch`)
        else:  # usable in (2, 3): bold, or bold+italic — no word-boundary restriction
            remaining = usable
            # Step 1: an exact-sum close across a contiguous same-char run from the stack
            # top — the only case where one delimiter run may close more than one marker
            # (e.g. "***" closing an italic+bold pair opened together as "***"). A run may
            # NOT close a narrower marker just because its size fits under `remaining`:
            # that was the bug ("*a **b** c*" let "**" pop the open italic because 1<=2),
            # silently dropping the bold that should have nested inside it.
            total, k = 0, 0
            while k < len(stack) and stack[-1 - k][0] == ch and total < remaining:
                total += stack[-1 - k][1]
                k += 1
            if total == remaining and k > 0:
                for _ in range(k):
                    _, size, _idx = stack.pop()
                    out.append(CLOSE[size])
                remaining = 0
            # Step 2: a single marker whose size exactly matches the run (the k==1 case of
            # step 1 already covers this; kept explicit per spec for auditability — a `**`
            # run must only ever close a `**` marker, never peel through a smaller one).
            elif stack and stack[-1][0] == ch and stack[-1][1] == remaining:
                _, size, _idx = stack.pop()
                out.append(CLOSE[size])
                remaining = 0
            # Step 3 (else, remaining unchanged): nothing closes cleanly — open new marker(s).
            if remaining == 3:
                for size in (2, 1):
                    stack.append((ch, size, len(out)))
                    out.append(OPEN[size])
            elif remaining:
                stack.append((ch, remaining, len(out)))
                out.append(OPEN[remaining])
            if run > usable:
                out.append(ch * (run - usable))
        i = j
    # Anything still open never found its match: revert to the literal marker, not a dangling tag.
    for ch, size, idx in stack:
        out[idx] = ch * size
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """Convert the common Markdown subset (``**bold**``, ``*italic*``/``_italic_``,
    `` `code` ``, fenced ``` blocks, ``# headings``, pipe tables) to the HTML Telegram
    supports. Without this, agents' Markdown shows literally (asterisks/backticks) in
    Telegram.

    Telegram's Bot API does not support real Markdown tables or Markdown headings. Agents
    still naturally write them, so the bridge renders headings as bold lines and preserves
    pipe-table blocks in ``<pre>`` instead of sending raw, unformatted text.
    """
    stash: list[str] = []

    def keep(s: str) -> str:
        stash.append(s)
        return f"\x00{len(stash) - 1}\x00"

    def stash_tables(src: str) -> str:
        lines = src.splitlines()
        out: list[str] = []
        i = 0
        while i < len(lines):
            if (i + 1 < len(lines)
                    and "|" in lines[i]
                    and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[i + 1])):
                block = [lines[i], lines[i + 1]]
                i += 2
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    block.append(lines[i])
                    i += 1
                out.append(keep(f"<pre>{_html.escape(chr(10).join(block))}</pre>"))
            else:
                out.append(lines[i])
                i += 1
        return "\n".join(out)

    text = re.sub(r"```(?:\w+)?\n?(.*?)```",
                  lambda m: keep(f"<pre>{_html.escape(m.group(1))}</pre>"), text, flags=re.S)
    text = stash_tables(text)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: keep(f"<code>{_html.escape(m.group(1))}</code>"), text)
    # Markdown links [text](url) → <a href>; stashed so the URL isn't escaped/mangled.
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: keep(f'<a href="{_html.escape(m.group(2), quote=True)}">{_html.escape(m.group(1))}</a>'),
        text)
    text = _html.escape(text)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    text = _parse_emphasis(text)
    for i, s in enumerate(stash):
        text = text.replace(f"\x00{i}\x00", s)
    return text


#: Tags markdown_to_html can emit, and the full set Telegram's Bot API HTML mode accepts.
_TELEGRAM_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "code", "pre", "blockquote",
}
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)(?:\s[^<>]*)?>")


def validate_telegram_html(html_text: str) -> tuple[bool, str]:
    """Check that *html_text* only uses tags Telegram's Bot API HTML mode supports and that
    they nest correctly (no crossing, nothing left unclosed).

    This is a pre-send safety net, NOT a substitute for a correct converter — the very bug
    it exists to catch (vada 1: crossed ``<b>``/``<i>`` tags) came from a converter that was
    wrong on its own terms, and no validator downstream changes that; ``markdown_to_html``
    must keep producing valid nesting by construction. Do not let this function's presence
    become a reason to stop caring whether the converter itself is correct.
    """
    stack: list[str] = []
    for m in _TAG_RE.finditer(html_text):
        name = m.group(2).lower()
        if name not in _TELEGRAM_ALLOWED_TAGS:
            return False, f"unsupported tag <{name}>"
        if not m.group(1):
            stack.append(name)
        elif not stack:
            return False, f"closing </{name}> with nothing open"
        elif stack[-1] != name:
            return False, f"crossed tags: expected </{stack[-1]}> before </{name}>"
        else:
            stack.pop()
    if stack:
        return False, f"unclosed tag(s): {', '.join(stack)}"
    if "<" in _TAG_RE.sub("", html_text):
        return False, "stray '<' outside a recognized tag"
    return True, ""


def _strip_markdown(text: str) -> str:
    return text.replace("**", "").replace("`", "")


def split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split *text* into Telegram-sized chunks, preferring paragraph then line then
    word boundaries, and hard-splitting only as a last resort. Pure function — tested."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer the latest natural boundary inside the window.
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > limit * 0.5:        # only if it doesn't waste too much of the window
                break
        else:
            cut = limit                  # no good boundary: hard cut
        cut = cut if cut > 0 else limit
        chunks.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return [c for c in chunks if c]



class TelegramError(Exception):
    def __init__(self, error_code: int | None, description: str, *, network_error: bool = False):
        self.error_code = error_code
        self.description = description
        self.network_error = network_error
        super().__init__(f"Telegram API error {error_code}: {description}")


# Ported from agent2telegram telegram.py:is_network_error (MIT).
def is_network_error(exc: BaseException) -> bool:
    if isinstance(exc, OSError) or getattr(exc, "network_error", False):
        return True
    cause = getattr(exc, "__cause__", None)
    return isinstance(cause, (OSError, json.JSONDecodeError))


class _PytestTransportError(RuntimeError):
    """Raised when a real HTTP request is attempted from a test process."""


def _real_transport_disabled() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


class TelegramClient:
    def __init__(self, token: str, *, opener=None, max_retries: int = 5):
        self._token = token
        self._max_retries = max_retries
        # `opener` is injectable so tests can run without touching the network.
        self._injected_opener = opener is not None
        self._opener = opener or urllib.request.build_opener()
        # Delta: rich messages are always enabled in agent2telegramtopic.
        self._rich_disabled = False

    def __repr__(self) -> str:
        return "TelegramClient(token=<redacted>)"

    def _safe_error(self, value) -> str:
        text = str(value)
        for token in (self._token, self._token.replace(":", "%3A")):
            if token:
                text = text.replace(token, "[REDACTED]")
        return redaction.redact(text)

    # ---- low-level ---------------------------------------------------------
    # Ported from agent2telegram telegram.py:_call (MIT)
    def _call(self, method: str, params: dict | None = None, *, timeout: float = 65,
              retries: int | None = None) -> dict:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = urllib.parse.urlencode(params or {}, doseq=True).encode()
        mx = self._max_retries if retries is None else retries
        attempt = 0
        while True:
            attempt += 1
            # Delta: the pytest guard raises before a real opener is used; an injected opener is allowed.
            if _real_transport_disabled() and not self._injected_opener:
                raise _PytestTransportError("Real Telegram HTTP transport is disabled under pytest; inject a fake opener")
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                with self._opener.open(req, timeout=timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if not body.get("ok"):
                    if body.get("error_code") == 401:
                        raise self._http_error(method, 401, "")
                    # Delta: TelegramError(code, description) signature.
                    raise TelegramError(
                        body.get("error_code"),
                        f"{method}: {self._safe_error(body.get('description', 'unknown error'))}",
                    )
                return body["result"]
            except HTTPError as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    log.warning("Flood control on %s, waiting %ss", method, retry_after)
                    time.sleep(retry_after + 0.5)
                    continue                         # do not count flood waits as failures
                if e.code >= 500 and attempt <= mx:
                    log.warning("%s: HTTP %s, retry %d/%d (backoff)", method, e.code, attempt, mx)
                    self._backoff(attempt)
                    continue
                raise self._http_error(method, e.code, self._error_description(e)) from None
            except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt <= mx:
                    log.warning("%s: %s, retry %d/%d (backoff)", method, self._safe_error(e), attempt, mx)
                    self._backoff(attempt)
                    continue
                raise TelegramError(None, f"{method}: {self._safe_error(e)}", network_error=True) from None

    @staticmethod
    def _error_description(err: HTTPError) -> str:
        """Pull the API's ``description`` out of an HTTP error body so exception messages
        say *why* a call failed (e.g. "rich message must be non-empty") instead of just the
        HTTP status line. Falls back to the bare reason phrase when the body is missing or
        isn't JSON — a non-JSON body must never raise out of error handling itself."""
        try:
            raw = err.read()
        except Exception:
            return err.reason
        if not raw:
            return err.reason
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err.reason
        return payload.get("description") or err.reason

    def get_me(self) -> dict:
        return self._call("getMe", timeout=15)

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
        if offset is not None:
            params["offset"] = offset
        # Network timeout must exceed the long-poll timeout, else we'd cancel mid-poll.
        return self._call("getUpdates", params, timeout=timeout + 15)

    # Delta: send_message and _send_legacy return the message ids they sent (a2t returns None):
    # Telegram reaction updates carry no message_thread_id, so the lane records its sent ids.
    @staticmethod
    def _message_id(result):
        return result.get("message_id") if isinstance(result, dict) else None

    def _send_legacy(self, key: LaneKey, text: str) -> list:
        ids = []
        for chunk in split_message(text, MAX_MESSAGE_LEN) or ["(empty response)"]:
            base = {**reply_params(key), "disable_web_page_preview": "true"}
            html_text = markdown_to_html(chunk)
            ok, reason = validate_telegram_html(html_text)
            if not ok:
                log.error("markdown_to_html produced invalid HTML (%s); sending plain-text fallback", reason)
                ids.append(self._message_id(
                    self._call("sendMessage", {**base, "text": _strip_markdown(chunk)}, timeout=SEND_TIMEOUT)))
                continue
            try:
                ids.append(self._message_id(
                    self._call("sendMessage", {**base, "text": html_text, "parse_mode": "HTML"}, timeout=SEND_TIMEOUT)))
            except TelegramError as e:
                log.warning("HTML send failed, falling back to plain text: %s", e)
                ids.append(self._message_id(
                    self._call("sendMessage", {**base, "text": _strip_markdown(chunk)}, timeout=SEND_TIMEOUT)))
        return [i for i in ids if i is not None]

    def send_message(self, key: LaneKey, text: str, *, parse_mode: str | None = "auto") -> list:
        ids = []
        if parse_mode == "auto" and not self._rich_disabled:
            for chunk in split_message(text, RICH_MESSAGE_MAX_CHARS) or ["(empty response)"]:
                if self._rich_disabled:
                    ids += self._send_legacy(key, chunk)
                    continue
                params = {**reply_params(key),
                          "rich_message": json.dumps({"markdown": chunk}),
                          "link_preview_options": json.dumps({"is_disabled": True})}
                try:
                    ids.append(self._message_id(self._call("sendRichMessage", params, timeout=SEND_TIMEOUT)))
                    continue
                except TelegramError as e:
                    if e.error_code == 404:
                        self._rich_disabled = True
                        log.warning("sendRichMessage unavailable (HTTP 404); disabling rich sends for this process: %s", e)
                    elif e.error_code == 400:
                        log.info("sendRichMessage rejected a chunk (HTTP %s): %s", e.error_code, e)
                    else:
                        raise
                    ids += self._send_legacy(key, chunk)
            return [i for i in ids if i is not None]
        if parse_mode == "auto":
            return self._send_legacy(key, text)
        for chunk in split_message(text, MAX_MESSAGE_LEN) or ["(empty response)"]:
            base = {**reply_params(key), "disable_web_page_preview": "true"}
            if parse_mode:
                try:
                    ids.append(self._message_id(
                        self._call("sendMessage", {**base, "text": chunk, "parse_mode": parse_mode}, timeout=SEND_TIMEOUT)))
                except TelegramError as e:
                    log.warning("send failed with parse_mode=%s, retrying plain: %s", parse_mode, e)
                    ids.append(self._message_id(self._call("sendMessage", {**base, "text": chunk}, timeout=SEND_TIMEOUT)))
            else:
                ids.append(self._message_id(self._call("sendMessage", {**base, "text": chunk}, timeout=SEND_TIMEOUT)))
        return [i for i in ids if i is not None]

    def get_file_path(self, file_id: str) -> str:
        return self._call("getFile", {"file_id": file_id}, timeout=20)["file_path"]

    def download(self, file_path: str, *, timeout: float = 120) -> bytes:
        """Download a file the bot has access to (returned by getFile)."""
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        last = None
        for attempt in range(1, 4):
            try:
                if _real_transport_disabled() and not self._injected_opener:
                    raise _PytestTransportError("Real Telegram downloads are disabled under pytest; inject a fake opener")
                with self._opener.open(urllib.request.Request(url), timeout=timeout) as resp: return resp.read()
            except _PytestTransportError: raise
            except HTTPError as e:
                if e.code < 500: raise self._http_error("download", e.code, e.reason) from None
                last = self._safe_error(e); self._backoff(attempt)
            except (URLError, TimeoutError, ConnectionError) as e:
                last = self._safe_error(e); self._backoff(attempt)
        raise TelegramError(None, "download failed: " + (last or "unknown error"), network_error=True) from None

    # Ported from agent2telegram telegram.py:send_plain_id (MIT). Delta: LaneKey target.
    def send_plain_id(self, key: LaneKey, text: str, *, parse_mode: str | None = None) -> int | None:
        """Send a message and return its message_id (for editable status bubbles)."""
        params = {**reply_params(key), "text": text, "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            return self._call("sendMessage", params, timeout=SEND_TIMEOUT).get("message_id")
        except TelegramError:
            return None

    # Ported from agent2telegram telegram.py:edit_plain (MIT). Delta: LaneKey target.
    def edit_plain(self, key: LaneKey, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        params = {**reply_params(key), "message_id": message_id, "text": text}
        if parse_mode:
            params["parse_mode"] = parse_mode
        try:
            self._call("editMessageText", params, timeout=SEND_TIMEOUT)
        except TelegramError:
            pass

    def send_chat_action(self, key: LaneKey, action: str = "typing"):
        try:
            # Cosmetic — fail fast (short timeout, no backoff) so a hiccup can't stall the turn.
            self._call("sendChatAction", {**reply_params(key), "action": action}, timeout=8, retries=0)
        except TelegramError:
            pass  # purely cosmetic; never let it break a turn

    def set_my_commands(self, commands: list[dict]) -> None:
        try:
            self._call("setMyCommands", {"commands": json.dumps(commands)}, timeout=15)
        except TelegramError as exc:
            log.warning("setMyCommands failed: %s", exc)

    def delete_message(self, key: LaneKey, message_id: int) -> None:
        try:
            self._call("deleteMessage", {**reply_params(key), "message_id": message_id}, timeout=SEND_TIMEOUT)
        except TelegramError:
            pass

    MAX_UPLOAD = 50 * 1024 * 1024
    _KINDS = {
        ".mp4": ("sendVideo", "video"), ".mov": ("sendVideo", "video"),
        ".m4v": ("sendVideo", "video"), ".webm": ("sendVideo", "video"),
        ".mp3": ("sendAudio", "audio"), ".m4a": ("sendAudio", "audio"),
        ".aac": ("sendAudio", "audio"), ".flac": ("sendAudio", "audio"),
        ".ogg": ("sendAudio", "audio"), ".opus": ("sendAudio", "audio"),
        ".wav": ("sendAudio", "audio"),
        ".jpg": ("sendPhoto", "photo"), ".jpeg": ("sendPhoto", "photo"),
        ".png": ("sendPhoto", "photo"), ".webp": ("sendPhoto", "photo"),
    }

    @classmethod
    def kind_for(cls, path) -> tuple[str, str]:
        """Pick the API method for a file. Unknown types go as a document, which always
        works and keeps the original bytes intact."""
        return cls._KINDS.get(os.path.splitext(str(path))[1].lower(), ("sendDocument", "document"))

    @staticmethod
    def _retry_after(err: HTTPError) -> int | None:
        if err.code != 429:
            return None
        try:
            payload = json.loads(err.read().decode("utf-8"))
            return int(payload.get("parameters", {}).get("retry_after", 1))
        except Exception:
            return int(err.headers.get("Retry-After", 1) or 1)

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 30))

    def _http_error(self, method: str, code: int, reason) -> TelegramError:
        if code == 401:
            return TelegramError(401, "Telegram rejected the token – copy it again in full from @BotFather")
        return TelegramError(code, self._safe_error(f"{method}: HTTP {code} {reason}"))

    def _call_multipart(self, method, fields, file_field, filename, payload, *, timeout=300):
        boundary = "----a2t" + secrets.token_hex(16)
        crlf = b"\r\n"
        body = bytearray()
        for key, val in fields.items():
            if val is None: continue
            body += b"--" + boundary.encode() + crlf
            body += f'Content-Disposition: form-data; name="{key}"'.encode() + crlf + crlf
            body += str(val).encode("utf-8") + crlf
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body += b"--" + boundary.encode() + crlf
        body += (f'Content-Disposition: form-data; name="{file_field}"; '
                 f'filename="{filename}"').encode() + crlf
        body += f"Content-Type: {ctype}".encode() + crlf + crlf + payload + crlf
        body += b"--" + boundary.encode() + b"--" + crlf
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        attempt = 0
        while True:
            attempt += 1
            if _real_transport_disabled() and not self._injected_opener:
                raise _PytestTransportError("Real Telegram uploads are disabled under pytest; inject a fake opener")
            req = urllib.request.Request(url, data=bytes(body), method="POST")
            req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
            try:
                with self._opener.open(req, timeout=timeout) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                if not out.get("ok"):
                    if out.get("error_code") == 401: raise self._http_error(method, 401, "")
                    raise TelegramError(out.get("error_code"), self._safe_error(f"{method}: {out.get('description', 'unknown error')}"))
                return out["result"]
            except HTTPError as e:
                retry_after = self._retry_after(e)
                if retry_after is not None:
                    log.warning("Flood control on %s, waiting %ss", method, retry_after)
                    time.sleep(retry_after + 0.5)
                    continue
                if e.code >= 500 and attempt <= self._max_retries:
                    log.warning("%s: HTTP %s, retry %d", method, e.code, attempt)
                    self._backoff(attempt)
                    continue
                raise self._http_error(method, e.code, e.reason) from None
            except (URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt <= self._max_retries:
                    log.warning("%s: %s, retry %d", method, self._safe_error(e), attempt)
                    self._backoff(attempt)
                    continue
                raise TelegramError(None, self._safe_error(f"{method}: {e}"), network_error=True) from None

    # Delta: returns the Telegram message (a2t returns None) so the lane can map reactions to its topic.
    def send_file(self, key: LaneKey, path, *, caption: str | None = None, meta: dict | None = None):
        path = os.fspath(path)
        size = os.path.getsize(path)
        if size > self.MAX_UPLOAD:
            raise TelegramError(None, f"{os.path.basename(path)} is {size // 1048576} MB; bots can send at most "
                                     f"{self.MAX_UPLOAD // 1048576} MB — share a link instead")
        method, field = self.kind_for(path)
        fields = {}
        if caption:
            fields["caption"] = caption[:1024]
        if method == "sendVideo":
            fields["supports_streaming"] = "true"
        fields.update(meta or {})
        # LaneKey owns the destination even when caller-supplied metadata has
        # colliding Telegram fields.
        fields.pop("chat_id", None)
        fields.pop("message_thread_id", None)
        fields.update(reply_params(key))
        with open(path, "rb") as fh:
            payload = fh.read()
        try:
            out = self._call_multipart(method, fields, field, os.path.basename(path), payload)
        except TelegramError:
            if method == "sendDocument":
                raise
            log.warning("%s rejected %s, retrying as a document",
                        method, os.path.basename(path))
            for name in ("supports_streaming", "width", "height", "duration", "title", "performer"):
                fields.pop(name, None)
            out = self._call_multipart("sendDocument", fields, "document", os.path.basename(path), payload)
            log.info("sent %s (sendDocument after %s, %d bytes)",
                     os.path.basename(path), method, size)
            return out
        log.info("sent %s (%s, %d bytes)", os.path.basename(path), method, size)
        return out

    def send_voice(self, key: LaneKey, path, *, duration: int | None = None):
        path = os.fspath(path)
        with open(path, "rb") as fh:
            payload = fh.read()
        fields = dict(reply_params(key))
        if duration:
            fields["duration"] = str(int(duration))
        out = self._call_multipart("sendVoice", fields, "voice", os.path.basename(path), payload)
        log.info("sent voice note (%d bytes, duration=%s)", len(payload), duration)
        return out


