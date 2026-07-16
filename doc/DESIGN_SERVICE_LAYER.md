# Service-Layer Redesign — reuse OpenClaw, keep the pipeline

Owner: Taichang Zhou (tzhouam)
Status: **implemented v1.0 (2026-07-09)** — M1–M4 all landed; see §7 for the
decisions taken and the one manual cutover step (gateway restart).
Supersedes nothing — refines the runtime topology of [DESIGN.md](DESIGN.md) §8 and
[WECHAT_OPENCLAW.md](WECHAT_OPENCLAW.md); the LangGraph pipeline and all task
semantics are **unchanged**.

> **Scope note (2026-07-16):** this doc describes the **`single_user`**
> deployment. In `multi_tenant` ([DESIGN_MULTI_USER.md](DESIGN_MULTI_USER.md))
> several claims below are superseded: Popen background jobs → the durable
> SQLite queue + in-process worker pool (`jobs.py`/`worker.py`); the optional
> `SERVE_TOKEN` → a **mandatory** bridge token on every endpoint; the
> `before_agent_reply` hook → `before_dispatch` accountId routing; the
> gateway-cron pipeline → the per-user fan-out scheduler (`scheduler.py`);
> "owner-only sender auth" → per-account allowlist + registry-resolved uid.

## 1. Why change anything

The current split (OpenClaw = transport via exec bridge, Python = brain) is
correct in philosophy but has five concrete frictions:

| # | Friction | Where it lives today |
|---|---|---|
| 1 | **Exec-per-message latency & amnesia** — every WeChat message spawns a fresh interpreter (`assistant ask`): ~2–5 s import/startup tax, zero multi-turn memory, 120 s hard cap, 4 MB buffer | `openclaw-plugin/index.js` `ask()` |
| 2 | **Stale long-lived config** — `chat-listen` builds `Settings`/`LLM` once at startup; the 2026-07-07→09 DeepSeek-key outage kept failing in the listener even after `.env` was fixed, until a manual kill/respawn | `chat/service.py` `run_listener()` |
| 3 | **Two channel stacks** — Python maintains IMAP + WeCom adapters while OpenClaw also owns channels (WeChat live). Double maintenance, double auth logic | `chat/{email_channel,wecom}.py` vs gateway |
| 4 | **Two delivery paths** — email is rendered/sent inside Phase `deliver`; the WeChat one-liner only exists because the *cron job* carries `--announce`. A manual or chat-triggered run never announces | `deliver/email.py`, cron job config |
| 5 | **Actions are hardcoded three times** — the action list lives in the chat system prompt, in `_execute()`, and partially as CLI subcommands. Adding one capability touches all three and OpenClaw can't reach any of them without an LLM round-trip | `chat/agent.py`, `cli.py` |

## 2. Target architecture — four layers

```
┌────────────────────────────────────────────────────────────┐
│ L4 · OpenClaw gateway (Node)  — TRANSPORT + SCHED + SUPERV │
│   wechat channel · command cron                            │
│   bridge plugin: hook→HTTP, /cmd→HTTP, serve-supervisor    │
└──────────────▲───────────────────────────▲─────────────────┘
               │ HTTP 127.0.0.1 (bearer)   │ spawn/respawn
┌──────────────┴───────────────────────────┴─────────────────┐
│ L3 · `assistant serve` (one long-lived Python daemon)      │
│   POST /chat {session,text}   → LLM reply + typed actions  │
│   POST /actions/{name}        → direct, no LLM             │
│   POST /run {resume?}         → pipeline (flock inside)    │
│   GET  /status /healthz                                    │
│   background task: email IMAP poll (absorbs chat-listen)   │
└──────────────▲─────────────────────────────────────────────┘
               │ imports
┌──────────────┴─────────────────────────────────────────────┐
│ L2 · Action Registry (single source of truth)              │
│   name · JSON schema · handler · owner_only · description  │
│   drives: chat prompt list · CLI subcommands · HTTP routes │
│   pipeline phases exposed as actions (run_pipeline, …)     │
└──────────────▲─────────────────────────────────────────────┘
               │ calls
┌──────────────┴─────────────────────────────────────────────┐
│ L1 · Core (UNCHANGED)                                      │
│   LangGraph 9-phase StateGraph · ProfileStore · TodoStore  │
│   EventsStore · research pipeline · resume approval gate   │
└────────────────────────────────────────────────────────────┘
```

**What is deliberately still NOT reused from OpenClaw:** its own LLM agent,
persona, and memory. The SOUL.md prompt-bridge experiment already proved that
path drifts; the brain stays Python. OpenClaw contributes exactly what it is
uniquely good at: WeChat access, persistent cron, and process supervision.

## 3. The pieces

### 3.1 L2 — Action Registry (`src/assistant/actions.py`, new)

One table replaces three hand-maintained copies:

```python
@dataclass
class Action:
    name: str            # "add_todo"
    schema: dict         # JSON schema for params (validated before handler)
    handler: Callable    # (settings, params) -> str  (one human line, from what code DID)
    description: str     # one line, feeds the chat prompt and /help
    slash: str | None    # optional OpenClaw command alias, e.g. "/todo add"
```

- Registered: `add_todo`, `done_todo`, `list_todos`, `done_reading`,
  `list_reading`, `trigger_run`, `run_status`, `show_profile_summary`.
  Pipeline itself: `trigger_run` keeps the existing Popen + state.json guard.
- `chat/agent.py` generates the "You may execute actions" prompt block from
  the registry; `_execute()` becomes a thin loop of schema-validate → handler.
- `cli.py` keeps its argparse UX but each subcommand body calls the same
  handler. Behavior identical; definitions single-sourced.

### 3.2 L3 — `assistant serve` (new, replaces `chat-listen` as the daemon)

Small FastAPI/uvicorn app (or stdlib `http.server` if we want zero deps),
**bound to 127.0.0.1** with a bearer token generated into `.env`
(`SERVE_TOKEN`). Same-host only; channels still authenticate the *sender*
before anything reaches L3 — the token just stops other local processes.

- `POST /chat {"session": "<channel:peer>", "text": ...}` → runs the existing
  `handle_message()` plus a **rolling per-session history** (last N=10
  exchanges, in-memory + JSON spill under `~/.personal-agent/sessions/`).
  This is what exec-per-message can never give: "what about the second one?"
  finally works on WeChat.
- `POST /actions/{name}` → registry dispatch, no LLM. Instant todo/status
  queries from slash commands.
- `POST /run` → same flock + `state.json` guard as today (`orchestrator.run()`
  already owns the canonical lock).
- **Fresh `Settings()`/`LLM()` per request.** `Settings()` re-reads `.env` on
  each instantiation and client construction is trivially cheap; this
  permanently deletes the stale-credential failure class (friction 2). No
  /reload endpoint needed.
- Email IMAP polling moves in as a background asyncio task (same
  `EmailChannel` code, same UID watermark). `chat-listen` remains as a CLI
  alias for one-cycle debugging (`--once`), but the daemon is the runtime.

### 3.3 L4 — bridge plugin v2 (`openclaw-plugin/index.js`)

- `before_agent_reply` hook: `fetch("http://127.0.0.1:<port>/chat", …)` with
  `session: ctx.conversationId ?? sender`, **exec fallback** to the current
  `assistant ask` path when the daemon is unreachable (keeps the proven
  degraded mode; log loudly when falling back).
- Slash commands: the hook currently ignores `body.startsWith("/")`, leaving
  them to the gateway LLM. Instead intercept a small allowlist *before* that
  check — `/todo …`, `/digest`, `/status`, `/read …` → `POST /actions/...`,
  formatted reply, `{handled: true}`. No model call at all for the common
  queries; the gateway LLM's remaining surface shrinks to OpenClaw built-ins.
- `chat-listen-supervisor` → `serve-supervisor`: identical spawn/respawn/
  backoff/stale-pid logic, now spawning `assistant serve`. Still exactly one
  process to keep alive, still revived by the `~/.bashrc` guard.

### 3.4 Scheduling & announce (friction 4)

- **Cron stays on `daily-run.sh` unchanged.** It is deliberately independent
  of the daemon (shell + flock + `run || run --resume`), so a dead daemon can
  never lose the 07:00 digest. Do NOT route cron through `/run`.
- Phase `deliver` gains a best-effort **announce step**: after the email
  sends, shell out to `openclaw message send --channel openclaw-weixin --to
  <owner> …` (the exact invocation the cron `--announce` uses today, already
  probe-verified). Guarded by try/except + a `WECHAT_ANNOUNCE=1` setting;
  failure is a footer warning, never a pipeline error. Then the cron job's
  `--announce` becomes redundant (keep it one release as belt-and-braces) and
  manual/chat-triggered runs announce too.

### 3.5 Channel consolidation (friction 3, later)

- **Slack**: dropped entirely (owner decision, 2026-07-09) — the adapter,
  settings, and env vars were removed; no OpenClaw Slack channel either.
- **Email**: stays Python forever (OpenClaw has no IMAP channel); it now lives
  inside the daemon instead of a separate process.
- **WeCom adapter** (`wecom.py`): keep dormant as the fallback WeChat path;
  it costs nothing while unconfigured.

## 4. What explicitly does not change

- The LangGraph 9-phase graph, its resume semantics, run artifacts, flock.
- Typed-actions-executed-by-code safety model; outcomes appended from what
  code did, never from LLM claims.
- Resume approval gate; profile education/experience protection; website
  direct-push exception; never-force-push.
- `daily-run.sh` + OpenClaw command cron as the scheduler.
- Owner-only sender authentication in every channel.

## 5. Migration plan (each step independently shippable & revertible)

| Step | Delivers | Kills friction | Status |
|---|---|---|---|
| **M1** | `actions.py` registry; `chat/agent.py` + `cli.py` consume it (pure refactor, tests green) | 5 | ✅ 2026-07-09 |
| **M2** | `assistant serve` (chat + actions + status endpoints, per-request Settings, email poll inside); plugin hook → HTTP with exec fallback; supervisor spawns `serve` | 1, 2 | ✅ 2026-07-09 |
| **M3** | Slash-command interception in the plugin → `/actions` | 5 (UX half) | ✅ 2026-07-09 |
| **M4** | Deliver-phase WeChat announce (`WECHAT_ANNOUNCE`, off by default); retire reliance on cron `--announce` | 4 | ✅ 2026-07-09 |

Rollback story: M2 is the only risky step and its failure mode is the exec
fallback — i.e., exactly the pre-daemon behavior.

## 6. Decisions taken (were "open decisions")

1. HTTP framework: **stdlib `ThreadingHTTPServer`** — zero new deps.
2. Session history: **JSON spill** under `~/.personal-agent/sessions/`
   (sha1-named file per session, last `SERVE_SESSION_TURNS`=10 exchanges).
3. Slash allowlist: **`/todo` (list/add/done), `/read` (list/done),
   `/digest`, `/status`** — everything else starting with "/" still falls
   through to OpenClaw built-ins.

## 7. Implementation notes & cutover

- Landed 2026-07-09: `src/assistant/actions.py`, `src/assistant/serve.py`,
  registry-backed `chat/agent.py` + `cli.py`, `deliver/announce.py`,
  orchestrator announce step, rewritten `openclaw-plugin/index.js`
  (HTTP + slash + `serve-supervisor`) and `test.mjs`. Python suite 59 green,
  plugin suite ALL PASS; end-to-end smoke verified on an isolated port
  (healthz / 401 / action / real-LLM chat / multi-turn recall).
- `serve` holds the same `chat_listener.pid` lock as the old listener, so the
  supervisor's stale-pid takeover migrates it automatically.
- **Cutover = one gateway restart** (loads the new plugin code, kills the old
  `chat-listen`, spawns `serve`):
  `pkill -x openclaw && nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &`
- Announce env (when enabling M4): `WECHAT_ANNOUNCE=true`,
  `ANNOUNCE_ACCOUNT=<GATEWAY_ACCOUNT>-im-bot`,
  `ANNOUNCE_TO=<YOUR_WECHAT_IM_ID>@im.wechat`, and remove
  `--announce` from the `daily-digest` cron job.
