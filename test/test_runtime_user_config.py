"""User configuration must serialize chat and background whole-file updates."""

from pathlib import Path
from threading import Event, Thread, current_thread
import multiprocessing

import pytest

from assistant.platform.locks import data_write_lock
from assistant.platform.user_config import update_user_config


def test_concurrent_website_and_credential_updates_preserve_both(tmp_path, monkeypatch):
    """Force the old stale-read schedule without deadlocking a correctly locked writer."""
    cfg = tmp_path / "config.env"
    cfg.write_text("# keep this comment\nGITHUB_TOKEN=old-value\nGITHUB_USER=owner\n")
    original_read = Path.read_text
    first_read, second_attempt, second_done = Event(), Event(), Event()
    errors = []

    def delayed_read(path, *args, **kwargs):
        """Old code lets credentials finish after this stale website read; fixed code waits."""
        text = original_read(path, *args, **kwargs)
        if path == cfg and current_thread().name == "website-writer":
            first_read.set()
            assert second_attempt.wait(2), "credential writer never started"
            # A fixed writer owns the common lock: the credential writer cannot
            # finish yet. Old unlocked code finishes here, then loses its update.
            second_done.wait(0.2)
        return text

    monkeypatch.setattr(Path, "read_text", delayed_read)

    def website():
        """Represent the background website-build update."""
        try:
            update_user_config(tmp_path, {"WEBSITE_REPO": "owner/owner.github.io"})
        except BaseException as exc:
            errors.append(exc)

    def credentials():
        """Represent a concurrent chat connection or token rotation."""
        try:
            assert first_read.wait(2), "website writer never read configuration"
            second_attempt.set()
            update_user_config(tmp_path, {"GITHUB_TOKEN": "rotated-value", "GITHUB_USER": "owner"})
        except BaseException as exc:
            errors.append(exc)
        finally:
            second_done.set()

    workers = [Thread(target=website, name="website-writer"),
               Thread(target=credentials, name="credential-writer")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(4)
    assert not any(worker.is_alive() for worker in workers), "configuration writer deadlocked"
    assert not errors, f"configuration writer failed: {errors!r}"
    result = original_read(cfg)
    assert "GITHUB_TOKEN=rotated-value\n" in result
    assert "WEBSITE_REPO=owner/owner.github.io\n" in result
    assert result.startswith("# keep this comment\n")
    assert cfg.stat().st_mode & 0o777 == 0o600


def test_user_config_reuses_existing_reentrant_owner_lock(tmp_path):
    """Chat action batches and direct writes nest under the same data-dir lock."""
    errors = []

    def write_under_batch_lock():
        """A different lock path or a non-reentrant acquisition would block this thread."""
        try:
            with data_write_lock(tmp_path):
                update_user_config(tmp_path, {"GITHUB_USER": "owner"})
                update_user_config(tmp_path, {"WEBSITE_REPO": "owner/site"})
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=write_under_batch_lock, daemon=True)
    worker.start()
    worker.join(3)
    assert not worker.is_alive(), "nested owner configuration lock deadlocked"
    assert not errors
    assert "WEBSITE_REPO=owner/site\n" in (tmp_path / "config.env").read_text()


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                    reason="cross-process flock regression uses POSIX fork")
def test_user_config_waits_for_same_owner_lock_across_processes(tmp_path, monkeypatch):
    """The direct config writer must honor a tenant transaction held by another process."""
    context = multiprocessing.get_context("fork")
    locked, release, second_read = context.Event(), context.Event(), context.Event()
    cfg = tmp_path / "config.env"
    cfg.write_text("GITHUB_USER=owner\n")
    original_read = Path.read_text

    def observe_read(path, *args, **kwargs):
        """Expose whether the second process read while the first still owns the lock."""
        if path == cfg and multiprocessing.current_process().name == "config-second":
            second_read.set()
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe_read)

    def hold_owner_transaction():
        """Represent an in-flight chat batch using the canonical owner lock."""
        with data_write_lock(tmp_path):
            locked.set()
            assert release.wait(4)
            update_user_config(tmp_path, {"GITHUB_TOKEN": "rotated-value"})

    def update_website():
        """The independent background process cannot read before acquiring that lock."""
        update_user_config(tmp_path, {"WEBSITE_REPO": "owner/site"})

    first = context.Process(target=hold_owner_transaction, name="config-first")
    second = context.Process(target=update_website, name="config-second")
    first.start()
    assert locked.wait(2)
    second.start()
    try:
        assert not second_read.wait(0.2), "config read bypassed another process's owner lock"
    finally:
        release.set()
        first.join(4)
        second.join(4)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(2)
    assert first.exitcode == second.exitcode == 0
    assert second_read.is_set()
    result = original_read(cfg)
    assert "GITHUB_TOKEN=rotated-value\n" in result
    assert "WEBSITE_REPO=owner/site\n" in result


def test_user_config_preserves_comments_order_and_private_permissions(tmp_path):
    """An upsert keeps hand-written configuration while replacing permissive file mode."""
    cfg = tmp_path / "config.env"
    cfg.write_text("# personal configuration\nGITHUB_USER=old\n\nOTHER=retained\n")
    cfg.chmod(0o644)
    update_user_config(tmp_path, {"GITHUB_USER": "new", "WEBSITE_REPO": "new/site"})
    assert cfg.read_text() == ("# personal configuration\nGITHUB_USER=new\n\n"
                               "OTHER=retained\nWEBSITE_REPO=new/site\n")
    assert cfg.stat().st_mode & 0o777 == 0o600
