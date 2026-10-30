"""Spoken replies come from ElevenLabs only: with a key the audio is requested from ElevenLabs,
without one the reply falls back to text and no other speech engine is tried."""
from pathlib import Path
from unittest.mock import patch

from agent2telegramtopic.config import Config
from agent2telegramtopic.routing import LaneKey
from test_voice import make_lane


class _Resp:
    def read(self):
        return b"MP3"
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self):
        self.urls = []
    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return _Resp()


def _fake_convert(args, **kwargs):
    Path(args[-1]).write_bytes(b"ogg")
    return type("Result", (), {"returncode": 0, "stderr": b""})()


def test_voice_reply_is_requested_from_elevenlabs(tmp_path):
    cfg = Config("el-keyed", "codex", frozenset({1}), elevenlabs_api_key="sk_" + "x" * 24, tts_voice_id="VOICE9")
    lane, client = make_lane(tmp_path, LaneKey(55, 7), config=cfg)
    opener = _Opener()
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.tts.urllib.request.build_opener", return_value=opener), \
         patch("agent2telegramtopic.lane.subprocess.run", side_effect=_fake_convert), \
         patch("agent2telegramtopic.lane._ogg_duration", return_value=1):
        assert lane._try_send_voice("Hello there.")
    assert len(client.voices) == 1
    assert len(opener.urls) == 1
    assert opener.urls[0].startswith("https://api.elevenlabs.io/v1/text-to-speech/VOICE9")


def test_without_a_key_no_voice_is_produced_and_no_local_engine_runs(tmp_path):
    cfg = Config("el-keyless", "codex", frozenset({1}))
    lane, client = make_lane(tmp_path, LaneKey(55, 7), config=cfg)
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.lane.subprocess.run") as run:
        assert not lane._try_send_voice("Hello there.")
    run.assert_not_called()
    assert not client.voices
