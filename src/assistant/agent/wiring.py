"""Single activation point for the agent's implementations of platform contracts.

The platform layer is agent-free: modules like `serve`, `llm`, `admin`, and
`onboarding` declare hooks and ask the agent to fill them. Importing this module
once at a composition root (`cli.main`, tests via `conftest`) registers all of
them:

- serve daemon behaviors  (`agent.app` → `serve.set_default_services`)
- MoA metrics sink         (`agent.observability` → `llm.set_default_metrics_sink`)
- shared-lessons store     (`admin.set_shared_lessons_factory`)
- tenant profile seeding   (`onboarding.set_profile_seeder`)
"""

from .. import admin as _admin
from .. import onboarding as _onboarding
from ..lessons_store import shared_store
from ..profile_store import ALIASES_TEMPLATE, ProfileStore
from . import app as _app          # noqa: F401 — import registers serve services
from . import observability as _obs  # noqa: F401 — import registers the MoA metrics sink


def _seed_profile(profile_dir, display: str, uid: str) -> None:
    """Seed a new tenant's minimal profile.yaml + aliases.yaml (the agent-owned
    provisioning step onboarding delegates here)."""
    store = ProfileStore(profile_dir)
    store.save({"identity": {"name": display}, "skills": [],
                "projects": [], "interests": []},
               f"onboard {uid}: seed profile")
    aliases = profile_dir / "aliases.yaml"
    if not aliases.exists():
        aliases.write_text(ALIASES_TEMPLATE)


_admin.set_shared_lessons_factory(shared_store)
_onboarding.set_profile_seeder(_seed_profile)
