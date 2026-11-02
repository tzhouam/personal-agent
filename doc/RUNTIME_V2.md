# Runtime v2: channels, personal projects, memory and skills

The default `assistant ask`, `assistant task` and `assistant serve` commands use
one bounded personal-agent kernel. Models and chat platforms are adapters; the
kernel owns trusted tools, sourced context, approvals and durable results.
Existing domain implementations remain the handlers behind personal skills.

This guide covers the new runtime. The [existing user guide](USER_GUIDE.md) and
[original design](DESIGN.md) remain references for the daily pipeline, personal
records, website, résumé and deployed WeChat/email integrations. Where an older
example uses `ask`, `task` or `serve`, use `assistant legacy …` to select that
previous implementation explicitly.

## Configuration and commands

Copy [runtime.example.toml](../runtime.example.toml) to
`~/.personal-agent/runtime.toml`, or pass `--config /absolute/path/runtime.toml`.
Configuration does not install a CLI, authenticate an account, start a OneBot
server or join a Slack workspace.

```sh
assistant doctor --config /absolute/path/runtime.toml
assistant backends --config /absolute/path/runtime.toml
assistant doctor --config /absolute/path/runtime.toml --probe
assistant ask "List my skills" --session personal
assistant chat "Continue the same conversation" --session personal
assistant serve
```

`doctor` and `backends` report routes without exposing credential values.
Availability means a CLI executable was found, or an API route has a configured
key and model. It is not an account/model-access check until `--probe` makes a
real request. Probe errors remain errors; an unavailable route is not replaced
with a different account or model.

Runtime commands accept:

| Option | Meaning |
| --- | --- |
| `--data-dir PATH` | Pin this command's personal-data root; otherwise use `DATA_DIR` or `~/.personal-agent` |
| `--config PATH` | Read this runtime TOML file |
| `--backend NAME` | Select an exact named route |
| `--owner UID` | Resolve an active roster owner in multi-tenant mode; single-owner default is `default` |
| `--session NAME` | Select a durable conversation and its approval scope; CLI default is `cli` |

Keep these options consistent across `skills run`, `approve`, project and memory
commands that should share state. The flags belong to the runtime command, for
example `assistant skills list --data-dir /tmp/my-agent`.

Backend selection is `--backend`, then `RUNTIME_BACKEND`, then
`[runtime].backend`. An `auto` selection chooses a configured Anthropic API, then
OpenAI API, then an installed Codex/Claude/Gemini/Pi executable at startup. There
is no request-time fallback after that selection. For reproducible behavior, name
the route explicitly.

`[runtime]` controls `max_steps` (1–64), `workers` (1–16), and per-request `timeout`
(1–3600 seconds). Project limits also accept `RUNTIME_PROJECT_TIMEOUT`,
`RUNTIME_BACKEND_TIMEOUT` and `RUNTIME_PROJECT_MAX_STEPS` settings. Personal
integration settings continue to use the existing `.env` configuration. Store
secrets outside Git; runtime TOML rejects inline backend `api_key` values.

## Model backends

| Kind | Protocol | Execution boundary |
| --- | --- | --- |
| `anthropic` | Anthropic-compatible Messages API | Typed host tools; provider/model/base URL are configurable |
| `openai` | OpenAI-compatible Chat Completions API | Typed host tools; also supports compatible third-party endpoints |
| `codex` | Authenticated `codex exec` JSON events | Controller tools are disabled natively; project execution uses an explicit workspace-write sandbox |
| `claude` | Claude Code print/stream-JSON mode | Controller native tools and MCP are disabled; native project writes require explicit unrestricted opt-in |
| `gemini` | Gemini CLI stream-JSON mode | Every call requires unrestricted/external-isolation opt-in because configured native tools cannot all be reliably disabled |
| `pi` | Pi JSON event mode | Controller tools, MCP, extensions, context files and skills are disabled; native project writes require explicit unrestricted opt-in |

API routes need an actual supported model and credential. CLI routes use their
own existing authentication; installing this Python project does not install or
log in to those CLIs. The runtime parses authoritative terminal events and exit
status, applies deadlines, and propagates cancellation. Partial output or a
provider error is not reported as completed work.

### API route examples

```toml
[runtime]
backend = "my_api"

[backends.my_api]
kind = "openai"
base_url = "https://your-provider.example/v1"
model = "your-model-id"
api_key_env = "MY_API_KEY"
```

Export `MY_API_KEY` in the service's environment. Standard credential names can
also be supplied through the project's supported `.env` settings; arbitrary
custom `*_env` names must be exported unless explicitly supported by the loader.
The configuration example contains the standard Anthropic and OpenAI routes.

For a compatible DeepSeek route:

```toml
[backends.deepseek]
kind = "openai"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"
```

Select it with `--backend deepseek`. Provider model availability and account
access are checked by a real `doctor --probe`, not inferred from this example.

### CLI route examples and permissions

```toml
[backends.codex]
kind = "codex"
command = ["codex"]
# model = "a-model-available-to-your-account"

[backends.pi]
kind = "pi"
command = ["pi"]
allow_unrestricted = false
```

A `command` is an argv array, never a shell string. Codex is launched with
`--ignore-user-config`; its existing login is retained, while inherited apps,
plugins, MCP configuration and model/provider settings are not loaded. If your
Codex account depends on custom routing, specify that routing explicitly through
the route's `model` and non-secret global `-c` arguments in `command`, or select a
direct API route. Controller turns cannot use Codex's native shell, browser or
other native execution tools to evade the host's approvals.

For Claude and Pi, `allow_unrestricted = true` is required before native project
writes. Gemini requires it for controller turns as well. These CLIs do not
provide this runtime with the same enforceable filesystem sandbox as Codex;
operate them within an externally isolated environment appropriate for the work.
Setting a process's working directory alone does not constrain filesystem or
network access. The runtime never silently opts into unrestricted mode.

There is one selected route per runtime application. Named routes make switching
explicit; they do not imply a concurrently running team of different CLI agents.

## Channels

`assistant serve` starts enabled native adapters, the durable inbox/project
workers, and the retained loopback bridge, email and WeCom lifecycle. Slack and
QQ require no public inbound HTTP callback: both use outbound WebSockets.

A transport validates its configured sender/account/workspace restrictions before
submitting a trusted event. Owner identity comes from configuration, not from a
message field. Sessions distinguish owner, platform, account, workspace, room,
thread and sender. A shared Slack thread and a QQ group do not share a session.

### Slack Socket Mode

1. Create a Slack app, enable Socket Mode, and create an app-level token with
   `connections:write`.
2. Add bot OAuth scopes `chat:write`, `app_mentions:read` and `im:history`.
   Subscribe to the bot events `app_mention` and `message.im`.
3. Install the app into your workspace and invite its bot to the intended
   channels. Configure the exact user IDs allowed to command it.
4. Enable the Slack table, provide tokens through environment references, and
   start `assistant serve`.

```toml
[channels.slack]
enabled = true
account_id = "work"
owner_id = "default"
app_token_env = "SLACK_APP_TOKEN"
bot_token_env = "SLACK_BOT_TOKEN"
team_id = "T_WORKSPACE_ID"
allowed_users = ["U_YOUR_USER_ID"]
allowed_channels = ["C_CHANNEL_ID", "D_DIRECT_MESSAGE_ID"]
```

You can omit `allowed_channels` or use an empty list to permit the configured
users in any admitted channel/DM. `team_id` pins the workspace; `bot_user_id` is
optional because startup can discover the bot identity. If you subscribe to
additional message events, give the app the matching Slack history scopes.
Unaddressed channel messages, bot messages and message edits are ignored; direct
messages and bot mentions are accepted. Replies preserve the original thread.

The provider ACK is sent after durable application admission, before model
execution. Transport redelivery cannot cause a second model turn for the same
source message.

### QQ through OneBot v11

Use an already configured NapCat, LLOneBot or another compatible OneBot v11
implementation. Enable its forward WebSocket server and keep the QQ account
logged in there. This runtime is the WebSocket client; it does not implement the
separate official QQ Bot API v2 protocol.

```toml
[channels.qq]
enabled = true
account_id = "personal"
owner_id = "default"
ws_url = "ws://127.0.0.1:3001"
access_token_env = "QQ_ACCESS_TOKEN"
allowed_users = ["123456789"]
allowed_groups = ["987654321"]
# self_id = "YOUR_BOT_QQ_ID"  # optional; otherwise discovered on connection
```

IDs are strings containing the actual numeric QQ IDs. `allowed_users` is required
and cannot be `*`. An empty `allowed_groups` list permits those allowed users in
any group, while private messages still use the user allowlist. Group commands
must mention the bot. OneBot array messages and CQ strings are normalized without
turning escaped text into active mentions.

Remote OneBot URLs require an access token; tokenless connections are accepted
only for loopback hosts. Keep credentials in `access_token`, resolved from its
environment reference, rather than embedding them in the WebSocket URL. Send
results are correlated with OneBot `echo` receipts; writing a socket frame is not
proof that a message was accepted.

### Attachments and retained channels

Native Slack/QQ events currently retain safe attachment metadata, not downloaded
image contents or authenticated download URLs. They therefore do not support
image analysis merely because the platform supports image attachments.

The existing authenticated loopback bridge, WeChat/WeCom and email integrations
remain connected through the new runtime callback. Image-bearing turns through
those retained paths, or `assistant ask "…" --image /path/photo.png`, use the
existing image arbiter and require an explicit compatible vision configuration.
Their append-only image-action restrictions remain in force. See
[the WeChat setup guide](WECHAT_OPENCLAW.md) and
[existing image design](DESIGN.md#images).

Retained reminders and recurring routines continue to use their configured
existing announcement/delivery path, including `ANNOUNCE_ACCOUNT` and
`ANNOUNCE_TO` for WeChat. They are not automatically redirected to the originating
Slack/QQ conversation. Native Slack/QQ support here covers conversational replies
and project completion notices.

### Delivery inspection and retry

```sh
assistant inbox list
assistant inbox retry-delivery RECEIPT_ID
```

The inbox saves text before sending it. A rejected send is `failed`; a send whose
acceptance is uncertain is `unknown`. Explicit retry sends the saved reply and
does not rerun the model or project. Inspect the original channel before retrying
an uncertain delivery, because the provider may already have accepted it.

Currently a runtime TOML config accepts one Slack account and one QQ account.
In multi-tenant mode each enabled adapter has one configured `owner_id`, which
must be an active registered user; personal data resolves under that user's
existing data directory. Payload-supplied owner IDs never select credentials.

## Personal projects

Registration is an operator action. It requires an existing directory outside
the assistant's private data directory, and honors `RUNTIME_PROJECT_ROOTS` when
that optional restriction is configured.

```sh
assistant project register orchid /absolute/path/to/orchid \
  --verify '["python", "-m", "pytest", "-q"]' \
  --verify '["python", "scripts/check_export.py"]'
assistant project run orchid "Implement CSV export and cover Unicode names"
assistant task "Add a release checklist" --project orchid --background
assistant project list
assistant project status TASK_ID
assistant project events TASK_ID
assistant project cancel TASK_ID
```

Repeat `--verify` for multiple argv arrays. These commands run project code on the
**trusted host**, independently of the model sandbox. Registration is your explicit
authorization for those verification commands; do not configure verification from
untrusted chat text. The model cannot choose a verification command and thereby
make its own success authoritative.

`project run` and `task --project` run synchronously unless `--background` is set.
Queued background work requires `assistant serve` with the same owner/data root.
In chat, `project_submit` first creates a scoped approval; approval queues it for
the worker. Register directories through the CLI, not through model-generated
paths. For one owner, tasks sharing a workspace are serialized.

API backends receive confined read/list/write tools. An arbitrary host subprocess
requested by an API model requires the operator's explicit unrestricted-mode
opt-in. Native backends use their own project execution path and the permissions
described above. Every run records its frozen workspace, full goal, state,
attempts, model session, report, verification results, changed artifacts and
content hashes.

Successful owner-configured checks produce `verified: true` and a separate
private proof file. Finishing without registered verification produces
`verified: false`; a model report alone cannot be used as proof for a learned
skill. Failed checks produce a failed task with the actual output retained.

The durable states include `queued`, `running`, `done`, `failed`, `cancelled` and
`interrupted`. Cancellation requests are persisted and propagated to active
provider/verification work. A service restart conservatively marks running work
interrupted; inspect its artifacts before explicitly submitting it again.

Completion reports for channel-originated tasks are added to the originating
conversation and saved delivery ledger. Reconciliation after restart deduplicates
that completion notice without repeating the coding task. CLI-only tasks remain
available through status/events; they do not invent a chat destination.

## Skills, learning and approvals

```sh
assistant skills list
assistant skills show query_transactions
assistant skills run query_transactions --args '{"date":"2026-10-01"}'
assistant skills run add_todo --args '{"title":"Review release evidence"}'
```

The catalog contains built-in executable handlers and documentation-only
operational runbooks. Discovery metadata is compact; `show`/`skill_describe` loads
the instructions and exact schema only when needed. Handlers, argument types and
effect classification live in code. A `SKILL.md` cannot add an import, run a shell
command or grant additional tool permissions.

The retained skill families are daily review, profile/evidence, research, website,
résumé, todos, reading, finance, health, reminders, routines, preferences, saved
workflows and memory. Operator-only administration and approval cannot be invoked
as ordinary personal skills. Saved workflows remain inspectable/editable plans;
the new host can follow them through its normal trusted tools rather than
recursively starting the old task runner.

To retain a successful project's procedure:

```sh
assistant skills learn orchid-release --from-task TASK_ID \
  --instructions-file /absolute/path/to/procedure.md
assistant skills approve-draft DRAFT_ID
assistant skills show orchid-release
```

The source task must have independently verified results and a private proof
file containing successful checks. Creating a draft does not activate it. Owner
approval revalidates the proof hash and writes an instruction-only skill under
`runtime/skills/`. In chat, `skill_draft` accepts the verified task ID and procedure;
`skill_activate` pauses for owner approval before activation.

Approvals bind the exact tool and arguments to the owner's original conversation,
expire after a bounded lifetime, and can be consumed once. A model-proposed
`project_submit` or `skill_activate`, publishing skill, or future outward message
first returns an approval ID and stops that turn:

```sh
assistant approve APPROVAL_ID --session ORIGINAL_SESSION
```

For a Slack/QQ request, reply `/approve APPROVAL_ID` in that original conversation.
Use the original data/config/owner options for CLI approval. Reusing a grant,
changing its arguments or using it from another session is refused.

Full daily aliases and standalone research/todos/consolidate require approval
because they can publish the website or send email. Even `daily` with
`dry_run: true` remains gated: the retained pipeline's dry-run only skips digest
email. Direct retained operator commands keep their old explicit operator
semantics. The résumé's final push remains the separate `assistant approve-resume`
command; a model cannot approve its own résumé publication through the catalog.

## Memory

```sh
assistant memory add "Orchid chose SQLite for local state" \
  --project orchid --source operator:architecture-decision
assistant memory search "SQLite" --project orchid
```

New facts are append-only and include a source, kind, timestamp and optional
project. Project-scoped retrieval includes that project's facts and owner-global
facts; old records lacking a project key must mention the requested project to
enter scoped retrieval. Existing profile, observation, session, todo, reading,
lesson and workflow stores are read in place, preserving their own IDs and
history. Health and finance are queried explicitly through their structured
skills and are excluded from ambient project-memory retrieval.

A remembered statement is not independent verification. Keep evidence/source
references when discussing past work, and distinguish explicit owner statements
from a project's recorded test/artifact proof. Retrieved text and learned
instructions are context, never authority to bypass a host tool or approval.

## Data and upgrading

The default root is unchanged. In multi-tenant mode the same layout is scoped
under `DATA_DIR/users/<uid>/`.

```text
~/.personal-agent/
├── runtime.toml                   # optional new route/channel configuration
├── runtime.sqlite3                # conversations, approvals, projects and run events
├── runtime_memory.sqlite          # append-only sourced facts
├── runtime_inbox.sqlite3          # native-channel input/reply/delivery receipts
├── runtime/
│   ├── project-evidence/          # independently verified run proof
│   ├── skill-drafts/              # reviewable proposed procedures
│   └── skills/                    # owner-approved instruction-only skills
├── profile/                       # existing Git-versioned YAML stores and history
│   ├── profile.yaml, aliases.yaml, ops_log.jsonl
│   ├── todos.yaml, reading_list.yaml, lessons.yaml, workflows.yaml
│   ├── finance/YYYY-MM-DD.yaml
│   └── health/YYYY-MM-DD.yaml, health/profile.yaml
├── events.db                      # existing observations, seen fingerprints and metrics
├── sessions/                      # retained old flat/day-sharded conversation files
├── tasks/                         # retained legacy task records and traces
├── runs/, state.json              # retained daily-run artifacts and resume checkpoint
└── shared/, users.yaml            # existing multi-user infrastructure when configured
```

Starting the runtime creates only its new stores; it does not bulk-migrate old
records, flatten them into a Wiki, or discard the old Git repositories. Explicit
use of legacy finance/health handlers retains their tested migration behavior:
old single-file records become readable through daily shards while the original
legacy file remains intact. New runtime conversations stay in the new store;
older conversations remain available through compatibility recall.

Back up the data root before switching an existing deployment, stop its old
process, and use one daemon for that root. Keep the previous code revision and
configuration for rollback. Existing `assistant run`, `todo`, `reading`,
`bootstrap`, `enrich-profile`, résumé and admin commands remain available.
`assistant legacy <command>` selects old implementations for names now owned by
the new runtime; consult `assistant legacy --help`.

The old `assistant init --check` diagnoses the retained personal integrations;
`assistant doctor` diagnoses the new runtime route/configuration. Existing
`scripts/` and `systemd/` paths describe the deployed container and are not changed
by a source rewrite. Review the deployed service command/configuration separately
when upgrading; source changes are not evidence of production deployment.

## Implementation and validation

| Module | Responsibility |
| --- | --- |
| `platform/runtime/contracts.py` | Provider and trusted channel envelopes |
| `platform/runtime/backends.py` | Bounded API and authenticated native-CLI adapters |
| `platform/runtime/channels/` | Slack/QQ admission, protocol ACK, reconnects and replies |
| `platform/runtime/store.py`, `inbox.py` | Durable state, atomic claims, approvals and delivery receipts |
| `agent/runtime/kernel.py` | Bounded personal tool loop and exact-action policy |
| `agent/runtime/projects.py` | Workspace execution, cancellation, verification and evidence |
| `agent/runtime/memory.py`, `skills.py` | Sourced context, preservation wrappers and reviewed procedures |
| `agent/runtime/app.py`, `runtime_cli.py` | Process/CLI composition and compatibility with retained services |

Dependency direction remains `agent/ → platform/`. Model and transport adapters
cannot import the personal agent layer. The design references
[bothouse](https://github.com/zuiho-kai/bothouse) for channel/workspace orchestration
and [Pi](https://github.com/earendil-works/pi) for a small extensible kernel and
progressive context/skills.

```sh
pip install -e . pytest
python -m pytest -q
```

Automated suites use real scratch stores/files and fixture model/transport edges,
including application/CLI journeys, approvals, restart recovery, tenant isolation,
channel deduplication and verification commands. The exact results and separately
attempted live-provider checks are recorded in [REWRITE_TODO.md](../REWRITE_TODO.md).
A passing fixture suite is not a real Slack/QQ account verification, a production
migration or a deployment. No live channel connection is enabled by default.
