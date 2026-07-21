"""Command-line interface for the assistant.

This was one 489-line module; it is now a package — `commands` (the `cmd_*`
handlers, one per subcommand) and `main` (argparse + dispatch). The public
surface (`main` — the `assistant` console-script — plus the `cmd_*` handlers
imported elsewhere) is re-exported so importers and the entry point are
unchanged.
"""

from assistant.cli.commands import cmd_bootstrap, cmd_consolidate, cmd_enrich_profile, cmd_reading, cmd_resume_init, cmd_resume_status, cmd_run_phase, cmd_show, cmd_test_email, cmd_todo
from assistant.cli.main import main

__all__ = [
    "main",
    "cmd_bootstrap", "cmd_show", "cmd_todo", "cmd_reading", "cmd_enrich_profile",
    "cmd_run_phase", "cmd_resume_init", "cmd_resume_status", "cmd_test_email",
    "cmd_consolidate",
]
