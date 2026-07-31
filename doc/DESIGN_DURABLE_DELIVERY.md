# Durable delivery (Track D) — design

Charter: the 2026-07 audit's Track D (findings F3, F9, F14, F18; F20b deferred).
This doc is the reviewed design the implementation follows; the binding
invariants were agreed in the 10-revision audit plan and its review record.
Rev 2 applies the design-review round: `outbox.db` (the name `delivery.db` is
already owned by the digest DeliveryLedger — no collision, no migration);
explicit `executing` states with claim tokens + CAS for email AND routines
(no implicit exactly-once claim: a turn interrupted mid-processing
dead-letters honestly instead of silently rerunning its actions); atomic
tmp+replace for every YAML/JSON store this design touches (reminders.yaml's
`_save` today is a bare write_text — fixed as part of D3); per-row
claim-immediately-before-send for reminders; durable receipt ids persisted
with the email outbox; reversible display ids; a system_notes table; narrow
corruption recovery; open failures exempt from pruning and from UIDVALIDITY
resets; softened guarantee wording.

## 0. The guarantee (exact wording)

For **owner-conversation pushes** — email chat replies (D2), routine output
(D4), reminder pushes (D3) — the product guarantee is **no-silent-loss**:
*durable bounded delivery attempts, plus a guaranteed owner-visible terminal
failure*. Precisely: every message persists intent before its first send
attempt and completion after; a crash between the two re-offers the send
(duplicates are accepted, loss is not); retries are bounded, then the failure
dead-letters into a surface (D5) that **remains eligible for insertion into
every future transport-accepted interaction** until it expires (48h after
first actually shown) or is acknowledged — nothing can guarantee the owner
LOOKS; the guarantee is durable eligibility + receipt-gated expiry. "Sent"
means transport acceptance, never proven owner visibility.

Declared exclusions (unchanged from the audit plan): the daily digest keeps its
`DeliveryLedger` claim-before-send loss window; interactive late replies stay
best-effort; the IMAP UIDVALIDITY-reset window may drop mail (logged AND
dead-lettered as a visible note); routine *execution* is at-most-once.

## 1. Storage (D1 primitives)

One NEW SQLite database per user, `data_dir/outbox.db`, owned by
`platform/delivery.py`. (NOT `delivery.db` — that file already belongs to the
digest `DeliveryLedger` (jobs.py) and its dedup state must never be touched
by this subsystem's lifecycle, least of all its corruption recovery.)

- **Connection policy:** WAL journal mode, `busy_timeout=5000`, foreign keys
  off (no cross-table FKs by design), one connection per operation
  (open→transact→close, matching `events.db` usage patterns; the per-user
  write lock already serializes writers within a process, WAL+busy_timeout
  covers the multi-thread daemon).
- **Schema versioning:** a `meta(key, value)` table carries `schema_version`;
  migrations run at open when behind, inside one transaction.
- **Corruption recovery — narrow by design:** only a
  `sqlite3.DatabaseError` whose message marks real corruption ("file is not
  a database" / "database disk image is malformed") moves the db (with its
  `-wal`/`-shm` sidecars) aside to `outbox.db.corrupt-<ts>` and recreates it,
  writing a `system_notes` row so the owner learns state was lost. Lock
  contention, permission errors, and schema surprises PROPAGATE — they are
  bugs or environment problems, not corruption. Fail-open for the pipeline
  (a broken ledger must not stop chat), fail-loud for the owner.
- **Permissions:** created 0600 (owner-only, like every other store).
- **Backups:** the db holds *transient* delivery state (pending/outbox rows
  age out); it is deliberately NOT in the profile git repo and not backed up —
  a lost db loses at most in-flight retries, which the failure surface says
  out loud on recreation.

Why one transactional store per producer: the audit rounds established that a
lock-coordinated *pair* of files cannot commit atomically — ledger state and
its frontier must move in one transaction. Where a producer's state already
lives in ONE atomically-replaced file (reminders.yaml), that file IS its
transactional store and stays authoritative (see D3) — migrating it into
SQLite would add a risky data migration for zero atomicity gain.

## 2. D2 — email chat ledger (F3)

Today: `poll()` advances a scalar watermark (`chat_state.json`) before
fetch/parse; a failed turn = the owner's mail silently ignored forever.

### Table

```
email_ledger(
  uidvalidity INTEGER NOT NULL,
  uid         INTEGER NOT NULL,
  state       TEXT NOT NULL,   -- pending | executing | processed | acked | ignored | dead
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  reply       TEXT,                   -- outbox: the composed reply (processed→acked)
  surfaced_ids TEXT,                  -- JSON: failure ids embedded in `reply` (D5 receipts)
  summary     TEXT,                   -- sender + subject snippet for the failure surface
  claim_token TEXT,
  created_at  TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (uidvalidity, uid))
meta: email_uidvalidity, email_baseline_uid, email_shadow_uid
```

**Idempotency key:** `(UIDVALIDITY, UID)`. **States:**

| state | meaning | next |
|---|---|---|
| pending | discovered, not yet processed | executing / ignored / dead |
| executing | handle_turn about to run (actions possible from here); claim_token set | processed / pending* / dead |
| processed | turn ran; reply + surfaced_ids persisted (outbox) | acked / dead |
| acked | reply send transport-accepted | terminal |
| ignored | parsed to None (wrong sender / non-prefixed) — settled | terminal |
| dead | attempts exhausted OR turn interrupted → failure surface | terminal (ack via D5) |

*a turn that raises CLEANLY returns to `pending` (attempts+1, retried next
cycle, bound 3). A STALE `executing` row (claim_token from a dead process —
CAS-checked) means the turn may have half-run its actions: it is **never
rerun**; it goes `dead` with reason "interrupted mid-processing — may have
partially run" (no implicit exactly-once claim; honesty over retry). Fetch
failures don't consume attempts (the row stays `pending` untouched);
parse-to-None inserts `ignored` directly.

**Processing vs delivery are separate:** actions execute inside the turn, so a
send-retry must never rerun it. `processed` rows retry **the send only**, from
the persisted reply (receipt ids ride along in `surfaced_ids`, so a
post-restart successful retry still marks its embedded failures surfaced).

**Coordinator contract:** `EmailChannel.poll()` stops advancing any watermark;
each message dict carries `uidvalidity`/`uid`. The serve loop (single poll
thread — natural quiescence) drives: discover (insert `pending` rows for new
UIDs; parse-to-None inserts `ignored` directly), then process rows ascending by
UID, **halting behind the first nonterminal UID** ("contiguous" = among
discovered UIDs; IMAP UID sequences have gaps). Poison bound: one attempt per
poll cycle, `attempts >= 3` → `dead` — a permanently failing message blocks
younger mail ≤3 cycles. Fetch failures leave `pending` untouched (no attempt
consumed); turn failures and send failures each bump `attempts`.

**Cutover:** on first ledger use, import the existing `chat_state.json`
watermark as `email_baseline_uid` paired with the mailbox's **current**
UIDVALIDITY (the old watermark recorded none — if the mailbox was reset
before the import without anyone noticing, the import detects it by the
mailbox's highest UID being BELOW the watermark and re-baselines to the
current highest with a `system_notes` row: skip-not-replay, said out loud;
a preparatory double-release to pre-record UIDVALIDITY is deliberately
rejected for this single-operator deployment). On a later UIDVALIDITY
change: old-epoch rows are **retained** (the composite key namespaces them;
open `dead` rows keep their failure-surface eligibility), and the new epoch
re-baselines to the mailbox's current highest UID with a `system_notes` row
(the declared exclusion, made visible). **Rollback shadow:** after each
transaction, `email_shadow_uid` (= highest contiguous settled UID) is also
written to `chat_state.json` via atomic tmp+replace (its writer today is a
bare write — fixed here); ledger-commit→shadow-write ordering means a crash
window can only make old code *re-answer* settled mail (accepted duplicate),
never skip mail. Rollback loses only pending-retry state.

## 3. D3 — reminders: claim tokens + honest completion (F14)

Today: `deliver_due` persists `sent_at` BEFORE sending — a crash between claim
and send records a delivery that never happened.

**Storage decision:** `reminders.yaml` stays authoritative — a single file
under the existing `_path_lock` needs no cross-store atomicity — but its
`_save` today is a bare `write_text` (a crash mid-write corrupts the file):
D3 makes it atomic tmp+`os.replace`, 0600, as part of this change. No
migration, no dual-write, no rollback gate.

**Changes:** logical idempotency key = `reminder_id`; each due reminder is
claimed **individually, immediately before its own send** (the old code
claimed the whole batch up front, so serial sends could outlive any lease):
claim writes `{claimed_at, claim_token}` (uuid per attempt — a **fencing
token**, not an identity); `sent_at` is written only **after** the send
returns, and only when, under the lock, the row's `claim_token` still equals
the claimant's AND `sent_at` is still empty AND the row wasn't cancelled
(cancellation clears the token, invalidating in-flight claimants). Stale
claims (claimed_at older than the lease, no sent_at) re-offer. **Lease:**
`max(2 × chat_poll_seconds, 2 × 90s send timeout)` ≥ 3 minutes — per-row
claiming means one lease covers exactly one send. Duplicate over loss
(declared).
The existing bounded-retry → `sent_at="failed"` dead-letter flow is unchanged;
D5 adds `failed_at`/`surfaced_at`/`acked_at` fields to failed rows.

Rollback: old code ignores the new fields (it reads `sent_at` only); its
claim-before-send behavior returns, which is the pre-D3 status quo.

## 4. D4 — routine per-occurrence ledger (F9)

Today: `fire_due` claims (marks `last_checked`), runs the TASK, and sends in
one motion — output is lost on send failure, claimed routines vanish on
mid-loop death, and nothing records what happened.

### Table

```
routine_ledger(
  routine_id  TEXT NOT NULL,
  occurrence  TEXT NOT NULL,          -- system-local wall clock "YYYY-MM-DD HH:MM"
  state       TEXT NOT NULL,
  claim_token TEXT,                   -- fencing: transitions CAS on it
  output      TEXT,                   -- persisted BEFORE delivery is attempted
  error       TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  claimed_at  TEXT, updated_at TEXT NOT NULL,
  surfaced_at TEXT, acked_at TEXT,
  PRIMARY KEY (routine_id, occurrence))
system_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL,
             failed_at TEXT NOT NULL, surfaced_at TEXT, acked_at TEXT)
```

**Idempotency key:** `(routine_id, occurrence)`; the occurrence is the
scheduled wall-clock instant in system-local time (schedulers are system-local
by design; DST wall-times that repeat/skip resolve to the wall-clock string —
accepted). The key is an immutable snapshot: editing a routine's schedule
affects future occurrences only.

**States** (all transitions in single transactions):

| state | set when | next |
|---|---|---|
| claimed | occurrence claimed this cycle | executing / cancelled |
| executing | TASK about to run (side effects possible from here) | executed / execution_failed / condition_false |
| condition_false | WHEN held but CONDITION didn't | terminal |
| executed | TASK done, `output` persisted | delivered / delivery_failed |
| delivered | send transport-accepted | terminal |
| delivery_failed | send attempts exhausted (3) → failure surface | terminal (ack via D5) |
| execution_failed | TASK raised cleanly → failure notice delivered instead | delivered / delivery_failed |
| cancelled | routine cancelled/retired mid-flight | terminal |
| execution_unknown | stale `executing` from a dead process — side effects may have started; NEVER retried; failure surface | terminal (ack) |

Every transition CASes on `claim_token` (a reclaimed occurrence mints a new
token; the displaced claimant's writes are rejected — "single poll thread"
does not survive process replacement plus lease recovery, which is exactly
what stale recovery exists for). The WHEN/CONDITION check runs in `claimed`
(side-effect-free reads) so a crash during it re-claims instead of becoming
`execution_unknown`; only the TASK runs inside `executing`. Recovery each
cycle: stale `claimed` (older than one poll interval, token CAS) is
**re-claimable**; stale `executing` → `execution_unknown`.
`executed`/`execution_failed` with undelivered output retry **delivery only**
(one attempt per cycle, bound 3). Missed occurrences: an occurrence is only
minted when `claim_due` fires on its day — a daemon down across a scheduled
time simply never mints it (pre-existing semantics, recorded as a non-goal:
the ledger tracks fired occurrences, not counterfactual ones).

**Rollback shadow (ledger-first):** the SQLite claim commits first, then
`last_checked` is written into `routines.yaml` via atomic tmp+replace (the
old-code guard; its writer gains the same atomicity fix as reminders). Crash
between the two: new code recovers via the ledger; rolled-back old code may
re-run that single occurrence (bounded, documented — the reverse order made
the crash window unrecordable, which is worse). Startup heals any claim
lacking its shadow. Retention: terminal rows older than 30 days are pruned by
the curate phase; `output` is capped at 4KB (truncated with a marker).

## 5. D5 — one failure surface + acknowledgment (F18)

**Derived, not duplicated:** the surface is a read API,
`delivery.open_failures(settings)`, aggregating the producers' own stores —
email `dead` rows, routine `delivery_failed`/`execution_unknown` rows,
reminder failed rows (+ `system` notes). No separate failure store exists, so
there is no cross-store handoff window.

- **Typed display ids — fully reversible:** `dfe<uidvalidity>-<uid>` /
  `dfo<routine_id>@<occurrence with ' '→'T'>` / `dfrem<id>` / `dfs<n>` —
  every id parses back to exactly one producer row (the `df` prefix cannot
  collide with the existing rt/rem/t/r id families the chat already uses).
- **Deterministic presentation:** the block is prepended to the outgoing
  reply **in code** (never model-dependent), rendered under a strict
  indivisible ≤512-byte cap (headline + up to 3 entries + "…and N more"), so
  F10's chunking always lands it entirely in part 1.
- **Receipt rule:** `surfaced_at` is set only when the caller that observes
  transport success reports it back — `TurnResult.surfaced_failure_ids`
  carries the shown ids; the serve send sites (and the /chat 200 write) call
  `delivery.mark_surfaced(ids)` after the send returns. The email outbox
  persists `surfaced_ids` next to the reply, so a post-restart delivery
  retry still reports the receipts its stored reply embeds. Scope: the block
  rides CHAT replies (serve loop, /chat, email replies) — routine/reminder
  pushes don't carry it (they are themselves producers). An unseen failure
  never starts its expiry clock.
- **Predicate:** unacknowledged AND (never surfaced OR first surfaced < 48h
  ago) — the block always expires 48h after it was actually delivered, never
  before, never permanently.
- **Acknowledgment:** a new llm-exposed registry action
  `acknowledge_failure(id)` sets `acked_at` on the producer row (distinct from
  cancellation; audit history kept). "知道了/别再提醒那条" → the model emits it.

## 6. Deployment / rollback matrix

| phase | new state | old-code behavior after rollback |
|---|---|---|
| D1 | outbox.db exists | ignored entirely |
| D2 | email ledger + shadow | reads chat_state.json shadow; re-answers ≤ the unsettled window; never skips |
| D3 | extra reminder fields | ignored (reads sent_at only); pre-D3 semantics return |
| D4 | routine ledger + shadow | reads last_checked; may re-run one crash-window occurrence |
| D5 | ack/surfaced fields + action | fields ignored; the old context block returns |

Phases land in order but each is separately revertible; activation needs no
flags — each producer switches when its code lands (single poll thread =
natural quiescence for D2's baseline import).

## 7. Explicit non-goals (recorded)

Unbounded at-least-once retries; durable interactive late replies; a unified
outbox replacing the digest DeliveryLedger; F20b registry locking/migration;
WeCom media intake; cross-user anything. Each is a recorded follow-up, not an
accident.
