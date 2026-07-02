# personal-agent

Daily self-assistant agent. See [DESIGN.md](DESIGN.md) for the full design;
this is **Milestone 1**: profile store + GitHub collector + notification digest + email + timer.

## Install

```bash
source /rebase/.venv/bin/activate
pip install -e /rebase/personal-agent
cp .env.template .env   # fill in tokens
```

## Usage

```bash
assistant bootstrap        # first time only: seed profile.yaml from GitHub
assistant show-profile
assistant run --dry-run    # full pipeline, digest written to disk, no email
assistant run              # daily: collect → profile → resume → digest → research → email → curate
assistant run --resume     # re-enter the phase where a crashed run stopped
assistant send-test-email
assistant resume-init      # clone the Overleaf project (set RESUME_REMOTE_URL first)
assistant resume-status    # show a resume update awaiting approval
assistant approve-resume   # push the approved update to Overleaf
```

### Resume sync (Overleaf)

Requires the Overleaf **git bridge** (premium): set `RESUME_REMOTE_URL` in `.env` to the
project's git URL and run `assistant resume-init`. The agent edits the LaTeX locally with
exact search/replace edits grounded in the profile, gates on a LaTeX compile when a
toolchain is installed, and commits **locally only** — the daily email shows the diff and
nothing reaches Overleaf until you run `assistant approve-resume` (which pulls/rebases
remote edits first and never force-pushes).

Data lives in `~/.personal-agent/`:
- `profile/` — git repo holding `profile.yaml` (source of truth) + `PROFILE.md` (render).
  Every daily update is a commit; `git -C ~/.personal-agent/profile log -p` is the audit trail.
  **Fill in `education:` / `experience:` manually — the agent never edits those sections.**
- `events.db` — SQLite+FTS5 raw observation log + surfaced-item dedup store.
- `runs/<run_id>/` — per-run artifacts (observations, digest JSON/HTML) used by `--resume`.
- `state.json` — phase marker (named phase = phase to re-enter).

## Schedule (daily 07:00 HKT)

On a systemd host:

```bash
sudo cp systemd/personal-agent.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now personal-agent.timer
```

In this container (PID 1 is tini — no systemd/cron), use the fallback loop scheduler:

```bash
nohup /rebase/personal-agent/scheduler.sh >/dev/null 2>&1 &
# logs: ~/.personal-agent/scheduler.log · stop: kill $(cat ~/.personal-agent/scheduler.pid)
```

## Architecture (M1 slice)

```
collect (GitHub events + notifications, pluggable registry)
  → profile update (LLM emits typed patch ops; code applies; git commit)
  → digest (deterministic reason-buckets + LLM triage vs profile; seen-dedup)
  → deliver (HTML email via Resend, SMTP fallback)
```

Later milestones (see DESIGN.md §9): Chrome + Gmail collectors (M2), arXiv/blog/中文媒体
research digest (M3), Overleaf resume sync with approval gate (M4), curator + skills (M5).
