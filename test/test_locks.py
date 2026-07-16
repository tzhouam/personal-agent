"""Per-user mutation lock — reentrant in-thread, exclusive across threads, and
independent across users (multi-user §8)."""
import threading
from types import SimpleNamespace

from assistant.locks import _local, user_write_lock


def _s(path):
    """A minimal settings stand-in — the lock only reads `.data_dir`."""
    return SimpleNamespace(data_dir=path)


def test_reentrant_in_same_thread_does_not_deadlock(tmp_path):
    s = _s(tmp_path)
    with user_write_lock(s):
        with user_write_lock(s):          # nested acquire reuses the held fd
            with user_write_lock(s):
                pass
    # fully released: no lingering per-thread bookkeeping
    assert not getattr(_local, "held", {})


def test_exclusive_across_threads(tmp_path):
    s = _s(tmp_path)
    t1_has = threading.Event()
    release_t1 = threading.Event()
    t2_got = threading.Event()

    def hold():
        with user_write_lock(s):
            t1_has.set()
            release_t1.wait(5)

    def contend():
        with user_write_lock(s):
            t2_got.set()

    threading.Thread(target=hold, daemon=True).start()
    assert t1_has.wait(5)
    threading.Thread(target=contend, daemon=True).start()
    # T2 cannot acquire while T1 holds the exclusive lock
    assert not t2_got.wait(0.4)
    release_t1.set()
    # once T1 releases, T2 proceeds
    assert t2_got.wait(5)


def test_independent_across_users(tmp_path):
    a, b = _s(tmp_path / "a"), _s(tmp_path / "b")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    both = threading.Event()
    release = threading.Event()

    def hold_b():
        with user_write_lock(b):
            both.set()
            release.wait(5)

    threading.Thread(target=hold_b, daemon=True).start()
    assert both.wait(5)
    # user A's lock is a different file → acquiring it never blocks on B's
    with user_write_lock(a):
        pass
    release.set()


def test_lock_file_lives_under_the_user_data_dir(tmp_path):
    s = _s(tmp_path)
    with user_write_lock(s):
        assert (tmp_path / "write.lock").exists()
