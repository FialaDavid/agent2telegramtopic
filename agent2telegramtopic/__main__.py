"""Command dispatcher: ``python3 -m agent2telegramtopic [run|setup|uninstall|service]``."""
import sys

from agent2telegramtopic import config

USAGE = """usage: agent2telegramtopic <command>

Every command that works on an install accepts --config PATH (default ~/.agent2telegramtopic/config.json;
env AGENT2TELEGRAMTOPIC_CONFIG is the fallback) to select one of several bots run side by side.

commands:
  setup       interactive setup wizard (bot token, Telegram checks, background service)
  run         run the bridge in the foreground (what the background service does)
  uninstall   stop the service and remove config, state and the installed launcher
  topic-edit  "<name>" "<emoji>": rename the current topic and set its icon (run from an agent session)
  close       --thread ID [--chat ID]: archive a topic
  statusline  <outfile>: Claude Code statusLine command that records rate limits for /usage
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        argv = config.pop_config_arg(argv)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    command = argv.pop(0) if argv and (not argv[0].startswith("-") or argv[0] in ("-h", "--help")) else "run"
    if command not in ("run", "setup", "uninstall", "topic-edit", "statusline", "close"):
        sys.stderr.write(USAGE)
        return 0 if command in ("-h", "--help") else 2
    if command == "setup":
        from agent2telegramtopic import setup
        return setup.main(argv)
    if command == "uninstall":
        from agent2telegramtopic import uninstall
        return uninstall.main(argv)
    if command == "topic-edit":
        from agent2telegramtopic import topicedit
        return topicedit.main(argv)
    if command == "close":
        from agent2telegramtopic import control
        return control.close_main(argv)
    if command == "statusline":
        from agent2telegramtopic import statusline
        return statusline.main(argv)
    from agent2telegramtopic import app
    return app.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
