"""Background service for the bridge: a launchd user agent (macOS) or a systemd --user unit (Linux).

Rendering is separate from installing so the generated files can be inspected (and tested)
without touching the machine. Nothing here writes outside the user's home directory.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .config import app_dir, config_file, is_default_instance

LABEL = "com.agent2telegramtopic.bridge"
UNIT = "agent2telegramtopic.service"
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _require_bot(bot: str | None) -> str:
    if not bot:
        raise ValueError("the bot username is needed to name the service of a non-default instance")
    return bot


def label(home: Path | None = None, bot: str | None = None) -> str:
    """launchd label: the default install keeps LABEL, another instance gets the bot's name appended."""
    return LABEL if is_default_instance(home) else f"{LABEL}.{_require_bot(bot)}"


def unit_name(home: Path | None = None, bot: str | None = None) -> str:
    return UNIT if is_default_instance(home) else f"agent2telegramtopic-{_require_bot(bot)}.service"


def plist_path(home: Path | None = None, bot: str | None = None) -> Path:
    return (home or Path.home()) / "Library/LaunchAgents" / f"{label(home, bot)}.plist"


def unit_path(home: Path | None = None, bot: str | None = None) -> Path:
    return (home or Path.home()) / ".config/systemd/user" / unit_name(home, bot)


def _config_args(home: Path | None) -> list[str]:
    """``--config <path>`` for a non-default instance; the default install's command is unchanged."""
    return [] if is_default_instance(home) else ["--config", str(config_file(home))]


def log_path(home: Path | None = None) -> Path:
    return app_dir(home) / "logs" / "bridge.log"


def environment(home: Path | None = None, path: str | None = None, source: Path | None = None) -> dict:
    """Variables the service needs: where the package lives, a PATH that finds the agent CLI and
    tmux, and a private tmux server directory so the bridge never mixes with the user's tmux."""
    return {
        "PYTHONPATH": str(source or SOURCE_ROOT),
        "PATH": path if path is not None else os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TMUX_TMPDIR": str(app_dir(home) / "tmux"),
    }


def launchd_plist(python: str, home: Path | None = None, path: str | None = None,
                  source: Path | None = None, bot: str | None = None) -> str:
    env = "".join(f"<key>{k}</key><string>{escape(v)}</string>"
                  for k, v in environment(home, path, source).items())
    log = escape(str(log_path(home)))
    args = "".join(f"<string>{escape(a)}</string>" for a in _config_args(home))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label(home, bot)}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(python)}</string><string>-m</string><string>agent2telegramtopic</string><string>run</string>{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>{env}</dict>
  <key>WorkingDirectory</key><string>{escape(str(home or Path.home()))}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def systemd_unit(python: str, home: Path | None = None, path: str | None = None,
                 source: Path | None = None) -> str:
    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n") + '"'
    exec_start = " ".join([quote(python), "-m", "agent2telegramtopic", "run",
                           *(quote(a) for a in _config_args(home))])
    env = "\n".join("Environment=" + quote(f"{k}={v}") for k, v in environment(home, path, source).items())
    return f"""[Unit]
Description=Agent2TelegramTopic bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
WorkingDirectory=%h
Restart=on-failure
RestartSec=5
# Exit 2 = invalid config / topics not enabled, 3 = already running: restarting cannot fix either.
RestartPreventExitStatus=2 3
{env}

[Install]
WantedBy=default.target
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def install(platform: str = sys.platform, *, python: str | None = None, home: Path | None = None,
            path: str | None = None, runner=None, bot: str | None = None) -> str:
    """Write the service file and start the service. Returns the path of the file written."""
    runner = runner or subprocess.run
    python = python or sys.executable or "python3"
    log_path(home).parent.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        target = plist_path(home, bot)
        _write(target, launchd_plist(python, home, path, bot=bot))
        domain = f"gui/{os.getuid()}"
        runner(["launchctl", "bootout", f"{domain}/{label(home, bot)}"], capture_output=True)
        result = runner(["launchctl", "bootstrap", domain, str(target)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {(result.stderr or '').strip()}")
        return str(target)
    target = unit_path(home, bot)
    _write(target, systemd_unit(python, home, path))
    for args in (["daemon-reload"], ["enable", "--now", unit_name(home, bot)]):
        result = runner(["systemctl", "--user", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"systemctl --user {' '.join(args)} failed: {(result.stderr or '').strip()}")
    return str(target)


def remove(platform: str = sys.platform, *, home: Path | None = None, runner=None,
           bot: str | None = None) -> bool:
    """Stop and delete the service. Returns True when a service file existed."""
    run = runner or subprocess.run

    def runner(args, **kwargs):   # a missing launchctl/systemctl must not leave the file behind
        try:
            return run(args, **kwargs)
        except OSError:
            return None

    if platform == "darwin":
        target = plist_path(home, bot)
        if not target.exists():
            return False
        runner(["launchctl", "bootout", f"gui/{os.getuid()}/{label(home, bot)}"], capture_output=True)
        target.unlink()
        return True
    target = unit_path(home, bot)
    if not target.exists():
        return False
    runner(["systemctl", "--user", "disable", "--now", unit_name(home, bot)], capture_output=True)
    target.unlink()
    runner(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True
