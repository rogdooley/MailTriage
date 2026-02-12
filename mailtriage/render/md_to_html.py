from __future__ import annotations

import html
from pathlib import Path


def markdown_to_html_body(markdown_text: str) -> str:
    """
    Minimal markdown-to-HTML renderer tuned for MailTriage output.

    We avoid adding heavyweight dependencies; output markdown is simple:
    - headings (#, ##, ###)
    - horizontal rule (---)
    - bullet lists (- ...)
    - basic emphasis (**bold**, _italics_)
    """
    lines = markdown_text.splitlines()
    out: list[str] = []
    in_ul = False

    def flush_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def inline(s: str) -> str:
        # Escape first, then re-introduce a small subset of formatting.
        esc = html.escape(s)
        # Bold: **text**
        esc = _replace_pairs(esc, "**", "<strong>", "</strong>")
        # Italics: _text_
        esc = _replace_pairs(esc, "_", "<em>", "</em>")
        return esc

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_ul()
            out.append('<div class="sp"></div>')
            continue

        if line.strip() == "---":
            flush_ul()
            out.append("<hr />")
            continue

        if line.startswith("### "):
            flush_ul()
            out.append(f"<h3>{inline(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            flush_ul()
            out.append(f"<h2>{inline(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            flush_ul()
            out.append(f"<h1>{inline(line[2:])}</h1>")
            continue

        stripped = line.lstrip()
        if stripped.startswith("- "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
            continue

        flush_ul()
        out.append(f"<p>{inline(line)}</p>")

    flush_ul()
    return "\n".join(out)


def render_report_html(*, title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #ffffff;
      --text: #0f172a;
      --muted: rgba(15, 23, 42, 0.70);
      --border: rgba(15, 23, 42, 0.14);
      --accent: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
      padding: 20px 18px 44px;
    }}
    .doc {{ max-width: 980px; margin: 0 auto; }}
    h1 {{ font-size: 22px; margin: 0 0 10px; letter-spacing: 0.2px; }}
    h2 {{ font-size: 16px; margin: 18px 0 10px; }}
    h3 {{ font-size: 14px; margin: 14px 0 8px; color: var(--accent); }}
    p {{ margin: 8px 0; }}
    ul {{ margin: 8px 0 8px 20px; padding: 0; }}
    li {{ margin: 6px 0; }}
    hr {{ border: 0; border-top: 1px solid var(--border); margin: 16px 0; }}
    .sp {{ height: 6px; }}
    em {{ color: var(--muted); }}
  </style>
</head>
<body>
  <div class="doc">
    {body_html}
  </div>
</body>
</html>
"""


def write_report_html(md_path: Path, html_path: Path) -> None:
    md = md_path.read_text(encoding="utf-8")
    title = md.splitlines()[0].lstrip("# ").strip() if md else "MailTriage"
    body = markdown_to_html_body(md)
    html_doc = render_report_html(title=title, body_html=body)
    html_path.write_text(html_doc, encoding="utf-8")


def _replace_pairs(s: str, token: str, open_tag: str, close_tag: str) -> str:
    # Replace paired tokens left-to-right. Good enough for this constrained markdown.
    out: list[str] = []
    i = 0
    on = False
    tlen = len(token)
    while i < len(s):
        if s.startswith(token, i):
            out.append(open_tag if not on else close_tag)
            on = not on
            i += tlen
            continue
        out.append(s[i])
        i += 1
    if on:
        # Unbalanced token: fall back by re-inserting the token.
        return s
    return "".join(out)

