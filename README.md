# Agent2TelegramTopic

Talk to your coding agents from Telegram, **one agent session per Telegram topic**.

You open a private chat with your own bot, switch it to topic mode, and every topic becomes an
independent, long-lived **Claude Code** or **Codex** session running in `tmux` on your own
machine (macOS or Linux). Start a new topic for a new task, come back to an old one days later
and the conversation is still there. The bridge runs as a background service
(launchd user agent on macOS, `systemd --user` unit on Linux) and uses only the Python standard
library.

> Ported from [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram) by Petr
> Ludwig and contributors (MIT). Sixteen modules (the setup wizard included) are ported from it and keep a
> `Ported from agent2telegram (MIT)` header; the topic multiplexer, durable queue, lane
> lifecycle and the rest are new. See [License](#license).

## What you get

- **A session per topic.** Each topic has its own agent session, working directory and state.
  At most `max_live_lanes` (default 4) sessions are live at once; idle ones are compacted and
  parked, and resumed when you write to the topic again.
- **A durable queue.** Messages are stored on disk before they are delivered. If the agent is
  busy, the message waits and you get a short acknowledgement; nothing is lost on a restart.
- **Files both ways.** Photos and documents you send are saved for the agent. The agent sends a
  file back by writing `[tg-file] /path/to/file` on its own line (the file must be inside the
  bot's outbox folder or a folder listed in `outbox_dirs`).
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

### Commands

| Command | What it does |
|---|---|
| *(any text)* | sent straight to the topic's agent session |
| `/start`, `/help` | short intro |
| `/status` | session, queue and voice status of this topic |
| `/usage` | rate-limit usage of the agent (5-hour / 7-day windows, when they reset) |
| `/new` | start a fresh conversation in this topic (`/clear` for Claude Code, `/new` for Codex) |
| `/setkey <key>` | store your ElevenLabs key (your message is deleted right after) |
| `/voice` | toggle spoken replies for this topic |
| `/id` | show your Telegram id |
| `/close` | park this topic's session (it resumes when you write again) |

Other `/commands` are passed through to the agent.

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
2. Turn on **topics for the bot's private chat** in BotFather's bot settings (BotFather mini
   app; the option is called "Threaded Mode" / topics). The bridge refuses to start if
   `getMe` does not report `has_topics_enabled: true`, and `setup` checks it for you with
   instructions if it is off. *(The exact BotFather menu wording changes; the check is the
   authority.)*
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
6. writes `~/.agent2telegramtopic/config.json` with mode `0600`;
7. installs and starts the background service:
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
  them: `TMUX_TMPDIR=~/.agent2telegramtopic/tmux tmux ls` (then `tmux attach -t "<name>"`).
- Every topic gets its own working directory under `~/.agent2telegramtopic/topics/<bot>/<chat>/<topic>`
  (override the root with `workdir`).

## Uninstall

```bash
agent2telegramtopic uninstall           # asks first; add --yes to skip the question
# or
curl -fsSL https://raw.githubusercontent.com/FialaDavid/agent2telegramtopic/main/uninstall.sh | bash
```

This stops and removes the launchd/systemd service, stops the bridge's tmux server, and deletes
`~/.agent2telegramtopic` (config with your token, state **and the topics' working directories**),
`~/.agent2telegramtopic-src` and the launcher. Delete the bot itself with @BotFather if you no
longer need it.

## Configuration

`~/.agent2telegramtopic/config.json` (see [`config.example.json`](config.example.json)). `setup`
writes the first four keys; everything else is optional. Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `agent` | | `claude-code` or `codex` |
| `token` | | Telegram bot token (**secret**) |
| `allowed_user_ids` | | Telegram user ids allowed to talk to the bot (non-empty list of integers) |
| `bot_username` | | Bot username; also names the state directory |
| `elevenlabs_api_key` | `""` | ElevenLabs key (**secret**). If empty, the `ELEVENLABS_API_KEY` environment variable is used |
| `elevenlabs_language` | `""` | Language hint for transcription (empty = auto-detect) |
| `tts_voice_id` | `21m00Tcm4TlvDq8ikWAM` | ElevenLabs voice for spoken replies |
| `tts_model_id` | `eleven_turbo_v2_5` | ElevenLabs TTS model |
| `tts_speed` | `1.25` | Playback speed of spoken replies (0.5 to 2.0) |
| `max_live_lanes` | `4` | Sessions kept live at once |
| `idle_compact_minutes` | `45` | Idle time before a session is compacted and parked |
| `compact_min_tokens` | `30000` | Minimum context size that is worth compacting |
| `claude_cmd` | `claude --dangerously-skip-permissions` | Command used to start Claude Code |
| `codex_cmd` | `codex -c check_for_update_on_startup=false --dangerously-bypass-approvals-and-sandbox` | Command used to start Codex |
| `workdir` | `""` | Root for topic working directories (empty = under `~/.agent2telegramtopic/topics`) |
| `file_marker` | `[tg-file]` | Line prefix the agent uses to send a file |
| `outbox_dirs` | `[]` | Extra folders the agent may send files from |
| `background_progress` | `false` | Also show sub-agent / background-task progress |
| `inbound_sender_label` | `false` | Prefix messages with sender and chat |

Everything the bridge owns lives under `~/.agent2telegramtopic/`: `config.json`, `state/`
(queues, offsets, per-topic state), `topics/`, `logs/`, `tmux/`.

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
  There is no keychain integration. Do not commit or share it. The token is redacted from logs
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
