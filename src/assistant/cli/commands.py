"""CLI command handlers: one `cmd_*(settings, ...) -> int` per subcommand,
each returning a process exit code. These carry the actual work (profile
bootstrap/backfill, todo/reading management, standalone phase runs, resume repo
setup, consolidation); `main` only parses argv and dispatches to them.
"""

from datetime import date

from ..config import Settings
from ..profile_store import ProfileStore, render_summary


def cmd_bootstrap(settings: Settings) -> int:
    """Seed profile.yaml from the GitHub account. Never overwrites an existing profile."""
    from ..collectors.github import GitHubCollector

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
    """Print a summary of the current profile (≤20 items/section) + its path."""
    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        print("no profile yet — run `assistant bootstrap`")
        return 1
    print(render_summary(store.load(), max_items=20))
    print(f"\nfull profile: {store.yaml_path}")
    return 0


def cmd_todo(settings: Settings, args) -> int:
    """`assistant todo list|add|done` — dispatch to the corresponding action.
    Returns non-zero on a usage error or a failed `done`."""
    from ..actions import run_action

    if args.action == "list":
        print(run_action("list_todos", {}, settings))
        return 0
    if args.action == "add":
        if not args.value:
            print("usage: assistant todo add \"<title>\" [--due YYYY-MM-DD]")
            return 1
        params = {"title": args.value, "source": "manual"}
        if args.due:
            params["due"] = args.due
        print(run_action("add_todo", params, settings))
        return 0
    if args.action == "done":
        if not args.value:
            print("usage: assistant todo done <id>")
            return 1
        result = run_action("done_todo", {"id": args.value}, settings)
        print(result)
        return 0 if "marked done" in result else 1
    return 1


def cmd_reading(settings: Settings, args) -> int:
    """`assistant reading list|done|unrelated` — dispatch to the corresponding
    action. Returns non-zero on a usage error or a failed mark."""
    from ..actions import run_action

    if args.action == "list":
        print(run_action("list_reading", {}, settings))
        return 0
    if args.action in ("done", "unrelated"):
        if not args.value:
            print(f"usage: assistant reading {args.action} <id>")
            return 1
        action = "done_reading" if args.action == "done" else "unrelated_reading"
        result = run_action(action, {"id": args.value}, settings)
        print(result)
        return 0 if "marked" in result else 1
    return 1


def cmd_enrich_profile(settings: Settings, args) -> int:
    """History backfill: sweep everything the owner authored, reviewed, and
    (optionally) commented on since --since, add per-repo commit summaries and
    repo background context, and fold it all into the profile chronologically.
    Finishes with the editorial consolidation pass unless --no-consolidate.

    Rate budget: ~5-6 search requests (paced by _SEARCH_PAGE_DELAY for the
    30/min search limit) + <=40 repo/readme + <=35 commit-page calls against
    the 5000/hr core limit — no backoff machinery needed."""
    import re as _re
    from collections import Counter
    from datetime import datetime, timezone

    from ..collectors.github import GitHubCollector, summarize_commits
    from ..events_store import EventsStore
    from ..llm import LLM
    from ..tasks.profile_update import update_profile

    if not _re.fullmatch(r"\d{4}-\d{2}", args.since):
        print(f"invalid --since {args.since!r} — expected YYYY-MM (e.g. 2025-07)")
        return 1
    year, month = args.since.split("-")
    since = datetime(int(year), int(month), 1, tzinfo=timezone.utc)

    gh = GitHubCollector(settings)
    llm = LLM(settings)
    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        print("no profile yet — run `assistant bootstrap` first")
        return 1

    observations = gh.fetch_authored_items(since=since, max_items=None)
    print(f"authored PRs/issues/RFCs since {args.since}: {len(observations)}")
    reviewed = gh.fetch_reviewed_items(since=since)
    print(f"reviewed (not authored): {len(reviewed)}")
    observations += reviewed
    if args.include_comments:
        commented = gh.fetch_commented_items(since=since)
        print(f"commented (not authored/reviewed): {len(commented)}")
        observations += commented

    # repos worth understanding: active owned repos ∪ repos seen in observations
    owned = [r for r in gh.fetch_recent_repos(limit=100) if not r.get("fork")]
    active_owned = [r["full_name"] for r in owned
                    if (r.get("pushed_at") or "") >= since.date().isoformat()]
    seen_repos = Counter(e for o in observations for e in o.get("entities", []) if e)
    repo_set = list(dict.fromkeys(  # active owned first, then by activity volume
        active_owned + [r for r, _ in seen_repos.most_common()]))[:20]

    context_lines = []
    for repo in repo_set:
        ctx = gh.fetch_repo_context(repo)
        if ctx is None:
            print(f"  warning: {repo} unreachable with this token (private?) — skipping context")
            continue
        context_lines.append(
            f"- {ctx['repo']}: {ctx['description'] or '(no description)'}"
            + (f" | topics: {', '.join(ctx['topics'])}" if ctx["topics"] else "")
            + (f" | README: {ctx['readme']}" if ctx["readme"] else ""))
    repo_context = "\n".join(context_lines)
    print(f"repo context built for {len(context_lines)}/{len(repo_set)} repos")

    for repo in active_owned:  # direct-push work has no PR trail — use commits
        commits = gh.fetch_repo_commits(repo, since)
        if commits is None:
            print(f"  warning: commits for {repo} not accessible with this token — skipped")
            continue
        observations += summarize_commits(repo, commits)

    events = EventsStore(settings.events_db)
    stored = events.add_observations(f"enrich-{date.today().isoformat()}",
                                     observations, dedupe=True)
    events.close()
    print(f"{len(observations)} observations ({len(stored)} new in events.db)")

    # ascending so the profile evolves in temporal order, oldest arc first
    observations.sort(key=lambda o: o.get("ts", ""))
    total_ops = 0
    batch_size = 60  # keep each LLM pass well under the observation cap
    batches = range(0, len(observations), batch_size)
    for i, start in enumerate(batches, 1):
        try:
            result = update_profile(llm, store, observations[start:start + batch_size],
                                    context=repo_context, backfill=True)
        except Exception as exc:  # one failed batch must not lose the run
            print(f"batch {i}/{len(batches)}: FAILED ({exc}) — continuing")
            continue
        total_ops += len(result["profile_ops"])
        print(f"batch {i}/{len(batches)}: {len(result['profile_ops'])} ops applied, "
              f"{len(result['rejected_ops'])} rejected"
              + (f" — {result['notes']}" if result.get("notes") else ""))

    print(f"backfill done: {total_ops} ops total")
    if not args.no_consolidate:
        from ..tasks.profile_consolidate import consolidate_profile

        result = consolidate_profile(llm, store, settings)
        print(f"consolidation: {len(result['applied'])} ops applied, "
              f"{len(result['rejected'])} rejected"
              + (f" — {result['notes']}" if result["notes"] else ""))
        if result["diff"]:
            print(result["diff"][:6000])
    print(f"review: `assistant show-profile` / `git -C {settings.profile_dir} log -p`; "
          f"rollback any step with `git -C {settings.profile_dir} revert <commit>`")
    return 0


def cmd_run_phase(settings: Settings, phase: str) -> int:
    """Run one standalone pipeline phase with live data (WeChat `run_phase`
    action lands here for the slow phases). Phases that need upstream state
    (collect/profile/digest/deliver) belong to the full `assistant run`."""
    import logging

    from ..events_store import EventsStore
    from ..llm import LLM
    from ..todo_store import ReadingList, TodoStore
    from ..urgency import urgency
    from ..website import sync_website

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = ProfileStore(settings.profile_dir)
    llm = LLM(settings)

    def _push_site() -> str:
        """Re-render and push the website from current state, returning the sync
        status — called after any phase that changes what the site shows."""
        todos = sorted(TodoStore(settings.profile_dir).open_items(),
                       key=urgency, reverse=True)
        result = sync_website(settings, store.load(), todos,
                              reading=ReadingList(settings.profile_dir).open_items())
        return result.get("status", "?")

    if phase == "research":
        from ..research.pipeline import run_research

        events = EventsStore(settings.events_db)
        try:
            research = run_research(llm, store.load(), events, settings)
            reading = ReadingList(settings.profile_dir)
            for paper in research.get("papers", []):
                reading.upsert(paper["seen_id"], title=paper["title"], url=paper["url"],
                               source="arxiv", why=paper.get("why", ""))
            events.mark_seen(research.get("seen_ids", []), context="run-phase research")
        finally:
            events.close()
        print(f"research: {len(research.get('papers', []))} papers, "
              f"{len(research.get('industry', []))} industry items; website {_push_site()}")
        return 0
    if phase == "website":
        print(f"website: {_push_site()}")
        return 0
    if phase == "todos":
        from ..collectors.github import GitHubCollector
        from ..marks import collect_marks
        from ..tasks.todos import update_todos

        events = EventsStore(settings.events_db)
        try:
            marks = collect_marks(settings, events)
        finally:
            events.close()
        if marks["applied"]:
            print(f"website marks: {marks['applied']} applied")
        github = GitHubCollector(settings) if settings.github_token else None
        todos = update_todos(TodoStore(settings.profile_dir), digest={}, resume={},
                             github=github, llm=llm)
        print(f"todos: {todos['open_count']} open, {len(todos['closed'])} auto-closed; "
              f"website {_push_site()}")
        return 0
    if phase == "resume":
        from ..tasks.resume import sync_resume

        result = sync_resume(llm, settings, store.load(), "")
        print(f"resume: {result.get('status')} {result.get('note', '')}".strip())
        return 0
    if phase == "curate":
        from ..tasks.curate import curate

        curated = curate(store)
        print(f"curate: {len(curated.get('decayed', []))} entries decayed")
        return 0
    if phase == "consolidate":
        from ..tasks.profile_consolidate import consolidate_profile

        result = consolidate_profile(llm, store, settings)
        print(f"consolidate: {len(result['applied'])} ops applied; website {_push_site()}")
        return 0
    print(f"unknown phase {phase!r}")
    return 1


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
    """Print any résumé update awaiting owner approval (summary, compile status,
    diff), or note that none is pending."""
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
    """Send a test email to verify delivery; reports the transport used."""
    from ..deliver.email import send_email

    transport = send_email(
        settings,
        "[assistant] test email",
        "<p>personal-agent email delivery works. ✅</p>",
    )
    print(f"test email sent to {settings.recipient} via {transport}")
    return 0


def cmd_consolidate(settings: Settings, args) -> int:
    """Weekly profile consolidation: merge fragments and promote evidence into
    contribution highlights, for one `--section` or all. `--dry-run` prints the
    ops that would apply and changes nothing."""
    import logging

    from ..llm import LLM
    from ..tasks.profile_consolidate import consolidate_profile

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = ProfileStore(settings.profile_dir)
    if not store.exists():
        print("no profile yet — run `assistant bootstrap` first")
        return 1
    result = consolidate_profile(LLM(settings), store, settings,
                                 section=args.section, dry_run=args.dry_run)
    print(f"{len(result['applied'])} ops applied, {len(result['rejected'])} rejected"
          + (" (dry-run)" if args.dry_run else "")
          + (f"\nnotes: {result['notes']}" if result["notes"] else ""))
    if args.dry_run and result["applied"]:
        for op in result["applied"]:
            print(f"  would apply: {op.get('op')} {op.get('name') or op.get('into', '')}")
    return 0
