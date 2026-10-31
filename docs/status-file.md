# Status file (schema 1)

Every bridge instance writes `<instance dir>/state/<Bot>.status.json` (next to `<Bot>.db` and
`<Bot>.lock`) for external monitors. Read-only for consumers. It contains no token and no chat
content or message text.

The file is replaced atomically (temp file + rename) with mode `0600`: when the bridge starts,
every 15 seconds, and shortly after any lane change (create, park, unpark, busy toggle,
compaction, title change).

```json
{
  "schema": 1,
  "bot": "ExampleBot",
  "version": "0.1.0",
  "pid": 12345,
  "config": "/abs/path/config.json",
  "tmux_tmpdir": "/abs/path/tmux",
  "started_at": 1793440000,
  "updated_at": 1793443221,
  "heartbeat_seconds": 15,
  "lanes": [
    {
      "chat": 123456789,
      "thread": 32732,
      "title": "Build",
      "agent": "claude-code",
      "cwd": "/abs/path/123456789/32732",
      "tmux_session": "Claude – topic 32732 · 123456789",
      "alive": true,
      "status": "live",
      "busy": false,
      "last_activity": 1793443000,
      "last_compact": 1793400000,
      "ctx_tokens": 84213,
      "ctx_window": null
    }
  ]
}
```

Rules:

- Timestamps are integer unix seconds. Unknown values are `null`; keys are never omitted.
- `status` is `live`, `parked`, or `waiting` (parked with inbound messages waiting for a slot).
- `alive`: the lane's tmux session exists on the instance's tmux server at write time.
- `title`: the name from `topic_titles`, recorded from topic service messages or immediately after a
  successful bridge or topic skill rename; else `null`.
  Topics renamed or created before the bridge first saw them stay `null` until their next rename.
- `last_compact`: the later of the bridge-recorded compaction and the Claude compact hook marker;
  `null` if never.
- `ctx_window`: Claude lanes use the latest `context_window.context_window_size` from statusLine,
  matched by resolved cwd; Codex lanes and unmatched or unavailable values are `null`.
  Compute a percentage only when both `ctx_tokens` and `ctx_window` are non-null.
- Staleness: treat the bridge as hung or stopped when
  `now - updated_at > 3 * heartbeat_seconds + 30`.
- Ignore unknown keys; tolerate a missing file (bridge never started). The file is not removed on
  shutdown.
- A breaking change bumps `schema` to 2.
