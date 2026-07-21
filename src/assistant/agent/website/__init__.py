"""Personal website generator + publisher.

The site is a DETERMINISTIC render of profile.yaml + todos — no LLM in the
loop, so nothing fabricated can reach a public page. Publishing pushes
directly to the repo's default branch (owner's explicit choice, 2026-07-02 —
was PR-gated before); remote edits are rebased in first, never force-pushed.

Todo pin/done buttons are client-side only (localStorage keyed by todo id):
the page has no backend, so "done" hides the item in that browser without
touching todos.yaml — the store is still closed by the agent's monitor pass
or `assistant todo done`. The buttons are owner-only (todos.html#owner enables
owner mode in a browser; #guest turns it off). A static page can't truly
authenticate, but a visitor who bypasses this only ever reorders their own
browser's view — guests always see the canonical list.

This was one 813-line module; it is now a package — `templates` (CSS/JS
constants), `sections` (profile renderers), `widgets` (calendar/reading/
routines/todo renderers), `render` (page assembly + AES-GCM encryption), and
`sync` (git publish). The public surface (`render_site`, `sync_website`) is
re-exported so `from .website import ...` importers are unchanged.
"""

from assistant.agent.website.render import render_site
from assistant.agent.website.sync import sync_website

__all__ = ["render_site", "sync_website"]
