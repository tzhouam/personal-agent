"""Default runtime CLI; legacy workflow commands remain explicitly available."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys

from assistant.platform.config import Settings
from assistant.platform.runtime.config import load_runtime_config
from assistant.platform.runtime.contracts import BackendRequest
from assistant.platform.runtime.inbox import Inbox
from assistant.platform.runtime.pi import build_pi_backend, install_pi, pi_status
from assistant.agent.runtime.app import RuntimeApplication

COMMANDS = {"ask", "chat", "task", "serve", "project", "skills", "memory", "backends",
            "doctor", "approve", "inbox", "pi"}


def handles(argv: list[str]) -> bool:
    """Only replace deliberate runtime entrypoints; preserve old workflow commands."""
    return bool(argv and argv[0] in COMMANDS)


def _emit(value) -> None:
    """Machine-readable output makes project/skill receipts reusable from scripts."""
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _common(parser):
    """Keep every operator command usable against an isolated data/config root."""
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config", type=Path, help="runtime TOML configuration")
    parser.add_argument("--backend", help="exact named backend route")
    parser.add_argument("--owner", default="default", help="registered owner in multi-user mode")
    parser.add_argument("--session", default="cli", help="conversation/approval scope")


def _parser() -> argparse.ArgumentParser:
    """Parse project work separately from chat and trusted skill invocation."""
    parser = argparse.ArgumentParser(prog="assistant", description="Personal agent runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("ask", "chat"):
        p = sub.add_parser(command, help="one durable conversational agent turn")
        p.add_argument("text")
        p.add_argument("--image", action="append", default=[])
        p.add_argument("--project", default="", help="project memory context")
        _common(p)
    task = sub.add_parser("task", help="execute a task or work in a registered project")
    task.add_argument("request", nargs="?", default="")
    task.add_argument("--project", default="")
    task.add_argument("--background", action="store_true", help="queue for assistant serve")
    task.add_argument("--approved-id", default="", help="consume an exact pending approval")
    task.add_argument("--no-notify", action="store_true", help="compatibility: results stay in CLI/history")
    _common(task)
    serve = sub.add_parser("serve", help="run Slack/QQ, project workers and the existing loopback bridge")
    _common(serve)
    project = sub.add_parser("project", help="register, execute and inspect personal projects")
    project.add_argument("operation", choices=["register", "list", "run", "status", "cancel", "events"])
    project.add_argument("name", nargs="?", default="")
    project.add_argument("value", nargs="?", default="", help="directory or task goal")
    project.add_argument("--verify", action="append", default=[], metavar="ARGV_JSON",
                         help="operator-authorized verification argv; executes project code on the host")
    project.add_argument("--background", action="store_true")
    _common(project)
    skills = sub.add_parser("skills", help="discover executable skills and review learned procedures")
    skills.add_argument("operation", choices=["list", "show", "run", "learn", "approve-draft"])
    skills.add_argument("name", nargs="?", default="")
    skills.add_argument("--args", default="{}", help="typed skill arguments as JSON")
    skills.add_argument("--from-task", default="", help="verified project run supplying evidence")
    skills.add_argument("--instructions-file", type=Path)
    _common(skills)
    memory = sub.add_parser("memory", help="search or record sourced durable memory")
    memory.add_argument("operation", choices=["search", "add"])
    memory.add_argument("text")
    memory.add_argument("--project", default="")
    memory.add_argument("--source", default="operator:cli")
    _common(memory)
    for command in ("backends", "doctor"):
        p = sub.add_parser(command, help="inspect configured model routes" if command == "backends" else
                           "check setup; --probe makes one real model request")
        p.add_argument("--probe", action="store_true")
        _common(p)
    approve = sub.add_parser("approve", help="execute one exact pending action in its original session")
    approve.add_argument("id")
    _common(approve)
    inbox = sub.add_parser("inbox", help="inspect durable chat/delivery receipts")
    inbox.add_argument("operation", choices=["list", "retry-delivery"])
    inbox.add_argument("id", nargs="?", default="")
    _common(inbox)
    pi = sub.add_parser("pi", help="install or inspect the pinned Pi SDK runtime")
    pi.add_argument("operation", choices=["install", "status"])
    _common(pi)
    return parser


def _routes(config, data_dir: Path):
    """Report availability and capability without outputting credentials."""
    rows = []
    for name, route in config.backends.items():
        api = route.kind not in {"codex", "claude", "gemini"}
        executable = (route.command or (route.kind,))[0]
        available = (bool(route.api_key and route.model) if api else
                     bool(shutil.which(executable)))
        rows.append({"name": name, "kind": route.kind, "model": route.model,
                     "selected": name == config.backend,
                     "available": available,
                     "credential_configured": bool(route.api_key) if api else None,
                     "allow_unrestricted": route.allow_unrestricted,
                     "engine": "pi",
                     "capabilities": sorted(build_pi_backend(route, data_dir).capabilities)})
    return rows


def main(argv: list[str] | None = None) -> int:
    """Build the composition root once and return honest command exit status."""
    args = _parser().parse_args(argv)
    settings = Settings()
    if args.data_dir:
        settings = settings.model_copy(update={"data_dir": args.data_dir.expanduser().resolve()})
    if args.config:
        settings = settings.model_copy(update={"runtime_config": args.config.expanduser().resolve()})
    try:
        if args.command == "pi":
            result = (install_pi(settings.data_dir) if args.operation == "install"
                      else pi_status(settings.data_dir))
            _emit(result)
            return 0 if result["available"] else 1
        config = load_runtime_config(settings, backend=args.backend)
        if args.command in ("backends", "doctor"):
            if args.command == "doctor":
                from assistant.platform.runtime.channels import QQConfig, SlackConfig
                for name, spec in config.channels.items():
                    if name == "slack":
                        SlackConfig(**spec)
                    elif name in {"qq", "qq_onebot"}:
                        QQConfig(**spec)
                    else:
                        raise ValueError(f"unknown channel type {name!r}")
            rows = _routes(config, settings.data_dir)
            pi = pi_status(settings.data_dir)
            result = {"backend": config.backend, "routes": rows,
                      "engine": "pi", "pi": pi,
                      "channels": sorted(config.channels),
                      "config": str(config.path), "live_probe": "not_requested"}
            okay = pi["available"] and any(r["selected"] and r["available"] for r in rows)
            if args.probe:
                settings.data_dir.mkdir(parents=True, exist_ok=True)
                response = build_pi_backend(config.route(), settings.data_dir).complete(BackendRequest(
                    messages=[{"role": "user", "content":
                               "Do not use tools. Reply with exactly PERSONAL_AGENT_RUNTIME_OK."}],
                    cwd=settings.data_dir, timeout=min(config.timeout, 60)))
                okay = response.text.strip() == "PERSONAL_AGENT_RUNTIME_OK"
                result["live_probe"] = "passed" if okay else "unexpected_response"
            _emit(result)
            return 0 if okay else 1
        app = RuntimeApplication(settings, config)
        if args.command == "serve":
            return app.serve()
        kernel = app.kernel(args.owner)
        if args.command in ("ask", "chat"):
            result = kernel.turn(args.text, session=args.session, project=args.project,
                                 image_paths=args.image)
            print(result.text)
            return 0 if result.status in {"done", "awaiting_approval"} else 1
        if args.command == "approve":
            _emit(asdict(kernel.approve(args.id, session=args.session)))
            return 0
        if args.command == "task":
            if args.approved_id:
                _emit(asdict(kernel.approve(args.approved_id, session=args.session)))
                return 0
            if not args.request:
                raise ValueError("task requires a request")
            if args.project:
                result = kernel.projects.submit(args.project, args.request, session=args.session)
                if not args.background:
                    result = kernel.projects.run(result["id"])
                    kernel.remember_project(result)
                _emit(result)
                return 0 if result["status"] in ("queued", "done") else 1
            _emit(asdict(kernel.turn(args.request, session=args.session)))
            return 0
        if args.command == "project":
            if args.operation == "list":
                _emit(kernel.projects.list())
            elif args.operation == "register":
                _emit(kernel.projects.register(args.name, args.value,
                    verification=[json.loads(v) for v in args.verify]))
            elif args.operation == "run":
                result = kernel.projects.submit(args.name, args.value, session=args.session)
                if not args.background:
                    result = kernel.projects.run(result["id"])
                    kernel.remember_project(result)
                _emit(result)
                return 0 if result["status"] in ("queued", "done") else 1
            elif args.operation == "status":
                _emit(kernel.projects.status(args.name))
            elif args.operation == "cancel":
                _emit(kernel.projects.cancel(args.name))
            elif args.operation == "events":
                _emit(kernel.store.events(args.name))
            return 0
        if args.command == "skills":
            if args.operation == "list":
                _emit(kernel.skills.list())
            elif args.operation == "show":
                _emit(kernel.skills.describe(args.name))
            elif args.operation == "run":
                params = json.loads(args.args)
                if not isinstance(params, dict):
                    raise ValueError("skill arguments must be an object")
                if kernel.skills.requires_approval(args.name, params):
                    approval = kernel.store.create_approval(args.session, "skill_run", {
                        "name": args.name, "arguments": params})
                    _emit({"status": "awaiting_approval", "approval": approval["id"],
                           "instruction": "assistant approve ID --session " + args.session})
                else:
                    result = kernel.skills.execute(args.name, params)
                    _emit(asdict(result))
                    return 0 if result.ok else 1
            elif args.operation == "learn":
                task = kernel.projects.status(args.from_task)
                if not task.get("verified") or not args.instructions_file:
                    raise ValueError("learning requires a verified project task and --instructions-file")
                _emit(kernel.skills.create_draft(args.name, args.instructions_file.read_text(),
                    [task["evidence_path"]], project=task["project"]))
            elif args.operation == "approve-draft":
                _emit(kernel.skills.approve_draft(args.name))
            return 0
        if args.command == "memory":
            if args.operation == "search":
                _emit(kernel.memory.recall(args.text, project=args.project))
            else:
                _emit(kernel.memory.remember(args.text, args.source, project=args.project))
            return 0
        if args.command == "inbox":
            inbox = Inbox(kernel.settings.data_dir)
            if args.operation == "list":
                _emit(inbox.list())
            else:
                _emit({"queued": inbox.retry_delivery(args.id)})
            return 0
        raise ValueError("unknown runtime command")
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        from assistant.platform.secrets import mask_secrets
        print(f"assistant: {mask_secrets(str(exc))}", file=sys.stderr)
        return 1
