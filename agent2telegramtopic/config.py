from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import agent2telegramtopic


@dataclass(frozen=True)
class Config:
    bot_name: str
    agent_kind: str
    allowed_users: frozenset[int]
    max_live_lanes: int = 4
    idle_compact_minutes: int = 45
    compact_min_tokens: int = 30000
    claude_cmd: tuple[str, ...] = ("claude", "--dangerously-skip-permissions")
    # The update prompt is a startup dialog that blocks injection into a fresh lane.
    codex_cmd: tuple[str, ...] = ("codex", "-c", "check_for_update_on_startup=false", "--dangerously-bypass-approvals-and-sandbox")
    hooks_settings_path: str | None = None
    elevenlabs_api_key: str = ""
    elevenlabs_language: str = ""
    tts_speed: float = 1.25
    tts_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    tts_model_id: str = "eleven_turbo_v2_5"
    file_marker: str = "[tg-file]"
    outbox_dirs: list[str] = field(default_factory=list)
    # Claude Code: show work that runs outside a Telegram turn — running sub-agents (one message
    # edited in place) and tool calls of turns not started from Telegram (e.g. a background-task
    # notification). On by default; set false to keep the chat quiet.
    background_progress: bool = True
    inbound_sender_label: bool = False  # display sender/chat after origin_prefix

    def __post_init__(self) -> None:
        if self.agent_kind not in {"claude-code", "codex"}:
            raise ValueError("agent_kind must be 'claude-code' or 'codex'")
        if not self.allowed_users:
            raise ValueError("allowed_users must contain at least one user id")
        if (not isinstance(self.tts_speed, (int, float)) or isinstance(self.tts_speed, bool)
                or not 0.5 <= self.tts_speed <= 2.0):
            raise ValueError("'tts_speed' must be between 0.5 and 2.0.")
        if type(self.background_progress) is not bool:
            raise ValueError("'background_progress' must be a boolean.")
        if type(self.inbound_sender_label) is not bool:
            raise ValueError("'inbound_sender_label' must be a boolean.")


APP_DIRNAME = ".agent2telegramtopic"

#: Optional settings accepted in config.json besides the required ones below.
_OPTIONAL_KEYS = {"elevenlabs_api_key", "elevenlabs_language", "tts_speed", "tts_voice_id",
                  "tts_model_id", "file_marker", "outbox_dirs", "background_progress",
                  "inbound_sender_label", "max_live_lanes", "idle_compact_minutes",
                  "compact_min_tokens", "claude_cmd", "codex_cmd", "workdir"}
_REQUIRED_KEYS = {"agent", "token", "allowed_user_ids", "bot_username"}
_FIELD_KEYS = {"elevenlabs_language", "tts_speed", "tts_voice_id", "tts_model_id", "file_marker",
               "outbox_dirs", "background_progress", "inbound_sender_label", "max_live_lanes",
               "idle_compact_minutes", "compact_min_tokens"}


def _home(home: Path | None = None) -> Path:
    return home if home is not None else Path.home()


def app_dir(home: Path | None = None) -> Path:
    """Everything the bridge owns lives under this one directory."""
    return _home(home) / APP_DIRNAME


def config_file(home: Path | None = None) -> Path:
    return app_dir(home) / "config.json"


def state_dir(home: Path | None = None) -> Path:
    return app_dir(home) / "state"


def load_config(home: Path | None = None) -> dict:
    """Read and validate config.json; raises ValueError (file missing, malformed or invalid)."""
    path = config_file(home)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise ValueError(f"No config file at {path}; run: python3 -m agent2telegramtopic setup") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid config file {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Config must be a JSON object")
    unknown = set(raw) - _OPTIONAL_KEYS - _REQUIRED_KEYS
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise ValueError(f"Missing config keys: {', '.join(sorted(missing))}")
    build_config(raw)
    return raw


def save_config(raw: dict, home: Path | None = None) -> Path:
    """Write config.json atomically, readable by the owner only (0600)."""
    path = config_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
        handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def save_elevenlabs_key(key: str, home: Path | None = None) -> None:
    raw = load_config(home)
    raw["elevenlabs_api_key"] = key
    save_config(raw, home)


def build_config(raw: dict) -> Config:
    """Turn a config.json mapping into a validated Config (the bot token is not part of it)."""
    if not isinstance(raw.get("token"), str) or not raw["token"].strip():
        raise ValueError("'token' must be a non-empty string")
    if not isinstance(raw.get("bot_username"), str) or not raw["bot_username"].strip():
        raise ValueError("'bot_username' must be a non-empty string")
    ids = raw.get("allowed_user_ids")
    if (not isinstance(ids, list) or not ids
            or any(type(i) is not int for i in ids)):
        raise ValueError("'allowed_user_ids' must be a non-empty list of integers")
    values = {key: raw[key] for key in _FIELD_KEYS if key in raw}
    for key in ("claude_cmd", "codex_cmd"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ValueError(f"'{key}' must be a non-empty command string")
            values[key] = tuple(shlex.split(raw[key]))
    key = raw.get("elevenlabs_api_key") or os.environ.get("ELEVENLABS_API_KEY", "")
    return Config(raw["bot_username"].strip(), raw["agent"], frozenset(ids),
                  elevenlabs_api_key=key, **values)


def state_db_path(bot_name: str, home: Path | None = None) -> Path:
    return state_dir(home) / f"{bot_name}.db"


def hooks_settings_file(bot_name: str, home: Path | None = None) -> Path:
    return state_dir(home) / f"{bot_name}.hooks.json"


def lanes_root(bot_name: str, home: Path | None = None) -> Path:
    return app_dir(home) / "topics" / bot_name


def outbox_path(bot_name: str, home: Path | None = None) -> Path:
    """Default directory for files a bot lane may send to Telegram."""
    return state_dir(home) / bot_name / "outbox"


def lane_dir(bot_name: str, chat_id: int, thread_id: int, home: Path | None = None) -> Path:
    return lane_path(lanes_root(bot_name, home), chat_id, thread_id)


def lane_path(root: str | Path, chat_id: int, thread_id: int) -> Path:
    """Resolve one chat and thread scoped lane directory."""
    return Path(root) / str(chat_id) / str(thread_id)


def lane_state_dir(bot_name: str, chat_id: int, thread_id: int, home: Path | None = None) -> Path:
    return state_dir(home) / bot_name / "lanes" / str(chat_id) / str(thread_id)


_LANE_STATE_FILES = {
    "sent": "attach_sent.txt",
    "outbox": "queue",
    "inbox": None,
    "turn_end": "turn_end",
    "status": "status_bubble",
    "session": "lane_session.json",
    "compact_done": "compact_done",
    "attachments": "attachments",
    "voice_mode": "voice_mode",
}


def lane_state_file(bot_name: str, chat_id: int, thread_id: int, kind: str, home: Path | None = None) -> Path:
    try:
        filename = _LANE_STATE_FILES[kind]
    except KeyError:
        raise ValueError(f"unknown lane state file kind: {kind}") from None
    directory = lane_state_dir(bot_name, chat_id, thread_id, home)
    return directory if filename is None else directory / filename


def claude_rate_limits_file(bot_name: str, home: Path | None = None) -> Path:
    return state_dir(home) / f"{bot_name}.claude_rate_limits.json"


def cli_command(python: str) -> str:
    """Shell prefix that runs this package's CLI: ``<prefix> topic-edit ...``."""
    repo_root = Path(agent2telegramtopic.__file__).resolve().parent.parent
    return f"PYTHONPATH={shlex.quote(str(repo_root))} {shlex.quote(python)} -m agent2telegramtopic"


def write_hooks_settings(path: Path, python: str, bot_name: str, lane_root: Path | None = None) -> None:
    repo_root = Path(agent2telegramtopic.__file__).resolve().parent.parent
    command = f"PYTHONPATH={shlex.quote(str(repo_root))} {shlex.quote(python)} -m agent2telegramtopic.hook --bot {shlex.quote(bot_name)}"
    if lane_root is not None:
        command += f" --lanes-root {shlex.quote(str(lane_root))}"
    # The statusLine records rate limits for /usage; it replaces the user's own statusLine in bridge sessions.
    limits_file = path.with_name(f"{bot_name}.claude_rate_limits.json")
    settings = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": command}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": command}]}],
        },
        "statusLine": {"type": "command",
                       "command": f"{cli_command(python)} statusline {shlex.quote(str(limits_file))}"},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
