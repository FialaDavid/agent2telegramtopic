from agent2telegramtopic.routing import GENERAL_THREAD_ID, LaneKey
from agent2telegramtopic.telegram import (TelegramClient, TelegramError, split_message,
                                   MAX_MESSAGE_LEN, RICH_MESSAGE_MAX_CHARS, SEND_TIMEOUT)
from urllib.error import HTTPError, URLError
import json
import traceback

import pytest

from telegram_fakes import TransportOpener


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr("agent2telegramtopic.telegram.time.sleep", waits.append)
    return waits


class FakeTransport:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"ok": True, "result": []}

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "sendRichMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        return self.response


def test_send_message_and_chat_action_route_general_and_topic():
    transport = FakeTransport()
    client = TelegramClient("secret", opener=TransportOpener(transport))
    client.send_message(LaneKey(12, GENERAL_THREAD_ID), "general")
    client.send_chat_action(LaneKey(12, GENERAL_THREAD_ID))
    client.send_message(LaneKey(12, 44), "topic")
    client.send_chat_action(LaneKey(12, 44))

    assert "message_thread_id" not in transport.calls[0][1]
    assert "message_thread_id" not in transport.calls[1][1]
    assert [p["message_thread_id"] for _, p in transport.calls[2:]] == [44, 44]
    assert all(p["chat_id"] == 12 for _, p in transport.calls)


def test_long_message_is_split_in_order_with_newline_preference():
    text = ("a" * 3000) + "\n" + ("b" * 2000) + "\n" + ("c" * 1500)
    transport = FakeTransport()
    chunks = split_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_MESSAGE_LEN for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_api_error_and_token_redaction():
    token = "sensitive-token"
    transport = FakeTransport({"ok": False, "error_code": 401, "description": f"bad {token}"})
    client = TelegramClient(token, opener=TransportOpener(transport))
    assert token not in str(client)
    assert token not in repr(client)
    try:
        client.get_me()
    except TelegramError as error:
        assert error.error_code == 401
        assert token not in str(error)
        assert token not in repr(error)
    else:
        assert False, "expected TelegramError"


def test_transport_error_redacts_token_bearing_url():
    token = "sensitive-token"

    def failing_transport(method, params):
        raise URLError(f"failed at https://api.telegram.org/bot{token}/getMe")

    try:
        TelegramClient(token, opener=TransportOpener(failing_transport), max_retries=0).get_me()
    except TelegramError as error:
        assert token not in str(error)
        assert token not in repr(error)
        formatted = "".join(traceback.format_exception(error))
        assert token not in formatted
        assert "[REDACTED]" in formatted
    else:
        assert False, "expected transport exception"


def test_get_updates():
    transport = FakeTransport({"ok": True, "result": [{"update_id": 2}]})
    client = TelegramClient("secret", opener=TransportOpener(transport))
    assert client.get_updates(7) == [{"update_id": 2}]
    assert transport.calls == [("getUpdates", {"timeout": 30, "allowed_updates": '["message"]', "offset": 7})]


def test_split_message_prefers_paragraph_line_and_word_boundaries():
    text = ("a" * 2500) + "\n\n" + ("b" * 2500) + " " + ("c" * 2500)
    chunks = split_message(text)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")
    assert all(len(chunk) <= MAX_MESSAGE_LEN for chunk in chunks)


def test_rich_400_falls_back_to_html_and_preserves_topic_on_every_call():
    calls = []
    responses = iter([
        {"ok": False, "error_code": 400, "description": "bad rich message"},
        {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(LaneKey(1, 44), "**bold**")
    assert [method for method, _ in calls] == ["sendRichMessage", "sendMessage"]
    assert all(params["message_thread_id"] == 44 for _, params in calls)
    assert calls[1][1]["text"] == "<b>bold</b>"
    assert calls[1][1]["parse_mode"] == "HTML"


def test_general_topic_sends_omit_thread_id_for_all_send_paths():
    def send_all_paths(thread_id):
        calls = []
        rich_responses = iter([
            {"ok": True, "result": {}},
            {"ok": False, "error_code": 400, "description": "bad rich message"},
            {"ok": True, "result": {}},
        ])

        def transport(method, params):
            calls.append((method, dict(params)))
            if method == "sendRichMessage":
                return next(rich_responses)
            return {"ok": True, "result": {}}

        client = TelegramClient("secret", opener=TransportOpener(transport))
        key = LaneKey(1, thread_id)
        client.send_message(key, "rich")
        client.send_message(key, "fallback")
        client.send_message(key, "plain", parse_mode=None)
        return calls

    general_calls = send_all_paths(1)
    assert [method for method, _ in general_calls] == [
        "sendRichMessage", "sendRichMessage", "sendMessage", "sendMessage",
    ]
    assert all("message_thread_id" not in params for _, params in general_calls)

    topic_calls = send_all_paths(77)
    assert all(params["message_thread_id"] == 77 for _, params in topic_calls)


def test_html_send_rejection_falls_back_to_plain_text():
    calls = []
    responses = iter([
        {"ok": False, "error_code": 400, "description": "bad rich message"},
        {"ok": False, "error_code": 400, "description": "bad html"},
        {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(LaneKey(1, 45), "**bold**")
    assert [method for method, _ in calls] == ["sendRichMessage", "sendMessage", "sendMessage"]
    assert calls[1][1]["text"] == "<b>bold</b>"
    assert calls[2][1]["text"] == "bold"
    assert "parse_mode" not in calls[2][1]
    assert all(params["message_thread_id"] == 45 for _, params in calls)


def test_markdown_parse_mode_retries_as_plain_text():
    calls = []
    responses = iter([
        {"ok": False, "error_code": 400, "description": "bad markdown"},
        {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(
        LaneKey(1, 47), "*bold*", parse_mode="Markdown")
    assert [method for method, _ in calls] == ["sendMessage", "sendMessage"]
    assert calls[0][1]["parse_mode"] == "Markdown"
    assert calls[1][1]["text"] == "*bold*"
    assert "parse_mode" not in calls[1][1]
    assert all(params["message_thread_id"] == 47 for _, params in calls)


def test_rich_404_latches_for_second_send_and_plain_parse_mode_sends_once():
    calls = []
    responses = iter([
        {"ok": False, "error_code": 404, "description": "missing"},
        {"ok": True, "result": {}}, {"ok": True, "result": {}}, {"ok": True, "result": {}}, {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    client = TelegramClient("secret", opener=TransportOpener(transport))
    client.send_message(LaneKey(1, 9), "first")
    client.send_message(LaneKey(1, 9), "second")
    client.send_message(LaneKey(1, 9), "plain", parse_mode=None)
    assert [method for method, _ in calls] == ["sendRichMessage", "sendMessage", "sendMessage", "sendMessage"]
    assert all(params["message_thread_id"] == 9 for _, params in calls)
    assert calls[-1][1]["text"] == "plain"


def test_rich_400_does_not_disable_rich_for_next_send():
    calls = []
    responses = iter([
        {"ok": False, "error_code": 400, "description": "bad rich message"},
        {"ok": True, "result": {}},
        {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    client = TelegramClient("secret", opener=TransportOpener(transport))
    client.send_message(LaneKey(1, 46), "first")
    client.send_message(LaneKey(1, 46), "second")
    assert [method for method, _ in calls] == ["sendRichMessage", "sendMessage", "sendRichMessage"]
    assert all(params["message_thread_id"] == 46 for _, params in calls)


def test_rich_404_mid_message_latches_remaining_chunks_to_legacy():
    calls = []
    def transport(method, params):
        calls.append((method, dict(params)))
        if len(calls) == 2:
            return {"ok": False, "error_code": 404, "description": "missing"}
        return {"ok": True, "result": {}}
    text = "a" * (RICH_MESSAGE_MAX_CHARS * 2) + "tail"
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(LaneKey(1, 17), text)
    assert [m for m, _ in calls[:2]] == ["sendRichMessage", "sendRichMessage"]
    assert all(m == "sendMessage" for m, _ in calls[2:])
    assert all(p["message_thread_id"] == 17 for _, p in calls)
    assert "".join(p["text"] for _, p in calls[2:]) == "a" * RICH_MESSAGE_MAX_CHARS + "tail"


def test_rich_success_then_400_falls_back_only_for_rejected_chunk():
    calls = []
    responses = iter([
        {"ok": True, "result": {}},
        {"ok": False, "error_code": 400, "description": "bad rich message"},
        {"ok": True, "result": {}},
    ])
    def transport(method, params):
        calls.append((method, dict(params)))
        return next(responses)
    first = "a" * RICH_MESSAGE_MAX_CHARS
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(LaneKey(1, 18), first + "tail")
    assert [m for m, _ in calls] == ["sendRichMessage", "sendRichMessage", "sendMessage"]
    assert all(p["message_thread_id"] == 18 for _, p in calls)
    assert json.loads(calls[0][1]["rich_message"])["markdown"] == first
    assert calls[2][1]["text"] == "tail"


def test_malformed_html_uses_plain_fallback(monkeypatch):
    import agent2telegramtopic.telegram as telegram
    calls = []
    monkeypatch.setattr(telegram, "markdown_to_html", lambda _: "<b>broken")
    def transport(method, params):
        calls.append((method, dict(params)))
        return {"ok": True, "result": {}}
    TelegramClient("secret", opener=TransportOpener(transport))._send_legacy(LaneKey(1, 19), "**plain**")
    assert len(calls) == 1
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["text"] == "plain"
    assert "parse_mode" not in calls[0][1]
    assert calls[0][1]["message_thread_id"] == 19


def test_rich_over_legacy_limit_is_one_send():
    calls = []
    TelegramClient("secret", opener=TransportOpener(lambda method, params: calls.append((method, params)) or {"ok": True})).send_message(
        LaneKey(1, 6), "x" * 5440)
    assert 5440 < RICH_MESSAGE_MAX_CHARS
    assert len(calls) == 1 and calls[0][0] == "sendRichMessage"
    assert calls[0][1]["message_thread_id"] == 6


def test_parse_mode_none_splits_long_text_into_legacy_chunks():
    text = "😀" * 5000
    transport = FakeTransport()
    TelegramClient("secret", opener=TransportOpener(transport)).send_message(LaneKey(1, 1), text, parse_mode=None)
    calls = [params for method, params in transport.calls if method == "sendMessage"]
    assert len(calls) == 2
    assert "".join(call["text"] for call in calls) == text
    assert all("message_thread_id" not in call for call in calls)


def test_rate_limit_waits_retry_after_then_succeeds(sleeps):
    calls = 0
    def transport(method, params):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"ok": False, "error_code": 429, "description": "Too Many Requests",
                    "parameters": {"retry_after": 7}}
        return {"ok": True, "result": {"username": "bot"}}
    client = TelegramClient("secret", opener=TransportOpener(transport))
    assert client.get_me() == {"username": "bot"}
    assert calls == 2
    assert sleeps == [7.5]


def test_flood_waits_do_not_count_as_failed_attempts(sleeps):
    # a2t: "do not count flood waits as failures" - three 429s still succeed with max_retries=0.
    calls = 0
    def transport(method, params):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return {"ok": False, "error_code": 429, "description": "limited",
                    "parameters": {"retry_after": 1}}
        return {"ok": True, "result": True}
    assert TelegramClient("secret", opener=TransportOpener(transport), max_retries=0).get_me() is True
    assert calls == 4 and sleeps == [1.5, 1.5, 1.5]


def test_network_error_retries_with_bounded_backoff(sleeps):
    calls = 0
    def transport(method, params):
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ConnectionError("offline")
        return {"ok": True, "result": {}}
    TelegramClient("secret", opener=TransportOpener(transport)).get_me()
    assert calls == 4
    assert sleeps == [2, 4, 8]


def test_persistent_network_error_raises_after_bounded_retries_with_redaction(sleeps):
    token = "123456:abcdefghijklmnopqrstuvwxyz"
    calls = []
    def transport(method, params):
        calls.append(1)
        raise ConnectionError("failed https://api.telegram.org/bot" + token + "/getMe")
    try:
        TelegramClient(token, opener=TransportOpener(transport), max_retries=2).get_me()
    except TelegramError as error:
        assert token not in str(error)
        assert error.network_error is True
        assert len(calls) == 3
        assert sleeps == [2, 4]
    else:
        assert False, "expected TelegramError"


def test_zero_retries_fails_on_first_error(sleeps):
    calls = []
    def transport(method, params):
        calls.append(method)
        raise ConnectionError("offline")
    client = TelegramClient("secret", opener=TransportOpener(transport))
    try:
        client._call("getMe", retries=0)
    except TelegramError:
        pass
    else:
        assert False, "expected TelegramError"
    assert calls == ["getMe"] and sleeps == []


def test_chat_action_is_attempted_once_and_never_raises(sleeps):
    calls = []
    def transport(method, params):
        calls.append((method, params))
        raise ConnectionError("offline")
    client = TelegramClient("secret", opener=TransportOpener(transport))
    client.send_chat_action(LaneKey(1, 44))
    assert [m for m, _ in calls] == ["sendChatAction"]
    assert calls[0][1]["message_thread_id"] == 44
    assert sleeps == []


def test_chat_action_5xx_is_not_retried(sleeps):
    calls = []
    def transport(method, params):
        calls.append(method)
        return {"ok": False, "error_code": 502, "description": "gateway"}
    TelegramClient("secret", opener=TransportOpener(transport)).send_chat_action(LaneKey(1, 1))
    assert calls == ["sendChatAction"] and sleeps == []


def test_5xx_on_rich_retries_then_reaches_caller_without_legacy_resend(sleeps):
    calls = []
    def transport(method, params):
        calls.append(method)
        return {"ok": False, "error_code": 502, "description": "gateway"}
    try:
        TelegramClient("secret", opener=TransportOpener(transport), max_retries=2).send_message(
            LaneKey(1, 1), "reply")
    except TelegramError as error:
        assert error.error_code == 502
    else:
        assert False, "persistent rich send failure must reach the caller"
    assert calls == ["sendRichMessage"] * 3
    assert sleeps == [2, 4]


def test_real_http_transport_is_disabled_under_pytest(monkeypatch):
    import os
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard test")
    try:
        TelegramClient("secret").get_me()
    except RuntimeError as error:
        assert "disabled under pytest" in str(error)
    else:
        assert False, "expected test safety guard"


def test_real_multipart_transport_is_disabled_under_pytest(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard test")
    path = tmp_path / "photo.png"
    path.write_bytes(b"png")
    try:
        TelegramClient("secret").send_file(LaneKey(1, 22), path)
    except RuntimeError as error:
        assert "disabled under pytest" in str(error)
    else:
        assert False, "expected upload safety guard"


def test_real_download_transport_is_disabled_under_pytest(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "guard test")
    try:
        TelegramClient("secret").download("photos/image.png")
    except RuntimeError as error:
        assert "disabled under pytest" in str(error)
    else:
        assert False, "expected download safety guard"


def test_persistent_non_json_transport_error_is_redacted_telegram_error(sleeps):
    token = "7654321:abcdefghijklmnopqrstuvwxyzABCDEF"
    def transport(method, params):
        raise json.JSONDecodeError("bad " + token, "not-json", 0)
    try:
        TelegramClient(token, opener=TransportOpener(transport), max_retries=1).get_me()
    except TelegramError as error:
        assert error.error_code is None
        assert token not in str(error)
    else:
        assert False, "expected TelegramError"


def test_http_5xx_retries_with_bounded_backoff(sleeps):
    calls = []
    def transport(method, params):
        calls.append(1)
        return {"ok": False, "error_code": 502, "description": "temporary"} if len(calls) < 3 else {"ok": True, "result": {"ok": True}}
    assert TelegramClient("secret", opener=TransportOpener(transport), max_retries=2).get_me() == {"ok": True}
    assert len(calls) == 3 and sleeps == [2, 4]


def test_401_gives_token_hint_and_keeps_error_code():
    try:
        TelegramClient("secret", opener=TransportOpener(lambda m, p: {"ok": False, "error_code": 401,
                                                  "description": "Unauthorized"})).get_me()
    except TelegramError as error:
        assert error.error_code == 401
        assert "rejected the token" in str(error)
    else:
        assert False, "expected TelegramError"


def test_http_error_non_json_falls_back_to_reason():
    err = HTTPError("https://example", 502, "gateway token=secret", {}, None)
    def transport(method, params):
        raise err
    try:
        TelegramClient("secret", opener=TransportOpener(transport), max_retries=0).get_me()
    except TelegramError as error:
        assert error.error_code == 502
        assert "gateway" in str(error)
        assert "secret" not in str(error)
    else:
        assert False, "expected TelegramError"


class _FakeResponse:
    def __init__(self, value):
        self.data = value if isinstance(value, bytes) else json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.data


class _FakeOpener:
    def __init__(self, script): self.script, self.calls = list(script), []
    def open(self, req, timeout=None):
        self.calls.append(req)
        result = self.script.pop(0)
        if isinstance(result, BaseException): raise result
        return _FakeResponse(result)


def _ok(result=None): return {"ok": True, "result": result or {}}


def _http_error(code, description, *, headers=None, payload=None):
    import io
    body = json.dumps(payload or {"description": description}).encode()
    return HTTPError("https://api.telegram.org/example", code, description,
                     headers or {}, io.BytesIO(body))


def _multipart_fields(request):
    body = request.data
    fields = {}
    import re
    for name, value in re.findall(rb'name="([^"\r]+)"\r\n\r\n([^\r]*)', body):
        fields[name.decode()] = value.decode()
    return fields


def test_photo_rejected_falls_back_to_document(tmp_path):
    op = _FakeOpener([_http_error(400, "photo rejected"), _ok({"message_id": 7})])
    path = tmp_path / "chart.png"; path.write_bytes(b"png")
    TelegramClient("secret", opener=op).send_file(LaneKey(42, 77), path, caption="graf")
    assert [r.full_url.rsplit("/", 1)[-1] for r in op.calls] == ["sendPhoto", "sendDocument"]
    assert all((_multipart_fields(r).get("chat_id"), _multipart_fields(r).get("message_thread_id")) == ("42", "77") for r in op.calls)
    assert _multipart_fields(op.calls[1])["caption"] == "graf"


def test_document_failure_is_not_retried_forever(tmp_path):
    op = _FakeOpener([_http_error(400, "bad document")])
    path = tmp_path / "notes.xyz"; path.write_bytes(b"x")
    try: TelegramClient("secret", opener=op).send_file(LaneKey(42, 1), path)
    except TelegramError as error: assert "bad document" in str(error)
    else: assert False, "expected TelegramError"
    assert len(op.calls) == 1 and _multipart_fields(op.calls[0])["chat_id"] == "42"
    assert "message_thread_id" not in _multipart_fields(op.calls[0])


def test_successful_photo_sends_once(tmp_path):
    op = _FakeOpener([_ok({"message_id": 3})]); path = tmp_path / "photo.jpg"; path.write_bytes(b"jpg")
    assert TelegramClient("secret", opener=op).send_file(LaneKey(1, 55), path) == {"message_id": 3}
    assert len(op.calls) == 1
    assert op.calls[0].full_url.endswith("/sendPhoto")
    fields = _multipart_fields(op.calls[0]); assert fields["chat_id"] == "1" and fields["message_thread_id"] == "55"


def test_video_meta_dropped_on_document_fallback(tmp_path):
    op = _FakeOpener([_http_error(400, "bad video"), _ok({})])
    path = tmp_path / "reel.mp4"; path.write_bytes(b"video")
    TelegramClient("secret", opener=op).send_file(LaneKey(1, 22), path, meta={"width": 1080, "height": 1920, "duration": 30})
    fields = _multipart_fields(op.calls[1]); assert fields["chat_id"] == "1" and fields["message_thread_id"] == "22"
    assert not any(k in fields for k in ("width", "height", "duration", "supports_streaming"))


def test_upload_429_waits_then_retries_same_method(monkeypatch, tmp_path):
    import agent2telegramtopic.telegram as telegram
    waits=[]; monkeypatch.setattr(telegram.time, "sleep", waits.append)
    err=_http_error(429, "Too Many Requests", headers={"Retry-After":"7"},
                    payload={"description":"Too Many Requests", "parameters":{"retry_after":7}})
    op=_FakeOpener([err,_ok({"message_id":11})]); path=tmp_path/"photo.png"; path.write_bytes(b"x")
    result=TelegramClient("secret",opener=op).send_file(LaneKey(6,32),path)
    assert result == {"message_id": 11} and len(op.calls)==2 and waits==[7.5]
    assert all(_multipart_fields(r)["message_thread_id"]=="32" for r in op.calls)


def test_upload_respects_configured_retry_limit(monkeypatch, tmp_path):
    import agent2telegramtopic.telegram as telegram
    waits = []
    monkeypatch.setattr(telegram.time, "sleep", waits.append)
    op = _FakeOpener([_http_error(500, "temporary")])
    path = tmp_path / "notes.xyz"
    path.write_bytes(b"x")

    try:
        TelegramClient("secret", opener=op, max_retries=0).send_file(LaneKey(6, 32), path)
    except TelegramError as error:
        assert error.error_code == 500
    else:
        assert False, "expected TelegramError"

    assert len(op.calls) == 1
    assert waits == []
    fields = _multipart_fields(op.calls[0])
    assert fields["chat_id"] == "6" and fields["message_thread_id"] == "32"


def test_chat_action_failure_is_swallowed():
    calls = []

    def transport(method, params):
        calls.append((method, params))
        return {"ok": False, "error_code": 400, "description": "Bad Request"}

    assert TelegramClient("secret", opener=TransportOpener(transport)).send_chat_action(LaneKey(3, 42)) is None
    assert len(calls) == 1
    assert calls[0][1]["message_thread_id"] == 42


def test_send_file_general_omits_thread_and_meta_cannot_change_lane(tmp_path):
    op=_FakeOpener([_ok({"message_id":5})]); path=tmp_path/"clip.mp4"; path.write_bytes(b"video")
    TelegramClient("secret",opener=op).send_file(LaneKey(9,1),path,meta={"chat_id":999,"message_thread_id":88})
    fields=_multipart_fields(op.calls[0]); assert fields["chat_id"]=="9" and "message_thread_id" not in fields


def test_voice_upload_is_targeted_and_general_omits_thread(tmp_path):
    op=_FakeOpener([_ok(),_ok()]); path=tmp_path/"note.ogg"; path.write_bytes(b"OggS")
    client=TelegramClient("secret",opener=op); client.send_voice(LaneKey(7,19),path,duration=4); client.send_voice(LaneKey(7,1),path)
    assert all(r.full_url.endswith("/sendVoice") for r in op.calls)
    assert _multipart_fields(op.calls[0])["chat_id"]=="7" and _multipart_fields(op.calls[0])["message_thread_id"]=="19"
    assert "message_thread_id" not in _multipart_fields(op.calls[1])


def test_download_failure_redacts_token_url(monkeypatch):
    import agent2telegramtopic.telegram as telegram
    token="123456:secret"; url="https://api.telegram.org/file/bot"+token+"/photos/secret.jpg"
    monkeypatch.setattr(telegram,"_real_transport_disabled",lambda:False)
    from urllib.error import URLError
    opener=_FakeOpener([URLError("failed "+url)]*3)
    try: TelegramClient(token,opener=opener).download("photos/secret.jpg")
    except TelegramError as error: assert token not in str(error) and url not in str(error)
    else: assert False,"expected download failure"


def test_download_http_error_uses_safe_http_reason():
    token = "123456:secret"
    url = "https://api.telegram.org/file/bot" + token + "/private/file.jpg"
    err = _http_error(400, "Bad Request", payload={"description": "bad file at " + url})
    opener = _FakeOpener([err])
    try: TelegramClient(token, opener=opener).download("private/file.jpg")
    except TelegramError as error:
        assert "Bad Request" in str(error)
        assert "bad file at" not in str(error)
        assert token not in str(error) and url not in str(error)
    else: assert False, "expected download error"


def test_file_and_command_calls_reach_telegram_http_transport():
    calls = []

    def transport(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"file_path": "docs/report.pdf"} if method == "getFile" else True}

    client = TelegramClient("secret", opener=TransportOpener(transport))
    assert client.get_file_path("file-id") == "docs/report.pdf"
    client.set_my_commands([{"command": "help", "description": "Help"}])
    client.delete_message(LaneKey(1, 7), 9)

    assert [method for method, _ in calls] == ["getFile", "setMyCommands", "deleteMessage"]
    assert calls[0][1] == {"file_id": "file-id"}
    assert json.loads(calls[1][1]["commands"]) == [{"command": "help", "description": "Help"}]
    assert calls[2][1]["chat_id"] == 1 and calls[2][1]["message_id"] == 9
    assert calls[2][1]["message_thread_id"] == 7


def test_http_error_json_body_exposes_description_and_code():
    body = json.dumps({"description": "bad request"}).encode()
    err = HTTPError("https://example", 403, "forbidden", {}, __import__("io").BytesIO(body))
    class Opener:
        def open(self, req, timeout=None):
            raise err
    try:
        TelegramClient("secret", opener=Opener()).get_me()
    except TelegramError as error:
        assert error.error_code == 403
        assert "bad request" in str(error)
    else:
        assert False, "expected TelegramError"


def test_non_json_502_is_retried_and_keeps_error_code(sleeps):
    from io import BytesIO
    calls = []
    class Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            raise HTTPError(req.full_url, 502, "Bad Gateway", {}, BytesIO(b"<html>nginx</html>"))

    try:
        TelegramClient("secret", opener=Opener(), max_retries=1).get_me()
    except TelegramError as error:
        assert error.error_code == 502
        assert len(calls) == 2
        assert "502" in str(error)
    else:
        assert False, "expected TelegramError"


# Ported from agent2telegram tests/test_telegram.py (CallErrorClassificationTests, test_api_error_raises).
def test_ok_false_body_with_http_200_raises_and_is_not_retried(sleeps):
    op = _FakeOpener([{"ok": False, "error_code": 400, "description": "Unauthorized"}])
    try:
        TelegramClient("123:abc", opener=op).get_me()
    except TelegramError as error:
        assert error.error_code == 400
    else:
        assert False, "expected TelegramError"
    assert len(op.calls) == 1 and sleeps == []


def test_call_http_error_json_body_exposes_description_and_error_code():
    op = _FakeOpener([_http_error(400, "can't parse rich message JSON object")])
    try:
        TelegramClient("123:abc", opener=op, max_retries=0)._call("sendRichMessage", {"chat_id": 1}, timeout=5)
    except TelegramError as error:
        assert error.error_code == 400
        assert "can't parse rich message JSON object" in str(error)
    else:
        assert False, "expected TelegramError"


def test_call_token_in_error_description_is_redacted_but_code_kept():
    token = "123456:SyntheticTokenNeverPublish"
    op = _FakeOpener([_http_error(400, "bad request for " + token)])
    try:
        TelegramClient(token, opener=op, max_retries=0)._call("sendMessage", {"chat_id": 1}, timeout=5)
    except TelegramError as error:
        assert token not in str(error)
        assert "bad request for" in str(error)
        assert error.error_code == 400
    else:
        assert False, "expected TelegramError"


def test_call_http_error_with_non_json_body_falls_back_to_reason():
    import io
    err = HTTPError("url", 502, "Bad Gateway", {}, io.BytesIO(b"<html>not json</html>"))
    try:
        TelegramClient("123:abc", opener=_FakeOpener([err]), max_retries=0)._call("sendRichMessage", {}, timeout=5)
    except TelegramError as error:
        assert error.error_code == 502
        assert "Bad Gateway" in str(error)
    else:
        assert False, "expected TelegramError"


def test_send_message_returns_the_ids_of_every_message_it_sent_in_its_topic():
    ids = iter(range(10, 20))
    calls = []
    def transport(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": next(ids)}}
    client = TelegramClient("secret", opener=TransportOpener(transport))
    assert client.send_message(LaneKey(1, 44), "hello") == [10]
    assert client.send_message(LaneKey(1, 44), "😀" * 5000, parse_mode=None) == [11, 12]
    assert all(p["message_thread_id"] == 44 for _, p in calls)
