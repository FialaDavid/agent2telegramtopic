"""Command dispatcher: ``python3 -m agent2telegramtopic [run|setup|uninstall|service]``."""
import sys

USAGE = """usage: agent2telegramtopic <command>

commands:
  setup       interactive setup wizard (bot token, Telegram checks, background service)
  run         run the bridge in the foreground (what the background service does)
  uninstall   stop the service and remove config, state and the installed launcher
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and (not argv[0].startswith("-") or argv[0] in ("-h", "--help")) else "run"
    if command not in ("run", "setup", "uninstall"):
        sys.stderr.write(USAGE)
        return 0 if command in ("-h", "--help") else 2
    if command == "setup":
        from agent2telegramtopic import setup
        return setup.main(argv)
    if command == "uninstall":
        from agent2telegramtopic import uninstall
        return uninstall.main(argv)
    from agent2telegramtopic import app
    return app.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
