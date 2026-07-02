import argparse
import sys
from datetime import date

from .config import Settings
from .profile_store import ProfileStore, render_summary


def cmd_bootstrap(settings: Settings) -> int:
    """Seed profile.yaml from the GitHub account. Never overwrites an existing profile."""
    from .collectors.github import GitHubCollector

    store = ProfileStore(settings.profile_dir)
    if store.exists():
        print(f"profile already exists at {store.yaml_path} — not overwriting")
        return 1

    gh = GitHubCollector(settings)
    user = gh.fetch_identity()
    repos = [r for r in gh.fetch_recent_repos() if not r.get("fork")]

    languages: dict[str, list[str]] = {}
    for repo in repos:
        if repo.get("language"):
            languages.setdefault(repo["language"], []).append(repo["name"])

    today = date.today().isoformat()
    profile = {
        "identity": {
            "name": user.get("name") or settings.github_user,
            "github": settings.github_user,
            "emails": [e for e in {settings.smtp_user, user.get("email")} if e],
            "affiliations": [user.get("company")] if user.get("company") else [],
            "links": [user.get("html_url")] + ([user["blog"]] if user.get("blog") else []),
        },
        "skills": [
            {
                "name": lang,
                "level": "working",
                "evidence": [f"GitHub repos: {', '.join(names[:4])}"],
                "first_seen": today,
                "last_seen": today,
                "status": "active",
            }
            for lang, names in sorted(languages.items(), key=lambda kv: -len(kv[1]))[:6]
        ],
        "interests": [],
        "projects": [
            {
                "name": repo["name"],
                "role": "owner",
                "period": {"start": repo.get("created_at", "")[:7], "end": None},
                "highlights": [repo["description"]] if repo.get("description") else [],
                "evidence": [repo["html_url"]],
                "last_seen": today,
                "status": "active",
            }
            for repo in repos[:8]
        ],
        "publications": [],
        "education": [],   # manual — the agent never touches these
        "experience": [],  # manual
        "preferences": {"digest_language": "zh+en"},
    }
    store.save(profile, "bootstrap from GitHub")
    print(f"profile bootstrapped at {store.yaml_path}")
    print("NOTE: fill in education/experience manually — the agent never edits them.")
    return 0


def cmd_show(settings: Settings) -> int:
    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        print("no profile yet — run `assistant bootstrap`")
        return 1
    print(render_summary(store.load(), max_items=20))
    print(f"\nfull profile: {store.yaml_path}")
    return 0


def cmd_enrich_profile(settings: Settings) -> int:
    """One-shot backfill: read the owner's full authored PR/issue/RFC history
    and fold it into the profile in batches."""
    from .collectors.github import GitHubCollector
    from .llm import LLM
    from .tasks.profile_update import update_profile

    gh = GitHubCollector(settings)
    llm = LLM(settings)
    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        print("no profile yet — run `assistant bootstrap` first")
        return 1

    items = gh.fetch_authored_items(since=None, max_items=200)
    print(f"fetched {len(items)} authored PRs/issues/RFCs")
    total_ops = 0
    batch_size = 60  # keep each LLM pass well under the observation cap
    for start in range(0, len(items), batch_size):
        result = update_profile(llm, store, items[start:start + batch_size])
        total_ops += len(result["profile_ops"])
        print(f"batch {start // batch_size + 1}: {len(result['profile_ops'])} ops applied"
              + (f" — {result['notes']}" if result.get("notes") else ""))
    print(f"done: {total_ops} ops total. Review with `assistant show-profile` or "
          f"`git -C {settings.profile_dir} log -p`")
    return 0


def cmd_resume_init(settings: Settings) -> int:
    """Clone the Overleaf project (git bridge / any remote) or init an empty repo."""
    import subprocess

    repo = settings.resume_dir
    if (repo / ".git").exists():
        print(f"resume repo already exists at {repo}")
        return 1
    repo.parent.mkdir(parents=True, exist_ok=True)
    if settings.resume_remote_url:
        subprocess.run(["git", "clone", settings.resume_remote_url, str(repo)], check=True)
        print(f"cloned resume from {settings.resume_remote_url} → {repo}")
    else:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        print(f"initialized empty resume repo at {repo} — add your .tex there.")
        print("Set RESUME_REMOTE_URL in .env (Overleaf git bridge URL) to enable pushing.")
    return 0


def cmd_resume_status(settings: Settings) -> int:
    import json

    pending_file = settings.data_dir / "resume_pending.json"
    if not pending_file.exists():
        print("no resume update pending approval")
        return 0
    pending = json.loads(pending_file.read_text())
    print(f"pending since {pending['date']}: {pending['summary']}")
    print(f"compile: {pending['compile']}\n\n{pending['diff'][:3000]}")
    print("\napprove with: assistant approve-resume")
    return 0


def cmd_test_email(settings: Settings) -> int:
    from .deliver.email import send_email

    transport = send_email(
        settings,
        "[assistant] test email",
        "<p>personal-agent email delivery works. ✅</p>",
    )
    print(f"test email sent to {settings.recipient} via {transport}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="assistant", description="Personal self-assistant agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute a daily run")
    run_p.add_argument("--dry-run", action="store_true", help="write digest to disk, send no email")
    run_p.add_argument("--resume", action="store_true", help="resume the last incomplete run")

    sub.add_parser("bootstrap", help="seed the profile from GitHub (first run only)")
    sub.add_parser("show-profile", help="print a summary of the current profile")
    sub.add_parser("send-test-email", help="verify email delivery")
    sub.add_parser("enrich-profile", help="backfill the profile from all authored PRs/issues/RFCs")
    sub.add_parser("resume-init", help="clone/init the resume repo (Overleaf git bridge)")
    sub.add_parser("resume-status", help="show any resume update pending approval")
    sub.add_parser("approve-resume", help="push the pending resume update to the remote")

    args = parser.parse_args()
    settings = Settings()

    if args.command == "run":
        from .orchestrator import run

        sys.exit(run(settings, dry_run=args.dry_run, resume=args.resume))
    elif args.command == "bootstrap":
        sys.exit(cmd_bootstrap(settings))
    elif args.command == "show-profile":
        sys.exit(cmd_show(settings))
    elif args.command == "send-test-email":
        sys.exit(cmd_test_email(settings))
    elif args.command == "enrich-profile":
        sys.exit(cmd_enrich_profile(settings))
    elif args.command == "resume-init":
        sys.exit(cmd_resume_init(settings))
    elif args.command == "resume-status":
        sys.exit(cmd_resume_status(settings))
    elif args.command == "approve-resume":
        from .tasks.resume import approve_resume

        sys.exit(approve_resume(settings))


if __name__ == "__main__":
    main()
