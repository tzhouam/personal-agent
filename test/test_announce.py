from assistant.agent.deliver.announce import announce_digest


def _enabled(settings, tmp_path, script: str):
    bin_path = tmp_path / "fake-openclaw"
    bin_path.write_text(script)
    bin_path.chmod(0o755)
    return settings.model_copy(update={
        "wechat_announce": True,
        "announce_account": "acct-1",
        "announce_to": "owner-id",
        "openclaw_bin": str(bin_path),
    })


def test_disabled_by_default(settings):
    assert announce_digest(settings, "hi") == "disabled"
    half = settings.model_copy(update={"wechat_announce": True})
    assert announce_digest(half, "hi").startswith("disabled (set ")


def test_sent_passes_exact_cli_args(settings, tmp_path):
    s = _enabled(settings, tmp_path,
                 '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/args.txt"\nexit 0\n')
    assert announce_digest(s, "Daily digest done.") == "sent"
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args == ["message", "send", "--channel", "openclaw-weixin",
                    "--account", "acct-1", "--target", "owner-id",
                    "-m", "Daily digest done."]


def test_failure_never_raises(settings, tmp_path):
    s = _enabled(settings, tmp_path, '#!/bin/sh\necho "boom" >&2\nexit 7\n')
    assert announce_digest(s, "x") == "failed: rc=7 boom"
    missing = s.model_copy(update={"openclaw_bin": str(tmp_path / "nope")})
    assert announce_digest(missing, "x").startswith("failed: ")
