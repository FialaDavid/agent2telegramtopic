"""Uninstall must end the bridge's agent sessions, and only those: a fake `tmux` records which server
each call would reach, chosen the way real tmux chooses it (-S, else $TMUX, else $TMUX_TMPDIR)."""
import json
import os
import stat

import pytest

from agent2telegramtopic import app, service, uninstall
from agent2telegramtopic.config import set_config_path

FAKE_TMUX = """#!/bin/sh
sock=""; cmd=""
while [ $# -gt 0 ]; do
  case "$1" in -S) sock="$2"; shift 2;; kill-server) cmd=kill-server; shift;; *) shift;; esac
done
[ -z "$sock" ] && [ -n "$TMUX" ] && sock="${TMUX%%,*}"
[ -z "$sock" ] && sock="$TMUX_TMPDIR/tmux-$(id -u)/default"
echo "$cmd $sock" >> "$FAKE_TMUX_LOG"
"""


@pytest.fixture
def fake_tmux(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "tmux"
    script.write_text(FAKE_TMUX)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "tmux.log"
    log.touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TMUX_LOG", str(log))
    monkeypatch.delenv("TMUX", raising=False)
    return lambda: log.read_text().splitlines()


def bridge_socket(app_dir):
    return app_dir / "tmux" / f"tmux-{os.getuid()}" / "default"


def make_instance(tmp_path, name=".agent2telegramtopic"):
    app_dir = tmp_path / name
    (app_dir / "tmux" / f"tmux-{os.getuid()}").mkdir(parents=True)
    bridge_socket(app_dir).touch()
    (app_dir / "config.json").write_text(json.dumps({"bot_username": "b"}))
    return app_dir


def run_uninstall(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "remove", lambda *a, **k: False)
    return uninstall.main(["--yes"], home=tmp_path, platform="linux")


def test_uninstall_kills_the_bridges_tmux_server_even_when_run_inside_another_tmux(tmp_path, monkeypatch, fake_tmux):
    app_dir = make_instance(tmp_path)
    monkeypatch.setenv("TMUX", "/somewhere/else/tmux-1/default,123,0")   # e.g. uninstall run from an agent session
    assert run_uninstall(tmp_path, monkeypatch) == 0
    assert fake_tmux() == [f"kill-server {bridge_socket(app_dir)}"]
    assert not app_dir.exists()


def test_uninstall_of_one_instance_kills_only_its_own_server(tmp_path, monkeypatch, fake_tmux):
    first = make_instance(tmp_path, "first")
    second = make_instance(tmp_path, "second")
    set_config_path(second / "config.json")
    assert run_uninstall(tmp_path, monkeypatch) == 0
    assert fake_tmux() == [f"kill-server {bridge_socket(second)}"]
    assert bridge_socket(first).exists()


def test_uninstall_does_not_touch_any_tmux_server_when_the_bridge_had_none(tmp_path, monkeypatch, fake_tmux):
    (tmp_path / ".agent2telegramtopic").mkdir()
    assert run_uninstall(tmp_path, monkeypatch) == 0
    assert fake_tmux() == []


def test_bridge_start_gives_its_agent_sessions_a_private_tmux_server(tmp_path, monkeypatch):
    # tmux silently falls back to the shared /tmp server when TMUX_TMPDIR does not exist, so the sessions
    # would live where uninstall (rightly) never looks.
    set_config_path(tmp_path / "bot" / "config.json")
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    app.use_private_tmux()
    assert os.environ["TMUX_TMPDIR"] == str(tmp_path / "bot" / "tmux")
    assert (tmp_path / "bot" / "tmux").is_dir()
