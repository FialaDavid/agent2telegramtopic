import json
import os
import sys

from agent2telegramtopic import lane as lane_module, stt
from test_lane_commands import make_lane


def make_voice_lane(tmp_path, monkeypatch):
    lane, _ = make_lane(tmp_path)
    lane.cfg.elevenlabs_api_key = "fake-key"
    lane.cfg.stt_fallback_cmd = []
    monkeypatch.setattr(lane.tg, "get_file_path", lambda _file_id: "voice.ogg")
    monkeypatch.setattr(lane.tg, "download", lambda _path: b"VOICE-BYTES")
    lane._send_final = lambda *args, **kwargs: None
    return lane


def test_local_fallback_transcript_is_injected_with_fallback_label(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    lane.cfg.stt_fallback_cmd = [sys.executable, "-c", "print('local words')"]
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.STTError("429")))
    notices = []
    lane._send_final = lambda text, **kwargs: notices.append(text)
    lane._handle({"message": {"voice": {"file_id": "voice"}}})
    assert lane._session.injected[-1] == (
        "[voice transcript (local fallback, ElevenLabs failed) – may contain recognition errors]\nlocal words"
    )
    assert not notices


def test_failed_fallback_sends_existing_failure_message(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    lane.cfg.stt_fallback_cmd = [sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(1)"]
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.STTError("ElevenLabs down")))
    notices = []
    lane._send_final = lambda text, **kwargs: notices.append(text)
    assert lane._transcribe({"file_id": "voice"}, lane.key.chat_id) is None
    assert "Voice transcription failed (ElevenLabs down)" in notices[0]


def test_empty_transcript_does_not_run_fallback(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    lane.cfg.stt_fallback_cmd = [sys.executable, "-c", "raise SystemExit(9)"]
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.EmptyTranscript()))
    notices = []
    lane._send_final = lambda text, **kwargs: notices.append(text)
    assert lane._transcribe({"file_id": "voice"}, lane.key.chat_id) is None
    assert "couldn't hear any speech" in notices[0]


def test_empty_fallback_command_keeps_existing_failure_behavior(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.STTError("offline")))
    notices = []
    lane._send_final = lambda text, **kwargs: notices.append(text)
    assert lane._transcribe({"file_id": "voice"}, lane.key.chat_id) is None
    assert "Voice transcription failed (offline)" in notices[0]


def test_fallback_temp_audio_is_removed_after_call(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    seen = tmp_path / "path.txt"
    lane.cfg.stt_fallback_cmd = [sys.executable, "-c", f"import pathlib,sys; pathlib.Path({str(seen)!r}).write_text(sys.argv[1]); print('ok')"]
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.STTError("offline")))
    assert lane._transcribe({"file_id": "voice"}, lane.key.chat_id).endswith("\nok")
    temp_audio = seen.read_text()
    assert not os.path.exists(temp_audio)


def test_fallback_receives_language_and_private_audio(tmp_path, monkeypatch):
    lane = make_voice_lane(tmp_path, monkeypatch)
    lane.cfg.elevenlabs_language = "cs"
    capture = tmp_path / "capture.json"
    lane.cfg.stt_fallback_cmd = [sys.executable, "-c", (
        "import json,os,sys; p=sys.argv[1]; "
        f"json.dump([os.environ.get('A2TT_STT_LANGUAGE'),open(p,'rb').read().decode(),os.stat(p).st_mode & 0o777],open({str(capture)!r},'w')); print('ok')"
    )]
    monkeypatch.setattr(stt, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(stt.STTError("offline")))
    lane._transcribe({"file_id": "voice"}, lane.key.chat_id)
    assert json.loads(capture.read_text()) == ["cs", "VOICE-BYTES", 0o600]
