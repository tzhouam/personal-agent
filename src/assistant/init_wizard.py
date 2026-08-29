"""`assistant init` — guided first-run setup, and `assistant init --check`,
the config doctor.

The wizard walks a new user through every setting group, writes .env
progressively (keeping template comments), and live-validates each group as
soon as its values are in (LLM ping, GitHub identity, repo push access,
marks-token scope). The doctor runs the same probes against the current
config and prints a ✅/⚠️/❌ report — use it any time something feels off.
"""

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from assistant.platform.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
OK, WARN, FAIL, SKIP = "✅", "⚠️ ", "❌", "◌ "


# ── .env editing (comment-preserving) ────────────────────────────────

def upsert_env(env_path: Path, key: str, value: str) -> None:
    """Set KEY=value in .env: replaces a live line, uncomments a template
    line, or appends — everything else (comments, order) is preserved."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    live = re.compile(rf"^{re.escape(key)}=")
    templated = re.compile(rf"^#\s*{re.escape(key)}=")
    new_line = f"{key}={value}"
    for i, line in enumerate(lines):
        if live.match(line) or templated.match(line):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    env_path.write_text("\n".join(lines) + "\n")


def _mask(value: str) -> str:
    """Redact a secret for on-screen echo: `(unset)` when empty, the value
    verbatim if short (≤8), else first-four…last-four so the user can confirm
    which key is set without exposing it."""
    if not value:
        return "(unset)"
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def _ask(prompt: str) -> str:  # seam for tests
    """Prompt on the terminal and return the stripped reply; a single
    indirection point so tests can stub interactive input."""
    return input(prompt).strip()


def _bounded_identifier(value, limit: int = 64) -> str:
    """Return a printable bounded model/error label with no control characters."""
    import re

    safe = re.sub(r"[^A-Za-z0-9._:/+-]", "?", str(value or "?"))
    return safe[:limit] or "?"


def _bounded_config_identifier(value, limit: int = 64) -> str:
    """Bound a config label and redact values that resemble URLs or secrets."""
    import re

    raw = str(value or "?")
    key_like = re.search(
        r"(?i)(?:^sk[-_]|api[_-]?key|secret|password|credential|bearer|"
        r"(?:^|[_-])token(?:$|[_-])|(?:^|[_-])key(?:$|[_-]))",
        raw)
    if "://" in raw or key_like:
        return "[redacted]"
    return _bounded_identifier(raw, limit)


def _bounded_failure(exc) -> str:
    """Describe only exception type and integer status, never its message/body."""
    error_type = _bounded_identifier(type(exc).__name__)
    try:
        status = getattr(exc, "status_code", None)
    except Exception:
        status = None
    if isinstance(status, int) and not isinstance(status, bool):
        return f"{error_type} HTTP {status}"
    return error_type


# ── probes (shared by wizard and doctor) ─────────────────────────────

def probe_llm(s: Settings):
    """Confirm the agent can actually think: with a key set, fire a one-token
    completion and report OK on any reply, WARN on an empty one. Returns the
    `(status, detail)` pair every probe yields for the wizard/doctor report."""
    if not s.anthropic_api_key:
        return FAIL, "ANTHROPIC_API_KEY unset — the agent cannot think without it"
    try:
        from assistant.platform.llm import LLM

        # An explicit operator health check is also the recovery path for a
        # route quarantined after an auth/entitlement failure.  The probe
        # bypasses quarantine and clears it only after a successful response.
        reply = LLM(s).force_probe(
            "Reply with the single word: ok", max_tokens=1500)
        model = _bounded_config_identifier(s.anthropic_model)
        return (OK, f"model {model} answers") if reply.strip() \
            else (WARN, "endpoint reachable but empty reply")
    except Exception as exc:
        return FAIL, (f"model {_bounded_config_identifier(s.anthropic_model)} failed: "
                      f"{_bounded_failure(exc)}")


def probe_github(s: Settings):
    """Validate the GitHub token against `/user`, and WARN when the
    authenticated login disagrees with a configured `GITHUB_USER` (a common
    wrong-token mistake). Returns `(status, detail)`."""
    if not s.github_token:
        return FAIL, "GITHUB_TOKEN unset — collectors and website push need it"
    try:
        import httpx

        r = httpx.get("https://api.github.com/user", timeout=15,
                      headers={"Authorization": f"Bearer {s.github_token}"})
        if r.status_code != 200:
            return FAIL, f"token rejected (HTTP {r.status_code})"
        login = r.json().get("login", "?")
        if s.github_user and login.lower() != s.github_user.lower():
            return WARN, f"token belongs to {login!r} but GITHUB_USER={s.github_user!r}"
        return OK, f"authenticated as {login}"
    except Exception as exc:
        return FAIL, f"GitHub unreachable: {str(exc)[:120]}"


def probe_email(s: Settings):
    """Check a digest delivery path exists — Resend key preferred, SMTP creds
    as fallback — and FAIL if neither is configured. Returns `(status,
    detail)`."""
    if s.resend_api_key:
        return OK, f"Resend configured → {s.recipient or '(set DIGEST_TO!)'}"
    if s.smtp_user and s.smtp_password:
        return OK, f"SMTP via {s.smtp_host} → {s.recipient}"
    return FAIL, "no delivery path — set RESEND_API_KEY or SMTP_USER/SMTP_PASSWORD"


def probe_website(s: Settings):
    """Verify push access to the Pages repo the site renders into (SKIP when
    disabled), and WARN if `WEBSITE_PASSWORD` is unset since the todos/reading
    pages would then publish unencrypted. Returns `(status, detail)`."""
    if not s.website_repo:
        return SKIP, "WEBSITE_REPO unset — personal site disabled"
    try:
        import httpx

        r = httpx.get(f"https://api.github.com/repos/{s.website_repo}", timeout=15,
                      headers={"Authorization": f"Bearer {s.github_token}"})
        if r.status_code != 200:
            return FAIL, f"{s.website_repo}: HTTP {r.status_code}"
        if not r.json().get("permissions", {}).get("push"):
            return FAIL, f"no push access to {s.website_repo}"
        if not s.website_password:
            # Was a WARN saying the pages "will be public" — they no longer are:
            # render_site refuses to emit them and sync deletes any published
            # copy. FAIL because the private surface is entirely missing until
            # a password is set, which is a configuration error, not a nuance.
            return FAIL, ("push access ok, but WEBSITE_PASSWORD unset — "
                          "todos/reading/routines pages will NOT publish")
        return OK, "push access ok"
    except Exception as exc:
        return FAIL, str(exc)[:120]


def probe_marks(s: Settings):
    """Vet the marks-sync push token that ships inside the encrypted pages:
    require `WEBSITE_PASSWORD` (it only travels encrypted), confirm the token
    reaches the marks repo, and WARN if the token can see *other* repos —
    since it lands in browsers, its scope should be that repo only. Returns
    `(status, detail)`."""
    if not (s.marks_repo and s.marks_push_token):
        return SKIP, "marks sync disabled (MARKS_REPO/MARKS_PUSH_TOKEN unset) — website clicks stay browser-local"
    if not s.website_password:
        return FAIL, "MARKS_PUSH_TOKEN needs WEBSITE_PASSWORD — the token only ships encrypted"
    try:
        import httpx

        headers = {"Authorization": f"Bearer {s.marks_push_token}"}
        if httpx.get(f"https://api.github.com/repos/{s.marks_repo}", timeout=15,
                     headers=headers).status_code != 200:
            return FAIL, f"push token cannot reach {s.marks_repo}"
        visible = httpx.get("https://api.github.com/user/repos?per_page=5", timeout=15,
                            headers=headers).json()
        others = [r["full_name"] for r in visible if isinstance(r, dict)
                  and r.get("full_name", "").lower() != s.marks_repo.lower()]
        if others:
            return WARN, (f"token also sees {others[0]} (+…) — it ships to browsers; "
                          f"prefer a fine-grained PAT scoped to {s.marks_repo} only")
        return OK, f"token scoped to {s.marks_repo}"
    except Exception as exc:
        return FAIL, str(exc)[:120]


def probe_resume(s: Settings):
    """Check the resume git remote answers an `ls-remote HEAD` (SKIP when
    unset), with terminal prompts disabled so a bad credential fails fast
    instead of hanging. Returns `(status, detail)`."""
    if not s.resume_remote_url:
        return SKIP, "RESUME_REMOTE_URL unset — resume sync disabled"
    try:
        r = subprocess.run(["git", "ls-remote", s.resume_remote_url, "HEAD"],
                           capture_output=True, text=True, timeout=20,
                           env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin"})
        return (OK, "remote reachable") if r.returncode == 0 \
            else (FAIL, (r.stderr.strip().splitlines() or ["unreachable"])[-1][:120])
    except Exception as exc:
        return FAIL, str(exc)[:120]


def probe_search(s: Settings):
    """Report the first configured web-search backend in preference order,
    else WARN that `/search` falls back to rate-limited keyless DuckDuckGo.
    Returns `(status, detail)`."""
    for name, key in (("Gemini grounding", s.gemini_api_key), ("Google CSE", s.google_api_key),
                      ("Tavily", s.tavily_api_key), ("Brave", s.brave_api_key)):
        if key:
            return OK, f"{name} configured"
    return WARN, "no search key — /search falls back to keyless DuckDuckGo Lite (rate-limited)"


def probe_collectors(s: Settings):
    """Summarise which activity collectors can run — chrome history file,
    gmail (needs SMTP creds), github token — as one `✓/✗` line; OK only when
    the github collector is wired, else WARN. Returns `(status, detail)`."""
    bits = []
    bits.append("chrome ✓" if s.chrome_history_path.exists() else "chrome ✗ (no History file)")
    bits.append("gmail ✓" if (s.gmail_enabled and s.smtp_user and s.smtp_password)
                else "gmail ✗ (needs SMTP creds)")
    bits.append("github ✓" if s.github_token else "github ✗")
    return (OK if "✗" not in " ".join(bits[2:]) else WARN), " · ".join(bits)


def probe_profile(s: Settings):
    """Report whether the profile has been seeded (WARN with the fix if not)
    and whether `aliases.yaml` exists, since its absence disables initiative
    merging. Returns `(status, detail)`."""
    from assistant.agent.profile_store import ProfileStore

    store = ProfileStore(s.profile_dir)
    if not store.exists():
        return WARN, "no profile yet — the wizard's last step (or `assistant bootstrap`) seeds it"
    aliases = "aliases.yaml ✓" if (s.profile_dir / "aliases.yaml").exists() \
        else "aliases.yaml missing (initiative merging disabled)"
    return OK, f"profile.yaml ✓ · {aliases}"


# Roles the code actually routes (llm.py): the five task roles plus the
# cheap-tier aliases accepted by _CHEAP_ROLES.
_KNOWN_ROLES = {"chat", "pipeline", "research", "task", "evolve",
                "cheap", "bulk", "score"}
_ROUTING_KEYS = ("LLM_ROLES", "LLM_MIXTURE", "LLM_REVIEW")


def _effective_raw(name: str, env_files: tuple | None = None) -> tuple[str, str]:
    """The raw string pydantic-settings would resolve for env var `name`, plus
    the source that supplied it ("process env" / the winning .env path / "").

    Mirrors the precedence `Settings` uses: process environment over dotenv,
    and later files in the env-file tuple over earlier ones (config.py: repo
    `.env`, then CWD `.env`). The value is returned for *parsing* only — callers
    must never print it (LLM_ROLES/LLM_MIXTURE entries can carry `api_key`)."""
    import os

    if os.environ.get(name):
        return os.environ[name], "process env"
    from dotenv import dotenv_values

    files = env_files or (_REPO_ROOT / ".env", Path(".env"))
    for path in reversed([Path(f) for f in files]):
        if path.is_file():
            value = (dotenv_values(path).get(name) or "").strip()
            if value:
                return value, path.name if path.parent == Path(".") else str(path)
    return "", ""


def _summarize_roles(parsed: dict) -> tuple[list[str], str]:
    """Safe one-line summary of a parsed LLM_ROLES dict (role→model ids only;
    never keys/URLs) plus structural warnings."""
    warns, bits = [], []
    for role, spec in parsed.items():
        safe_role = _bounded_config_identifier(role)
        model = spec.get("model") if isinstance(spec, dict) else None
        bits.append(f"{safe_role}→{_bounded_config_identifier(model)}")
        if role not in _KNOWN_ROLES:
            warns.append(f"unknown role {safe_role!r} (known: "
                         f"{', '.join(sorted(_KNOWN_ROLES))})")
        if not model:
            warns.append(f"role {safe_role!r} has no \"model\"")
    return warns, "roles " + (", ".join(bits) or "(empty)")


def _summarize_mixture(parsed: dict) -> tuple[list[str], str]:
    """Safe one-line summary of a parsed LLM_MIXTURE dict (member/aggregator
    model ids + roles) plus structural warnings — including the chat-latency
    one (priority: chat stays single-model by default)."""
    warns = []
    raw_members = parsed.get("members")
    if raw_members in (None, []):
        members = []
    elif isinstance(raw_members, list):
        members = raw_members
    else:
        members = []
        warns.append('mixture "members" must be a list')
    models = [m.get("model") if isinstance(m, dict) else None for m in members]
    invalid_members = [m for m in members if not (
        isinstance(m, dict)
        and isinstance(m.get("model"), str) and m.get("model", "").strip()
        and (m.get("base_url") is None or isinstance(m.get("base_url"), str))
        and (m.get("api_key") is None or isinstance(m.get("api_key"), str)))]
    if invalid_members:
        warns.append("invalid mixture member route(s) are ignored")
    if len(members) < 2:
        warns.append("fewer than 2 members — MoA stays off")
    raw_aggregator = parsed.get("aggregator")
    agg = raw_aggregator or (members[0] if members else {})
    if raw_aggregator is not None and not isinstance(raw_aggregator, dict):
        warns.append('mixture "aggregator" must be an object')
    agg_model = agg.get("model", "?") if isinstance(agg, dict) else "?"
    raw_roles = parsed.get("roles")
    if raw_roles in (None, []):
        roles = ["pipeline", "research", "task", "evolve"]
    elif isinstance(raw_roles, list):
        roles = raw_roles
    else:
        roles = []
        warns.append('mixture "roles" must be a list')
    if "chat" in roles:
        warns.append("mixture includes the chat role — MoA ~doubles reply "
                     "latency; interactive chat is best single-model")
    return warns, (f"mixture {len(members)} member(s) "
                   f"({', '.join(_bounded_config_identifier(m) for m in models) or 'none'}) "
                   f"→ agg {_bounded_config_identifier(agg_model)}; "
                   f"roles {', '.join(_bounded_config_identifier(r) for r in roles) or '(invalid)'}")


def _summarize_review(parsed: dict) -> tuple[list[str], str]:
    """Safe one-line summary of a parsed LLM_REVIEW spec (the plan-review
    model slot; model id only)."""
    model = parsed.get("model")
    warns = [] if model else ['LLM_REVIEW has no "model"']
    return warns, f"review→{_bounded_config_identifier(model)}"


def _summarize_live_mixture(value: dict, s: Settings) -> tuple[list[str], str]:
    """Summarize the exact normalized/deduplicated mixture runtime will use."""
    from assistant.platform.llm import normalize_mixture

    raw = value if isinstance(value, dict) else {}
    normalized = normalize_mixture(raw, s)
    raw_warns, _raw_summary = _summarize_mixture(raw)
    effective_warns, summary = _summarize_mixture(normalized)
    raw_members = raw.get("members")
    raw_count = len(raw_members) if isinstance(raw_members, list) else 0
    if raw_count != len(normalized["members"]):
        raw_warns.append("invalid or duplicate mixture member route(s) dropped")
    warns = list(dict.fromkeys(raw_warns + effective_warns))
    return warns, summary


def _configured_nondefault_routes(s: Settings) -> list[dict]:
    """Resolve and deduplicate every configured route beyond the default.

    Deduplication uses the same canonical URL + credential HMAC + model scope
    as runtime quarantine. The returned specs stay in memory and must never be
    rendered because they can contain API keys and credential-bearing URLs.
    """
    from assistant.platform.llm import normalize_mixture
    from assistant.platform.llm_health import route_scopes

    # The cheap tier is an implicit route, not an LLM_ROLES entry. Probe it
    # whenever its model differs; exact equality with the default is removed by
    # the canonical-scope dedupe below.
    candidates = [{"model": s.cheap_model}]
    roles = s.llm_roles if isinstance(s.llm_roles, dict) else {}
    candidates.extend(spec for spec in roles.values() if isinstance(spec, dict))

    mixture = normalize_mixture(s.llm_mixture, s)
    members = mixture["members"]
    candidates.extend(spec for spec in members if isinstance(spec, dict))
    aggregator = mixture.get("aggregator")
    if isinstance(aggregator, dict):
        candidates.append(aggregator)

    review = s.llm_review if isinstance(s.llm_review, dict) else {}
    if review:
        candidates.append(review)

    default_scope = route_scopes(
        s.anthropic_base_url, s.anthropic_api_key, s.anthropic_model)[1]
    seen = {default_scope}
    resolved = []
    for spec in candidates:
        model = spec.get("model")
        if not model:
            continue
        base_url = spec.get("base_url") or s.anthropic_base_url
        api_key = spec.get("api_key") or s.anthropic_api_key
        scope = route_scopes(base_url, api_key, str(model))[1]
        if scope in seen:
            continue
        seen.add(scope)
        resolved.append({"model": str(model), "base_url": base_url,
                         "api_key": api_key})
    return resolved


def _probe_configured_routes(s: Settings) -> tuple[list[str], bool, bool]:
    """Force-probe unique optional routes, returning safe summaries and flags.

    Results contain only a bounded model id plus success/empty/error type and
    integer status. Provider output, exception text, URLs, and credentials are
    deliberately discarded.
    """
    routes = _configured_nondefault_routes(s)
    if not routes:
        return [], False, False

    from assistant.platform.llm import LLM

    try:
        llm = LLM(s)
    except Exception as exc:
        failure = _bounded_failure(exc)
        return ([f"{_bounded_config_identifier(route['model'])} ✗ {failure}"
                 for route in routes], True, False)
    results = []
    failed = warned = False
    for route in routes:
        model = _bounded_config_identifier(route["model"])
        try:
            reply = llm.force_probe_route(
                route["model"], base_url=route["base_url"],
                api_key=route["api_key"], max_tokens=1500)
        except Exception as exc:
            failed = True
            results.append(f"{model} ✗ {_bounded_failure(exc)}")
            continue
        if reply.strip():
            results.append(f"{model} ✓")
        else:
            warned = True
            results.append(f"{model} ? empty")
    return results, failed, warned


def probe_model_routing(s: Settings, env_files: tuple | None = None):
    """Diagnose the three optional LLM routing JSON knobs and live routes.

    ``LLM_ROLES`` / ``LLM_MIXTURE`` / ``LLM_REVIEW`` deliberately degrade to
    ``{}`` on malformed input so a broken config can never crash startup. The
    flip side is that a typo
    silently turns the whole feature off; this probe makes that visible. Reads
    the effective raw value from the same ordered sources Settings uses and
    names the winning source — never the value. With a real ``Settings`` it
    also force-probes every unique non-default route; ``None`` remains an
    offline parser seam. Returns `(status, detail)`."""
    import json

    parts, warns, failed = [], [], False
    for name, summarize in ((_ROUTING_KEYS[0], _summarize_roles),
                            (_ROUTING_KEYS[1], _summarize_mixture),
                            (_ROUTING_KEYS[2], _summarize_review)):
        raw, source = _effective_raw(name, env_files)
        if not raw:
            continue
        safe_source = _bounded_config_identifier(source, 120)
        try:
            parsed = json.loads(raw)
        except ValueError:
            failed = True
            parts.append(f"{name} malformed JSON (from {safe_source}) — dotenv reads "
                         "a multi-line value as only its first physical line; "
                         "keep it on one line or wrap the whole value in "
                         "'single quotes'")
            continue
        if not isinstance(parsed, dict):
            failed = True
            parts.append(f"{name} is valid JSON but not an object "
                         f"(from {safe_source})")
            continue
        if s is None:
            w, summary = summarize(parsed)
            warns += w
            parts.append(summary)

    # Tests may construct Settings directly rather than through env JSON. Live
    # checks still summarize those effective objects, while ``s=None`` remains
    # the intentionally offline raw-parser path.
    if s is not None:
        effective = (("LLM_ROLES", s.llm_roles, _summarize_roles),
                     ("LLM_MIXTURE", s.llm_mixture,
                      lambda value: _summarize_live_mixture(value, s)),
                     ("LLM_REVIEW", s.llm_review, _summarize_review))
        for name, parsed, summarize in effective:
            if not isinstance(parsed, dict) or not parsed:
                continue
            w, summary = summarize(parsed)
            warns += w
            parts.append(summary)

        live_results, live_failed, live_warned = _probe_configured_routes(s)
        if live_results:
            parts.append("route checks " + ", ".join(live_results))
        failed = failed or live_failed
        if live_warned:
            warns.append("a configured model returned an empty health response")
    if not parts:
        return SKIP, ("LLM_ROLES/LLM_MIXTURE/LLM_REVIEW unset — every role "
                      "runs the default model")
    detail = " · ".join(parts + [f"⚠ {w}" for w in warns])
    return (FAIL if failed else WARN if warns else OK), detail


def probe_schedule(s: Settings):
    """Check the OpenClaw cron has both the daily-digest and weekly-consolidate
    jobs registered (WARN, not FAIL, since cron/systemd is a valid alternative
    when OpenClaw is absent). Returns `(status, detail)`."""
    if not Path(s.openclaw_bin).exists():
        return WARN, ("OpenClaw not found — schedule with cron/systemd instead "
                      "(see README 'Schedule'); WeChat channel unavailable")
    try:
        import os

        env = {**os.environ,  # the launcher resolves `node` from PATH — make
               "PATH": f"{Path(s.openclaw_bin).parent}:{os.environ.get('PATH', '')}"}
        r = subprocess.run([s.openclaw_bin, "cron", "list"], capture_output=True,
                           text=True, timeout=20, env=env)
        jobs = [j for j in ("daily-digest", "weekly-consolidate") if j in r.stdout]
        if len(jobs) == 2:
            return OK, "daily-digest + weekly-consolidate scheduled"
        return WARN, f"cron jobs found: {', '.join(jobs) or 'none'} — see README to add them"
    except Exception as exc:
        return WARN, f"openclaw cron unreachable: {str(exc)[:80]}"


# ── the step table ───────────────────────────────────────────────────

@dataclass
class Step:
    """One config group the wizard walks and the doctor checks: `title`/`intro`
    frame it, `fields` are the `(ENV_KEY, prompt, secret)` tuples to ask for,
    and `probe` (optional) validates the group once its values are in."""
    title: str
    intro: str
    fields: list = field(default_factory=list)  # (ENV_KEY, prompt, secret)
    probe: Callable | None = None


STEPS = [
    Step("LLM", "The agent's brain — an Anthropic-compatible API. For DeepSeek use\n"
         "base URL https://api.deepseek.com/anthropic and a deepseek-* model name.",
         [("ANTHROPIC_API_KEY", "API key", True),
          ("ANTHROPIC_BASE_URL", "base URL (empty = api.anthropic.com)", False),
          ("ANTHROPIC_MODEL", "main model", False),
          ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "cheap model for bulk scoring (optional)", False)],
         probe_llm),
    Step("GitHub", "A personal access token — read scope powers the activity collector;\n"
         "repo write is needed only if the website repo is private or for marks.",
         [("GITHUB_TOKEN", "GitHub token", True),
          ("GITHUB_USER", "GitHub username", False)],
         probe_github),
    Step("Email", "Daily digest delivery. Easiest: a free resend.com API key.\n"
         "SMTP (e.g. Gmail app password) is the fallback AND powers the gmail\n"
         "collector + email chat channel, so setting both is best.",
         [("RESEND_API_KEY", "Resend API key (optional)", True),
          ("SMTP_USER", "SMTP user / Gmail address (optional)", False),
          ("SMTP_PASSWORD", "SMTP app password (optional)", True),
          ("DIGEST_TO", "digest recipient email", False)],
         probe_email),
    Step("Website", "A GitHub Pages repo (username.github.io) the agent renders your\n"
         "profile/todos/reading pages into. The password encrypts private pages\n"
         "client-side — pick a strong one, it is the only gate.",
         [("WEBSITE_REPO", "Pages repo owner/name (empty = disabled)", False),
          ("WEBSITE_PASSWORD", "private-pages password", True)],
         probe_website),
    Step("Website marks sync", "Lets Done/Unrelated clicks on the site reach the agent: create a\n"
         "PRIVATE repo (e.g. <user>/agent-marks) and a fine-grained PAT scoped\n"
         "to ONLY that repo with Contents read/write — it ships inside the\n"
         "encrypted pages, so keep its scope minimal.",
         [("MARKS_REPO", "marks repo owner/name (empty = disabled)", False),
          ("MARKS_PUSH_TOKEN", "repo-scoped push token", True)],
         probe_marks),
    Step("Resume sync", "Optional: Overleaf git-bridge URL (premium feature) as\n"
         "https://git:TOKEN@git.overleaf.com/<project-id>, or any git remote.\n"
         "Pushes are always approval-gated (`assistant approve-resume`).",
         [("RESUME_REMOTE_URL", "resume git remote (empty = disabled)", True)],
         probe_resume),
    Step("Web search", "Backends for the /search chat action, best first: Gemini AI-Studio\n"
         "key (free grounded search) > Google CSE > Tavily > Brave. All optional —\n"
         "keyless DuckDuckGo is the fallback.",
         [("GEMINI_API_KEY", "Gemini API key (optional)", True),
          ("BRAVE_API_KEY", "Brave Search key (optional)", True),
          ("TAVILY_API_KEY", "Tavily key (optional)", True)],
         probe_search),
]

# doctor-only checks (no fields to prompt for)
EXTRA_CHECKS = [("Model routing", probe_model_routing),
                ("Collectors", probe_collectors), ("Profile", probe_profile),
                ("Schedule", probe_schedule)]


# ── doctor ───────────────────────────────────────────────────────────

def run_check(settings: Settings) -> int:
    """The doctor (`assistant init --check`): run every step's probe plus the
    doctor-only extras against the live config, print a status line each, and
    return exit code 1 if any probe reported FAIL else 0."""
    print("personal-agent config check\n" + "─" * 46)
    failures = 0
    for title, probe in [(s.title, s.probe) for s in STEPS if s.probe] + EXTRA_CHECKS:
        status, detail = probe(settings)
        print(f"{status} {title:<20} {detail}")
        failures += status == FAIL
    print("─" * 46)
    if failures:
        print(f"{failures} blocking issue(s) — run `assistant init` to fix interactively")
    else:
        print("all required config healthy 🎉")
    return 1 if failures else 0


# ── wizard ───────────────────────────────────────────────────────────

def run_wizard(settings: Settings, env_path: Path | None = None) -> int:
    """Interactive first-run setup. Seeds `.env` from the template if missing,
    then walks each `Step`: prompts per field (Enter keeps, `-` clears),
    upserts answers into `.env` immediately so later steps see them, and runs
    the step's probe when values changed or the user opts in. Finally seeds
    `profile.yaml` + `aliases.yaml` and ends by returning `run_check`'s code.
    `env_path` overrides the default repo `.env` (a test seam)."""
    env_path = env_path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        template = _REPO_ROOT / ".env.template"
        env_path.write_text(template.read_text() if template.exists() else "")
        print(f"created {env_path} from template")

    print("personal-agent setup — Enter keeps the shown value, '-' clears it.\n")
    for step in STEPS:
        print(f"\n━━ {step.title} " + "━" * max(0, 44 - len(step.title)))
        print(step.intro)
        current = Settings(_env_file=env_path)  # earlier writes visible
        changed = False
        for env_key, prompt, secret in step.fields:
            existing = getattr(current, env_key.lower(), "") or ""
            shown = _mask(str(existing)) if secret else (str(existing) or "(unset)")
            answer = _ask(f"  {prompt} [{shown}]: ")
            if answer == "-":
                upsert_env(env_path, env_key, "")
                changed = True
            elif answer:
                upsert_env(env_path, env_key, answer)
                changed = True
        if step.probe and (changed or _ask("  validate this section? [Y/n]: ").lower() != "n"):
            status, detail = step.probe(Settings(_env_file=env_path))
            print(f"  {status} {detail}")

    # post-env: seed the profile + aliases so the first run has something to build on
    from assistant.agent.profile_store import ALIASES_TEMPLATE, ProfileStore

    final = Settings(_env_file=env_path)
    store = ProfileStore(final.profile_dir)
    if not store.exists() and final.github_token:
        if _ask("\nseed profile.yaml from your GitHub account now? [Y/n]: ").lower() != "n":
            from assistant.cli import cmd_bootstrap

            cmd_bootstrap(final)
    aliases = final.profile_dir / "aliases.yaml"
    if not aliases.exists():
        aliases.parent.mkdir(parents=True, exist_ok=True)
        aliases.write_text(ALIASES_TEMPLATE)
        print(f"wrote {aliases} — group your repos into initiatives there (see README)")

    print("""
next steps (see README for detail):
  1. assistant send-test-email        — verify delivery end to end
  2. assistant run --dry-run          — full pipeline, digest written to disk only
  3. schedule the daily 07:00 run     — OpenClaw cron (WeChat users) or cron/systemd
  4. optional backfill: assistant enrich-profile --since YYYY-MM
  5. optional deep check any time:    assistant init --check""")
    return run_check(Settings(_env_file=env_path))


def run_init(settings: Settings, check_only: bool = False) -> int:
    """Entry point for `assistant init`: dispatch to the doctor when
    `check_only`, and otherwise to the wizard — but fall back to the doctor
    when stdin is not a TTY, since the wizard cannot prompt without one."""
    if check_only:
        return run_check(settings)
    if not sys.stdin.isatty():
        print("no interactive terminal — running the config check instead "
              "(edit .env by hand or rerun `assistant init` in a terminal)")
        return run_check(settings)
    return run_wizard(settings)
