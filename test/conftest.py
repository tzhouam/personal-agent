import base64
import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import assistant.agent.wiring  # noqa: F401 — registers all agent impls of platform contracts
from assistant.platform.config import Settings


def _body(files, filename, password="pw"):
    """The DECRYPTED body of a private website page.

    Private pages only render when a password is set — an empty
    WEBSITE_PASSWORD used to publish them in plaintext on a public Pages site,
    so render_site now omits them entirely. Any assertion about their *content*
    therefore has to decrypt. Nav and hero live outside the ciphertext and stay
    assertable on the raw page."""
    m = re.search(r"data-salt='([^']+)' data-iv='([^']+)' data-ct='([^']+)'",
                  files[filename])
    salt, iv, ct = (base64.b64decode(g) for g in m.groups())
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=100_000).derive(password.encode())
    return AESGCM(key).decrypt(iv, ct, None).decode()


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
