"""CLI entry: the argparse definition and the subcommand dispatch. Each branch
either calls a `commands.cmd_*` handler or a subsystem entry (orchestrator,
init wizard, chat listener, HTTP server) and exits with its code. `main` is the
`assistant` console-script and `python -m assistant.cli` entry point.
"""

import argparse
import sys

from ..config import Settings
from .commands import (
    cmd_bootstrap,
    cmd_consolidate,
    cmd_enrich_profile,
    cmd_reading,
    cmd_resume_init,
    cmd_resume_status,
    cmd_run_phase,
    cmd_show,
    cmd_test_email,
    cmd_todo,
)


def _dispatch_admin(settings: Settings, args) -> int:
    """Run one `assistant admin …` operator command and print its result. These
    manage the roster / deletion / migration protocols a tenant can never invoke
    (§10, §14). Returns a process exit code."""
    from .. import admin

    try:
        if args.admin_cmd == "add-user":
            print(admin.add_user(settings, args.uid, args.display))
        elif args.admin_cmd == "remove-user":
            from pathlib import Path

            print(admin.delete_user(settings, args.uid,
                                    export_to=Path(args.export) if args.export else None))
        elif args.admin_cmd == "list":
            print(admin.list_users(settings))
        elif args.admin_cmd == "bind-channel":
            print(admin.bind_channel(settings, args.uid, args.channel, args.external_id))
        elif args.admin_cmd == "set-bridge-token":
            print(admin.set_bridge_token(settings, args.token))
        elif args.admin_cmd == "migrate-single-user":
            print(admin.migrate_single_user(settings, args.uid, dry_run=args.dry_run))
        elif args.admin_cmd == "reboot":
            from ..serve import reboot

            result = reboot(settings)
            print(f"[{result['status']}] {result.get('note', result.get('pid', ''))}")
            return 0 if result["status"] == "rebooted" else 1
    except (ValueError, KeyError, TimeoutError) as exc:
        print(f"admin: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    """Parse argv and dispatch to the matching command handler / subsystem
    entry, exiting with its return code."""
    parser = argparse.ArgumentParser(prog="assistant", description="Personal self-assistant agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="execute a daily run")
    run_p.add_argument("--dry-run", action="store_true", help="write digest to disk, send no email")
    run_p.add_argument("--resume", action="store_true", help="resume the last incomplete run")

    init_p = sub.add_parser("init", help="guided first-run setup (writes .env, "
                                         "validates each section, seeds the profile)")
    init_p.add_argument("--check", action="store_true",
                        help="no prompts — validate the current config and report")

    sub.add_parser("bootstrap", help="seed the profile from GitHub (first run only)")
    sub.add_parser("show-profile", help="print a summary of the current profile")
    sub.add_parser("send-test-email", help="verify email delivery")

    enrich_p = sub.add_parser("enrich-profile",
                              help="backfill the profile from GitHub history "
                                   "(authored + reviewed + commits + repo context)")
    enrich_p.add_argument("--since", default="2025-07", metavar="YYYY-MM",
                          help="start of the backfill window (default 2025-07)")
    enrich_p.add_argument("--include-comments", action="store_true",
                          help="also sweep commented-not-reviewed PRs/issues (noisier)")
    enrich_p.add_argument("--no-consolidate", action="store_true",
                          help="skip the final editorial consolidation pass")
    sub.add_parser("resume-init", help="clone/init the resume repo (Overleaf git bridge)")
    sub.add_parser("resume-status", help="show any resume update pending approval")
    sub.add_parser("approve-resume", help="push the pending resume update to the remote")

    todo_p = sub.add_parser("todo", help="manage todos: list / add / done")
    todo_p.add_argument("action", choices=["list", "add", "done"])
    todo_p.add_argument("value", nargs="?", help="title for add, id (t3) for done")
    todo_p.add_argument("--due", help="due date YYYY-MM-DD (add only)")

    reading_p = sub.add_parser("reading",
                               help="manage the reading list: list / done / unrelated")
    reading_p.add_argument("action", choices=["list", "done", "unrelated"])
    reading_p.add_argument("value", nargs="?", help="id (r3) for done/unrelated")

    chat_p = sub.add_parser("chat-listen",
                            help="answer owner messages from email/WeCom (foreground loop)")
    chat_p.add_argument("--once", action="store_true", help="single poll cycle, then exit")

    sub.add_parser("serve", help="local HTTP daemon: chat/actions/run endpoints "
                                 "for the OpenClaw bridge + email chat polling")

    reboot_p = sub.add_parser("reboot", help="restart the serve daemon so it "
                                             "reloads code (after a code update)")
    reboot_p.add_argument("--delay", type=float, default=0.0,
                          help="wait N seconds before stopping the daemon "
                               "(the chat action uses this so its reply sends first)")

    phase_p = sub.add_parser("run-phase",
                             help="run one standalone pipeline phase now")
    phase_p.add_argument("phase", choices=["research", "website", "todos", "resume",
                                           "curate", "consolidate"])

    cons_p = sub.add_parser("consolidate",
                            help="weekly profile consolidation: merge fragments, "
                                 "promote evidence into contribution highlights")
    cons_p.add_argument("--section", choices=["projects", "skills", "interests"],
                        help="consolidate one section only (default: all)")
    cons_p.add_argument("--dry-run", action="store_true",
                        help="show what would be applied, change nothing")

    sub.add_parser("evolve", help="self-evolve: distill behavior lessons from "
                                  "recent chats and task runs")

    task_p = sub.add_parser("task", help="agentically execute a multi-step task now")
    task_p.add_argument("request", help="the task, quoted")
    task_p.add_argument("--no-notify", action="store_true",
                        help="print the report only; skip the WeChat push")

    ask_p = sub.add_parser("ask", help="ask the chat agent one question locally")
    ask_p.add_argument("text", help="the message, quoted (may be empty with --image)")
    ask_p.add_argument("--image", action="append", default=[], metavar="PATH",
                       help="attach a local image (repeatable); described by the "
                            "vision chain so the agent can answer about it")

    admin_p = sub.add_parser("admin", help="operator tools (multi-tenant): manage "
                                           "users, channels, the bridge token, migration")
    admin_sub = admin_p.add_subparsers(dest="admin_cmd", required=True)
    au = admin_sub.add_parser("add-user", help="register a new active user")
    au.add_argument("uid")
    au.add_argument("--display", default="", help="human display name (metadata)")
    ru = admin_sub.add_parser("remove-user", help="delete a user (ordered §14 protocol)")
    ru.add_argument("uid")
    ru.add_argument("--export", metavar="PREFIX",
                    help="export the user's data to PREFIX.tar.gz before deleting")
    admin_sub.add_parser("list", help="list users, status, and bound channels")
    bc = admin_sub.add_parser("bind-channel", help="bind a channel id to a user")
    bc.add_argument("uid")
    bc.add_argument("channel", choices=["weixin", "email"])
    bc.add_argument("external_id", help="weixin accountId or email mailbox address")
    st = admin_sub.add_parser("set-bridge-token", help="store the bridge token hash")
    st.add_argument("token")
    mig = admin_sub.add_parser("migrate-single-user",
                               help="fold the current single-user DATA_DIR into users/<uid>/")
    mig.add_argument("uid")
    mig.add_argument("--dry-run", action="store_true", help="print the plan only")
    admin_sub.add_parser("reboot", help="restart the shared daemon (graceful; "
                                        "serve-supervisor respawns it)")

    args = parser.parse_args()
    settings = Settings()

    if args.command == "admin":
        sys.exit(_dispatch_admin(settings, args))

    if args.command == "init":
        from ..init_wizard import run_init

        sys.exit(run_init(settings, check_only=args.check))
    elif args.command == "run":
        from ..orchestrator import run

        sys.exit(run(settings, dry_run=args.dry_run, resume=args.resume))
    elif args.command == "bootstrap":
        sys.exit(cmd_bootstrap(settings))
    elif args.command == "show-profile":
        sys.exit(cmd_show(settings))
    elif args.command == "send-test-email":
        sys.exit(cmd_test_email(settings))
    elif args.command == "enrich-profile":
        sys.exit(cmd_enrich_profile(settings, args))
    elif args.command == "resume-init":
        sys.exit(cmd_resume_init(settings))
    elif args.command == "resume-status":
        sys.exit(cmd_resume_status(settings))
    elif args.command == "approve-resume":
        from ..tasks.resume import approve_resume

        sys.exit(approve_resume(settings))
    elif args.command == "todo":
        sys.exit(cmd_todo(settings, args))
    elif args.command == "reading":
        sys.exit(cmd_reading(settings, args))
    elif args.command == "chat-listen":
        from ..chat.service import run_listener

        sys.exit(run_listener(settings, once=args.once))
    elif args.command == "serve":
        from ..serve import run_serve

        sys.exit(run_serve(settings))
    elif args.command == "reboot":
        from ..serve import reboot

        result = reboot(settings, delay=args.delay)
        print(f"[{result['status']}] "
              + (f"daemon pid {result.get('pid')} healthy"
                 if result["status"] == "rebooted" else result.get("note", "")))
        sys.exit(0 if result["status"] == "rebooted" else 1)
    elif args.command == "run-phase":
        sys.exit(cmd_run_phase(settings, args.phase))
    elif args.command == "consolidate":
        sys.exit(cmd_consolidate(settings, args))
    elif args.command == "evolve":
        from ..actions import run_action

        print(run_action("self_evolve", {}, settings))
        sys.exit(0)
    elif args.command == "task":
        from ..task_runner import run_task

        record = run_task(args.request, settings, notify=not args.no_notify)
        print(f"[{record['status']}] {record['id']} — {len(record['steps'])} step(s)")
        print(record["report"])
        sys.exit(0 if record["status"] == "done" else 1)
    elif args.command == "ask":
        from ..chat.agent import handle_message

        print(handle_message(args.text, settings, image_paths=args.image or None))
        sys.exit(0)
