# Changelog

All notable changes are documented here, following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Add tap-to-answer buttons to agent replies using the `[tg-buttons]` marker.

## [0.4.0] - 2026-11-10

### Added
- Add `notify` for posting a system notification in a topic without waking its agent. A quoted reply forwards the quoted text to the agent with the reply.

### Changed
- `send` now delivers a prompt to the topic's agent without posting the prompt in the topic; only the agent's reply appears there.

## [0.3.0] - 2026-11-02

### Added
- Show the transcript model in topic status.
- Add `/renew` to archive the current conversation and start a fresh one, with a clearer reply from `/new`.
- Add the `close`, `send` and `restart` commands for controlling topic sessions from local automation.
- Show blocking dialogs with an inner separator and report silent Telegram turns.

### Changed
- Closing a topic no longer waits for compaction to finish.
- Preserve single line breaks when sending rich text.
- Format new-conversation replies with plain effort text and status dots on usage lines.

## [0.2.0] - 2026-11-01

### Added
- Archive topics with `/close`; a new message during closing resumes the topic instead.

### Changed
- Pass unknown slash commands to the agent without a `[TG]` prefix, and keep `/exit` and `/quit` from being forwarded.
- Find the composer across the full terminal pane after typing a long message.

### Fixed
- Make topic closing and archiving reliable when messages arrive during a close, and prefer the final summary when saving the archive.

## [0.1.0] - 2026-10-31

Agent2TelegramTopic lets you talk to long-lived Claude Code or Codex sessions from Telegram, with one independent session per topic.

### Added
- Add a startup log line and warn when the configured bot username differs from Telegram's bot username.
- Write a status file for external monitoring.
- Forward blocking terminal dialogs to Telegram as buttons.
- Add `/rename` to rename a topic.
- Document that the configured work directory is the lane root itself.
- Support an optional custom text-to-speech endpoint.

[0.4.0]: https://github.com/FialaDavid/agent2telegramtopic/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/FialaDavid/agent2telegramtopic/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/FialaDavid/agent2telegramtopic/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/FialaDavid/agent2telegramtopic/releases/tag/v0.1.0
