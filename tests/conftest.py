import pytest

from agent2telegramtopic.config import CONFIG_ENV


@pytest.fixture(autouse=True)
def default_instance(monkeypatch):
    """--config sets an environment variable; no test may inherit or leak one."""
    monkeypatch.delenv(CONFIG_ENV, raising=False)
