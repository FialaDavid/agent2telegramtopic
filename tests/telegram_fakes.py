"""Test doubles for TelegramClient: an opener that replays a (method, params) -> dict function
the way the real Bot API answers (an ``ok: false`` body arrives as an HTTP error)."""
import io
import json
import urllib.parse
from urllib.error import HTTPError

_INT_KEYS = {"chat_id", "message_thread_id", "message_id", "offset", "timeout"}


def decode_params(data: bytes) -> dict:
    out = {k: v[0] for k, v in urllib.parse.parse_qs(data.decode(), keep_blank_values=True).items()}
    return {k: int(v) if k in _INT_KEYS else v for k, v in out.items()}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TransportOpener:
    def __init__(self, fn):
        self.fn = fn

    def open(self, req, timeout=None):
        method = req.full_url.rsplit("/", 1)[1]
        result = self.fn(method, decode_params(req.data or b""))
        if result.get("ok") is False:
            code = result.get("error_code", 400)
            raise HTTPError(req.full_url, code, result.get("description", "Error"), {},
                            io.BytesIO(json.dumps(result).encode()))
        result = dict(result)
        result.setdefault("result", {})
        return _Response(json.dumps(result).encode())
