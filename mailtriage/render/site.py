from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReportEntry:
    label: str  # YYYY-MM-DD
    rel_html_path: str  # YYYY/MM/DD.html


_PATH_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})\.html$")


def _scan_reports(rootdir: Path) -> list[ReportEntry]:
    out: list[ReportEntry] = []
    for p in rootdir.rglob("*.html"):
        # Skip viewer file itself and local state.
        if p.name == "index.html":
            continue
        if ".mailtriage" in p.parts:
            continue
        rel = p.relative_to(rootdir).as_posix()
        m = _PATH_RE.match(rel)
        if not m:
            continue
        yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
        out.append(ReportEntry(label=f"{yyyy}-{mm}-{dd}", rel_html_path=rel))
    out.sort(key=lambda e: e.label)
    return out


def render_index(rootdir: Path) -> None:
    """Write static index.html under rootdir. No server required."""
    reports = _scan_reports(rootdir)
    reports.sort(key=lambda e: e.label, reverse=True)  # newest -> oldest

    try:
        limit = int((os.environ.get("MAILTRIAGE_VIEW_DAYS", "14") or "14").strip())
    except ValueError:
        limit = 14
    if limit < 0:
        limit = 14

    total = len(reports)
    if limit == 0 or limit >= total:
        visible = reports
        hidden: list[ReportEntry] = []
    else:
        visible = reports[:limit]
        hidden = reports[limit:]

    items_visible = "\n".join(
        f'<a class="item" href="#{e.rel_html_path}" data-path="{e.rel_html_path}">{e.label}</a>'
        for e in visible
    )
    items_hidden = "\n".join(
        f'<a class="item extra" href="#{e.rel_html_path}" data-path="{e.rel_html_path}" style="display:none;">{e.label}</a>'
        for e in hidden
    )
    items = "\n".join([items_visible, items_hidden]).strip()
    default_path = reports[0].rel_html_path if reports else ""

    html = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>MailTriage</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: rgba(17, 26, 51, 0.92);
      --text: #e8ecff;
      --muted: rgba(232, 236, 255, 0.72);
      --border: rgba(232, 236, 255, 0.16);
      --accent: #7d9fff;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; }}
    body {{
      margin: 0;
      color: var(--text);
      background: radial-gradient(1100px 500px at 20% 0%, rgba(125,159,255,0.35), transparent 70%),
                  radial-gradient(900px 500px at 95% 15%, rgba(79,255,207,0.18), transparent 60%),
                  var(--bg);
      font: 14px/1.4 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
    }}
    .app {{ display: grid; grid-template-columns: 320px 1fr; height: 100%; }}
    .side {{
      border-right: 1px solid var(--border);
      background: var(--panel);
      padding: 14px 12px;
      overflow: auto;
    }}
    .brand {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      padding: 6px 6px 10px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 10px;
    }}
    .brand h1 {{ font-size: 16px; margin: 0; letter-spacing: 0.2px; }}
    .brand .meta {{
      color: var(--muted);
      font-size: 12px;
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    .toggle {{
      border: 1px solid var(--border);
      background: rgba(0,0,0,0.10);
      color: var(--text);
      border-radius: 10px;
      padding: 6px 8px;
      font-weight: 650;
      cursor: pointer;
      font-size: 12px;
    }}
    .toggle:hover {{ background: rgba(0,0,0,0.16); }}
    .list {{ display: flex; flex-direction: column; gap: 4px; padding: 2px; }}
    .item {{
      display: block;
      padding: 10px 10px;
      border-radius: 10px;
      text-decoration: none;
      color: var(--text);
      border: 1px solid transparent;
    }}
    .item:hover {{ border-color: var(--border); background: rgba(0,0,0,0.12); }}
    .item.active {{ border-color: rgba(125,159,255,0.45); background: rgba(125,159,255,0.10); }}
    .main {{ padding: 0; overflow: hidden; }}
    iframe {{ width: 100%; height: 100%; border: 0; background: white; }}
    .empty {{
      height: 100%;
      display: grid;
      place-items: center;
      color: var(--muted);
      padding: 24px;
    }}
    @media (max-width: 900px) {{
      .app {{ grid-template-columns: 1fr; grid-template-rows: 260px 1fr; }}
      .side {{ border-right: 0; border-bottom: 1px solid var(--border); }}
    }}
  </style>
</head>
<body>
  <div class=\"app\">
    <aside class=\"side\">
      <div class=\"brand\">
        <h1>MailTriage</h1>
        <div class=\"meta\">
          <span id=\"count\">{len(visible)} of {total} days</span>
          <button id=\"toggle\" class=\"toggle\" type=\"button\" style=\"display:{'inline-flex' if hidden else 'none'};\">Show all</button>
        </div>
      </div>
      <nav class=\"list\" id=\"list\">
        {items or '<div class="empty">No reports yet.</div>'}
      </nav>
    </aside>
    <main class=\"main\">
      <div id=\"empty\" class=\"empty\" style=\"display:none;\">Select a report.</div>
      <iframe id=\"frame\" title=\"Report\"></iframe>
    </main>
  </div>
  <script>
    const list = document.getElementById('list');
    const frame = document.getElementById('frame');
    const empty = document.getElementById('empty');
    const defaultPath = {default_path!r};
    const toggle = document.getElementById('toggle');
    const count = document.getElementById('count');
    const total = {total};
    const visibleCount = {len(visible)};
    let showAll = false;

    function setExtrasVisible(on) {{
      const extras = list.querySelectorAll('a.item.extra');
      extras.forEach(a => a.style.display = on ? 'block' : 'none');
      showAll = on;
      if (toggle) toggle.textContent = on ? 'Show recent' : 'Show all';
      if (count) count.textContent = (on ? total : visibleCount) + ' of ' + total + ' days';
    }}

    if (toggle) {{
      toggle.addEventListener('click', () => setExtrasVisible(!showAll));
    }}

    function setActive(path) {{
      const links = list.querySelectorAll('a.item');
      links.forEach(a => a.classList.toggle('active', a.dataset.path === path));
    }}

    function loadFromHash() {{
      let path = (location.hash || '').replace(/^#/, '');
      if (!path) path = defaultPath;
      if (!path) {{
        frame.style.display = 'none';
        empty.style.display = 'grid';
        return;
      }}

      const link = list.querySelector(`a.item[data-path="${{CSS.escape(path)}}"]`);
      if (link && link.classList.contains('extra')) {{
        setExtrasVisible(true);
      }}

      frame.style.display = 'block';
      empty.style.display = 'none';
      frame.src = path;
      setActive(path);
    }}

    window.addEventListener('hashchange', loadFromHash);
    setExtrasVisible(false);
    loadFromHash();
  </script>
</body>
</html>
"""

    (rootdir / "index.html").write_text(html, encoding="utf-8")
