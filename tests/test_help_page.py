"""help_page renders the guide that ships with the code, and serves nothing outside docs/."""

import help_page
from help_page import page, render


def test_render_covers_what_the_guide_uses() -> None:
    md = """# Title

Text with `code`, **bold** and a [link](AGENT_PROTOCOL.md).

- one
  - nested
- two
  carried on

1. first
2. second

```json
{"a": <1>}
```
"""
    out = render(md)
    assert "<h1>Title</h1>" in out and "<code>code</code>" in out and "<b>bold</b>" in out
    assert '<a href="/help/AGENT_PROTOCOL.md">link</a>' in out
    assert out.count("<ul>") == 2 and "<ol>" in out and "carried on" in out
    assert "<pre><code>{&quot;a&quot;: &lt;1&gt;}</code></pre>" in out


def test_the_guide_and_its_image_are_served() -> None:
    body, kind = page() or (b"", "")
    assert kind.startswith("text/html") and b"<title>SparkGap Web" in body
    assert (page("web.png") or (b"", ""))[1] == "image/png"


def test_nothing_outside_docs() -> None:
    for name in ("../sparkgap.py", "..", ".hidden.md", "x/../../etc", "agent.json", "nothing.md"):
        assert page(name) is None, name
    assert help_page.DOCS.name == "docs"


def test_only_the_guide_and_what_it_links_are_served() -> None:
    assert page("SERVER_PROTOCOL.md") is not None
    assert page("production_ab_runbook.md") is None and page("ITILA2.md") is None
