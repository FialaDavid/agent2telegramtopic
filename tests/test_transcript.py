import json

from agent2telegramtopic.transcript import (
    claude_ctx_tokens,
    claude_model,
    claude_transcript_path,
    codex_ctx_tokens,
    codex_model,
    codex_rollout_id,
    codex_rollout_path,
    find_codex_rollout,
)


def write_jsonl(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


def test_claude_transcript_path_mangles_project_directory(tmp_path):
    assert claude_transcript_path("session", "/home/me/work.project", tmp_path) == (
        tmp_path / "-home-me-work-project" / "session.jsonl"
    )


def test_claude_ctx_uses_last_assistant_usage_and_cache_fields(tmp_path):
    path = write_jsonl(tmp_path / "claude.jsonl", [
        {"type": "assistant", "message": {"usage": {"input_tokens": 3}}},
        {"type": "user", "message": {"usage": {"input_tokens": 500}}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 30,
        }}},
    ])
    assert claude_ctx_tokens(path) == 60


def test_claude_model_tracks_latest_real_assistant_model(tmp_path):
    path = write_jsonl(tmp_path / "claude-model.jsonl", [
        {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
        {"type": "assistant", "message": {"model": "claude-opus-4-8"}},
        {"type": "assistant", "message": {"model": "<synthetic>"}},
        {"type": "assistant", "message": {}},
    ])
    assert claude_model(path) == "claude-opus-4-8"
    assert claude_model(tmp_path / "missing.jsonl") is None


def test_context_readers_return_none_for_missing_files_or_no_usage(tmp_path):
    missing = tmp_path / "missing.jsonl"
    assert claude_ctx_tokens(missing) is None
    assert codex_ctx_tokens(missing) is None
    path = write_jsonl(tmp_path / "empty-usage.jsonl", [{"type": "assistant", "message": {}}])
    assert claude_ctx_tokens(path) is None
    assert codex_ctx_tokens(path) is None


def test_codex_ctx_uses_last_token_count_and_total_usage_fallback(tmp_path):
    path = write_jsonl(tmp_path / "rollout.jsonl", [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 11},
        }}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 22},
        }}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 33},
            "total_token_usage": {"input_tokens": 44},
        }}},
    ])
    assert codex_ctx_tokens(path) == 33
    fallback = write_jsonl(tmp_path / "fallback.jsonl", [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 22},
        }}},
    ])
    assert codex_ctx_tokens(fallback) == 22
    present_but_empty = write_jsonl(tmp_path / "present-but-empty.jsonl", [
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"cached_input_tokens": 5},
            "total_token_usage": {"input_tokens": 55},
        }}},
    ])
    assert codex_ctx_tokens(present_but_empty) == 55


def test_codex_model_reads_turn_context_model(tmp_path):
    path = write_jsonl(tmp_path / "rollout-model.jsonl", [
        {"type": "event_msg", "payload": {"type": "turn_context", "model": "gpt-6-sol"}},
        {"type": "event_msg", "payload": {"type": "turn_context", "model": "gpt-6-luna"}},
    ])
    assert codex_model(path) == "gpt-6-luna"


def test_find_codex_rollout_selects_newest_recent_matching_cwd(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    cwd = tmp_path / "lane"
    other = tmp_path / "other"

    def rollout(name, path_cwd, mtime, first_line=None):
        path = root / "2026" / "10" / "27" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if first_line is None:
            first_line = json.dumps({"type": "session_meta", "payload": {
                "cwd": str(path_cwd), "id": "id",
            }})
        path.write_text(first_line + "\n")
        import os
        os.utime(path, (mtime, mtime))
        return path

    old = rollout("rollout-old-a.jsonl", cwd, 10)
    unrelated = rollout("rollout-other-b.jsonl", other, 30)
    malformed = rollout("rollout-bad-c.jsonl", cwd, 40, "{bad}")
    newest = rollout("rollout-new-d.jsonl", cwd, 20)
    assert find_codex_rollout(root, cwd, 15) == newest
    assert find_codex_rollout(root, cwd, 50) is None


def test_codex_rollout_id_uses_meta_then_filename_uuid(tmp_path):
    meta = write_jsonl(tmp_path / "rollout-any-name.jsonl", [
        {"type": "session_meta", "payload": {"id": "from-meta"}},
    ])
    fallback = write_jsonl(tmp_path / "rollout-2026-10-27T12-00-00-12345678-1234-5678-9abc-def012345678.jsonl", [])
    assert codex_rollout_id(meta) == "from-meta"
    assert codex_rollout_id(fallback) == "12345678-1234-5678-9abc-def012345678"


def test_codex_rollout_path_finds_newest_matching_id(tmp_path):
    root = tmp_path / "sessions"
    old = root / "a" / "rollout-old-session-id.jsonl"
    new = root / "b" / "rollout-new-session-id.jsonl"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("")
    new.write_text("")
    import os
    os.utime(old, (10, 10))
    os.utime(new, (20, 20))
    assert codex_rollout_path(root, "session-id") == new
    assert codex_rollout_path(root, "missing") is None
