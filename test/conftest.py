import pytest

import assistant.agent.wiring  # noqa: F401 — registers all agent impls of platform contracts
from assistant.platform.config import Settings


@pytest.fixture
def settings(tmp_path):
    """Settings isolated from .env and the real data dir."""
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        github_token="test-token",
        github_user="tester",
        smtp_user="tester@example.com",
        chrome_history_path=tmp_path / "History",
        sources_file=tmp_path / "sources.yaml",
    )
