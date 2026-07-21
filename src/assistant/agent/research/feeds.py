"""RSS/Atom feed reader for the research digest's industry and 中文媒体 sections.
Exports `load_sources` (the enabled entries from the sources YAML), `fetch_feed`,
and `parse_feed` — which normalize both RSS 2.0 and Atom into a uniform item list."""

import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import yaml

_ATOM_NS = "{http://www.w3.org/2005/Atom}"


def load_sources(sources_file: Path) -> list[dict]:
    """Read the feed sources from the YAML file, keeping only entries where
    `enabled` is unset or true. Returns [] if the file is absent, so a missing
    config degrades to no feeds rather than crashing."""
    if not sources_file.exists():
        return []
    data = yaml.safe_load(sources_file.read_text()) or {}
    return [s for s in data.get("sources", []) if s.get("enabled", True)]


def fetch_feed(url: str, timeout: int = 30) -> list[dict]:
    """Parse RSS 2.0 or Atom into a uniform item list."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                     headers={"User-Agent": "personal-agent/0.1 (+rss reader)"})
    resp.raise_for_status()
    return parse_feed(resp.text)


def parse_feed(xml_text: str) -> list[dict]:
    """Normalize an RSS 2.0 or Atom document into a uniform item list (title,
    url, published, summary). Dispatches on the root tag; for Atom it prefers the
    `alternate` link and strips HTML from the summary/content, capped at 600 chars."""
    root = ET.fromstring(xml_text)
    items = []

    if root.tag == f"{_ATOM_NS}feed":
        for entry in root.findall(f"{_ATOM_NS}entry"):
            link = ""
            for l in entry.findall(f"{_ATOM_NS}link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href", "")
                    break
            items.append(
                {
                    "title": " ".join((entry.findtext(f"{_ATOM_NS}title") or "").split()),
                    "url": link,
                    "published": entry.findtext(f"{_ATOM_NS}published")
                    or entry.findtext(f"{_ATOM_NS}updated") or "",
                    "summary": _strip_html(
                        entry.findtext(f"{_ATOM_NS}summary")
                        or entry.findtext(f"{_ATOM_NS}content") or ""
                    )[:600],
                }
            )
    else:  # RSS 2.0
        for item in root.iter("item"):
            items.append(
                {
                    "title": " ".join((item.findtext("title") or "").split()),
                    "url": (item.findtext("link") or "").strip(),
                    "published": item.findtext("pubDate") or "",
                    "summary": _strip_html(item.findtext("description") or "")[:600],
                }
            )
    return items


def _strip_html(text: str) -> str:
    """Crude tag-strip for feed summaries: drop `<...>` tags and unescape
    `&nbsp;` to a space. Not a full HTML parser — just enough to make a summary
    readable as plain text."""
    import re

    return re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").strip()
