"""A pasted credential must never reach the daemon log.

secrets.py's chokepoint covered SessionStore and the trace writer; the poll
loops logged raw inbound text, so a token pasted over email landed verbatim in
a never-rotated 0644 serve.log while the masked history copy made it look like
it was never written down. Covered twice now: at the call sites, and by a root
handler filter so a future log line cannot reintroduce it."""
import logging

from assistant.platform.secrets import mask_secrets
from assistant.platform.serve import _install_masking_filter

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _capture(fn):
    """Run fn() with a real handler attached; return everything it FORMATTED."""
    out = []

    class _Sink(logging.Handler):
        def emit(self, record):
            out.append(self.format(record))

    root = logging.getLogger()
    sink = _Sink()
    sink.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(sink)
    prev = root.level
    root.setLevel(logging.INFO)
    try:
        _install_masking_filter()      # filter goes on every root handler
        fn()
    finally:
        root.removeHandler(sink)
        root.setLevel(prev)
    return "\n".join(out)


def test_mask_secrets_redacts_a_classic_token():
    assert TOKEN not in mask_secrets(f"my token is {TOKEN} ok")


def test_filter_masks_a_token_passed_as_a_log_arg():
    """The real shape: log.info("%s message from %s: %.80s", ..., text)."""
    log = logging.getLogger("assistant.test")
    text = _capture(lambda: log.info("%s message from %s: %.80s",
                                     "email", "owner@example.com", TOKEN))
    assert TOKEN not in text
    assert "ghp_" in text          # fingerprint survives for diagnostics


def test_filter_masks_a_token_in_the_format_string_itself():
    log = logging.getLogger("assistant.test")
    text = _capture(lambda: log.info(f"owner pasted {TOKEN}"))
    assert TOKEN not in text


def test_filter_is_idempotent_and_leaves_non_str_args_alone():
    _install_masking_filter()
    _install_masking_filter()
    root = logging.getLogger()
    for h in root.handlers:
        assert sum(type(f).__name__ == "_MaskingFilter" for f in h.filters) <= 1
    log = logging.getLogger("assistant.test")
    assert "42" in _capture(lambda: log.info("count=%d ok=%s", 42, True))
