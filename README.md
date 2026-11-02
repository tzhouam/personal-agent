# personal-agent

A personal agent that chats through **Slack, QQ, WeChat or email**, works on your
registered projects, and remembers the decisions behind them. Its agent engine is
the actual **Pi SDK 1.4.0**: `@earendil-works/pi-coding-agent`, `pi-agent-core` and
`pi-ai`. Choose an API model or an optional authenticated CLI model adapter.
Existing personal workflows are discoverable skills.

Pi owns the model/tool loop and durable JSONL conversations. Python supplies
trusted personal tools, project jobs, sourced memory and explicit approvals. The
nine-phase daily workflow runs deterministically without LangGraph. Personal work
remains available: profile maintenance, GitHub and research digests, reading and todos,
finance and health records, website publishing, résumé proposals, reminders,
routines and reusable workflows.

[Runtime guide](doc/RUNTIME_V2.md) · [Configuration example](runtime.example.toml) ·
[Existing personal-workflow guide](doc/USER_GUIDE.md) ·
[Pi adoption plan](PI_PLAN.md) · [Current checklist](PI_TODO.md)

## Install and choose a model

Requirements: **Python 3.11+**, **Node.js 22.19+**, npm and a configured model route.
The example selects DeepSeek's `deepseek-flash` API model; use the model ID your
provider exposes. GitHub, email and chat-platform accounts are optional.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
mkdir -p ~/.personal-agent
cp runtime.example.toml ~/.personal-agent/runtime.toml
assistant pi install
assistant pi status
# Export DEEPSEEK_API_KEY securely in this shell or your service environment.
assistant doctor --backend deepseek
assistant doctor --backend deepseek --probe
assistant ask "Remember that my Orchid project uses SQLite" --session personal
assistant ask "What database does Orchid use?" --session personal
```

`assistant pi install` explicitly installs the pinned SDK dependencies using
`npm ci --ignore-scripts` under the data root. Ordinary chat never installs
packages. `doctor` checks Pi and model configuration; `--probe` makes a real model
request through Pi. A failed selected route reports a failure. Slack and QQ stay
disabled in the sample until configured.

`assistant init` is still the setup wizard for the retained GitHub/email/website
integrations. It is optional for the basic new runtime.

## Work on a personal project

Register a real directory through the operator CLI, then give the agent a goal.
The model cannot register an arbitrary path from a chat message.

```sh
assistant project register orchid /absolute/path/to/orchid \
  --verify '["python", "-m", "pytest", "-q"]'
assistant project run orchid "Implement CSV export and add tests"
assistant project list
assistant project status TASK_ID
assistant project events TASK_ID
```

Pi runs with confined host tools for the registered workspace. The host then executes your
configured verification commands independently and records their exit codes,
output, changed artifacts and hashes. A model saying “tests passed” is not
verification. A task can finish with `verified: false` when no verification command
was registered.

For work that should continue while you chat:

```sh
assistant serve
# In another terminal, or through an approved chat project request:
assistant task "Implement CSV export and add tests" --project orchid --background
assistant project cancel TASK_ID
```

`serve` drains queued work and sends project completion back to the originating
Slack/QQ conversation when there is one. CLI-started tasks remain inspectable in
the CLI. Interrupted work is marked for inspection after restart rather than
silently repeated.

**Execution boundaries:** every project uses Pi's model/tool loop and the same
path-checked host file tools. Model-requested host commands require explicit
`RUNTIME_ALLOW_UNRESTRICTED=true`; a working directory is not an OS sandbox.
Your registered `--verify` commands execute project code on the **trusted host**.
Optional Codex/Claude/Gemini adapters supply individual model completions to Pi;
they do not take over project execution. See the [backend details](doc/RUNTIME_V2.md#model-backends).

## Connect Slack or QQ

Enable the relevant table in [runtime.example.toml](runtime.example.toml), configure
its credentials through environment references, and run `assistant serve`.

| Channel | Connection | Admission and replies |
| --- | --- | --- |
| Slack | Socket Mode; app-level and bot tokens | Explicit user allowlist, optional workspace/channel restrictions; direct messages or bot mentions; replies retain the originating thread |
| QQ | OneBot v11 forward WebSocket to an existing NapCat/LLOneBot-style server | Explicit numeric user allowlist, optional group restrictions; private messages or bot mentions; correlated send receipts |
| WeChat / WeCom / email | Retained OpenClaw bridge and existing configured listeners | Existing authentication and image handling feed the new runtime through the retained service layer |

QQ support here means **OneBot v11**, not Tencent's separate official QQ Bot API.
The assistant does not install or log in to NapCat for you. Native Slack/QQ
attachments currently provide metadata only; they do not imply image analysis.
The retained bridge and `assistant ask --image` pass image turns through Pi with
the existing append-only logging policy; configure an image-capable model.

Detailed scopes, event subscriptions, configuration and delivery recovery are in
[the channel setup guide](doc/RUNTIME_V2.md#channels).

## Memory and skills

```sh
assistant memory add "Orchid exports UTF-8 CSV" --project orchid --source operator:decision
assistant memory search "CSV" --project orchid
assistant skills list
assistant skills show finance_summary
assistant skills run list_todos
assistant skills run add_todo --args '{"title":"Review the Orchid export"}'
```

Memory retains source references and project scope. Existing profile, observation,
conversation, todo, reading, lesson and saved-workflow records remain readable in
place. Finance and health stay in their structured stores and are queried through
their dedicated skills rather than injected as ambient project context.

Skills expose typed, code-owned handlers and load their `SKILL.md` instructions
on demand. Existing operational runbooks are bundled too. A Markdown document
cannot install a handler or grant itself permission to run commands.

After a project has independently verified results, keep the reusable procedure:

```sh
assistant skills learn orchid-release --from-task TASK_ID \
  --instructions-file /absolute/path/to/procedure.md
assistant skills approve-draft DRAFT_ID
assistant skills show orchid-release
```

In chat, the equivalent `skill_draft` tool saves a candidate and `skill_activate`
requires an owner approval. Draft evidence is checked again before activation.
Approved procedures supply instructions to the normal tool loop; every action
still uses the host's existing tools and policy.

## Existing personal work remains available

| Work | Skills / retained commands |
| --- | --- |
| Daily collection and triaged digest | `daily`, `run_status`; `assistant run [--resume]` |
| Evidence-backed profile and history | `profile`, `bootstrap_profile`, `enrich_profile`, `consolidate`, `curate`; `bootstrap`, `enrich-profile`, `show-profile` |
| GitHub, Gmail and browser activity | Existing collectors and privacy filters, used by the daily/profile skills |
| Papers, news and reading | `research`, `web_search`, `web_research`, reading-list skills; `assistant reading …` |
| Todos | `add_todo`, `list_todos`, `done_todo`; `assistant todo …` |
| Finance | Transaction logging, queries, computed summaries, recategorization and voiding |
| Health | Meal, exercise and weight logging, body facts, nutrient needs, queries and computed summaries |
| Personal website | `website`, `build_personal_website`; deterministic public render and encrypted private pages |
| Résumé | `resume`; retained `resume-init`, `resume-status`, `approve-resume` human publishing gate |
| Reminders and routines | Scheduling/listing/cancellation skills and retained delivery scheduler |
| Preferences and learning | Preference and evolution skills, versioned personal lessons |
| Reusable workflows | Saved workflow inspection/editing skills; the new host executes procedures using its normal tools |
| Metrics and administration | Existing metrics, delivery-failure records and operator-only `assistant admin …` commands |

The [personal-workflow user guide](doc/USER_GUIDE.md) documents each integration;
[the original design](doc/DESIGN.md) explains the retained stores and pipeline.
Commands whose names now select the new runtime can still be invoked explicitly:

```sh
assistant legacy ask "Use the previous chat implementation"
assistant legacy task "Use the previous bounded task runner"
assistant legacy --help
```

Host skills that publish or send/schedule outward messages, full daily pipelines,
and chat-driven project submissions require scoped approval. The gate also covers
indirect website publication by `research`, `todos` and `consolidate`.
An approval is single-use and belongs to its original owner/conversation:

```sh
assistant approve APPROVAL_ID --session ORIGINAL_SESSION
# In the originating chat, reply: /approve APPROVAL_ID
```

The direct legacy/operator commands retain their existing explicit operator
semantics. In particular, `assistant run --dry-run` skips digest email but can
still update the website; use an isolated configuration when testing the pipeline.

## Data and migration

The default data directory remains `~/.personal-agent/`, overridable with `DATA_DIR`
or the new runtime commands' `--data-dir`. Existing profile Git history, evidence,
finance/health shards, sessions and task records are retained. Starting the new
runtime creates separate stores; it does not bulk-convert existing personal data.
Explicit use of an old-format finance/health skill retains the existing tested
migration behavior and preserves the original legacy file.

Pi conversation transcripts and exact-session manifests live under `pi/sessions/`.
`runtime.sqlite3` keeps conversation audit, approvals and project state; sourced
facts live in `runtime_memory.sqlite`, and channel receipts in `runtime_inbox.sqlite3`.
Project proof and reviewed skill procedures live under `runtime/`. See the
[full data layout and upgrade notes](doc/RUNTIME_V2.md#data-and-upgrading).

Records stay on your machine, while selected model providers and configured chat,
email and publishing services receive the data needed for their operations.
A local CLI can itself call a remote model service.

## Development and verification

```sh
pip install -e . pytest
python -m pytest -q
cd src/assistant/platform/runtime/pi_host
npm ci --ignore-scripts --no-audit --no-fund
npm run check
node --test test/session.test.mjs
```

Tests use scratch data and fixture boundaries for model/channel services. Live
provider checks and deployment status are recorded separately in
[PI_TODO.md](PI_TODO.md); fixture success does not establish that a real
Slack/QQ account is connected. No production deployment is implied by this rewrite.

The dependency boundary remains `agent/ → platform/`; composition roots may import
both. See [CLAUDE.md](CLAUDE.md) for repository conventions and
[the runtime guide](doc/RUNTIME_V2.md) for the new module map.

The runtime builds directly on [Pi](https://github.com/earendil-works/pi); its
development fork is [tzhouam/pi](https://github.com/tzhouam/pi). The pinned SDK
currently comes from the published upstream packages. [bothouse](https://github.com/zuiho-kai/bothouse)
informs the channel and workspace design. Personal stores and workflow handlers
remain maintained in this repository.

License: [MIT](LICENSE).
