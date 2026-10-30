"""Service files are rendered and asserted; nothing is installed on the machine running the tests."""
import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

from agent2telegramtopic import service, uninstall

PY = "/opt/py thon/bin/python3"
PATH = "/home/u/.local/bin:/usr/bin:/bin"
SRC = Path("/home/u/.agent2telegramtopic-src")


def test_launchd_plist_runs_the_bridge_and_restarts_it_on_failure(tmp_path):
    data = plistlib.loads(service.launchd_plist(PY, tmp_path, PATH, SRC).encode())
    assert data["Label"] == "com.agent2telegramtopic.bridge"
    assert data["ProgramArguments"] == [PY, "-m", "agent2telegramtopic", "run"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["EnvironmentVariables"] == {
        "PYTHONPATH": str(SRC), "PATH": PATH, "TMUX_TMPDIR": str(tmp_path / ".agent2telegramtopic/tmux")}
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(tmp_path / ".agent2telegramtopic/logs/bridge.log")


def test_launchd_plist_escapes_xml_special_characters(tmp_path):
    data = plistlib.loads(service.launchd_plist("/a&b/<py>", tmp_path, "/x&y", SRC).encode())
    assert data["ProgramArguments"][0] == "/a&b/<py>"
    assert data["EnvironmentVariables"]["PATH"] == "/x&y"


def test_systemd_unit_runs_the_bridge_as_a_user_service(tmp_path):
    unit = service.systemd_unit(PY, tmp_path, PATH, SRC)
    lines = unit.splitlines()
    assert f'ExecStart="{PY}" -m agent2telegramtopic run' in lines
    assert f'Environment="PYTHONPATH={SRC}"' in lines
    assert f'Environment="PATH={PATH}"' in lines
    assert f'Environment="TMUX_TMPDIR={tmp_path}/.agent2telegramtopic/tmux"' in lines
    assert "Restart=on-failure" in lines
    assert "RestartPreventExitStatus=2 3" in lines
    assert "WantedBy=default.target" in lines
    assert "[Unit]" in lines and "[Service]" in lines and "[Install]" in lines


def test_systemd_unit_escapes_percent_and_quotes(tmp_path):
    unit = service.systemd_unit('/p"y', tmp_path, "/a%b", SRC)
    assert 'ExecStart="/p\\"y"' in unit
    assert 'Environment="PATH=/a%%b"' in unit


class Runner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.returncode, stderr="boom")


def test_install_on_macos_writes_the_plist_under_home_and_loads_it(tmp_path):
    runner = Runner()
    target = service.install("darwin", python=PY, home=tmp_path, path=PATH, runner=runner)
    assert Path(target) == tmp_path / "Library/LaunchAgents/com.agent2telegramtopic.bridge.plist"
    assert plistlib.loads(Path(target).read_bytes())["Label"] == service.LABEL
    domain = f"gui/{os.getuid()}"
    assert runner.calls == [["launchctl", "bootout", f"{domain}/{service.LABEL}"],
                            ["launchctl", "bootstrap", domain, target]]


def test_install_on_linux_writes_the_unit_under_home_and_enables_it(tmp_path):
    runner = Runner()
    target = service.install("linux", python=PY, home=tmp_path, path=PATH, runner=runner)
    assert Path(target) == tmp_path / ".config/systemd/user/agent2telegramtopic.service"
    assert "ExecStart=" in Path(target).read_text()
    assert runner.calls == [["systemctl", "--user", "daemon-reload"],
                            ["systemctl", "--user", "enable", "--now", "agent2telegramtopic.service"]]


def test_failed_activation_is_an_error_not_silent(tmp_path):
    import pytest
    with pytest.raises(RuntimeError, match="systemctl"):
        service.install("linux", python=PY, home=tmp_path, path=PATH, runner=Runner(returncode=1))
    with pytest.raises(RuntimeError, match="launchctl"):
        service.install("darwin", python=PY, home=tmp_path, path=PATH, runner=Runner(returncode=1))


def test_remove_stops_and_deletes_the_service_file(tmp_path):
    for platform, path in (("darwin", service.plist_path(tmp_path)), ("linux", service.unit_path(tmp_path))):
        path.parent.mkdir(parents=True)
        path.write_text("x")
        runner = Runner()
        assert service.remove(platform, home=tmp_path, runner=runner) is True
        assert not path.exists()
        assert runner.calls
        assert service.remove(platform, home=tmp_path, runner=runner) is False


def test_uninstall_removes_service_config_and_state_but_only_under_the_given_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    app = home / ".agent2telegramtopic"
    (app / "state").mkdir(parents=True)
    (app / "config.json").write_text("{}")
    unit = service.unit_path(home)
    unit.parent.mkdir(parents=True)
    unit.write_text("x")
    src = home / ".agent2telegramtopic-src"
    (src / "agent2telegramtopic").mkdir(parents=True)
    launcher_dir = home / ".local/bin"
    launcher_dir.mkdir(parents=True)
    (launcher_dir / "agent2telegramtopic").write_text("#!/bin/sh\nexec env PYTHONPATH=x python3 -m agent2telegramtopic\n")
    outside = tmp_path / "bin"
    outside.mkdir()
    (outside / "agent2telegramtopic").write_text("#!/bin/sh\nexec python3 -m agent2telegramtopic\n")
    monkeypatch.setenv("PATH", f"{outside}{os.pathsep}{launcher_dir}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: calls.append(a[0]) or SimpleNamespace(returncode=0))
    assert uninstall.main(["--yes"], home=home, platform="linux") == 0
    assert not app.exists() and not unit.exists() and not src.exists()
    assert not (launcher_dir / "agent2telegramtopic").exists()
    assert (outside / "agent2telegramtopic").exists()
    assert ["systemctl", "--user", "disable", "--now", service.UNIT] in calls


def test_uninstall_asks_first_and_keeps_everything_on_no(tmp_path, monkeypatch):
    app = tmp_path / ".agent2telegramtopic"
    app.mkdir()
    monkeypatch.setattr("builtins.input", lambda _p="": "n")
    assert uninstall.main([], home=tmp_path, platform="linux") == 0
    assert app.exists()
