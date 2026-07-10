"""Page assembly: `render_site` ties the profile sections and interactive
widgets into a set of `{filename: html}` pages (one per section, plus the CSS/JS
assets), and `_encrypt_body` wraps a private page's body as AES-GCM ciphertext
for the WebCrypto unlock. Deterministic — no LLM in the loop, so nothing
fabricated can reach a published page.
"""

import base64
import html
from datetime import date

from .sections import (
    _about_html,
    _education_html,
    _experience_html,
    _projects_html,
    _skills_html,
)
from .templates import _CSS, _JS
from .widgets import _render_calendar, _render_reading, _render_routines

_PROTECTED_PAGES = {"todos.html", "reading.html", "routines.html"}
_PBKDF2_ITERATIONS = 100_000  # must match the WebCrypto params in templates._JS


def _encrypt_body(body: str, password: str) -> str:
    """AES-GCM-encrypt a page body; the browser decrypts with WebCrypto after
    the owner enters the password (real client-side auth for a static site —
    the published HTML contains only ciphertext)."""
    import os as _os

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt, iv = _os.urandom(16), _os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=_PBKDF2_ITERATIONS).derive(password.encode())
    ciphertext = AESGCM(key).encrypt(iv, body.encode(), None)
    b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
    return (
        f"<section class='card lock' data-salt='{b64(salt)}' data-iv='{b64(iv)}'"
        f" data-ct='{b64(ciphertext)}'>"
        "<h2>🔒 Private</h2><p class='empty'>This section is encrypted.</p>"
        "<form class='lock-form'><input type='password' placeholder='Password'"
        " autocomplete='current-password'>"
        "<button type='submit'>Unlock</button>"
        "<span class='lock-err'></span></form></section>"
    )


def render_site(profile: dict, todos: list[dict], today: date | None = None,
                reading: list[dict] | None = None,
                routines: list[dict] | None = None,
                reminders: list[dict] | None = None,
                password: str = "",
                marks_cfg: dict | None = None) -> dict[str, str]:
    """Returns {filename: content} for the generated site — one page per section.

    Every page is always rendered (an empty section shows a placeholder) so a
    previously published page never goes stale-but-orphaned in the repo."""
    today = today or date.today()
    ident = profile.get("identity", {})
    e = html.escape
    name = ident.get("name", "")
    photo = ident.get("photo") or (
        f"https://github.com/{ident['github']}.png" if ident.get("github") else ""
    )

    def actives(section):
        return [x for x in profile.get(section, []) if x.get("status", "active") == "active"]

    link_pills = [
        f"<a class='pill' href='{e(link)}'>{e(link.split('//')[-1].rstrip('/'))}</a>"
        for link in ident.get("links", []) if link
    ]
    if ident.get("emails"):
        link_pills.append(f"<a class='pill' href='mailto:{e(ident['emails'][0])}'>✉ email</a>")

    pages = [
        ("index.html", "Home", _about_html(profile) + _skills_html(actives("skills"))),
        ("experience.html", "Experience", _experience_html(profile.get("experience", []))),
        ("education.html", "Education", _education_html(profile.get("education", []))),
        ("projects.html", "Projects", _projects_html(actives("projects"))),
        ("todos.html", "Todos", _render_calendar(todos, today)),
        ("reading.html", "Reading", _render_reading(reading or [], today)),
        ("routines.html", "Routines", _render_routines(routines or [], reminders or [])),
    ]

    files = {"agent-site.css": _CSS, "agent-site.js": _JS}
    for filename, label, body in pages:
        nav = "<nav class='anchors'>" + "".join(
            f"<a href='{fn}'{' class=active' if fn == filename else ''}>{lbl}</a>"
            for fn, lbl, _ in pages
        ) + "</nav>"
        head = (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<meta property='og:title' content='{e(name)}'>"
            f"<meta property='og:image' content='{e(photo)}'>"
            f"<title>{e(name) if filename == 'index.html' else f'{e(name)} — {label}'}</title>"
            "<link rel='icon' href='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 "
            "viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🚀</text></svg>'>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'>"
            "<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap'"
            " rel='stylesheet'>"
            "<link rel='stylesheet' href='agent-site.css'>"
            "<script src='agent-site.js' defer></script></head><body>"
        )
        if filename == "index.html":  # full hero: photo + personal info
            hero = (
                "<header class='hero'><div class='hero-inner'>"
                + (f"<img class='avatar' src='{e(photo)}' alt='{e(name)}'>" if photo else "")
                + f"<h1>{e(name)}</h1>"
                + f"<p class='tagline'>{e(' · '.join(ident.get('affiliations', [])))}</p>"
                + f"<nav class='pills'>{''.join(link_pills)}</nav>"
                + nav + "</div></header>"
            )
        else:  # compact banner: name links home, same nav
            hero = (
                "<header class='hero compact'><div class='hero-inner'>"
                f"<h1><a href='index.html'>{e(name)}</a></h1>"
                + nav + "</div></header>"
            )
        body = body or "<section class='card'><p class='empty'>Nothing here yet.</p></section>"
        if password and filename in _PROTECTED_PAGES:
            # marks config (incl. the repo-scoped push token) ships ONLY inside
            # the ciphertext — a page without the password never reveals it
            if marks_cfg and marks_cfg.get("repo") and marks_cfg.get("token") \
                    and filename in ("todos.html", "reading.html"):
                body = (f"<div id='marks-cfg' hidden data-repo='{e(marks_cfg['repo'])}'"
                        f" data-token='{e(marks_cfg['token'])}'></div>" + body)
            body = _encrypt_body(body, password)
        files[filename] = (
            head + hero + "<main>" + body
            + f"<footer>Maintained automatically by personal-agent · updated {today.isoformat()}"
              "</footer></main></body></html>"
        )
    return files
