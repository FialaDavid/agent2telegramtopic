# Agent2TelegramTopic

Talk to your coding agents from Telegram, **one agent session per Telegram topic**.

You open a private chat with your own bot, switch it to topic mode, and every topic becomes an
independent, long-lived **Claude Code** or **Codex** session running in `tmux` on your own
machine (macOS or Linux). Start a new topic for a new task, come back to an old one days later
and the conversation is still there. The bridge runs as a background service
(launchd user agent on macOS, `systemd --user` unit on Linux) and uses only the Python standard
library.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each version.

> Ported from [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram) by Petr
> Ludwig and contributors (MIT). Sixteen modules (the setup wizard included) are ported from it and keep a
> `Ported from agent2telegram (MIT)` header; the topic multiplexer, durable queue, lane
> lifecycle and the rest are new. See [License](#license).

## What is different from Agent2Telegram

- One Claude Code or Codex session per Telegram topic, each with its own working directory and
  state; `max_live_lanes` limits how many run at once.
- Incoming messages are written to a durable on-disk queue before delivery.
- Idle sessions are compacted and parked, and resumed when their topic gets a message.
- `/close` writes a summary of the topic to an archive and ends its session; a later message in the
  topic resumes it. `/rename <name>` renames the topic.
- Each bridge writes a status file (lanes, state, heartbeat) for external monitors such as
  Agents Monitoring.
- In Claude sessions the bridge sets its own `statusLine` command to record rate limits for
  `/usage`; it replaces your statusline in those sessions.

## What you get

- **A session per topic.** Each topic has its own agent session, working directory and state.
  At most `max_live_lanes` (default 4) sessions are live at once; idle ones are compacted and
  parked, and resumed when you write to the topic again.
- **A durable queue.** Messages are stored on disk before they are delivered. If the agent is
  busy, the message waits and you get a short acknowledgement; nothing is lost on a restart.
- **Files both ways.** Photos and documents you send are saved for the agent. The agent sends a
  file back by writing `[tg-file] /path/to/file` on its own line (the file must be inside the
  bot's outbox folder or a folder listed in `outbox_dirs`).
- **Answer buttons.** Add `[tg-buttons] option 1 | option 2` to an agent reply to show up to 12
  choices. Buttons are arranged in rows; tapping one sends its label as your message, and the
  chosen answer remains visible as `✓ label`. Buttons expire when a new button prompt or your
  message arrives, when the topic is parked, and after a bridge restart.
- **Reactions.** Reacting to a message with an emoji sends the agent a quick-feedback prompt
  and it answers with a one-liner.
- **Voice (optional, ElevenLabs).** Voice notes are transcribed with ElevenLabs Scribe;
  `/voice` switches replies to spoken voice notes (ElevenLabs text-to-speech, needs `ffmpeg`).
- **Topic naming.** The first message of a newly created topic carries a note asking the agent to
  name the topic after its subject and give it an icon. The agent does it with
  `python3 -m agent2telegramtopic topic-edit "<name>" "<emoji>"` (run from the topic's session; it
  reads the bot token from `config.json`, uses the topic of the working directory, and exits
  non-zero listing the allowed icons when the emoji is not one). Opening a conversation again with
  `/new` never triggers it. `setup` installs a small `telegram-topic` skill for the chosen agent
  (`~/.claude/skills/` or `~/.codex/skills/`; an existing different file is left alone and
  `uninstall` removes only an unchanged copy).
- **Usage limits.** `/usage` shows the agent's rate-limit windows without any credentials. Claude
  Code: the bridge passes a `statusLine` command that records the `rate_limits` Claude Code reports
  after each reply; **this replaces your own statusline in sessions started by the bridge** (your
  other Claude Code sessions are untouched). Codex: read from the `token_count` events in its
  rollout log. Data appears after the agent's first reply and is marked stale after an hour.
- **Progress.** A live status bubble shows what the agent is doing while it works.

### Dialogs that block a message

If a message arrives while the agent's terminal shows an interactive dialog (a trust prompt, a
nudge, a picker), the bridge cannot type into it. It then sends **one** bubble into the topic with
the dialog's text and a button per option read from the screen, plus `Zrušit (Esc)`. Your message
stays queued (it is not dropped or counted as a failed delivery) and is delivered as soon as the
dialog is gone, whether you tapped a button or closed it in the terminal. A tap sends only that
choice's keys, after re-checking that the same dialog is still on screen; the bridge never picks an
option by itself. A recognised dialog that stays open is also reported after 10 seconds, even
when no message is waiting; streaming and unrecognised screens are ignored.

### Commands

| Command | What it does |
|---|---|
| *(any text)* | sent straight to the topic's agent session |
| `/start`, `/help` | short intro |
| `/status` | session, queue and voice status of this topic |
| `/usage` | rate-limit usage of the agent (5-hour / 7-day windows, when they reset) |
| `/new` | start a fresh conversation in this topic (`/clear` for Claude Code, `/new` for Codex) |
| `/renew` | archive this conversation with a summary and start a fresh one |
| `/rename <name>` | rename this topic |
| `/setkey <key>` | store your ElevenLabs key (your message is deleted right after) |
| `/voice` | toggle spoken replies for this topic |
| `/id` | show your Telegram id |
| `/exit`, `/quit` | not passed to the agent; use `/close` or `/new` instead |
| `/close` | Archive this topic (summary saved; writing again resumes) |

Other `/commands` are passed through to the agent.

To hand a message to a topic's agent from a local automation, run
`agent2telegramtopic send --thread ID [--chat ID] (--text TEXT | --text-file PATH)`.
The bridge delivers it through that lane's inbound queue without posting it in the topic; only the agent's reply appears there.
To post a system notification that needs no agent action, run
`agent2telegramtopic notify --thread ID [--chat ID] (--text TEXT | --text-file PATH)`.
The bridge posts the text (Markdown) in the topic and does not wake the agent; if you reply quoting it, the agent receives the quoted text with your reply.
To archive a topic from local automation, run `agent2telegramtopic close --thread ID [--chat ID]`.
To restart a topic's agent session from local automation while keeping the conversation, run
`agent2telegramtopic restart --thread ID [--chat ID]`.

## Requirements

- **macOS or Linux.** Linux needs `systemd --user` for the background service (without it, run
  `agent2telegramtopic run` under your own supervisor).
- **Python 3.11+** (the version declared in `pyproject.toml`; there are no third-party
  dependencies).
- **tmux**.
- **Claude Code** (`claude`) and/or **Codex** (`codex`), installed and logged in. The bridge
  drives their normal terminal UI, so whatever they can do in your terminal they can do from
  Telegram.
- Optional: **ffmpeg** (+ `ffprobe`) for spoken replies, and an **ElevenLabs** API key for voice.

## Telegram prerequisites

This bridge works in the **private chat between you and your bot**, using Telegram's topics in
private chats. It does **not** work in groups: messages from groups and supergroups are ignored,
so there is no group to create and no admin rights to grant.

1. Create a bot with [@BotFather](https://t.me/BotFather) and keep its token.
2. Turn on **topics for the bot's private chat**: @BotFather → Mini App (Open) → select the bot →
   Bot Settings → Threaded Mode → On. It is not visible in the BotFather chat, and `getMe` may
   report it only after you have opened the chat with the bot. If it is off, `setup` and the bridge
   still start; the bridge logs a warning and, on your first message, tells you once how to turn
   it on. Until then everything goes to one conversation (no topics).
3. Open the chat with your bot and create topics there (one per task).

You do not need to change the bot's group privacy settings.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/install.sh | bash
```

The installer checks `python3` (3.11+), `tmux` and the agent CLI, fetches the source into
`~/.agent2telegramtopic-src`, puts an `agent2telegramtopic` launcher on your `PATH` and starts the
setup wizard. Nothing needs `sudo`. Add `--no-setup` to only install:

```bash
curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/install.sh | bash -s -- --no-setup
```

From a clone: `git clone https://github.com/FialaDavid/agent2telegramtopic && cd agent2telegramtopic && ./install.sh`.

## Setup

```bash
agent2telegramtopic setup        # same as: python3 -m agent2telegramtopic setup
```

The wizard:

1. detects which agent is installed (Claude Code / Codex) and picks one;
2. asks for the bot token (hidden input) and validates it with `getMe`;
3. checks that the bot has **topics enabled** and explains how to fix it if not;
4. authorizes you: you send the bot a message in its private chat and the wizard reads your
   Telegram user id from it (messages from groups are refused);
5. optionally stores an ElevenLabs key;
6. on macOS, offers to keep the bot token (and the ElevenLabs key) in the Keychain instead of
   `config.json` (default yes, see [Keychain](#keychain-macos));
7. writes `~/.agent2telegramtopic/config.json` with mode `0600`;
8. installs and starts the background service:
   - macOS: `~/Library/LaunchAgents/com.agent2telegramtopic.bridge.plist` (launchd user agent);
   - Linux: `~/.config/systemd/user/agent2telegramtopic.service` (`systemd --user`). To keep it
     running after you log out: `loginctl enable-linger $USER`.

Then open the chat with your bot, create a topic and write in it.

**Claude Code hooks.** Nothing global is edited. For every Claude Code session the bridge starts
`claude --settings ~/.agent2telegramtopic/state/<bot>.hooks.json ...`; that file registers the
`Stop` and `SessionStart` hooks which tell the bridge a turn has ended. It is rewritten on every
bridge start, and `~/.claude` is not modified. Codex needs no hook (turn boundaries come from its
rollout log).

### Install with an agent

If you already have Claude Code or Codex installed and logged in, you can let it do the
installation. The wizard asks for your bot token with hidden input, so run that last step
yourself rather than pasting the token into an agent chat.

> Install **Agent2TelegramTopic** from `https://github.com/FialaDavid/agent2telegramtopic`: run its
> `install.sh --no-setup`, then check `agent2telegramtopic --help` works. Do not ask me for my bot
> token; tell me to run `agent2telegramtopic setup` in my own terminal. Afterwards confirm the
> background service is running (`launchctl list | grep agent2telegramtopic` on macOS,
> `systemctl --user status agent2telegramtopic` on Linux) and read the log for errors.

## Using it

- The service runs `python3 -m agent2telegramtopic run`. Logs: `~/.agent2telegramtopic/logs/bridge.log`
  (macOS) or `journalctl --user -u agent2telegramtopic` (Linux).
- Sessions live in a **private tmux server** so they never mix with your own tmux. To look at
  them: `TMUX_TMPDIR=~/.agent2telegramtopic/tmux tmux ls` (then `tmux attach -t "<name>"`). The bridge
  creates that directory itself at start; tmux would otherwise ignore a missing `TMUX_TMPDIR` and
  fall back to the shared default server.
- Every topic gets its own working directory under `~/.agent2telegramtopic/topics/<bot>/<chat>/<topic>`.
  `workdir` replaces that whole root (`topics/<bot>`): lanes then live at `<workdir>/<chat>/<topic>`,
  and the bot name is not appended.

## Status file for monitors

Each bridge writes `state/<Bot>.status.json` (lanes, liveness, busy, context tokens, topic titles,
a heartbeat) every 15 seconds and on lane changes, so a monitor never has to read the database or
the config. Schema and rules: [docs/status-file.md](docs/status-file.md).

## Several bots

One machine can run several bots side by side, each with its own token, allowed users, state and
service. Give every extra bot its own config file in its own directory:

```bash
agent2telegramtopic setup --config ~/.agent2telegramtopic-work/config.json
agent2telegramtopic run --config ~/.agent2telegramtopic-work/config.json   # what the service runs
agent2telegramtopic uninstall --config ~/.agent2telegramtopic-work/config.json
```

Every command that works on an install accepts `--config PATH`; without it the environment
variable `AGENT2TELEGRAMTOPIC_CONFIG` is used, and without that `~/.agent2telegramtopic/config.json`.
The **instance directory is the config file's directory**: `state/`, `topics/`, `logs/` and `tmux/`
sit in it exactly as in `~/.agent2telegramtopic`. The default install is unchanged (same paths,
launchd label `com.agent2telegramtopic.bridge`, unit `agent2telegramtopic.service`).

For an instance chosen with `--config`:

- macOS: label and file `com.agent2telegramtopic.bridge.<bot_username>` (plist in
  `~/Library/LaunchAgents/`); Linux: unit `agent2telegramtopic-<bot_username>.service`. The service
  runs `... run --config <absolute path>`.
- The Claude Code hooks settings carry `--config <path>`, and the agent sessions start with
  `AGENT2TELEGRAMTOPIC_CONFIG=<path>`, so `topic-edit` and other calls made by the agent reach the
  right bot.
- `uninstall --config PATH` removes only that bot (its service and its directory). The
  `agent2telegramtopic` launcher, the `telegram-topic` skill and `~/.agent2telegramtopic-src` are
  shared and are removed only when no other bot's service file remains in
  `~/Library/LaunchAgents/` or `~/.config/systemd/user/`. It refuses a config file placed directly
  in your home directory, because that directory would be deleted.
- Use a different bot token per instance; two bridges polling the same bot would fight over updates.

## Uninstall

```bash
agent2telegramtopic uninstall           # asks first; add --yes to skip the question
# or
curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/uninstall.sh | bash
```

This stops and removes the launchd/systemd service, ends every agent session by killing the bridge's
private tmux server (addressed by its own socket under `~/.agent2telegramtopic/tmux`; no other tmux
server is touched, even if you run it inside tmux), and deletes
`~/.agent2telegramtopic` (config with your token, state **and the topics' working directories**),
`~/.agent2telegramtopic-src` and the launcher. Delete the bot itself with @BotFather if you no
longer need it.

## Configuration

`~/.agent2telegramtopic/config.json` (see [`config.example.json`](config.example.json)). `setup`
writes the first four keys; everything else is optional. Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `agent` | | `claude-code` or `codex` |
| `token` | | Telegram bot token (**secret**). Exactly one of `token` and `token_keychain` is required |
| `token_keychain` | | macOS only: `{"service": "...", "account": "..."}` (`account` optional), the Keychain item that holds the bot token |
| `allowed_user_ids` | | Telegram user ids allowed to talk to the bot (non-empty list of integers) |
| `bot_username` | | Bot username; also names the state directory |
| `elevenlabs_api_key` | `""` | ElevenLabs key (**secret**). If empty, the `ELEVENLABS_API_KEY` environment variable is used |
| `elevenlabs_api_key_keychain` | | macOS only: Keychain item for the ElevenLabs key, same form as `token_keychain`; not together with `elevenlabs_api_key` |
| `elevenlabs_language` | `""` | Language hint for transcription (empty = auto-detect) |
| `tts_voice_id` | `21m00Tcm4TlvDq8ikWAM` | ElevenLabs voice for spoken replies |
| `tts_model_id` | `eleven_turbo_v2_5` | ElevenLabs TTS model |
| `tts_speed` | `1.25` | Playback speed of spoken replies (0.5 to 2.0); applied by ffmpeg, whichever speech engine is used |
| `tts_url` | `""` | Custom speech endpoint (`http://` or `https://`) used instead of ElevenLabs, see [Custom speech endpoint](#custom-speech-endpoint) |
| `tts_params` | `{}` | Object merged into the JSON body sent to `tts_url` |
| `tts_timeout` | `120` | Seconds to wait for `tts_url` (a positive number) |
| `max_live_lanes` | `4` | Sessions kept live at once |
| `idle_compact_minutes` | `45` | Idle time before a session is compacted and parked |
| `compact_min_tokens` | `30000` | Minimum context size that is worth compacting |
| `claude_cmd` | `claude --dangerously-skip-permissions` | Command used to start Claude Code |
| `codex_cmd` | `codex -c check_for_update_on_startup=false --dangerously-bypass-approvals-and-sandbox` | Command used to start Codex |
| `workdir` | `""` | The lane root itself: topic directories are `<workdir>/<chat>/<topic>`, the bot name is not appended (empty = `~/.agent2telegramtopic/topics/<bot>`) |
| `file_marker` | `[tg-file]` | Line prefix the agent uses to send a file |
| `outbox_dirs` | `[]` | Extra folders the agent may send files from |
| `background_progress` | `true` | Also show sub-agent / background-task progress |
| `inbound_sender_label` | `false` | Prefix messages with sender and chat |

Everything the bridge owns lives under `~/.agent2telegramtopic/`: `config.json`, `state/`
(queues, offsets, per-topic state), `topics/`, `logs/`, `tmux/`.

## Custom speech endpoint

Spoken replies come from ElevenLabs unless `tts_url` is set. With it, every spoken reply is one
`POST` to that URL with the JSON body `{"text": "<the reply, prepared for speech>", ...tts_params}`
(`Content-Type: application/json`), waiting at most `tts_timeout` seconds. The answer must be WAV,
MP3 or OGG audio (recognized by its first bytes or its `Content-Type`); the bridge converts it with
`ffmpeg` to a Telegram voice note exactly as it does for ElevenLabs, including the `tts_speed` tempo.
No ElevenLabs key is needed for replies. If the endpoint fails or answers with something that is not
audio, the reply is sent as text, as when ElevenLabs fails; there is no retry and no other engine.
Transcription of incoming voice notes is unaffected (still ElevenLabs Scribe).

Example, a local loopback service that takes only `text`:

```json
{"tts_url": "http://127.0.0.1:8478/synthesize", "tts_params": {}, "tts_timeout": 120, "tts_speed": 1.25}
```

`tts_params` carries whatever extra fields your service understands, for example
`{"voice": "anna"}`; the bridge does not interpret them.

## Keychain (macOS)

Instead of writing secrets into `config.json` you can point it at Keychain items:

```json
{"agent": "claude-code", "allowed_user_ids": [123456789], "bot_username": "my_bot",
 "token_keychain": {"service": "agent2telegramtopic.my_bot"},
 "elevenlabs_api_key_keychain": {"service": "agent2telegramtopic.my_bot.elevenlabs"}}
```

At startup the bridge reads each item with `security find-generic-password -s <service> [-a <account>] -w`;
if an item is missing or unreadable (or the machine is not a Mac) it exits with an error naming the
service, never the secret. A missing ElevenLabs item only means "no key yet". On macOS the setup wizard
asks (default yes) whether to store the token, and the ElevenLabs key if you enter one, this way, under
the service names `agent2telegramtopic.<bot_username>` and `agent2telegramtopic.<bot_username>.elevenlabs`;
on Linux it asks nothing. With `elevenlabs_api_key_keychain` set, `/setkey` updates the Keychain item
(account `agent2telegramtopic` when the config names none) and leaves `config.json` untouched; the key goes
to `security -i` on standard input, so it does not show up in a process listing. Values read from the
Keychain are masked in logs.

## Security notes

- **The agent runs unattended with full permissions.** The default commands pass
  `--dangerously-skip-permissions` (Claude Code) and `--dangerously-bypass-approvals-and-sandbox`
  (Codex), because nobody is at the keyboard to approve prompts. Anyone who can message the bot
  as an allowed user can make the agent run commands as **you**. Use a dedicated bot, keep
  `allowed_user_ids` to yourself, and run it on a machine/account where that is acceptable.
  You can change `claude_cmd` / `codex_cmd` to stricter commands, but then prompts will stall
  the session.
- Only users in `allowed_user_ids` are served, and only in private chats; everything else is
  logged and ignored.
- `config.json` holds the bot token and the ElevenLabs key; it is written with mode `0600`.
  On macOS the secrets can live in the Keychain instead (see [Keychain](#keychain-macos)). Do not commit or share it. The token is redacted from logs
  and error messages, and `/setkey` deletes your message so the key does not stay in the chat.
- If a token leaks, revoke it with @BotFather (`/revoke`) and run `setup` again.

## Development

```bash
python3 -m pytest        # needs pytest; no network, no real Telegram
```

The tests use fake Telegram clients and temporary home directories; the service tests render
launchd/systemd files and assert their content without installing anything.

## License

MIT. Copyright (c) 2026 Agent2Telegram contributors and (c) 2026 David Fiala; see
[`LICENSE`](LICENSE). Based on [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram).
