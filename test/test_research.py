from assistant.agent.research.arxiv import parse_feed as parse_arxiv
from assistant.agent.research.feeds import parse_feed as parse_rss

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2501.12345v2</id>
    <title>Efficient  LLM
      Serving</title>
    <summary>We present a method.</summary>
    <published>2026-07-01T00:00:00Z</published>
    <author><name>A. Author</name></author>
    <category term="cs.LG"/>
    <category term="cs.DC"/>
  </entry>
</feed>"""

RSS2 = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>机器之心</title>
<item><title>大模型新进展</title><link>https://example.com/a</link>
<description>&lt;p&gt;正文摘要&lt;/p&gt;</description><pubDate>Wed, 01 Jul 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title>
<entry><title>Post One</title>
<link rel="alternate" href="https://example.com/p1"/>
<updated>2026-07-01T00:00:00Z</updated><summary>Hello &lt;b&gt;world&lt;/b&gt;</summary></entry>
</feed>"""


def test_parse_arxiv_atom():
    papers = parse_arxiv(ARXIV_ATOM)
    assert len(papers) == 1
    p = papers[0]
    assert p["id"] == "2501.12345v2"
    assert p["title"] == "Efficient LLM Serving"  # whitespace collapsed
    assert p["url"] == "https://arxiv.org/abs/2501.12345"  # version stripped
    assert p["categories"] == ["cs.LG", "cs.DC"]


def test_parse_rss2():
    items = parse_rss(RSS2)
    assert items == [{
        "title": "大模型新进展",
        "url": "https://example.com/a",
        "published": "Wed, 01 Jul 2026 08:00:00 GMT",
        "summary": "正文摘要",
    }]


def test_parse_atom():
    items = parse_rss(ATOM)
    assert items[0]["title"] == "Post One"
    assert items[0]["url"] == "https://example.com/p1"
    assert "world" in items[0]["summary"] and "<b>" not in items[0]["summary"]
