# Pi runtime: channels, personal projects, memory and skills

The default `assistant ask`, `chat`, `task` and `serve` commands run the actual
**Pi SDK 1.4.0**. `createAgentSession`, `ModelRuntime` and `SessionManager` own
model calls, tool sequencing, retries, compaction and persisted JSONL context.
Python supplies typed personal tools, approvals, memory, delivery and project
verification through a bounded stdio bridge.

The engine uses the published `@earendil-works/pi-coding-agent` and `pi-ai`
packages, including Pi's agent core. Development follows the
[tzhouam/pi fork](https://github.com/tzhouam/pi) of canonical upstream
[earendil-works/pi](https://github.com/earendil-works/pi). The pinned packages
currently come from upstream npm releases. The host has no Python model/tool
loop fallback and loads only the tools and resources explicitly supplied by this
application.

The nine-phase daily workflow remains a deterministic Python skill with its
checkpoint, artifacts and existing stores. LangGraph is no longer a dependency.
The [personal-workflow guide](USER_GUIDE.md) and [domain design](DESIGN.md)
document those integrations. `assistant legacy …` explicitly selects preserved
older chat/task entrypoints; these are not the default Pi runtime.

## Installation, configuration and commands

Install Python 3.11+, Node.js 22.19+ and npm. Then install the Python package and
explicitly install the pinned Pi host dependencies:

```sh
pip install -e .
assistant pi install
assistant pi status
mkdir -p ~/.personal-agent
cp runtime.example.toml ~/.personal-agent/runtime.toml
# Export DEEPSEEK_API_KEY securely in your shell or service environment.
assistant doctor --backend deepseek
assistant doctor --backend deepseek --probe
assistant ask "List my skills" --session personal
assistant chat "Continue the same conversation" --session personal
assistant serve
```

`pi install` runs `npm ci --ignore-scripts --no-audit --no-fund` in the private
`DATA_DIR/pi/host/` directory. Installation is an explicit operator command;
ordinary turns do not fetch packages. `pi status` inspects the installed SDK
without making a model request. Use `--data-dir PATH` consistently when operating
an isolated root. Installing Pi does not authenticate a model account, start a
OneBot server or join a Slack workspace.

`doctor` reports both Pi availability and route configuration without exposing
credential values. A configured API key/model or installed CLI executable does
not prove account access. `doctor --probe` makes a real request through Pi to the
selected route. Provider failures remain failures; the runtime does not silently
select a different route.

Runtime commands accept:

| Option | Meaning |
| --- | --- |
| `--data-dir PATH` | Personal-data root; otherwise `DATA_DIR` or `~/.personal-agent` |
| `--config PATH` | Runtime TOML file |
| `--backend NAME` | Exact named model route; the engine remains Pi |
| `--owner UID` | Active roster owner in multi-tenant mode; default `default` |
| `--session NAME` | Durable conversation and approval scope; CLI default `cli` |

Keep these options consistent across `skills run`, `approve`, project and memory
commands that should share state. Options follow the command, for example
`assistant skills list --data-dir /tmp/my-agent`.

Route selection is `--backend`, then `RUNTIME_BACKEND`, then `[runtime].backend`.
`auto` chooses a configured Anthropic API, then OpenAI API, then an installed
Codex/Claude/Gemini model adapter at startup. It does not change the engine or
create request-time failover. Name the route explicitly for reproducibility.

`[runtime]` controls `max_steps` (1–64), `workers` (1–16), and session `timeout`
(1–3600 seconds). Project sessions also use `RUNTIME_PROJECT_TIMEOUT` and
`RUNTIME_PROJECT_MAX_STEPS`; route timeouts bound their duration too. Existing
personal integration settings remain in `.env`. TOML rejects inline backend
`api_key` values: use `api_key_env` instead. Export custom environment names in
the service environment; supported Settings fields can also come from `.env`.

## Model backends

Every route supplies a model to Pi. API routes use Pi's native provider transport;
optional CLI adapters return individual completions to Pi, which continues to own
the model/tool loop, conversation and tool policy.

| Route kind | Model transport | Tool execution |
| --- | --- | --- |
| `openai` | Pi OpenAI-compatible Chat Completions | Typed Python host tools |
| `anthropic` | Pi Anthropic Messages | Typed Python host tools |
| `api` | Explicit Pi `api` transport and `provider` identifier | Typed Python host tools |
| `codex` | Optional authenticated `codex exec` completion adapter | Pi dispatches host tools; native tools disabled |
| `claude` | Optional Claude Code completion adapter | Pi dispatches host tools; native tools/MCP disabled |
| `gemini` | Optional Gemini CLI completion adapter | Requires explicit external-isolation opt-in |

A separately installed Pi CLI is not required. `--backend` chooses a model route,
not an alternative agent engine. Inherited Pi skills, agent files, extensions,
MCP servers and built-in filesystem/shell tools are not discovered automatically.
The application provides its reviewed skill catalog and confined project tools.

### API route examples

The sample selects DeepSeek's compatible API:

```toml
[runtime]
backend = "deepseek"

[backends.deepseek]
kind = "openai"
api = "openai-completions"
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"
thinking_level = "off"
context_window = 128000
```

Use the provider's actual model ID, rather than assuming a marketing name is an
API identifier. Model access and endpoint compatibility require a real probe;
this example is not a verification result.

For other Pi-supported transports, configure an explicit API route:

```toml
[backends.my_api]
kind = "api"
api = "openai-completions"
provider = "my-provider"
base_url = "https://your-provider.example/v1"
model = "your-model-id"
api_key_env = "MY_API_KEY"
thinking_level = "off"
context_window = 128000
```

| Field | Purpose |
| --- | --- |
| `kind` | `openai`, `anthropic`, explicit `api`, or an optional CLI adapter |
| `api` | Pi wire API, such as `openai-completions`, `openai-responses` or `anthropic-messages`; must match the endpoint |
| `provider` | Explicit Pi provider identifier for the configured model |
| `model`, `base_url` | Exact model ID and provider endpoint |
| `api_key_env` | Environment variable holding the credential |
| `thinking_level` | Pi reasoning level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`; model support varies |
| `context_window` | Advertised model context size in tokens, validated from 1024 to 10000000 |

Existing `ANTHROPIC_*` and `OPENAI_*` settings still seed their named routes.
Personal workflow `LLM` calls, including role routing and mixture proposals, use
Pi underneath while retaining their existing text/structured-output interface.

### Optional CLI model adapters

```toml
[backends.codex]
kind = "codex"
command = ["codex"]
# model = "a-model-available-to-your-account"

[backends.claude]
kind = "claude"
command = ["claude"]

[backends.gemini]
kind = "gemini"
command = ["gemini"]
allow_unrestricted = false
```

These optional adapters use their own existing login. A `command` is an argv
array, never a shell string. Codex ignores inherited user apps/plugins/MCP
configuration and runs with native execution disabled. Specify custom routing
explicitly through `model` or non-secret command arguments. Claude's native tools
and MCP are also disabled. Gemini requires `allow_unrestricted = true` and an
externally isolated environment because its configured native tools cannot all
be reliably disabled. No adapter takes over the project's coding loop.

There is one selected route per runtime application. Named routes do not create
a parallel team of different CLI agents. Development verification can use fake
Pi providers or a separately configured API route; a CLI adapter is not required.

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
those paths, or `assistant ask "…" --image /path/photo.png`, run through Pi with
an image-capable configured model. The host retains append-only image logging
and requires text-only confirmation for other mutations. See
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

Every project uses one Pi session with confined read/list/write host tools.
An arbitrary model-requested host subprocess requires the operator's explicit
`RUNTIME_ALLOW_UNRESTRICTED=true` opt-in. Optional CLI model adapters use this
same project path. Every run records its frozen workspace, full goal, state,
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

An approval pause aborts the active Pi run before another tool executes. The
approved action executes once at the host boundary, and its outcome enters the
scoped Pi transcript as context for a subsequent turn.

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
├── runtime.sqlite3                # conversation audit, approvals, projects and run events
├── runtime_memory.sqlite          # append-only sourced facts
├── runtime_inbox.sqlite3          # native-channel input/reply/delivery receipts
├── pi/
│   ├── host/                     # explicitly installed SDK 1.4.0 host/dependencies
│   └── sessions/<scope-hash>/     # authoritative Pi JSONL, manifest and session lock
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
legacy file remains intact. Pi's scoped JSONL files hold authoritative model
context; SQLite keeps audit and delivery state. Exact session manifests prevent
cross-room or cross-owner resumption, with a process lock around each transcript.
History from a retained bridge is imported once into a fresh Pi session; older
records also remain available through compatibility recall. Session directories
are private, and prompt/tool credentials are redacted before persistence.

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
| `platform/runtime/pi.py` | Scoped Pi process/session bridge, installation and host tool RPC |
| `platform/runtime/pi_host/` | Actual Pi SDK sessions, native provider transports and JSONL context |
| `platform/runtime/backends.py` | Optional CLI completion adapters and retained compatibility utilities |
| `platform/runtime/channels/` | Slack/QQ admission, protocol ACK, reconnects and replies |
| `platform/runtime/store.py`, `inbox.py` | Durable state, atomic claims, approvals and delivery receipts |
| `agent/runtime/kernel.py` | Personal tools, sourced context and exact-action policy supplied to Pi |
| `agent/runtime/projects.py` | One Pi session per job, confined host tools, cancellation and independent evidence |
| `agent/orchestrator.py` | Framework-free deterministic daily workflow and resume checkpoint |
| `agent/runtime/memory.py`, `skills.py` | Sourced context, preservation wrappers and reviewed procedures |
| `agent/runtime/app.py`, `runtime_cli.py` | Process/CLI composition and compatibility with retained services |

Dependency direction remains `agent/ → platform/`. Model and transport adapters
cannot import the personal agent layer. The application builds directly on
[Pi](https://github.com/earendil-works/pi), with development fork
[tzhouam/pi](https://github.com/tzhouam/pi). [bothouse](https://github.com/zuiho-kai/bothouse)
informs channel/workspace orchestration.

```sh
pip install -e . pytest
python -m pytest -q
cd src/assistant/platform/runtime/pi_host
npm ci --ignore-scripts --no-audit --no-fund
npm run check
node --test test/session.test.mjs
```

Automated suites use scratch stores/files and fixture model/transport edges,
including application/CLI journeys, approvals, restart recovery, tenant isolation,
channel deduplication and verification commands. The exact results and separately
attempted live-provider checks are recorded in [PI_TODO.md](../PI_TODO.md).
A passing fixture suite is not a real Slack/QQ account verification, a production
migration or a deployment. No live channel connection is enabled by default.
