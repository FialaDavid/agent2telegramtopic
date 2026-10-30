"""Lane-scoped voice reply behavior, adapted from a2t test_voice.py (MIT)."""
import tempfile
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent2telegramtopic.config import Config
from agent2telegramtopic.durable import DurableOutbox
from agent2telegramtopic.lane import LaneBridge, VOICE_MAX_CHARS, VOICE_MODE_HINT
from agent2telegramtopic.routing import LaneKey, reply_params
from agent2telegramtopic.telegram import TelegramClient


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.voices = []

    def send_message(self, key, text):
        self.sent.append((key, text, reply_params(key)))

    def send_voice(self, key, path, **kwargs):
        payload = path if isinstance(path, bytes) else Path(path).read_bytes()
        self.voices.append((key, payload, kwargs, reply_params(key)))

    def send_chat_action(self, *_args):
        pass


class FakeSession:
    def __init__(self):
        self.injected = []

    def inject(self, text):
        self.injected.append(text)


def make_lane(root, key, client=None, *, config=None):
    state = Path(root) / str(key.chat_id) / str(key.thread_id)
    client = client or FakeTelegram()
    cfg = config or Config("test", "codex", frozenset({1}))
    lane = LaneBridge(client, key, state / "sent", state / "queue", config=cfg)
    lane._session = FakeSession()
    return lane, client


def test_voice_mode_persists_and_toggles_per_lane(tmp_path):
    key = LaneKey(55, 7)
    lane, _ = make_lane(tmp_path, key)
    assert not lane._voice_reply_on()
    lane._toggle_voice(key.chat_id)
    assert lane._voice_reply_on()
    assert (tmp_path / "55/7/voice_mode").read_text() == "on"
    resumed, _ = make_lane(tmp_path, key)
    assert resumed._voice_reply_on()
    resumed._toggle_voice(key.chat_id)
    assert not resumed._voice_reply_on()
    assert (tmp_path / "55/7/voice_mode").read_text() == "off"


def test_voice_mode_in_one_topic_does_not_affect_another(tmp_path):
    first, _ = make_lane(tmp_path, LaneKey(55, 7))
    second, _ = make_lane(tmp_path, LaneKey(55, 8))
    first._toggle_voice(55)
    assert first._voice_reply_on()
    assert not second._voice_reply_on()
    assert (tmp_path / "55/7/voice_mode").exists()
    assert not (tmp_path / "55/8/voice_mode").exists()

    client = FakeTelegram()
    first.tg.client = second.tg.client = client
    def fake_convert(args, **kwargs):
        Path(args[-1]).write_bytes(b"ogg")
        return type("Result", (), {"returncode": 0, "stderr": b""})()
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.lane.tts.sanitize_for_speech", return_value="Hi."), \
         patch("agent2telegramtopic.lane.tts.synthesize", return_value=b"audio"), \
         patch("agent2telegramtopic.lane.subprocess.run", side_effect=fake_convert), \
         patch("agent2telegramtopic.lane._ogg_duration", return_value=1):
        first._send_final("Hi.", key="a")
        second._send_final("Hi.", key="b")
        second._flush_pending()
    assert [v[0] for v in client.voices] == [LaneKey(55, 7)]
    assert [(m[0], m[1]) for m in client.sent] == [(LaneKey(55, 8), "Hi.")]


def test_voice_hint_is_injected_only_when_enabled(tmp_path):
    lane, _ = make_lane(tmp_path, LaneKey(55, 7))
    lane._set_voice_state(True)
    assert lane._handle({"message": {"text": "hello"}})
    assert VOICE_MODE_HINT in lane._session.injected[-1]
    lane._set_voice_state(False)
    assert lane._handle({"message": {"text": "again"}})
    assert VOICE_MODE_HINT not in lane._session.injected[-1]


@pytest.mark.parametrize("message", [{"text": ""}, {"caption": ""}])
def test_empty_text_or_caption_does_not_create_voice_prompt(tmp_path, message):
    lane, _ = make_lane(tmp_path, LaneKey(55, 7))
    lane._set_voice_state(True)

    assert lane._handle({"message": message})
    assert lane._session.injected == []


def test_voice_hint_counts_toward_inbound_limit(tmp_path):
    lane, client = make_lane(tmp_path, LaneKey(55, 7))
    lane._set_voice_state(True)
    text = "x" * (12_000 - len(VOICE_MODE_HINT) - 1)
    assert lane._handle({"message": {"text": text}})
    assert VOICE_MODE_HINT in lane._session.injected[-1]
    assert len(lane._session.injected[-1]) == 12_000
    assert lane._handle({"message": {"text": text + "x"}})
    assert len(lane._session.injected) == 1
    assert "too long" in client.sent[-1][1].lower()


def test_voice_success_replaces_text_and_targets_topic(tmp_path):
    key = LaneKey(55, 7)
    lane, client = make_lane(tmp_path, key)
    lane._set_voice_state(True)
    def fake_convert(args, **kwargs):
        Path(args[-1]).write_bytes(b"converted ogg payload")
        return type("Result", (), {"returncode": 0, "stderr": b""})()
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.lane.tts.sanitize_for_speech", return_value="Hello."), \
         patch("agent2telegramtopic.lane.tts.synthesize", return_value=b"source audio"), \
         patch("agent2telegramtopic.lane.subprocess.run", side_effect=fake_convert), \
         patch("agent2telegramtopic.lane._ogg_duration", return_value=4):
        lane._send_final("Hello.", key="reply")
    assert client.voices == [(key, b"converted ogg payload", {"duration": 4}, {"chat_id": 55, "message_thread_id": 7})]
    assert client.sent == []
    assert "reply" in lane._sent_keys


def test_voice_failure_and_long_reply_fall_back_to_text(tmp_path):
    key = LaneKey(55, 7)
    lane, client = make_lane(tmp_path, key)
    lane._set_voice_state(True)
    lane._try_send_voice = lambda _text: False
    lane._send_final("Hello.", key="failure")
    lane._flush_pending()
    assert client.sent[0][0] == key
    assert "voice unavailable" in client.sent[0][1].lower()
    assert client.sent[0][2] == {"chat_id": 55, "message_thread_id": 7}

    another, client2 = make_lane(tmp_path, LaneKey(55, 8))
    another._set_voice_state(True)
    attempted = []
    another._try_send_voice = lambda text: attempted.append(text) or True
    another._send_final("x" * (VOICE_MAX_CHARS + 1), key="long")
    another._flush_pending()
    assert attempted == []
    assert "too long" in client2.sent[0][1].lower()
    assert client2.sent[0][0] == LaneKey(55, 8)


def test_voice_off_sends_text_without_attempting_voice(tmp_path):
    lane, client = make_lane(tmp_path, LaneKey(55, 7))
    attempted = []
    lane._try_send_voice = lambda text: attempted.append(text) or True
    lane._send_final("Hello.", key="text")
    lane._flush_pending()
    assert attempted == []
    assert client.sent[0][1] == "Hello."


def test_missing_ffmpeg_falls_back_to_text(tmp_path):
    lane, client = make_lane(tmp_path, LaneKey(55, 7))
    lane._set_voice_state(True)
    with patch("agent2telegramtopic.lane.shutil.which", return_value=None):
        lane._send_final("Hello.", key="no-ffmpeg")
    lane._flush_pending()
    assert not client.voices
    assert "voice unavailable" in client.sent[0][1].lower()


@pytest.mark.parametrize("speed", [1.0, 1.25])
def test_converted_voice_note_keeps_silent_tail_after_speed_change(tmp_path, speed):
    key = LaneKey(55, 7)
    cfg = Config("test", "codex", frozenset({1}), tts_speed=speed)
    lane, client = make_lane(tmp_path, key, config=cfg)
    lane._set_voice_state(True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required to verify the encoded voice note")
    source = tmp_path / "source.wav"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "48000", "-ac", "1", str(source)], check=True,
                   capture_output=True)
    source_audio = source.read_bytes()
    with patch("agent2telegramtopic.lane.shutil.which", return_value=ffmpeg), \
         patch("agent2telegramtopic.lane.tts.synthesize", return_value=source_audio):
        assert lane._try_send_voice("Hello.")
    assert len(client.voices) == 1
    sent_path = tmp_path / "sent.ogg"
    sent_path.write_bytes(client.voices[0][1])
    from agent2telegramtopic.lane import _ogg_duration
    duration = _ogg_duration(str(sent_path))
    assert duration is not None
    assert duration >= 3 if speed == 1.0 else duration >= 2
    tail = subprocess.run([ffmpeg, "-v", "error", "-sseof", "-0.5", "-i", str(sent_path),
                           "-f", "f32le", "-ac", "1", "-ar", "48000", "pipe:1"],
                          check=True, capture_output=True).stdout
    import array
    samples = array.array("f")
    samples.frombytes(tail)
    assert samples
    rms = (sum(sample * sample for sample in samples) / len(samples)) ** 0.5
    assert rms < 0.01


def test_send_voice_uses_send_voice_method_and_voice_upload_field(tmp_path):
    """Telegram's sendVoice endpoint requires the multipart upload field `voice`."""
    captured = {}

    def fake_multipart(method, fields, file_field, filename, payload, **kwargs):
        captured.update(method=method, fields=fields, file_field=file_field,
                        filename=filename, payload=payload)
        return {}

    client = TelegramClient("123:abc")
    client._call_multipart = fake_multipart
    path = tmp_path / "reply.ogg"
    path.write_bytes(b"OggS-fake")
    client.send_voice(LaneKey(55, 7), path, duration=4)

    assert captured["method"] == "sendVoice"
    assert captured["file_field"] == "voice"
    assert captured["fields"] == {"chat_id": 55, "message_thread_id": 7, "duration": "4"}
    assert captured["payload"] == b"OggS-fake"
