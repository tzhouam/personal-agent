"""Profile-section HTML renderers: About, Skills, Experience, Education,
Projects. Each takes the relevant profile slice and returns an HTML `<section>`
string (empty string when the slice is empty). Deterministic and pure — no LLM,
so nothing here can fabricate content that reaches a public page.
"""

import html


def _about_html(profile: dict) -> str:
    """Short self-introduction: identity.bio (owner-editable, never touched by the
    LLM — identity is a protected section) with a deterministic fallback composed
    from profile facts, so the section can't fabricate anything."""
    e = html.escape
    bio = str(profile.get("identity", {}).get("bio", "") or "").strip()
    if not bio:
        bio = _fallback_bio(profile)
    if not bio:
        return ""
    paragraphs = "".join(f"<p class='bio'>{e(p.strip())}</p>"
                         for p in bio.split("\n") if p.strip())
    return f"<section id='about' class='card'><h2>About</h2>{paragraphs}</section>"


def _fallback_bio(profile: dict) -> str:
    """One factual sentence straight from the profile when no bio is written."""
    name = profile.get("identity", {}).get("name", "")
    bits = []
    experience = profile.get("experience", [])
    if experience:
        job = experience[0]
        if job.get("title") and job.get("org"):
            bits.append(f"{job['title']} at {job['org']}")
    projects = [p["name"] for p in profile.get("projects", [])
                if p.get("status", "active") == "active" and p.get("name")][:3]
    if projects:
        bits.append("currently working on " + ", ".join(projects))
    if not (name and bits):
        return ""
    return f"{name} — {'; '.join(bits)}."


def _skills_html(skills: list[dict]) -> str:
    """Render active skills as a row of chips; empty string when there are none."""
    e = html.escape
    if not skills:
        return ""
    return ("<section id='skills' class='card'><h2>Skills</h2><p class='chips'>"
            + "".join(f"<span class='chip'>{e(s['name'])}</span>" for s in skills)
            + "</p></section>")


def _experience_html(experience: list[dict]) -> str:
    """Render experience entries as a timeline (title · org · period + highlight
    bullets); empty string when there is no experience."""
    e = html.escape
    if not experience:
        return ""
    parts = ["<section id='experience' class='card'><h2>Experience</h2><ul class='timeline'>"]
    for job in experience:
        period = job.get("period", {})
        when = f"{period.get('start', '')} – {period.get('end') or 'present'}"
        parts.append(f"<li><div class='t-head'><b>{e(str(job.get('title', '')))}</b>"
                     f" · {e(str(job.get('org', '')))}"
                     f"<span class='when'>{e(when)}</span></div>")
        for h in job.get("highlights", []):
            parts.append(f"<div class='hl'>{e(str(h))}</div>")
        parts.append("</li>")
    parts.append("</ul></section>")
    return "".join(parts)


def _education_html(education: list[dict]) -> str:
    """Render education entries as a timeline (school · degree · period); empty
    string when there is no education."""
    e = html.escape
    if not education:
        return ""
    parts = ["<section id='education' class='card'><h2>Education</h2><ul class='timeline'>"]
    for school in education:
        parts.append(f"<li><div class='t-head'><b>{e(str(school.get('school', '')))}</b>"
                     f" · {e(str(school.get('degree', '')))}"
                     f"<span class='when'>{e(str(school.get('period', '')))}</span></div></li>")
    parts.append("</ul></section>")
    return "".join(parts)


def _projects_html(projects: list[dict]) -> str:
    """Render active projects as a card grid; the title links to the first
    http(s) evidence URL when present. Empty string when there are no projects."""
    e = html.escape
    if not projects:
        return ""
    parts = ["<section id='projects' class='card'><h2>Projects</h2><div class='grid'>"]
    for p in projects:
        link = next((str(l) for l in p.get("evidence", []) if str(l).startswith("http")), None)
        title = f"<a href='{e(link)}'>{e(p['name'])}</a>" if link else e(p["name"])
        highlights = "".join(f"<div class='hl'>{e(str(h))}</div>" for h in p.get("highlights", []))
        parts.append(f"<div class='proj'><h3>{title}</h3>"
                     f"<span class='role'>{e(p.get('role', ''))}</span>{highlights}</div>")
    parts.append("</div></section>")
    return "".join(parts)
