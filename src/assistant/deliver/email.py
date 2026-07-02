import html
import smtplib
from email.mime.text import MIMEText

import httpx

from ..config import Settings
from ..utils import ref_label

_PRIORITY_META = {
    "red": ("🔴 Action needed", "#c0392b"),
    "yellow": ("🟡 Worth knowing", "#b7791f"),
    "white": ("⚪ FYI", "#6b7280"),
}


def render_html(run_date: str, digest: dict, research: dict, resume: dict,
                todos: dict, reading: list[dict], website: dict,
                profile_diff: str, profile_ops: list[dict], stats: dict) -> str:
    parts = [
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:720px;margin:auto;color:#1f2937'>",
        f"<h2 style='border-bottom:2px solid #e5e7eb;padding-bottom:8px'>Daily digest — {run_date}</h2>",
    ]

    open_todos = todos.get("open", [])
    if open_todos:
        added_today = set(todos.get("added", []))
        parts.append(f"<h3>✅ Todos ({len(open_todos)} open)</h3><ul style='margin-top:4px'>")
        for todo in open_todos:
            title = f"<b>{html.escape(todo.get('title', ''))}</b>"
            label = ref_label(todo.get("url"), todo.get("detail", "") or todo.get("title", ""),
                              todo.get("type", ""))
            if label:  # short bracketed link, summary stays plain text
                title = f"<a href='{todo['url']}'>[{label}]</a>: {title}"
            elif todo.get("url"):
                title = f"<a href='{todo['url']}'>[link]</a>: {title}"
            badge = (" <b style='color:#c0392b'>NEW</b>" if todo.get("id") in added_today else "")
            due = f" · due {html.escape(str(todo['due']))}" if todo.get("due") else ""
            detail = (f"<br><span style='color:#6b7280;font-size:13px'>{html.escape(todo['detail'])}</span>"
                      if todo.get("detail") else "")
            parts.append(
                f"<li style='margin-bottom:6px'>[<code>{todo.get('id')}</code>] {title}{badge}"
                f"<span style='color:#9ca3af'> ({todo.get('source', '')}, since {todo.get('created', '')}{due})"
                f"</span>{detail}</li>"
            )
        parts.append("</ul><p style='font-size:12px;color:#9ca3af'>"
                     "close with <code>assistant todo done &lt;id&gt;</code></p>")

    sections = digest.get("sections", {})
    if not any(sections.values()):
        parts.append("<p>No GitHub notifications today. 🎉</p>")
    for key in ("red", "yellow", "white"):
        items = sections.get(key, [])
        if not items:
            continue
        label, color = _PRIORITY_META[key]
        parts.append(f"<h3 style='color:{color};margin-bottom:4px'>{label} ({len(items)})</h3><ul style='margin-top:4px'>")
        for item in items:
            summary = html.escape(item.get("summary", ""))
            repo = html.escape(item.get("repo", ""))
            url = item.get("url") or "#"
            action = item.get("action")
            action_html = (
                f" <em style='color:{color}'>→ {html.escape(action)}</em>" if action else ""
            )
            ref = ref_label(item.get("url"), item.get("title", ""), item.get("type", ""))
            link = f"<a href='{url}'>[{ref or item.get('type') or 'link'}]</a>"
            parts.append(
                f"<li style='margin-bottom:6px'>{link} "
                f"<span style='color:#6b7280'>{repo}</span>: {summary}{action_html}</li>"
            )
        parts.append("</ul>")

    if reading:
        new_paper_summaries = {p.get("seen_id"): p for p in research.get("papers", [])}
        parts.append(f"<h3>📚 Reading list ({len(reading)} unread)</h3>")
        for item in reading[:15]:
            paper = new_paper_summaries.get(item.get("key"))
            badge = " <b style='color:#c0392b'>NEW</b>" if paper else ""
            body = ""
            if paper:  # today's additions carry their full summary
                body = f"<br>{html.escape(paper.get('summary', ''))}"
            if item.get("why"):
                body += f"<br><em style='color:#4b5563'>Why: {html.escape(item['why'])}</em>"
            parts.append(
                f"<p style='margin:8px 0'>[<code>{item.get('id')}</code>] "
                f"<a href='{item.get('url', '#')}'><b>{html.escape(item.get('title', ''))}</b></a>"
                f"{badge}{body}</p>"
            )
        if len(reading) > 15:
            parts.append(f"<p style='color:#9ca3af;font-size:12px'>…and {len(reading) - 15} more; "
                         "mark read with <code>assistant reading done &lt;id&gt;</code></p>")
    for key, label in (("industry", "🌐 Industry"), ("chinese", "🇨🇳 中文媒体")):
        items = research.get(key, [])
        if not items:
            continue
        parts.append(f"<h3>{label} ({len(items)})</h3><ul style='margin-top:4px'>")
        for item in items:
            parts.append(
                f"<li style='margin-bottom:6px'><a href='{item.get('url', '#')}'>"
                f"{html.escape(item.get('title', ''))}</a> <span style='color:#9ca3af'>"
                f"({html.escape(item.get('source', ''))})</span><br>"
                f"{html.escape(item.get('takeaway', ''))}</li>"
            )
        parts.append("</ul>")

    failed_sources = {k: v for k, v in research.get("source_health", {}).items()
                      if str(v).startswith("FAILED")}
    if failed_sources:  # never let a broken scraper vanish silently
        parts.append(
            "<p style='font-size:12px;color:#b7791f'>⚠️ sources not fetched today: "
            + ", ".join(html.escape(k) for k in failed_sources) + "</p>"
        )

    if website.get("status") == "pushed":
        parts.append(
            f"<p>🌐 <a href='https://{html.escape(website.get('url', '').removeprefix('https://'))}'>"
            f"Personal site</a> updated (commit <code>{html.escape(website.get('commit', ''))}</code>).</p>"
        )
    elif website.get("status") == "failed":
        parts.append(
            f"<p style='color:#c0392b'>🌐 Website sync failed: {html.escape(website.get('note', ''))}</p>"
        )

    if resume.get("status") == "pending_approval":
        parts.append(
            "<h3>📝 Resume update pending approval</h3>"
            f"<p>{html.escape(resume.get('summary', ''))} — review and push with "
            "<code>assistant approve-resume</code> (compile: "
            f"{html.escape(resume.get('compile', '?'))})</p>"
            "<pre style='background:#f3f4f6;padding:10px;border-radius:6px;font-size:12px;"
            f"overflow-x:auto'>{html.escape(resume.get('diff', '')[:3000])}</pre>"
        )
    elif resume.get("status") == "failed":
        parts.append(
            f"<p style='color:#c0392b'>📝 Resume sync failed: {html.escape(resume.get('note', ''))}</p>"
        )

    if profile_ops:
        parts.append(f"<h3>📋 Profile changes today ({len(profile_ops)} ops)</h3>")
        parts.append(
            "<pre style='background:#f3f4f6;padding:10px;border-radius:6px;font-size:12px;"
            f"overflow-x:auto'>{html.escape(profile_diff[:4000])}</pre>"
        )

    footer_bits = [f"{k}: {v}" for k, v in stats.items()]
    parts.append(
        "<hr style='border:none;border-top:1px solid #e5e7eb'>"
        f"<p style='font-size:11px;color:#9ca3af'>personal-agent · {' · '.join(footer_bits)}</p></div>"
    )
    return "".join(parts)


def send_email(settings: Settings, subject: str, html_body: str) -> str:
    """Try the Resend HTTP API first, fall back to SMTP. Returns the transport used."""
    recipient = settings.recipient
    if not recipient:
        raise RuntimeError("no recipient configured (DIGEST_TO / SMTP_USER)")

    if settings.resend_api_key:
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={
                    "from": f"Personal Agent <{settings.resend_from}>",
                    "to": [recipient],
                    "subject": subject,
                    "html": html_body,
                },
                timeout=30,
            )
            if resp.status_code in (200, 201):
                return "resend"
        except httpx.HTTPError:
            pass  # fall through to SMTP

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_user
    msg["To"] = recipient
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.sendmail(settings.smtp_user, [recipient], msg.as_string())
    return "smtp"
