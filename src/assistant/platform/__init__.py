"""System / platform layer: the multi-tenant runtime that hosts agents.

Modules here own hosting, tenancy, and shared services. The one rule: platform
code must never import agent code (`assistant.agent.*` / the per-owner modules).
Where the runtime needs agent behavior, it defines a contract here and the agent
registers an implementation, wired at the composition root (`cli`, `serve`)."""
