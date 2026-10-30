import os
import subprocess
import sys
import time

import pytest

from agent2telegramtopic import app as app_module
from agent2telegramtopic.lock import AlreadyRunning, lock_file, single_instance_lock


def test_lock_released_after_context_and_pid_written(tmp_path):
    path = tmp_path / "bot.lock"
    with single_instance_lock(path):
        assert path.read_text() == str(os.getpid())
        with pytest.raises(AlreadyRunning):
            with single_instance_lock(path):
                pass
    with single_instance_lock(path):
        assert path.read_text() == str(os.getpid())


def test_lock_excludes_a_second_process(tmp_path):
    path = tmp_path / "bot.lock"
    ready = tmp_path / "ready"
    holder_code = (
        "import sys; from agent2telegramtopic.lock import single_instance_lock; "
        "cm=single_instance_lock(sys.argv[1]); cm.__enter__(); "
        "open(sys.argv[2], 'w').write('ready'); "
        "sys.stdin.read()"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_code, str(path), str(ready)],
                              stdin=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "lock holder did not signal readiness"
        contender_code = (
            "import sys; from agent2telegramtopic.lock import single_instance_lock, AlreadyRunning; "
            "\ntry:"
            "\n with single_instance_lock(sys.argv[1]): pass"
            "\nexcept AlreadyRunning: raise SystemExit(9)"
        )
        result = subprocess.run([sys.executable, "-c", contender_code, str(path)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 9
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_main_exits_before_startup_when_lock_is_held(monkeypatch, tmp_path, caplog):
    path = tmp_path / "bot.lock"
    monkeypatch.setattr(app_module, "lock_file", lambda bot: path)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from agent2telegramtopic.config import save_config
    save_config({"agent": "codex", "token": "1234567:fake-token-for-tests-only-aaaaaaaaaaaa",
                 "allowed_user_ids": [1], "bot_username": "x"}, tmp_path)
    monkeypatch.setattr(app_module, "build_app", lambda *a, **k: pytest.fail("build_app called"))
    with single_instance_lock(path):
        assert app_module.main([]) == 3
    assert "already running" in caplog.text
    assert str(path) in caplog.text


def test_lock_file_uses_agent2telegramtopic_state_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert lock_file("bot") == tmp_path / ".agent2telegramtopic/state/bot.lock"
