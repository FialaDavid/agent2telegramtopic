"""Spoken replies from a custom endpoint (`tts_url`): request, conversion to a Telegram voice note, failure."""
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from agent2telegramtopic import tts
from agent2telegramtopic.config import Config, build_config
from agent2telegramtopic.routing import LaneKey
from test_voice import make_lane

WAV = b"RIFF" + b"\x24\x10\x00\x00" + b"WAVEfmt " + b"\x00" * 2048
URL = "http://127.0.0.1:8478/synthesize"


class _Headers:
    def __init__(self, content_type):
        self.content_type = content_type

    def get(self, name, default=None):
        return self.content_type if name.lower() == "content-type" else default


class _Resp:
    def __init__(self, data, content_type=""):
        self._data, self.headers = data, _Headers(content_type)

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, data=WAV, content_type="audio/wav", error=None):
        self.data, self.content_type, self.error, self.reqs, self.timeouts = data, content_type, error, [], []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return _Resp(self.data, self.content_type)


def body(req):
    return json.loads(req.data.decode())


def test_request_is_the_text_merged_with_the_configured_params_and_timeout():
    op = _Opener()
    assert tts.synthesize_custom("Hello.", url=URL, params={"voice": "anna", "steps": 8}, timeout=33, opener=op) == WAV
    req = op.reqs[0]
    assert req.full_url == URL and req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert body(req) == {"text": "Hello.", "voice": "anna", "steps": 8}
    assert op.timeouts == [33]


def test_default_params_send_only_the_text():
    op = _Opener()
    tts.synthesize_custom("Hi.", url=URL, opener=op)
    assert body(op.reqs[0]) == {"text": "Hi."}


@pytest.mark.parametrize("data,ctype", [(WAV, ""), (b"OggS" + b"\0" * 40, ""), (b"ID3\x04" + b"\0" * 40, ""),
                                        (b"\xff\xfb\x90\x00" + b"\0" * 40, ""), (b"opaque", "audio/mpeg; charset=x")])
def test_wav_mp3_and_ogg_are_accepted_by_magic_bytes_or_content_type(data, ctype):
    assert tts.synthesize_custom("Hi.", url=URL, opener=_Opener(data, ctype)) == data


@pytest.mark.parametrize("data,ctype", [(b"", "audio/wav"), (b'{"error": "busy"}', "application/json")])
def test_a_reply_that_is_not_audio_is_an_error(data, ctype):
    with pytest.raises(tts.TTSError, match="WAV, MP3 or OGG"):
        tts.synthesize_custom("Hi.", url=URL, opener=_Opener(data, ctype))


def test_network_errors_become_tts_errors_without_the_url():
    secret_url = "http://127.0.0.1:1/synthesize?token=supersecret"
    for error in (urllib.error.URLError("refused"), TimeoutError("slow"),
                  urllib.error.HTTPError(secret_url, 500, "boom", {}, None)):
        with pytest.raises(tts.TTSError) as err:
            tts.synthesize_custom("Hi.", url=secret_url, opener=_Opener(error=error))
        assert "supersecret" not in str(err.value)


def _fake_convert(calls):
    def convert(args, **kwargs):
        calls.append(args)
        Path(args[-1]).write_bytes(b"ogg")
        return type("Result", (), {"returncode": 0, "stderr": b""})()
    return convert


def lane_with(tmp_path, **extra):
    cfg = Config("custom-tts", "codex", frozenset({1}), tts_url=URL, **extra)
    return make_lane(tmp_path, LaneKey(55, 7), config=cfg)


def test_voice_reply_posts_to_the_endpoint_and_sends_an_ogg_opus_voice_note(tmp_path):
    lane, client = lane_with(tmp_path, tts_params={"voice": "anna"}, tts_timeout=7, tts_speed=1.5)
    opener, converted, sources = _Opener(), [], []

    def convert(args, **kwargs):
        sources.append(Path(args[args.index("-i") + 1]).read_bytes())
        return _fake_convert(converted)(args, **kwargs)

    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.tts.urllib.request.build_opener", return_value=opener), \
         patch("agent2telegramtopic.lane.subprocess.run", side_effect=convert), \
         patch("agent2telegramtopic.lane._ogg_duration", return_value=2):
        assert lane._try_send_voice("**Hello** there.")
    assert [r.full_url for r in opener.reqs] == [URL] and opener.timeouts == [7]
    assert body(opener.reqs[0]) == {"text": "Hello there.", "voice": "anna"}
    assert sources == [WAV]                                   # the WAV is what ffmpeg converts
    args = converted[0]
    assert "libopus" in args and "atempo=1.5" in args[args.index("-af") + 1] and args[-1].endswith(".ogg")
    assert client.voices[0][1] == b"ogg"


def test_no_elevenlabs_key_is_needed_with_a_custom_endpoint(tmp_path):
    lane, client = lane_with(tmp_path)
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.tts.urllib.request.build_opener", return_value=_Opener()), \
         patch("agent2telegramtopic.lane.subprocess.run", side_effect=_fake_convert([])), \
         patch("agent2telegramtopic.lane._ogg_duration", return_value=1):
        assert lane._try_send_voice("Hello.") and len(client.voices) == 1


def test_endpoint_failure_behaves_like_an_elevenlabs_failure_text_fallback_no_other_engine(tmp_path):
    lane, client = lane_with(tmp_path)
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.tts.urllib.request.build_opener",
               return_value=_Opener(error=urllib.error.URLError("refused"))) as build, \
         patch("agent2telegramtopic.lane.subprocess.run") as run:
        assert not lane._try_send_voice("Hello.")
    run.assert_not_called()
    assert build.call_count == 1 and not client.voices        # no retry, no ElevenLabs request


def test_ffmpeg_failure_is_reported_as_no_voice(tmp_path):
    lane, client = lane_with(tmp_path)
    failed = type("Result", (), {"returncode": 1, "stderr": b""})()
    with patch("agent2telegramtopic.lane.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("agent2telegramtopic.tts.urllib.request.build_opener", return_value=_Opener()), \
         patch("agent2telegramtopic.lane.subprocess.run", return_value=failed):
        assert not lane._try_send_voice("Hello.")
    assert not client.voices


def minimal(**extra):
    return {"agent": "codex", "token": "1:x", "allowed_user_ids": [1], "bot_username": "b", **extra}


def test_config_accepts_and_defaults_the_new_keys():
    cfg = build_config(minimal(tts_url=URL, tts_params={"voice": "a"}, tts_timeout=30))
    assert (cfg.tts_url, cfg.tts_params, cfg.tts_timeout) == (URL, {"voice": "a"}, 30)
    default = build_config(minimal())
    assert (default.tts_url, default.tts_params, default.tts_timeout) == ("", {}, 120)


@pytest.mark.parametrize("extra,key", [
    ({"tts_url": "ftp://x/y"}, "tts_url"), ({"tts_url": "http://"}, "tts_url"), ({"tts_url": 5}, "tts_url"),
    ({"tts_params": ["a"]}, "tts_params"), ({"tts_params": "voice=a"}, "tts_params"),
    ({"tts_timeout": 0}, "tts_timeout"), ({"tts_timeout": "30"}, "tts_timeout"), ({"tts_timeout": True}, "tts_timeout")])
def test_config_rejects_wrongly_typed_new_keys(extra, key):
    with pytest.raises(ValueError, match=key):
        build_config(minimal(**extra))


def test_unknown_keys_are_still_rejected(tmp_path):
    from agent2telegramtopic.config import load_config, save_config
    save_config(minimal(tts_urll=URL), tmp_path)
    with pytest.raises(ValueError, match="Unknown config keys: tts_urll"):
        load_config(tmp_path)
