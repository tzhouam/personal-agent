# Runtime rewrite validation

Validated in the isolated implementation checkout; no production deployment or
real Slack/QQ message was performed. The verification credential was supplied
through a non-echoing process input, held in memory, and discarded after use.
It is absent from source, configuration, test fixtures and this report.

## Automated checks

- Original baseline: 854 passing tests.
- Final full suite: `python -m pytest -q` — **1011 passed in 83.96 seconds**.
- `pip check`, `compileall`, and `git diff --check` passed.
- Built a wheel and installed it with dependencies into a fresh virtualenv.
  Imports resolved from its `site-packages`, not the editable checkout.
- The installed wheel contains all 28 SKILL.md assets: 14 built-in family guides
  and 14 retained operational runbooks. Its catalog exposes 53 executable skills
  and 14 documentation-only procedures.
- CLI checks exercised skill discovery, project listing and durable memory write/
  retrieval against scratch data directories.

The regression suite covers provider wire protocols and bounded subprocesses;
Slack/QQ authorization and normalization; a local OneBot WebSocket server;
end-to-end channel ingress, reply delivery and restart deduplication; exact scoped
approvals; project execution/evidence/cancellation; tenant isolation; existing
HTTP/email/WeChat delivery; and legacy data/skill compatibility.

## Direct live DeepSeek verification

Used the official `https://api.deepseek.com` endpoint and `deepseek-flash` model,
which the official documentation identifies as DeepSeek V4.1 Flash. Only synthetic
facts and a temporary coding project were provided to the model.

| Check | Actual outcome |
| --- | --- |
| Authenticated model discovery | `deepseek-flash` present |
| OpenAI-compatible adapter | Exact marker response, terminal `stop` |
| Anthropic-compatible adapter | Exact marker response, terminal `end_turn` |
| Kernel memory tools | Model called `memory_remember`; a fresh kernel and different session retrieved the sourced fact |
| Dynamic project execution | Model read a broken arithmetic module and tests, changed only `calc.py`, and returned a report |
| Independent project verification | Real Python unittest command exited 0; runtime recorded `verified: true` and artifact hashes |
| Retained procedure | Created a draft from the successful verification attestation, rechecked proof, and activated the instruction-only skill |

The temporary project started with subtraction in an `add(a, b)` implementation.
Its unchanged tests asserted `add(2, 3) == 5` and `add(-1, 1) == 0`. The API-backed
workspace tool loop corrected the implementation; the host independently ran
the tests. The proof records host verification honestly as `trusted-host`, not
as a sandbox supplied by the model API.

## Verification boundaries

- At the user's final direction, live verification used **direct DeepSeek only**.
  Codex and other CLI adapters were verified with protocol/process fixtures.
- An earlier Codex authentication attempt failed because its ChatGPT refresh
  token was stale; the pending device login ended. No later Codex verification
  was performed after the user prohibited it.
- No actual Slack workspace, QQ account, production memory store, email inbox,
  website or résumé repository was connected or modified.
- Native Slack/QQ attachments are metadata-only. Existing image-aware bridge
  behavior requires its separately configured vision route.
- Existing reminder/routine delivery retains its configured outbound integration;
  native Slack/QQ project completion delivery is covered by the new runtime.

Primary references: [DeepSeek API setup](https://api-docs.deepseek.com/quick_start/agent_integrations/),
[runtime guide](RUNTIME_V2.md), [implementation checklist](../REWRITE_TODO.md).
