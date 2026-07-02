import html
import smtplib
from email.mime.text import MIMEText

import httpx

from ..config import Settings

_PRIORITY_META = {
    "red": ("🔴 Action needed", "#c0392b"),
    "yellow": ("🟡 Worth knowing", "#b7791f"),
    "white": ("⚪ FYI", "#6b7280"),
}


def render_html(run_date: str, digest: dict, research: dict, resume: dict,
                profile_diff: str, profile_ops: list[dict], stats: dict) -> str:
    parts = [
        "<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:720px;margin:auto;color:#1f2937'>",
        f"<h2 style='border-bottom:2px solid #e5e7eb;padding-bottom:8px'>Daily digest — {run_date}</h2>",
    ]

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
            parts.append(
                f"<li style='margin-bottom:6px'><a href='{url}'>{repo}</a>: {summary}{action_html}</li>"
            )
        parts.append("</ul>")

    papers = research.get("papers", [])
    if papers:
        parts.append(f"<h3>📄 Papers ({len(papers)})</h3>")
        for p in papers:
            why = (
                f"<br><em style='color:#4b5563'>Why: {html.escape(p.get('why', ''))}</em>"
                if p.get("why") else ""
            )
            parts.append(
                f"<p style='margin:8px 0'><a href='{p.get('url', '#')}'><b>{html.escape(p.get('title', ''))}</b></a><br>"
                f"{html.escape(p.get('summary', ''))}{why}</p>"
            )
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
