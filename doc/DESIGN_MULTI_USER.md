# personal-agent — Multi-User Support Plan (v3.5)

Status: **implemented behind `DEPLOYMENT_MODE` (branch `feat/multiuser-phase0`)**
— default `single_user` is unchanged; production enablement of `multi_tenant` is
**gated on the §A.8 weixin spike** (real-hardware accountId verification) plus
the enablement checklist in
[WECHAT_OPENCLAW.md](WECHAT_OPENCLAW.md#multi-user-multi_tenant). Details in the
implementation-status note under §15. Current single-user architecture:
[DESIGN.md](DESIGN.md).

> **v2 revision.** Incorporates a design review. The direction (Settings as the
> isolation seam, per-user DATA_DIR, backward-compatible rollout, shared-WeCom
> routing, staggered scheduling) held up. Fixed: subprocess UID propagation,
> auth→UID (never caller-provided), session/media isolation, same-user write
> serialization, explicit config precedence, UID/path safety, admin-only reboot,
> and security testing moved to Phase 0/1. The **prime directive** now leads the
> doc and every phase is checked against it.
>
> **v3 revision (pre-implementation blockers resolved).** (1) The **global
> tracer** (`tracing._default`) breaks concurrent tenant isolation — added to the
> audit (§3), fixed via a ContextVar-scoped tracer, tested in Phase 1. (2) The
> UID env var is **propagation, not authentication** — a local process could set
> it; replaced with a signed short-lived **job capability** (or internal queue)
> and an explicit local-process trust boundary (§6). (3) **No DEFAULT_UID
> fallback in multi-tenant mode** — missing identity is rejected; the default
> only exists in an explicit legacy single-user deployment mode (§6, §14). Plus:
> email identity from the mailbox/poller context (§4.1/§11), hashed bearer
> tokens (§4.1), full-transaction mutation locks with defined ordering (§8), a
> safe user-deletion protocol (§14), the process-model decision fixed to Phase 0
> only (§15), and encrypted-secrets timing tied to the threat model (§13/§15).
>
> **v3.1 — WeChat/OpenClaw coverage (§11 rewritten).** The v2/v3 plan treated
> WeChat like WeCom. It isn't: the shipping channel is **personal WeChat via
> OpenClaw** (the owner's own QR-logged account), and the bridge asserts **no
> per-sender identity** (weixin exposes only a `sessionKey`, "neither
> conversationId nor SenderId"). Personal WeChat is single-user *per account* —
> so multi-user is done with **one account per user, routed by `accountId`**
> (§11.3). Trust model, media isolation, and the gateway's cron→scheduler move
> are spelled out in §11.
>
> **v3.2 — scope narrowed: WeChat + email only (no WeCom, no CLI).** Per owner
> decision, the supported user channels are **personal WeChat via OpenClaw
> (one account per user, §11.3)** and **email (per-user mailbox)**. This
> *simplifies* the design: each channel binds the tenant at the **connection**
> (a WeChat account / a mailbox; a sender policy then authorizes who may act), so
> there are **no per-user bearer tokens / direct-HTTP surface**
> (the only token is the bridge↔daemon token), **WeCom is dropped** as a channel,
> and — since there's no CLI surface — background jobs move off CLI subprocess
> spawns onto an **internal worker queue**, which *removes the §6 subprocess trust
> boundary entirely*. Sections below reflect this scope; WeCom notes are kept only
> as "why we didn't."
>
> **v3.3 — OpenClaw API corrections (post-review).** (1) **Hook fix:** the bridge
> must intercept at **`before_dispatch`** (which exposes `accountId`), *not*
> `before_agent_reply` (only `cleanedBody`, no `accountId`). (2) **Endpoint
> scoping:** slash commands hit `/actions/*` & `/run` — those must carry the same
> authenticated channel context as `/chat`. (3) **Unproven plugin:** the external
> `@tencent-weixin/openclaw-weixin` isn't in-source, so a **Phase-0 two-account
> spike (A.8) gates all registry work.** Corrections: `session.dmScope =
> per-account-channel-peer` is mandatory config+test; **`accountId` is the
> receiving account, not the authorized sender** — WeChat DMs use pairing /
> `allowFrom`, so an owner-only allowlist per account is the real auth (if no
> sender id, every admitted sender = owner privileges); media keyed by
> **`(accountId, sessionKey)`** with TTL/size/MIME/count caps; **proactive
> outbound** (reminders/routines/reports/digests) needs each user's own
> `accountId` pinned (§A.9); and the gateway+plugins sit **inside the TCB** (the
> bridge can impersonate any account) — a self-host posture. §11 renumbered.
>
> **v3.4 — payload/queue fixes + stale cleanup (post-review).** (1) `before_dispatch`
> reads **`event.body ?? event.content`** (not `cleanedBody`, which is empty here)
> and has **no `trigger`** field (guard dropped). (2) The internal queue is
> **durable (SQLite)** with states/retries/idempotency, per-user fairness, and
> restart recovery — an in-memory queue would drop jobs on respawn. (3) **Admin
> reboot** exits gracefully → `serve-supervisor` respawns (a process can't restart
> itself). Also: **`ctx.accountId`** (not `event.`) in Phase 2; a **`senderId` +
> sender-authorization** step in the spike; **bridge token is two-sided** (daemon
> stores the hash, bridge holds plaintext); "account selects tenant, sender policy
> authorizes" (an account can accept many senders); media-cache-race test; A.2
> code fence; "**no CLI**" = no *user* CLI (admin CLI stays); and removed the
> duplicate §6.1, the §8 "separate processes", the §16 capability test, the §11.4
> `askExec`, and stale per-user-bearer-token references.
>
> **v3.5 — pre-implementation hardening (post-review).** (1) **Bridge fails
> *closed*:** the hook is scoped to `ctx.channel === "weixin"` and, for a weixin
> turn, **always** returns `{handled:true, text: safeError}` on missing/unknown
> `accountId`, daemon-down, auth-fail, or bad response — never a bare `return`
> (which would fall through to OpenClaw's own model). (2) **Queue delivery is
> at-least-once:** `dedupe_key` stops duplicate *enqueue* but not duplicate *side
> effects* — added an **outbox/delivery ledger**, and state that duplicate
> delivery can still happen. (3) **Global queue:** `DATA_DIR/shared/jobs.db`,
> admin-only, redacted/encrypted payloads (not per-user `events.db`). (4)
> **Cooperative cancellation** (no thread force-kill) for deletion + reboot
> drain/requeue. Plus: **mandatory bridge token in multi-tenant** (empty ≠ open)
> across **all** endpoints incl. `/status`; **same-turn** media-race test; Phase 2
> registry = the one bridge-token hash; §13 bearer text removed; prime-directive/
> summary say *queued jobs*; title → v3.4; hosted-service decision narrowed to
> **trusted self-host only**.

---

## 0. Prime directive (the one acceptance test)

> **An interleaved request or detached job for user B must be technically
> incapable of reading, writing, logging, or delivering anything belonging to
> user A.**

Every phase below ends with tests asserting this. If a change can't be shown to
preserve it, it doesn't ship.

---

## 1. Goal & non-goals

**Goal.** One deployment serves *N* independent owners, each with their own
profile, GitHub/email/WeChat identity, data, website, résumé, schedule, and chat
— fully isolated.

**Supported channels (scope).** **Personal WeChat via OpenClaw** (one account per
user, §11.3) and **email** (per-user mailbox). **Out of scope by decision:**
WeCom, and any **user-facing** CLI / direct-HTTP surface (an **admin CLI** —
`assistant admin migrate/add-user/reboot` — stays; it's an operator tool, not a
tenant surface). Consequences threaded below: no per-user bearer tokens (the only
token is the bridge↔daemon one), and background jobs run on a **durable internal
worker queue** instead of CLI subprocesses.

**Non-goals (v1).** Not collaborative (users never see each other's data); not
open public sign-up on day one (admin-provisioned roster first); the agent's
per-user behavior is unchanged — only the tenancy and isolation are new.

**Load-bearing insight (still true, with caveats).** The system is parameterized
by one object: `Settings` (`config.py`) carries `data_dir` and every path +
secret; the serve daemon rebuilds it **per request** (`settings_factory`); and
`run(settings, …)` and `handle_message(text, settings, …)` take it as a
parameter. So per-user isolation is largely "make `Settings` per-user and resolve
which user each request/run belongs to." **But** three things do *not* isolate on
Settings alone and are called out as first-class work: **background jobs** (the
durable worker queue, §6), the **boot-bound SessionStore** (§7), the
**module-global tracer** (§3), and **unserialized same-user writes** (§8).
Getting those wrong silently breaks the prime directive.

---

## 2. Threat model (do this first — Phase 0)

Assets: each user's profile/finance/health/todos data, secrets (GitHub/email/
WeChat tokens, website/résumé creds), chat history, and delivery channels.

Threats to design against, each mapped to a control:

| Threat | Control |
|---|---|
| Cross-tenant **read/write** (A's data in B's request/job) | Per-user `Settings` + `data_dir`; path containment (§5); mutation lock (§8); isolation tests |
| **Session/media bleed** (B sees A's chat/images) | Per-request per-user `SessionStore`, uid-scoped paths (§7) |
| **Background job runs as wrong user** (detached job defaults to owner) | No CLI spawns (§1): jobs run on an **internal worker queue** holding the authenticated `UserContext` — no child process, no forgeable env/capability (§6) |
| **Concurrent-run trace bleed** (A's spans in B's `trace.jsonl`) | `tracing._default` is a module global overwritten by each `run()`; make it **ContextVar-scoped** or thread it through `Deps` (§3, Phase 1) |
| **Caller-forged identity** (client claims a UID / sets the UID env) | Auth **resolves** UID (token/sender→uid); a UID is never accepted from the caller **or a bare env var** (§4, §6) |
| **Legacy fallback abused in multi-tenant** (missing id → default user) | No `DEFAULT_UID` fallback when a registry exists; missing id ⇒ reject; default only in explicit legacy single-user mode (§6) |
| **Channel identity spoof / gateway over-trust** (shared `SERVE_TOKEN` = authority to impersonate anyone; weixin exposes no per-sender id) | One WeChat account per user, routed by authenticated **`accountId`** (§11.3); the gateway→daemon token is a privileged bridge credential; media as bytes, scoped to the resolved uid (§11) |
| **Path traversal / symlink escape** via UID | Opaque validated UIDs, resolved-path containment, no symlinks, atomic registry (§5) |
| **Secret leakage in logs** | uid-tagged logs, secret redaction, per-user log scoping (§9) |
| **Privilege escalation** (user → admin) | Admin is a registry role checked server-side; admin actions (reboot) never in the per-user action set (§10) |
| **Shared-daemon DoS** (one user restarts/floods) | Admin-only reboot; per-user quotas + rate limiting (Phase 3/5) |

---

## 3. Single-user chokepoints (audit)

| Assumption | Where (verified) | Change |
|---|---|---|
| One global `Settings` from repo `.env` | `config.py` `env_file`, `data_dir` default | `Settings.for_user(uid)` from validated shared+user layers (§4) |
| One `DATA_DIR` holds everything | `config.py` `profile_dir`/`events_db`/`state_file`/`runs_dir`/… | Root = `DATA_DIR/users/<uid>/`; derived paths follow |
| "The owner" is implicit | `chat/service.py` `_owner_addresses`; `wecom_owner_userid`; serve bearer | `UserRegistry`: authenticated (channel,sender)/token → uid |
| **SessionStore bound at boot** | `serve.py:139` `sessions = SessionStore(boot.data_dir, …)` | Resolve a per-user `SessionStore` **per request** (§7) |
| **Detached jobs carry no UID** | `handlers.py` `Popen(… "run-phase"/"task"/"reboot")`, `_trigger_run`, `serve.py` `reboot`→`serve` | Drop CLI spawns → **internal worker queue** with the caller's `UserContext` (§6) |
| **Global tracer overwritten per run** | `tracing.py` `_default` (module global) set by `tracing.init()` in `orchestrator.run()`; read by `tracing.span()` in `llm.py`/phases | ContextVar-scoped tracer (mirror the existing `_current` ContextVar) or pass via `Deps` (§ Phase 1) |
| **No lock around store+git writes** | `*_store.py` `_save` = write + `git commit`, under `ThreadingHTTPServer` | Per-user mutation lock over the **whole** load→…→commit transaction (§8) |
| **reboot is a per-user chat action** | `registry.py` `name="reboot"`, `llm=True` | Admin-only; remove from the tenant action set (§10) |
| One inbox reader / one daily run | `chat_listener.pid`, `run.lock`, `state.json` | Per-user (data_dir-scoped) + a fan-out scheduler (§11–12) |
| **WeChat bridge asserts no sender id; one shared token; gateway drives cron** | `openclaw-plugin/index.js` (`{session,text}` only, `SERVE_TOKEN`); weixin exposes only `sessionKey`; `WECHAT_OPENCLAW.md` cron | One account per user → route by `accountId`; bridge reads it + scopes media; cron → scheduler (§11.3, App. A) |
| One website/résumé/marks repo | per-user secrets | Per-user config layer (§4) |

---

## 4. Identity, authentication, and config

### 4.1 Authentication resolves to exactly one UID

**Never accept a caller-provided UID — even over loopback.** With the scope
narrowed to WeChat + email (§1), there are exactly **two** identity paths. In
both, the **connection selects the tenant** (a WeChat *account* / a *mailbox*) and
a **sender policy then authorizes** who may act (WeChat pairing/`allowFrom`; email
allow-list) — one account can accept multiple senders, so account ≠ authorization:

- **WeChat (OpenClaw, one account per user)**: identity = the **`accountId`** that
  received the message. The bridge (holding the bridge↔daemon token) asserts it;
  the daemon resolves `registry.by_channel("weixin", accountId) → uid` (§11.3,
  Appendix A). The daemon honors an `account_id` **only** when the caller holds
  the bridge token; it never derives uid from the caller-controlled `session`.
- **Email**: identity comes from the **mailbox/poller context** — a per-user
  poller runs with that user's IMAP creds, so the mailbox that received the
  message *is* the uid. The sender allow-list is a second check (a sender address
  alone is spoofable and is never the identity).

There are **no per-user bearer tokens** and **no direct-HTTP/CLI user surface**
(dropped in §1). The single token in the system is the **bridge↔daemon token** —
a privileged secret that authorizes the bridge to assert `accountId`s (§11.4). It
is **two-sided**: the daemon registry stores only its **hash** (verify-only),
while the **bridge keeps the plaintext** in a protected env / secret store (it
must present the token to authenticate). Loopback-only, rotatable. The resolver returns a
`UserContext{uid, settings}`; if nothing resolves, the request is refused. No
code path lets the body/query/**env** choose the uid.

### 4.2 Config precedence (explicit, validated layers)

Do **not** rely solely on pydantic's layered `.env` behavior (it's positional and
has sharp edges — e.g. the multi-line-JSON trap we already hit). Build `Settings`
from explicit, individually-validated layers. Precedence, highest wins:

```
1. explicit runtime override (kwargs; tests, admin tools)
2. per-user config     (users/<uid>/config.env  — GITHUB_*, email, website, …)
3. shared config       (shared/.env             — LLM_*, bridge token, vision, defaults)
4. process environment  (deploy-time)
5. code defaults        (config.py)
```

`Settings.for_user(uid)` loads shared + user layers, validates each (secrets
present, tokens least-privilege), merges by the order above, sets
`settings.uid = uid` and `settings.data_dir = users_root/uid`. **Cost model
decision (open):** per-user `ANTHROPIC_*` in the user layer enables *bring-your-
own-key*, which cleanly isolates cost and rate limits.

### 4.3 UID & path safety

- UIDs are **opaque, immutable, generated** (e.g. ULID / 16-hex), validated
  `^[0-9a-z]{8,32}$`. Human display names are separate metadata, never in a path.
- Every path derivation does: `p = (users_root / uid).resolve()` then assert
  `p.is_relative_to(users_root.resolve())`; refuse symlinked components; reject
  any uid failing the regex **before** touching the filesystem.
- The registry is updated **atomically** (write-temp + `os.replace`, or under a
  registry lock); reads tolerate concurrent writers.

---

## 5. Target architecture

```
  WeChat/OpenClaw ┐     ┌───────────────────────────────────────────────┐
  (1 acct/user →  ├───▶ │ serve daemon (multi-tenant, ThreadingHTTP)     │─▶ handle_message(text, settings_uid)
   accountId→uid) │     │  auth → uid → UserContext(settings_uid)        │
  Email (per-user │     │  per-uid SessionStore · per-uid mutation lock  │─▶ enqueue(uid, job) ─┐
   mailbox→uid) ──┘     └───────────────────────────────────────────────┘                       │
                        ┌───────────────────────────────────────────────┐   in-process workers ◀┘
  cron/timer ─────────▶ │ scheduler: bounded fan-out over active users   │─▶ run(settings_uid)  (per-uid run.lock)
                        └───────────────────────────────────────────────┘   (no CLI spawns — §6)

  DATA_DIR/
  ├── shared/.env                       # shared infra secrets/config (+ bridge-token hash)
  ├── shared/jobs.db                    # GLOBAL admin-only worker queue (§6) — not per-user
  ├── registry.db|users.yaml            # roster: accountId/mailbox → uid (atomic writes)
  └── users/<uid>/  config.env  profile/(git)  events.db  sessions/  media/  runs/  state.json  {write,run}.lock
```

`UserContext = {uid, settings}`. Downstream code already accepts `settings`, so it
threads unchanged — *provided* §6–8 are honored.

---

## 6. Background jobs: durable internal worker queue (no *user* CLI) (Phase 0)

Today background work spawns CLIs — `_trigger_run` (`run`), `_run_phase`
(`run-phase`), `_execute_task` (`task`) all `Popen([… "assistant.cli", …])` with
**no identity**. A bare `PERSONAL_AGENT_UID` env var would be *propagation, not
authentication*. Dropping the **user** CLI surface (§1) lets us remove that
subprocess trust boundary — but the replacement must be a **durable** queue, not a
best-effort in-memory list.

**"No CLI" means no *user* CLI.** Admin commands — migration, user add/remove,
reboot — still use an **admin CLI** (`assistant admin …`) gated on an admin role;
those are operator tools, not tenant surfaces.

**Durable worker queue (spec, not a TODO):**
- **Storage & isolation: `DATA_DIR/shared/jobs.db`** — a **global, admin-only**
  SQLite queue (scheduling and fairness need one cross-user view; it is **not**
  per-user `events.db`). No tenant-facing endpoint can read it. **Payloads are
  minimized / redacted / encrypted** — a `task` arg can hold a sensitive user
  request, so store a reference or an encrypted blob, not plaintext.
- **Job = `(id, uid, kind, args_ref, state, attempts, dedupe_key, ts)`;** states
  `queued → running → done | failed | cancelled`. On daemon start, **recover**:
  `running` jobs with no live worker are requeued (idempotent kinds) or failed.
- **Delivery is *at-least-once*, not exactly-once.** A `dedupe_key`
  (`uid+kind+date`) blocks duplicate *enqueue*, but **cannot** undo a side effect
  that already happened (email sent, WeChat announced) if the process crashes
  **before** the job is marked `done` and is then replayed. Guard the side
  effects, not just the queue: an **outbox / delivery ledger** — record
  `(uid, kind, date)` as delivered **transactionally with / before** the send, and
  skip on replay (the seen-store already does this for digest *items*; extend it to
  the *delivery* itself). Residual risk: if the send succeeds but the ledger write
  fails, a duplicate is still possible — **state that duplicate delivery remains
  possible** rather than claiming exactly-once.
- **Per-user fairness & concurrency:** ≤1 pipeline run per uid (the per-user
  `run.lock`), a global worker-pool cap, round-robin across users (feeds §13
  quotas).
- **Cancellation = cooperative, never a thread kill.** Python worker threads can't
  be safely force-terminated, so jobs check a **cancellation token** at
  checkpoints (phase boundaries in the pipeline; per-step in the task loop). User
  deletion (§14) sets the token and **waits (bounded)** for the worker to yield;
  the escape hatch for a stuck worker is a daemon restart (recovery requeues). If
  hard-kill is ever required, run workers as **separate processes** instead
  (topology choice, Phase 0).

Workers hold the authenticated `UserContext` and call `run(Settings.for_user(uid))`
/ the task loop directly — no child process, no env var, no forgeable capability.
The chat actions `trigger_run` / `run_phase` / `execute_task` become
**enqueue-with-the-caller's-uid** (the handler already holds `settings.uid`); they
never name another user.

**Admin reboot restarts the shared daemon — a process can't restart itself.** The
admin `reboot` action **stops accepting new jobs, lets in-flight jobs reach a
checkpoint (or requeues them), then exits gracefully**, and OpenClaw's
`serve-supervisor` respawns it (confirmed: it "spawns and respawns `assistant
serve` with exponential backoff"). Define per deployment whether reboot **drains**
(wait) or **requeues** (mark `running→queued` for post-restart recovery).

Covered by a **job-context + durability test** in Phase 0: an enqueued job for B
touches only B's data dir and runs as no other uid; queued/running jobs survive a
simulated daemon restart; a duplicate enqueue is idempotent; and a crash *after*
delivery but *before* `done` does not re-deliver (ledger check).

### 6.1 No default-user fallback in multi-tenant mode

A missing/invalid identity must be **rejected**, never silently mapped to a
default user. Introduce an explicit deployment mode:

- `DEPLOYMENT_MODE=single_user` (legacy): a `DEFAULT_UID` exists; `Settings()` ≡
  `Settings.for_user(DEFAULT_UID)`; today's behavior, unchanged.
- `DEPLOYMENT_MODE=multi_tenant`: **no** `DEFAULT_UID`. Any request or queued job
  without a resolved uid (authenticated account/mailbox) is refused. The
  registry's existence implies multi-tenant; the fallback is guarded on the mode,
  not on "is a uid present".

---

## 7. Sessions & media isolation (Phase 1)

`serve.py` builds **one** `SessionStore` from the boot data_dir; per-request
`Settings` does not rebind it, so chat history would pool under the boot user.
Fix: resolve the `SessionStore` **per request** from the authenticated user's
`settings` (it's just a dir + caps — cheap to construct), and key session files
`uid:channel:sender`. Staged media (`data_dir/media/…`) is already data_dir-scoped
→ per-user once `data_dir` is; the curate/prune paths run per user. Test: two
users' interleaved `/chat` calls never see each other's history.

---

## 8. Same-user write serialization (Phase 1)

The stores' `_save` = write YAML + `git add/commit` with **no lock**, and the
daemon is a `ThreadingHTTPServer`; background jobs run on the internal worker
queue (§6, still concurrent with request threads). So a
chat action, a firing routine, a running task, and the daily pipeline can
**interleave git/YAML writes on the same user's repo** → corruption or lost
writes (a real hazard even single-user today). Add a **per-user mutation lock**:
a `flock` on `users/<uid>/write.lock` acquired across threads *and* processes.

Two requirements the naïve "lock inside `_save`" version gets wrong:

- **Lock the whole transaction, not just the write.** The hazard is a
  read-modify-write: `load()` → validate/dedup → mutate → `save()` + commit. A
  lock around `_save` alone still loses updates (two callers both `load()` the
  same state, then serialize only the writes). The lock is acquired at the
  **operation boundary** (the handler / store method that performs the full
  `load…commit`) and released after the commit.
- **Reentrancy & ordering (no deadlock).** One operation may touch several stores
  (e.g. a task logs finance *and* health, plus the profile repo). Use a **single
  per-user lock** held for the whole operation and made **reentrant** (or a
  per-user `RLock`/`flock` refcounted per process) so nested store calls reuse
  the held lock instead of re-acquiring. If ever more than one lock is needed,
  fix a **global acquisition order** (e.g. user-lock → repo-lock) to preclude
  cycles. Different users' locks are independent, so cross-user parallelism is
  unaffected.

(The existing `run.lock` only guards the whole daily run — insufficient.)
Alternative: a single-writer per-user queue that all mutations funnel through.

---

## 9. Logging & observability isolation

- Every log line and metric is tagged with `uid`; a request/job context sets it.
- **Secret redaction** in all log paths; never log another user's data — the
  prime directive covers *logging* too. A user's error digest must not surface
  another user's content.
- Per-user metrics in `events.db` (add a `uid` column / per-user db) for quotas,
  cost attribution, and an admin view.

---

## 10. Action taxonomy: per-user vs shared/admin

- **Per-user actions** (only touch that user's data — safe once §6/§8 hold):
  todos, finance, health, reminders, routines, `trigger_run`, `run_phase`,
  `execute_task`, `plan_task`, `web_search`, retrieval, learn/evolve.
- **Shared/admin actions** (affect the whole deployment): **`reboot`** restarts
  the shared daemon and disrupts *every* tenant → **remove from the per-user
  `llm` action set**; expose as `assistant admin reboot` gated on an admin role
  in the registry. Same for user add/remove, migration, quota changes.

---

## 11. Channels & the OpenClaw / WeChat gateway (the RFC's biggest gap)

v2 said "WeCom = the multi-tenant channel" and stopped there. But the channel
personal-agent actually ships on is **personal WeChat via OpenClaw**, which is a
different — and much harder — multi-tenant problem. This section covers it.

### 11.1 How the WeChat path works today (single-user)

Personal WeChat → **OpenClaw gateway** (Tencent iLink, the owner's **own**
QR-logged account — "a real client, not a ban-risk bot") → this repo's bridge
`openclaw-plugin/index.js` → HTTP `POST /chat` on the daemon. Salient facts (all
verified in code / `WECHAT_OPENCLAW.md`):

- The bridge authenticates to the daemon with **one shared `SERVE_TOKEN`** and
  passes only `{session, text, image_paths}`. It asserts **no sender identity** —
  the single-user daemon treats *every* inbound message as the owner.
- The `session` key is `oc:<conversationId ?? senderId ?? sessionKey>` — and the
  bridge notes **"weixin provides neither conversationId nor SenderId, so
  sessionKey is what keys memory."**
- OpenClaw also **supervises the daemon** and optionally runs the **daily cron**
  (`0 7 * * * → scripts/daily-run.sh`).
- Images are staged by the gateway to `~/.openclaw/media/inbound/<uuid>.jpg` and
  the path is handed to the daemon as `image_paths`.

### 11.2 The hard blocker: no authenticated per-sender identity

Multi-user routing **requires a stable, authenticated per-sender id**
(sender → uid). The current weixin integration does **not** reliably expose one —
only a `sessionKey`. Two consequences:

1. You cannot securely tell one user from another on a shared account. A
   `sessionKey` is a conversation handle, not an authenticated identity to map to
   a tenant's data and secrets.
2. Even if it distinguished conversations, a **single account DMing many
   strangers is exactly the "ban-risk bot"** the current design deliberately
   avoids — WeChat's anti-spam bans it, and one ban takes down **every** user.

**Therefore a *single shared account* is inherently single-user** — don't build
shared-account sender-routing on it. **But multi-user WeChat *is* achievable a
different way: one account per user, routed by `accountId`** (§11.3). Identity
then comes from *which account received the message* — authenticated by the
gateway/iLink login — instead of the missing per-sender id.

### 11.3 Multiple users, different WeChats — the viable model

OpenClaw is natively **multi-account**: each `channels login` binds one WeChat
account (the one that scanned), credentials live per account
(`~/.openclaw/credentials/<channel>/<accountId>/`), and — crucially — the plugin
**inbound event exposes `accountId`** (`docs/plugins/sdk-channel-inbound.md`).
OpenClaw even ships native per-account routing (`match: {channel, accountId}`).

So each user connects **their own** WeChat (QR-logs their own account), and the
daemon resolves `registry.by_channel("weixin", accountId) → uid`.

**Prerequisite spike — prove the weixin plugin actually supports this (Phase 0,
gates everything).** OpenClaw's *generic* SDK supports multi-account + optional
`accountId`, but the external `@tencent-weixin/openclaw-weixin` plugin is **not in
the checked source**, so distinct, stable account ids through its inbound path are
**unproven**. Before any registry work, run the spike (Appendix A.8): two accounts
→ distinct/stable `accountId`; survives restart + re-login; replies route to the
receiving account; the second login does **not** overwrite `default` credentials.

**`accountId` is the *receiving* account, not the *authorized sender*.** WeChat
DMs use OpenClaw pairing / `allowFrom` allowlists — so "the sender is inherently
you" from earlier drafts is **wrong**. Per account:
- Enforce an **owner-only sender allowlist / pairing policy** (only the account
  owner may command that tenant).
- Key identity on **`(accountId, senderId)`** where a sender id is available.
- If weixin exposes **no** sender id, **document explicitly** that *every* sender
  OpenClaw admits to that account receives the account owner's tenant privileges
  — so the allowlist/pairing *is* the security boundary.

**Ban blast-radius is per-user** — each account is a real personal client, so a
ban (still a ToS risk) hits only that one user.

**Mandatory OpenClaw config:** `session.dmScope = per-account-channel-peer`
(official requirement for multiple accounts; without it DMs collapse across
accounts). Add as config + an acceptance test.

Two code changes (small, localized):

1. **Bridge** (`openclaw-plugin/index.js`): **switch the intercept hook from
   `before_agent_reply` to `before_dispatch`** — `before_agent_reply` carries only
   `cleanedBody` and **no `accountId`**; `before_dispatch` exposes `accountId`
   (populated from the inbound channel context) and can still return
   `{handled:true, text}` to skip OpenClaw's model while delivering through the
   originating account. Read `accountId` + a canonical `sessionKey` there, pass
   **channel context** to the daemon on **every** tenant call, and scope media by
   `(accountId, sessionKey)` (Appendix A).
2. **Daemon/registry**: `accountId → uid` map (recorded at onboarding); build
   `Settings.for_user(uid)`. The `SERVE_TOKEN` authorizes the *bridge* to assert
   `accountId`s (§11.4); no other caller's account id is trusted.

**Topologies:**
- **B′ (recommended): one gateway, N accounts, route by `accountId`.** One Node
  process; natively supported; cheapest.
- **B (isolation-heavy fallback): one gateway per user, one account each.**
  Stronger process isolation; heavier; use if you hit a per-gateway account cap
  or want hard per-user process boundaries.

**The real costs (be honest):**
- **Per-user QR login + session renewal.** Each user scans a QR to log in their
  WeChat, and must **re-scan when the iLink session expires** (WeChat sessions
  aren't permanent). This is the main operational friction and can't be
  engineered away — it's inherent to personal-WeChat automation.
- **The host holds every user's live WeChat session**, and the single bridge
  token can impersonate **any** account. So the gateway + all loaded plugins are
  **inside the tenant-isolation boundary** (the TCB) — this is a **family/team
  self-host** posture, not an untrusted hosted service (which, with WeCom out of
  scope §1, we don't offer). For stronger isolation, run **one gateway + bridge
  credential per user** (topology B).
- **Proactive outbound needs per-user routing.** Replies piggyback on the inbound
  event, but **reminders, routines, task reports, and digest announces have no
  inbound route.** Each user's settings must carry `{channel, accountId, outbound
  target}`, and `send_wechat()` (already `--account`-aware) must send on **that
  user's** account. Acceptance test: user A's reminder **cannot** be delivered
  through user B's account.
- **Verify OpenClaw/iLink per-host account caps** before assuming N scales.

### 11.4 Trust model, *if* a shared account is ever used

Documented for completeness — not recommended (see 11.2):

- The bridge's `SERVE_TOKEN` would become **the authority to assert *any* user's
  identity** to the daemon — a privileged, high-value shared secret (leak ⇒
  impersonate anyone). It must be loopback-only, rotated, and treated as a bridge
  credential, not a per-user one.
- The bridge would have to pass the **authenticated sender** as a first-class,
  gateway-attested field (`sender:{channel:"weixin", id:<stable-wxid>}`); the
  daemon resolves `sender → uid` via the registry and **never** derives uid from
  the caller-controlled `session` string (session is memory-keying only).
- Staged-media isolation: the daemon must accept only the paths for *this*
  authenticated sender's current message, containment-check them, and copy into
  the resolved user's `media/` — never let one user reference another's staged
  file under the shared `~/.openclaw/media/`.

### 11.5 Gateway responsibilities move under multi-user

- OpenClaw's **command-cron daily trigger → replaced by the multi-user
  scheduler** (§12); the gateway no longer schedules per-user runs.
- Daemon **supervision/reaping stays** (shared, one daemon) — unchanged.

### 11.6 Recommended deployment shapes

- **A. One account per user, routed by `accountId` — THE design.** Detailed in
  **§11.3**. Each user connects their own WeChat; identity is the authenticated
  `accountId`; no cross-user ban blast-radius. Prefer **B′** (one gateway, N
  accounts); fall back to one gateway per user for hard isolation. Cost: per-user
  QR login + session renewal, and the host holds every user's live WeChat session
  (self-host-appropriate).
- **B. Email** (per-user mailbox, §11.7) — the second supported channel.
- ~~**WeCom**~~ — **out of scope by decision (§1).** Kept here only as "why we
  didn't": it *would* be the clean hosted multi-tenant channel (authenticated
  sender userid, no ban risk), but it's not wanted.
- ~~**Shared bot account + sender routing**~~ — **not viable** regardless:
  blocked by §11.2 (no authenticated per-sender id) and the ban blast-radius.

### 11.7 Other channels (unchanged from v2)

- **Email = per-user poller**: each user's own IMAP creds; the mailbox context is
  the identity (§4.1); watermark under their data dir; a bounded pool of pollers;
  `EmailChannel` reused with that user's `settings`.
- The single-inbox `chat_listener.pid` becomes one **daemon** lock (one
  multiplexing process), not a per-owner lock.

### 11.8 Recommendation

Two channels: **per-user WeChat (one account per user, routed by `accountId`,
§11.3)** and **email (per-user mailbox)**. Because the host holds each user's
live WeChat session + IMAP creds, this is a **self-host / trusted family-team**
posture, not an untrusted hosted service. Document the WeChat ban/ToS risk
(contained per-user) and the per-user QR-login + session-renewal operational
cost.

---

## 12. Scheduling the daily pipeline for N users

- A **bounded fan-out scheduler** replaces the single 07:00 cron: iterate
  `registry.active()`, enqueue `run(Settings.for_user(uid))`, run through a
  worker pool with **jittered start times** to avoid rate-limit spikes.
- Per-user `run.lock`/`state.json` already isolate runs; **failure isolation** so
  one user's broken collector/CI can't stall another's; per-user **timezone** for
  the "morning" run.

---

## 12b. Weekly self-evolution: three layers (implemented)

Every user of the deployment has **mutually authorized** using their traces and
chat history to improve the shared agent. The Sunday fan-out
(`scheduler.enqueue_weekly_jobs`, gated `weekly_day`/`weekly_hour`, idempotent
per ISO week) schedules:

1. **Personal** (per active user): `run_phase:consolidate` (profile editorial)
   + `evolve` (personal lessons `L*` from their own chats/tasks —
   preferences differ, so these never cross users and always take precedence).
2. **Global lessons** (one `global_evolve` job, GLOBAL_UID): mines all active
   users' sessions + task records + pipeline traces for **user-agnostic** rules
   → shared store (`shared/lessons/`, ids `G*`, cap 12) injected into every
   user's prompts before their personal block. Privacy: hard prompt constraints
   + a deterministic post-filter (no uids/display names/emails/channel ids in a
   rule); provenance kept in `why`, never rendered. Admin reviews via
   `assistant admin lessons list|retire`.
3. **Code/workflow self-improvement** (one `self_improve` job, GLOBAL_UID): the
   `scripts/self-improve.sh` harness — cross-user evidence brief → Opus edits in
   a throwaway worktree off origin/main → sensitive-path guard → full pytest →
   **PR only, never merged** (the human-review gate for self-modifying changes);
   artifacts under `shared/self-improve/`.

---

## 13. Scale, cost, secrets

- **LLM cost/rate limits are the dominant constraint** (N × daily pipeline + MoA
  + chat). Needs a **shared rate limiter**, **per-user quotas/budgets**, cost
  attribution by uid, and back-pressure. MoA (2×) is opt-in per user. BYO-key
  (§4.2) is the cleanest cost isolation.
- **Secrets at rest — timing is set by the threat model, not the phase number:**
  - *Hosted/custodial* (we hold many users' GitHub/email/WeChat creds):
    **encryption at rest is a launch requirement — Phase 1, not Phase 5.**
    Encrypted store (age/sops or KMS), decrypt in-memory per request, rotation &
    revocation, never logged.
  - *Trusted family/team self-host*: per-user `config.env` with strict perms is
    acceptable for v1; encryption can follow later.
  - Either way: least-privilege tokens (read-only GitHub collector; repo-scoped
    marks token — already the discipline, now per user), and the single
    **bridge token stored hashed in the daemon** (§4.1).
- **Process model:** multi-tenant single process (recommended, cheapest) vs
  process-per-user (stronger isolation, heavier). **Decided in Phase 0** — it
  shapes everything downstream. Phase 5 only *reassesses scaling*, it does not
  re-open this choice.

---

## 14. Migration, backward compatibility & deletion

- **Legacy single-user mode remains the default** (`DEPLOYMENT_MODE=single_user`,
  §6.1): with no registry, the system runs as today under `DEFAULT_UID`;
  `Settings()` ≡ `Settings.for_user(DEFAULT_UID)`. Switching to `multi_tenant`
  removes the fallback.
- **Reversible migration**: `assistant admin migrate-single-user <uid>` moves the
  current `DATA_DIR` into `users/<uid>/`, splits `.env` into shared + user layers,
  and is reversible (keeps a backup, dry-run first).
- **User deletion is an ordered protocol, not an `rm -rf`** (Phase 4). A running
  job could otherwise recreate files after deletion. Order:
  1. **Deactivate** in the registry (status → `deleting`) so no new request/job/
     login resolves to this uid.
  2. **Cancel & drain** active work: set the user's jobs to `cancelled` and their
     running job's **cancellation token**, then **wait (bounded)** for the worker
     to reach a checkpoint and yield (Python threads can't be safely force-killed,
     §6); stop their email poller. If a worker won't yield, the escape is a daemon
     restart (recovery requeues, and the now-`cancelled` jobs don't re-run).
  3. **Revoke credentials**: unbind the user's channel ids (WeChat `accountId`,
     mailbox) in the registry and drop their per-user secrets, so nothing can
     re-authenticate to this uid mid-delete.
  4. **Acquire** the per-user `write.lock` + `run.lock` (now uncontended).
  5. **Delete** the data dir (containment-checked path) and offer a prior
     **export** ("forget me" ⇒ export-then-delete); remove the registry entry
     atomically last.

---

## 15. Revised phased rollout

Each phase is shippable, backward-compatible, and ends with prime-directive
tests. **Security work is front-loaded, not deferred.**

> **Implementation status (as of this branch).** The full multi-tenant machinery
> is implemented behind `DEPLOYMENT_MODE` (default `single_user` = unchanged), with
> unit tests; single-user behavior is byte-for-byte the same. Landed:
> `Settings.for_user`/`uid`/`DEFAULT_UID` + `shared_dir` (§4); `uidsafe` opaque
> uid + path containment (§4.3); `UserRegistry` (accountId/mailbox→uid, one
> bridge-token hash, atomic writes — §A.1); authenticated `resolve_uid` with **no
> fallback** in multi_tenant + mandatory bridge token on **every** endpoint (§A.2);
> per-request per-user `SessionStore` + uid-scoped keys, and network image-paths
> refused (§7, §A.4); ContextVar-scoped tracer + MoA `copy_context` (§3); reentrant
> per-user `write.lock` (§8); the **durable SQLite job queue** with recovery /
> dedupe / per-user fairness / cooperative cancellation, the in-process **worker
> pool**, and the **DeliveryLedger** outbox (§6); the fan-out **scheduler** (§12);
> `reboot` removed from the tenant action set (§10); the **admin CLI**
> (`add-user`/`remove-user`/`list`/`bind-channel`/`set-bridge-token`/
> `migrate-single-user`/`reboot`), the **ordered deletion protocol** + export, and
> reversible single-user **migration** (§14); the bridge's `before_dispatch`
> accountId routing, **fail-closed** weixin handling, capped image **bytes**, and
> no-CLI fallback (§A.3, §A.5) — with a Node test suite; **per-user email
> pollers** (§11.7 — each user's own IMAP creds are the identity, per-user
> watermark/sessions, mailbox dedupe, failure isolation) ticked from the serve
> poll loop. **Still gated:** the **two-account weixin spike (§A.8)** must
> confirm stable per-account `accountId` at `before_dispatch` on real hardware
> before `multi_tenant` is enabled in production; per-user timezone, rate
> limiting, quotas, and encrypted secret storage (Phases 3/5) remain future
> work.

- **Phase 0 — foundations, threat model & process-model decision.** Write the
  threat model (§2); **choose the process model** (single multi-tenant process vs
  process-per-user — this shapes everything and is decided **only here**).
  Introduce `UserContext`, `Settings.for_user`, `DEPLOYMENT_MODE` (§6.1),
  **authenticated identity resolution** (WeChat `accountId`→uid via the bridge
  token; email mailbox→uid; never caller-/env-provided), the **internal worker
  queue** replacing CLI spawns (§6), UID/path validation (§4.3). **Hard gate: the
  two-account weixin spike (§A.8)** — no registry/routing work until it proves the
  plugin supplies stable distinct `accountId`s. Tests: authz, job-context, UID
  validation, no-fallback-in-multi-tenant, log redaction — *all here*.
- **Phase 1 — per-user storage, isolation & safety.** `DATA_DIR/users/<uid>/`;
  **per-request SessionStore + media** (§7); **ContextVar-scoped tracer** (§3);
  **full-transaction mutation serialization** with reentrancy/ordering (§8);
  shared/user config layers with explicit precedence (§4.2); reversible migration
  (§14). If **hosted/custodial**, **encrypted secret storage lands here** (§13).
  Tests: cross-tenant read/write isolation, session bleed, **trace-file
  isolation**, concurrent-write integrity.
- **Phase 2 — registry & channel routing.** `UserRegistry` (`accountId`/mailbox →
  uid, atomic writes; the **one bridge-token hash** — no per-user tokens);
  **WeChat `accountId`→uid routing** (bridge reads `ctx.accountId` at
  `before_dispatch`,
  media as bytes, drop the `"*"` cache fallback — App. A); per-user email
  (mailbox-context identity); unknown-account handling; admin-only reboot (§10).
- **Phase 3 — scheduler, rate limiting, failure isolation.** Bounded fan-out,
  jitter, per-user timezone, shared rate limiter, per-user failure isolation.
- **Phase 4 — onboarding & lifecycle.** `admin add/remove/list`; guided per-user
  connect + `--check`; **token rotation/revocation**; the **ordered deletion
  protocol** + export ("forget me") (§14).
- **Phase 5 — scale & hardening.** Quotas/budgets, admin observability, abuse
  enforcement, and **reassess scaling** against the Phase-0 process-model choice
  (reassess only — the choice itself isn't re-opened). Encrypted secret storage
  here **only** if the deployment is trusted self-host (else already in Phase 1).

---

## 16. Testing strategy

**Security tests are Phase 0/1, not the end.**

- **Prime-directive isolation (the top acceptance test):** interleave requests
  and queued jobs for users A and B on one daemon; assert B can never read,
  write, **log**, **trace**, or **deliver** anything of A's (paths, sessions,
  media, git, `trace.jsonl`, digests, WeChat/email targets).
- **Concurrent-run trace isolation:** run A's and B's pipelines concurrently
  in-process; assert each user's spans land only in their own `trace.jsonl`
  (guards the ContextVar-scoped tracer — a global tracer fails this).
- **Authorization & no-fallback:** token/sender→uid resolves to exactly one user;
  forged/absent identity refused; **no body- or env-provided uid is ever
  honored**; in `multi_tenant` mode a missing identity is **rejected**, not
  defaulted.
- **Queued-job identity & durability:** an enqueued `run/run-phase/task` runs
  under the caller's uid only (touches only that user's data dir); queued/running
  jobs **survive a simulated daemon restart** (durable SQLite queue); a duplicate
  enqueue is **idempotent** (no double digest); deletion cancels/drains a user's
  jobs (§6, §14).
- **UID validation:** traversal/symlink/oversized/malformed uids rejected before
  fs access; path containment holds.
- **Concurrent writes:** parallel same-user mutations serialize (no git/YAML
  corruption); different users proceed in parallel.
- **Migration:** single-user → `users/<uid>/` is behavior-preserving and
  reversible.
- Reuse the scratch-data discipline — tests never touch a live user's data dir.

---

## 17. Open decisions

1. **Process model** — single multi-tenant process (recommended) vs
   process-per-user. Decide in Phase 0.
2. **Cost model** — shared budget + quotas vs **BYO-LLM-key per user** (caps cost,
   isolates rate limits, simplifies §13). Leaning BYO-key.
3. **Email at scale** — how many per-user IMAP pollers one host runs before it
   needs pooling/batching (WeChat + email are the only channels, §1).
4. **Secrets custody** — encrypted vault vs self-hosted-per-user (product/legal as
   much as technical).
5. **MoA per user** — default off (2×), opt-in.
6. **Deployment posture — decided, not open.** The chosen WeChat topology
   (per-user accounts, host holds each session + creds, gateway/plugins in the
   TCB) is **trusted self-host only** (family/team). An untrusted custodial hosted
   service is **out of scope** — it would need WeCom (also out, §1). Left open
   only: *how large* a trusted self-host can grow before the TCB assumption
   strains.

---

### TL;DR

Scope: two channels — **per-user WeChat** (one OpenClaw account per user, routed
by authenticated `accountId`) and **email** (per-user mailbox); no WeCom, no CLI
(§1). `Settings` is the isolation seam and it's threaded everywhere, so the core
barely changes — **but** several things don't isolate on Settings alone and are
Phase 0/1 work: **background jobs run on an internal worker queue** (no CLI
spawns → no forgeable identity), the **SessionStore must be per-user** (not
boot-bound), the **module-global tracer must be ContextVar-scoped** (or
concurrent runs cross-write `trace.jsonl`), and **same-user writes must serialize
over the whole load→commit transaction**. Identity resolves to exactly one UID
from authenticated inputs only (`accountId` via the bridge token; mailbox
context) — never caller-, body-, or env-provided; in multi-tenant mode a missing
identity is **rejected** (no default fallback); UIDs are opaque and
path-validated; the bridge token is the only token (hashed); reboot is
admin-only; config is built from explicit validated layers; deletion is an
ordered drain-revoke-lock-delete protocol; and (if custodial) secrets are
encrypted at rest from Phase 1. Everything is measured against one acceptance
test: a request or job for B can't
read, write, log, trace, or deliver anything of A's.

---

## Appendix A — `accountId` → uid routing (implementation sketch)

Concrete changes to route per-user WeChat (§11.3). Sketch, not final code; file
refs are current. **Design rule (unchanged):** identity is *resolved* from
authenticated inputs — an `account_id` is only honored when the **bridge**
asserts it (holding the shared bridge token); email resolves from the mailbox
context; nothing else selects a user, and there is no default in
`multi_tenant` mode.

### A.1 Registry (accountId/token → uid)

```python
# registry.py  (users.yaml or a users table; the bridge token stored HASHED, §4.1)
class UserRegistry:
    def by_channel(self, channel: str, external_id: str) -> str | None:
        return self._by_channel.get((channel, external_id))          # ("weixin", accountId) -> uid
    bridge_token_hash: str        # the ONE privileged gateway secret (§11.4)
```

Onboarding records the binding when a user logs their WeChat in:
`openclaw channels login` → capture the resulting `accountId` →
`admin bind-channel <uid> weixin <accountId>`.

### A.2 Daemon: resolve uid, then build per-user context

In `serve.py` `do_POST` (`/chat`, `/actions/*`, `/run`), replace the single
`settings = settings_factory()` with an authenticated resolve:

```python
# Only the bridge holds a token (no per-user/CLI tokens — §1). WeChat is the only
# thing that comes through /chat; email is resolved in its own poller (below).
def resolve_uid(headers, body, reg, mode) -> str:
    tok = bearer(headers)
    if tok and consteq(sha256(tok), reg.bridge_token_hash):    # trusted bridge only
        acct = str(body.get("account_id") or "")
        if acct and (uid := reg.by_channel(str(body.get("channel", "weixin")), acct)):
            return uid                                         # WeChat: accountId → uid
    if mode == "single_user":
        return DEFAULT_UID                                     # legacy only (§6.1)
    raise Unauthorized()                                       # multi_tenant: no fallback

# Email needs no /chat auth: the per-user poller already ran with that user's
# mailbox creds, so it calls Settings.for_user(uid) directly (§4.1, §11.7).

# /chat handler (WeChat):
uid      = resolve_uid(self.headers, body, registry, settings_shared.deployment_mode)
settings = Settings.for_user(uid)                           # §4.2
sessions = SessionStore(settings.data_dir, keep=settings.serve_session_turns,
                        context_hours=settings.chat_history_max_age_hours,
                        retention_days=settings.chat_history_retention_days)  # §7 per-user, NOT boot-bound
skey     = f"{uid}:{body.get('session','default')}"         # uid-scoped session key
images   = _staged_images(body, settings, uid)             # §A.4
reply    = handle_message(text, settings, make_llm(settings),
                          history=sessions.history(skey), image_paths=images or None)
sessions.append(skey, noted, reply)
```

`handle_message`, `run`, and the stores need **no change** — they already take
`settings`. **Every tenant endpoint** must resolve the same way, not just `/chat`:

```python
# EVERY non-health endpoint resolves a uid — /chat, /actions/<name>, /run, AND
# /status (per-user status), plus any future route. Only /healthz is unauthenticated.
uid = resolve_uid(self.headers, body, registry, mode)      # was: settings_factory()
settings = Settings.for_user(uid)
result = run_action(name, body.get("params", {}), settings)    # /actions/<name>
# ... run_action("trigger_run", …, settings) enqueues with THIS uid (§6)
```

**Mandatory token in `multi_tenant`.** Today `_authorized` returns *open* when no
`serve_token` is set — that is a single-user default and **must not** carry over:
in `multi_tenant` an **absent or empty bridge token is rejected**, never "open
access". `resolve_uid` already refuses when the bridge token is absent/mismatched;
just make sure the gate can't be short-circuited by an unset token. Audit **all**
routes (`/status` included) — none may bypass `resolve_uid`.

### A.3 Bridge: `before_dispatch` (not `before_agent_reply`), read `accountId`

`before_agent_reply` carries only `cleanedBody` and **no `accountId`** — so the
intercept must move to **`before_dispatch`**, whose context exposes `accountId`
(from the inbound channel context) and can still short-circuit OpenClaw's model
(`{handled:true}` is terminal). In `openclaw-plugin/index.js`:

```js
const SAFE = "系统暂时不可用，请稍后再试 🙏";               // never leaks internals

api.on("before_dispatch", async (event, ctx) => {
  if (ctx?.channel !== "weixin") return;                   // (1) SCOPE: don't touch other channels
  // From here it's a weixin turn — we OWN it: every path returns {handled:true}
  // so a failure is a safe reply, NOT a fall-through to OpenClaw's own model.
  const accountId  = ctx?.accountId;
  if (!accountId) return { handled: true, text: SAFE };    // (2) FAIL CLOSED: no id → don't route it
  const sessionKey = ctx?.sessionKey;
  const body       = String(event?.body ?? event?.content ?? "").trim();  // NOT cleanedBody
  const images     = takeInboundMedia([`${accountId}::${sessionKey}`]);   // keyed by BOTH (§A.4)
  const chan       = { channel: "weixin", account_id: accountId, session: `oc:${accountId}:${sessionKey}` };
  try {
    const parsed = body.startsWith("/") ? parseSlash(body) : null;
    const result = parsed ? { ok: true, text: await handleSlash(parsed, chan) }
                          : await ask(body, chan, images);
    return { handled: true, text: (result?.ok && result.text) ? result.text : SAFE };
  } catch {
    return { handled: true, text: SAFE };                  // daemon down / bad response → safe, still handled
  }
});

async function ask(text, chan, imagePaths) {
  const b = { ...chan, text };                             // {channel, account_id, session, text}
  if (imagePaths.length) b.images = await encodeBase64Capped(imagePaths);  // §A.4 limits
  const data = await daemonPost("/chat", b, …);            // Bearer = the bridge token (throws on !ok)
  return { ok: !!data?.reply, text: data?.reply };
}
// handleSlash(parsed, chan) merges `chan` into the /actions/* & /run POST bodies.
```

Notes:
- **Fail *closed*, scoped to weixin.** For a weixin turn the bridge **always**
  returns `{handled:true, text}` — a missing/unknown `accountId`, an unreachable
  daemon, an auth failure, or an invalid response all yield a **safe error reply**,
  never a bare `return` (which would let OpenClaw answer with its own model and no
  tenant context — a leak). Other channels pass through untouched (step 1).
- **Payload fields:** `before_dispatch` exposes `event.body` / `event.content`
  (**not** `cleanedBody` — empty here) and has **no `trigger`** field (old guard
  dropped; filter non-user turns from other event metadata if needed — confirm in
  A.8).
- **Reply routing is automatic:** `{handled:true, text}` from `before_dispatch`
  delivers on the originating account.
- A.8 must confirm `ctx.channel`, `ctx.accountId`, and a stable `sessionKey` at
  `before_dispatch` for `@tencent-weixin/openclaw-weixin`.

### A.4 Media isolation — keyed by `(accountId, sessionKey)`, bytes, and capped

Two problems, two fixes:

- **Keying.** `accountId` alone is insufficient — one account can have concurrent
  conversations, so an image could attach to the wrong turn. Key the media cache
  by **`accountId + canonical sessionKey`** (prefer a stable message/run id if the
  spike A.8 shows one exists at both `message_received` and `before_dispatch`).
  **Drop the `"*"` fallback** — it binds an unmatched image to the *next* message
  regardless of account (cross-user leak).
- **Bytes, not paths, and capped.** In multi-user the daemon must never trust a
  caller-supplied filesystem path (traversal / cross-user reference). The bridge
  sends `images:[{media_type,data}]`; `_staged_images(body, settings, uid)`
  decodes into **that user's** `data_dir/media/` (existing path — scope to the
  resolved uid; stop accepting network `image_paths` in `multi_tenant`). Enforce
  **TTL + per-image size + MIME allowlist + count** limits **before** base64
  encoding (don't ship a 50 MB or non-image blob through the daemon).

### A.5 No CLI: background jobs on the internal queue

With the CLI surface dropped (§1, §6): the bridge's `askExec` CLI fallback is
**removed** (if the daemon is down the bridge just reports it — never a
default-user CLI run), and the daemon's background jobs (`run`, `run-phase`,
`task`) are **enqueued with the caller's uid** and run in-process by workers
holding the authenticated `UserContext` — no `Popen`, no capability to forge.
`reboot` is an **admin CLI** action; since a process can't restart itself, it
**exits the daemon gracefully** and OpenClaw's `serve-supervisor` respawns it (§6).

### A.6 Trust boundary recap

- **Bridge token** (`SERVE_TOKEN`): the **only** token — one high-privilege
  secret authorizing the bridge to assert *any* `account_id`. **Two-sided:** the
  daemon stores only its **hash** (verify), the **bridge holds the plaintext** in
  a protected secret store (to present it). Loopback-only, rotated. No per-user
  tokens exist (no CLI/HTTP surface, §1).
- **The gateway + all loaded plugins are inside the TCB.** With one gateway and
  one bridge token, that bridge can impersonate every account — acceptable only
  because gateway+plugins are trusted as part of the isolation boundary
  (family/team self-host). For stronger isolation: one gateway + bridge per user.
- **`account_id`**: honored *only* alongside the bridge token; never from an
  arbitrary caller or from the `session` string.
- **Per account, an owner-only sender allowlist/pairing** is the actual auth for
  who may command that tenant (§11.3) — `accountId` is the *receiving* account.
- No default uid in `multi_tenant`; no `"*"` media fallback; images as capped
  bytes; proactive outbound pinned to the user's own `accountId` (§A.9).

### A.7 Test hooks (Phase 1–2)

- Two accounts (`acctA→uidA`, `acctB→uidB`) interleaved on one daemon: each
  reply, **slash command** (`/actions/*`, `/run`), session file, and staged image
  lands only under its own uid.
- A `/chat` **or** `/actions/*` with a forged `account_id` but **no** bridge token
  → rejected; an absent `accountId` at the bridge → message not claimed.
- Media: keyed by `(accountId, sessionKey)` — an image from acct A never attaches
  to acct B's turn, and concurrent conversations on one account don't cross.
- **Media cache races (two of them):**
  1. **Same-turn ordering:** `message_received` is fire-and-forget, so
     `before_dispatch` for the *same* message may run **before** the image finishes
     caching → the turn misses its own image. Test this; mitigate (await the cache
     for that `(accountId, sessionKey)`, or reconcile a late image into the turn).
  2. **Cross-account:** a `before_dispatch` never picks up an image the concurrent
     `message_received` cached for a *different* `(accountId, sessionKey)`.
- **Proactive outbound:** user A's reminder/routine/task-report/digest is sent on
  A's `accountId` and **cannot** be delivered through B's account (§A.9).

### A.8 Phase-0 spike — prove `@tencent-weixin/openclaw-weixin` (gate)

> **✅ Spike run 2026-07-16 (single-account deployment) — PASSED with findings.**
> `ctx.accountId` present and equal to the registered account id
> (`accounts.json`) at both `message_received` and `before_dispatch`; text in
> `event.body`/`event.content` (`cleanedBody` absent, as predicted). **Findings
> that corrected the bridge:** the channel is `event.channel =
> "openclaw-weixin"` (full plugin id — there is **no `ctx.channel`**, only
> `ctx.channelId`), and `ctx.sessionKey` is **account-global**
> (`agent:main:main`) — per-peer session memory must key on
> `ctx.conversationId`. `ctx.senderId` is also present at `before_dispatch`.
> The *distinct-across-accounts* property still needs a second account when one
> joins; presence/stability/shape are verified.

**Do not start registry work until this passes.** The generic SDK ≠ this plugin.

1. Log in **two** WeChat accounts on one gateway.
2. Confirm **distinct, stable** `accountId` values at `before_dispatch` (and a
   usable `sessionKey`).
3. **Restart + re-login** → bindings/ids stay stable.
4. A reply routes back through the **receiving** account (not the other).
5. The **second login does not overwrite** the `default` account's credentials.
6. Confirm `session.dmScope = per-account-channel-peer` keeps DMs from collapsing
   across accounts.
7. **`senderId` availability + sender authorization.** Check whether
   `before_dispatch` exposes a stable `senderId`. `accountId` selects the *tenant*;
   an **allowlist/pairing policy authorizes the sender**. Test that an
   **unauthorized sender to an account is refused** (cannot command that tenant);
   if no `senderId` is available, record that *every* admitted sender gets the
   account owner's privileges (§11.3) so the allowlist is the only gate.

If any step fails, per-user WeChat is not deliverable on the current plugin —
stop and reassess (email-only, or contribute upstream).

### A.9 Proactive outbound routing

Replies ride the inbound event, but **reminders, routines, task reports, and
digest announces originate with no inbound route.** Each user's settings must
carry `announce_channel`, **`announce_account` = their own `accountId`**, and
`announce_to`. `notify.send_wechat()` already shells `--account`; make it use the
user's `settings.announce_account`. Test: A's reminder can never be delivered
through B's account (mismatched uid↔account is refused).
