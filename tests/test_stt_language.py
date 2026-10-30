"""Transcription language and key validation.

A three-second Czech voice note came back as the English word "Down": Scribe was never told
the language, so it guessed — and on a short clip it guesses badly.
"""
import unittest

from agent2telegramtopic import stt


class LanguageIsSent(unittest.TestCase):
    def _capture(self, **kw):
        """Run a transcription against a fake opener and return the request body."""
        captured = {}

        class FakeResp:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner): return b'{"text": "hello"}'

        class FakeOpener:
            def open(self_inner, req, timeout=None):
                captured["body"] = req.data.decode("utf-8", "replace")
                return FakeResp()

        text = stt.transcribe_elevenlabs(b"audio", api_key="sk_x", opener=FakeOpener(), **kw)
        return text, captured["body"]

    def test_language_appears_in_the_request(self):
        text, body = self._capture(language="cs")
        self.assertEqual(text, "hello")
        self.assertIn("language_code", body)
        self.assertIn("cs", body)

    def test_no_language_means_no_field(self):
        """Auto-detect stays available — we must not force a language on everyone."""
        _, body = self._capture()
        self.assertNotIn("language_code", body)

    def test_model_is_always_sent(self):
        _, body = self._capture(language="de")
        self.assertIn("scribe_v1", body)

    def test_dispatcher_passes_the_language_through(self):
        seen = {}
        orig = stt.transcribe_elevenlabs
        stt.transcribe_elevenlabs = lambda audio, **kw: seen.update(kw) or "x"
        try:
            stt.transcribe(b"a", api_key="sk_x", language="cs")
        finally:
            stt.transcribe_elevenlabs = orig
        self.assertEqual(seen.get("language"), "cs")


class KeyShape(unittest.TestCase):
    """The web UI offers "Copy Key ID" but never shows the key again, so pasting the ID is
    the easy mistake. Caught here it costs one retype; caught later it looks like a broken
    bridge answering HTTP 400."""

    def test_key_id_is_rejected(self):
        self.assertFalse(stt.looks_like_api_key("a21b9f0c4e2d4f8b9c1a2b3c4d5e6f70"))

    def test_real_key_is_accepted(self):
        self.assertTrue(stt.looks_like_api_key("sk_abc123"))

    def test_blank_is_rejected(self):
        self.assertFalse(stt.looks_like_api_key(""))


