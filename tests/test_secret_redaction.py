import json
import logging

from agent2telegramtopic.redaction import (MASK, RedactingFilter, install_log_redaction,
                                    message_metadata, redact, update_metadata)

SECRET = "SyntheticCredentialNeverPublish123456789"


def samples():
    for key in ("api_key", "apiKey", "token", "refresh_token", "password", "secret", "authorization"):
        yield json.dumps({key: SECRET})
        yield "tool --" + key + " " + SECRET
    yield "X=sk-proj-" + SECRET + " command"
    yield "Authorization: Bearer " + SECRET
    yield "https://user:" + SECRET + "@example.org/?token=" + SECRET
    yield "https://api.example.org/bot1234567:" + SECRET + "/getMe"
    yield json.dumps(json.dumps({'api_key': SECRET}))
    yield 'echo ' + json.dumps(json.dumps({'password': SECRET}))


def test_samples_are_redacted():
    for sample in samples():
        assert SECRET not in redact(sample)


def test_env_keeps_safe_values_around_token():
    text = "PORT=8080\nTELEGRAM_BOT_TOKEN=1234567:" + SECRET + "\nMODE=prod"
    assert redact(text) == "PORT=8080\nTELEGRAM_BOT_TOKEN=[REDACTED]\nMODE=prod"


def test_env_replacement_has_no_extra_bracket():
    assert redact("TELEGRAM_BOT_TOKEN=" + SECRET) == "TELEGRAM_BOT_TOKEN=[REDACTED]"


def test_empty_secret_does_not_consume_next_line():
    text = "TOKEN=\nMODE=prod\nPORT=8080"
    assert redact(text) == text


def test_redaction_is_idempotent():
    for text in ("TOKEN=synthetic-value", "TOKEN='synthetic value'", "TOKEN=[REDACTED]",
                 "tool --token synthetic-value", "TOKEN=https://example.org/?token=synthetic-value"):
        once = redact(text)
        assert "[REDACTED]]" not in once
        assert redact(once) == once


def test_env_secret_keys():
    for key in ("KEY", "SERVICE_KEY", "AUTH", "AUTH_HEADER", "TOKEN_FILE", "PASSWORD", "SECRET_ACCESS_KEY"):
        assert redact(key + "=synthetic-value") == key + "=[REDACTED]"


def test_neutral_env_key_redacts_recognized_token():
    assert redact("X=sk-proj-" + SECRET) == "X=[REDACTED]"


def test_safe_text_and_selective_json_unchanged():
    text = "PORT=8080\nMODE=prod\nMONKEY=capuchin\ngit status --short"
    assert redact(text) == text
    assert json.loads(redact(json.dumps({"PORT": 8080, "MODE": "prod", "token": SECRET}))) == {
        "PORT": 8080, "MODE": "prod", "token": MASK
    }


def test_message_and_update_metadata_are_content_free():
    assert SECRET not in message_metadata("private " + SECRET)
    metadata = update_metadata({"message": {"message_id": 8, "text": "private " + SECRET}})
    assert "private" not in metadata and SECRET not in metadata
    assert "message_id=8" in metadata


def test_redacting_filter_masks_message_and_exception_traceback():
    filt = RedactingFilter()
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "token=%s", (SECRET,), None)
    assert filt.filter(record)
    assert SECRET not in record.getMessage()
    bot_token = "1234567:abcdefghijklmnopqrstuvwxyzABCDEF"
    try:
        raise RuntimeError("request https://api.telegram.org/bot" + bot_token + "/getMe")
    except RuntimeError:
        import sys
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    assert filt.filter(record)
    assert bot_token not in record.exc_text
    assert record.exc_info is None


def test_install_log_redaction_adds_filter_once():
    logger = logging.Logger("isolated")
    handler = logging.NullHandler()
    logger.addHandler(handler)
    install_log_redaction(logger)
    install_log_redaction(logger)
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
