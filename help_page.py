"""The SparkGap Web guide as a page, from the docs that ship with this code.

The server serves it at /help (docs/WEBUI.md) and /help/<NAME>.md for the docs it links
to and theirs, and no others, so the guide always matches the version running.  The Markdown
here is what docs/ uses: headings, paragraphs, lists (nested by two spaces), fenced code,
inline code, bold, links and images.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent / "docs"
GUIDE = "WEBUI.md"
SERVED = {".md": "text/html; charset=utf-8", ".png": "image/png"}
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/style.css">
<style>html, body {{ height: auto; }} body {{ overflow: auto; }}
.help {{ display: block; }}
.help {{ max-width: 820px; margin: 0 auto; padding: 24px 20px 60px; line-height: 1.55; }}
.help pre {{ background: var(--sunken); padding: 10px 12px; border-radius: 8px; overflow-x: auto; }}
.help code {{ font-family: "JetBrains Mono", monospace; font-size: 0.92em; }}
.help img {{ max-width: 100%; border-radius: 8px; }} .help a {{ color: var(--accent); }}
.help h1, .help h2, .help h3 {{ font-family: "Sora", system-ui, sans-serif; }}</style>
</head><body><main class="help">{body}</main></body></html>
"""


def _inline(text: str) -> str:
    """Escape, then code spans, bold, images and links."""
    parts = re.split(r"(`[^`]*`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        s = html.escape(part, quote=False)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(
            r"!\[([^\]]*)\]\(([^)\s]+)\)",
            lambda m: f'<img alt="{m.group(1)}" src="{_href(m.group(2))}">',
            s,
        )
        s = re.sub(
            r"\[([^\]]+)\]\(([^)\s]+)\)",
            lambda m: f'<a href="{_href(m.group(2))}">{m.group(1)}</a>',
            s,
        )
        out.append(s)
    return "".join(out)


def _href(target: str) -> str:
    """A link from a doc: another doc or an image beside it is served here; the rest as written."""
    if re.match(r"^[A-Za-z0-9_.-]+\.(md|png)(#[\w-]*)?$", target):
        return "/help/" + target
    if target.startswith(("http://", "https://", "#")):
        return html.escape(target)
    return "#"


def render(markdown: str) -> str:
    """docs/ Markdown to HTML."""
    lines = markdown.splitlines()
    out: list[str] = []
    para: list[str] = []
    lists: list[tuple[int, str]] = []  # open lists: (indent, "ul" or "ol")
    item_open: list[bool] = []
    k = 0

    def end_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_lists(to_indent: int = -1) -> None:
        while lists and lists[-1][0] > to_indent:
            if item_open and item_open[-1]:
                out.append("</li>")
            item_open.pop()
            out.append(f"</{lists.pop()[1]}>")

    while k < len(lines):
        line = lines[k]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("```"):
            end_para()
            if not lists or indent <= lists[-1][0]:
                close_lists(indent - 1)  # a block at an item's level or the margin ends it
            code = []
            k += 1
            while k < len(lines) and not lines[k].strip().startswith("```"):
                code.append(lines[k][indent:] if lines[k][:indent].strip() == "" else lines[k])
                k += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code))}</code></pre>")
            k += 1
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m and indent == 0:
            end_para()
            close_lists()
            n = len(m.group(1))
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif m := re.match(r"^([-*]|\d+\.)\s+(.*)$", stripped):
            end_para()
            kind = "ol" if m.group(1)[0].isdigit() else "ul"
            if not lists or indent > lists[-1][0]:
                out.append(f"<{kind}>")
                lists.append((indent, kind))
                item_open.append(False)
            else:
                close_lists(indent)
                if lists and lists[-1][1] != kind:  # a list of the other kind at this level
                    close_lists(indent - 1)
                if not lists:
                    out.append(f"<{kind}>")
                    lists.append((indent, kind))
                    item_open.append(False)
            if item_open[-1]:
                out.append("</li>")
            out.append(f"<li>{_inline(m.group(2))}")
            item_open[-1] = True
        elif not stripped:
            end_para()
        elif lists and indent > lists[-1][0]:
            out.append(" " + _inline(stripped))  # an item's next line
        else:
            close_lists()
            para.append(stripped)
        k += 1
    end_para()
    close_lists()
    return "\n".join(out)


def served() -> set[str]:
    """The guide, the docs and images it links to, and theirs: other docs (runbooks) stay off."""
    out, todo = {GUIDE}, [GUIDE]
    for _ in range(2):
        found = set()
        for name in todo:
            path = DOCS / name
            if name.endswith(".md") and path.is_file():
                found |= set(re.findall(r"\]\(([A-Za-z0-9_.-]+\.(?:md|png))", path.read_text()))
        todo = sorted(found - out)
        out |= found
    return out


def page(name: str = GUIDE) -> tuple[bytes, str] | None:
    """(body, content type) for the guide or a doc or image it links to, else None."""
    if not re.match(r"^[A-Za-z0-9_.-]+$", name) or name.startswith(".") or name not in served():
        return None
    path = DOCS / name
    if path.suffix not in SERVED or not path.is_file() or not path.resolve().is_relative_to(DOCS):
        return None
    if path.suffix == ".png":
        return path.read_bytes(), SERVED[".png"]
    text = path.read_text()
    title = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), name)
    return PAGE.format(title=html.escape(title), body=render(text)).encode(), SERVED[".md"]
