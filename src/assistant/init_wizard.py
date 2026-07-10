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

from .config import Settings

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
    if not value:
        return "(unset)"
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def _ask(prompt: str) -> str:  # seam for tests
    return input(prompt).strip()


# ── probes (shared by wizard and doctor) ─────────────────────────────

def probe_llm(s: Settings):
    if not s.anthropic_api_key:
        return FAIL, "ANTHROPIC_API_KEY unset — the agent cannot think without it"
    try:
        from .llm import LLM

        reply = LLM(s).complete("Reply with the single word: ok", max_tokens=1500)
        return (OK, f"model {s.anthropic_model} answers") if reply.strip() \
            else (WARN, "endpoint reachable but empty reply")
    except Exception as exc:
        return FAIL, f"LLM call failed: {str(exc)[:120]}"


def probe_github(s: Settings):
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
    if s.resend_api_key:
        return OK, f"Resend configured → {s.recipient or '(set DIGEST_TO!)'}"
    if s.smtp_user and s.smtp_password:
        return OK, f"SMTP via {s.smtp_host} → {s.recipient}"
    return FAIL, "no delivery path — set RESEND_API_KEY or SMTP_USER/SMTP_PASSWORD"


def probe_website(s: Settings):
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
        note = "" if s.website_password else " (WEBSITE_PASSWORD unset — todos/reading pages will be public)"
        return (WARN if note else OK), f"push access ok{note}"
    except Exception as exc:
        return FAIL, str(exc)[:120]


def probe_marks(s: Settings):
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
    for name, key in (("Gemini grounding", s.gemini_api_key), ("Google CSE", s.google_api_key),
                      ("Tavily", s.tavily_api_key), ("Brave", s.brave_api_key)):
        if key:
            return OK, f"{name} configured"
    return WARN, "no search key — /search falls back to keyless DuckDuckGo Lite (rate-limited)"


def probe_collectors(s: Settings):
    bits = []
    bits.append("chrome ✓" if s.chrome_history_path.exists() else "chrome ✗ (no History file)")
    bits.append("gmail ✓" if (s.gmail_enabled and s.smtp_user and s.smtp_password)
                else "gmail ✗ (needs SMTP creds)")
    bits.append("github ✓" if s.github_token else "github ✗")
    return (OK if "✗" not in " ".join(bits[2:]) else WARN), " · ".join(bits)


def probe_profile(s: Settings):
    from .profile_store import ProfileStore

    store = ProfileStore(s.profile_dir)
    if not store.exists():
        return WARN, "no profile yet — the wizard's last step (or `assistant bootstrap`) seeds it"
    aliases = "aliases.yaml ✓" if (s.profile_dir / "aliases.yaml").exists() \
        else "aliases.yaml missing (initiative merging disabled)"
    return OK, f"profile.yaml ✓ · {aliases}"


def probe_schedule(s: Settings):
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
EXTRA_CHECKS = [("Collectors", probe_collectors), ("Profile", probe_profile),
                ("Schedule", probe_schedule)]


# ── doctor ───────────────────────────────────────────────────────────

def run_check(settings: Settings) -> int:
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
    from .profile_store import ALIASES_TEMPLATE, ProfileStore

    final = Settings(_env_file=env_path)
    store = ProfileStore(final.profile_dir)
    if not store.exists() and final.github_token:
        if _ask("\nseed profile.yaml from your GitHub account now? [Y/n]: ").lower() != "n":
            from .cli import cmd_bootstrap

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
    if check_only:
        return run_check(settings)
    if not sys.stdin.isatty():
        print("no interactive terminal — running the config check instead "
              "(edit .env by hand or rerun `assistant init` in a terminal)")
        return run_check(settings)
    return run_wizard(settings)
