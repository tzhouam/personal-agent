"""Composition root for the serve daemon — where the agent layer meets the
platform runtime.

`platform.serve` is agent-free: it declares a `ServeServices` contract and asks
for it. This module builds that contract from the agent subsystems (chat,
actions, routines, channels) and the job dispatch, then either hands it to
`run_serve` directly (`run`) or registers it as the default (`set_default_services`)
so the existing test/CLI call sites that don't pass one still work.

Each service is a thin wrapper that imports its agent target **at call time**, so
tests monkeypatching the source module (e.g. `assistant.agent.routines.fire_due`) take
effect exactly as they did when serve.py imported these lazily itself.
"""

from assistant.platform.config import Settings
from assistant.platform import serve  # platform module (still at the legacy root; agent → platform is allowed)
from assistant.platform.serve import ServeServices, run_serve
from assistant.agent.dispatch import build_dispatch


def _run_action(name, params, settings):
    from assistant.agent.actions import run_action
    return run_action(name, params, settings)


def _handle_turn(text, settings, llm, *, history=None, image_paths=None):
    from assistant.agent.chat.agent import handle_turn
    return handle_turn(text, settings, llm, history=history, image_paths=image_paths)


def _build_channels(settings, *, log_wecom=False):
    from assistant.agent.chat.service import build_channels
    return build_channels(settings, log_wecom=log_wecom)


def _email_channel(settings):
    from assistant.agent.chat.email_channel import EmailChannel
    from assistant.agent.chat.service import _owner_addresses
    return EmailChannel(settings, _owner_addresses(settings))


def _fire_due(settings):
    from assistant.agent.routines import fire_due
    return fire_due(settings)


def _acquire_pid_lock(settings):
    from assistant.agent.chat.service import _acquire_pid_lock as acquire
    return acquire(settings)


def build_services() -> ServeServices:
    """Assemble the agent behaviors + job dispatch the serve daemon needs."""
    return ServeServices(
        run_action=_run_action,
        handle_turn=_handle_turn,
        build_channels=_build_channels,
        email_channel=_email_channel,
        fire_due=_fire_due,
        acquire_pid_lock=_acquire_pid_lock,
        worker_dispatch=build_dispatch(),
    )


def run(settings: Settings) -> int:
    """`assistant serve` entry point: build the services and start the daemon."""
    return run_serve(settings, build_services())


# Register the default so callers that pass no `services` (tests via conftest,
# any legacy call site) still resolve the real agent behaviors.
serve.set_default_services(build_services)
