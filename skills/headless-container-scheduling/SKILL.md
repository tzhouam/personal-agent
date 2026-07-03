---
name: headless-container-scheduling
description: Scheduling daily agent runs in a container without systemd/cron (PID 1 = tini), and working with permission classifiers that block persistent daemons or credential-bearing test commands
trigger: systemctl says "offline"/fails, crontab is missing, or an agent-launched background daemon / credential curl is denied by a permission classifier
modules: [ops]
status: active
created_at: 2026-07-02
last_used_at: 2026-07-03
run_count: 0
---

## Diagnose
- `ps -p 1 -o comm=` → `tini` (or similar): you're in a container; there is no
  systemd (`systemctl is-system-running` → offline) and often no crontab
  binary. Timer units will never fire here.
- Separately, agent sandboxes/permission classifiers may deny (a) launching
  detached long-lived daemons (persistence) and (b) ad-hoc `curl` commands that
  ship a live credential to an endpoint whose name doesn't match the key.

## Fix
1. Ship BOTH scheduling paths: systemd units for real hosts
   (`systemd/personal-agent.{service,timer}`) and a fallback loop script
   (`scheduler.sh`: sleep-until-07:00-HKT loop, pid-file guard, log file,
   `run || run --resume`).
2. For `Type=oneshot` retries, note `$EXIT_STATUS` is NOT available in
   `ExecStartPost` — chain in ExecStart: `sh -c 'run || run --resume'`.
3. If the classifier blocks starting the daemon, don't work around it — hand
   the owner the exact one-liner (`nohup ./scheduler.sh &`) and say why. The
   denial is about who authorizes persistence, not about the script.
4. Credential test calls: exercise the credential through the application's own
   code path (SDK with base_url from the owner's .env) instead of a raw curl
   with the key pasted into the command line.
5. Full daemon set (as of 2026-07-03) — everything dies with the container;
   bring it all back with:
   ```bash
   nohup /rebase/personal-agent/scheduler.sh >/dev/null 2>&1 &                          # daily digest, 07:00 HKT
   nohup /rebase/.venv/bin/assistant chat-listen >> ~/.personal-agent/chat.log 2>&1 &   # email chat listener
   nohup ~/.openclaw/start-gateway.sh >> ~/.openclaw/logs/gateway-nohup.log 2>&1 &      # WeChat gateway
   ```
   The gateway process is titled `openclaw` (not "openclaw gateway"):
   restart it with `pkill -x openclaw` + the third line. Details:
   `doc/WECHAT_OPENCLAW.md` and the `wechat-openclaw-bridge` skill.

## Verification
`bash scheduler.sh` (foreground) logs "scheduler started"; a second copy exits
with "already running". On a systemd host: `systemctl list-timers` shows the
next 07:00 firing.

## Anti-patterns
- Installing cron inside a container image at runtime — it dies with the
  container and surprises the next rebuild.
- Sleeping in round wall-clock increments without a pid-file guard — restarts
  double-fire.
- Retrying a classifier-denied command verbatim or laundering it through
  another tool — surface it to the owner instead.
