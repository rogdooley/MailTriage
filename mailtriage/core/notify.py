from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import webbrowser
from pathlib import Path


def notify(title: str, message: str) -> None:
    """
    Best-effort desktop notification.

    This is intentionally dependency-free. If no notification mechanism is
    available, it silently no-ops (stdout/stderr logging should still exist).
    """
    if os.environ.get("MAILTRIAGE_DISABLE_NOTIFICATIONS"):
        return

    plat = sys.platform

    # macOS: avoid AppleScript notifications because clicking them often opens Script Editor.
    # Use terminal-notifier if installed; otherwise, no-op.
    if plat == "darwin" and shutil.which("terminal-notifier"):
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message],
            check=False,
        )
        return

    # Linux (common desktop environments)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], check=False)
        return

    # Windows (best-effort PowerShell popup)
    if plat.startswith("win") and shutil.which("powershell"):
        ps = (
            "Add-Type -AssemblyName PresentationFramework; "
            f"[System.Windows.MessageBox]::Show('{message}', '{title}') | Out-Null"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        return


def open_file_in_browser(path) -> None:
    try:
        open_uri(path.as_uri())
    except Exception:
        pass


def open_uri(uri: str) -> None:
    plat = sys.platform
    if plat == "darwin" and shutil.which("open"):
        subprocess.run(["open", uri], check=False)
        return
    if plat.startswith("linux") and shutil.which("xdg-open"):
        subprocess.run(["xdg-open", uri], check=False)
        return
    webbrowser.open(uri)


def copy_to_clipboard(text: str) -> bool:
    plat = sys.platform

    if plat == "darwin" and shutil.which("pbcopy"):
        try:
            subprocess.run(["pbcopy"], input=text, text=True, check=False)
            return True
        except Exception:
            return False

    if plat.startswith("linux"):
        if shutil.which("wl-copy"):
            try:
                subprocess.run(["wl-copy"], input=text, text=True, check=False)
                return True
            except Exception:
                return False
        if shutil.which("xclip"):
            try:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=text,
                    text=True,
                    check=False,
                )
                return True
            except Exception:
                return False

    return False


def show_command_page(title: str, message: str, command: str) -> None:
    """
    Open a local HTML page in the user's browser that shows a copy/paste command.
    This avoids OS-specific GUI scripting (AppleScript, etc).
    """
    _ = copy_to_clipboard(command)

    safe_title = title
    safe_message = message
    safe_command = command

    html = textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>{_html_escape(safe_title)}</title>
            <style>
              :root {{
                --bg: #0b1020;
                --panel: #111a33;
                --text: #e8ecff;
                --muted: rgba(232, 236, 255, 0.75);
                --border: rgba(232, 236, 255, 0.16);
                --btn: #e8ecff;
                --btnText: #0b1020;
                --btn2: transparent;
                --btn2Text: #e8ecff;
              }}
              html, body {{ height: 100%; }}
              body {{
                margin: 0;
                font: 15px/1.4 ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, \"Apple Color Emoji\", \"Segoe UI Emoji\";
                color: var(--text);
                background: radial-gradient(1100px 500px at 20% 0%, rgba(125, 159, 255, 0.35), transparent 70%),
                            radial-gradient(900px 500px at 95% 15%, rgba(79, 255, 202, 0.18), transparent 60%),
                            var(--bg);
              }}
              .wrap {{ max-width: 900px; margin: 0 auto; padding: 28px 18px 46px; }}
              .panel {{
                background: color-mix(in srgb, var(--panel) 92%, transparent);
                border: 1px solid var(--border);
                border-radius: 14px;
                padding: 18px;
                box-shadow: 0 20px 70px rgba(0,0,0,0.45);
              }}
              h1 {{ margin: 0 0 6px; font-size: 20px; letter-spacing: 0.2px; }}
              p {{ margin: 0 0 14px; color: var(--muted); white-space: pre-wrap; }}
              textarea {{
                width: 100%;
                min-height: 110px;
                resize: vertical;
                font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace;
                color: var(--text);
                background: rgba(0,0,0,0.25);
                border: 1px solid var(--border);
                border-radius: 10px;
                padding: 12px;
                box-sizing: border-box;
              }}
              .row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
              button {{
                border: 1px solid var(--border);
                border-radius: 10px;
                padding: 10px 12px;
                font-weight: 650;
                cursor: pointer;
              }}
              .primary {{ background: var(--btn); color: var(--btnText); border-color: transparent; }}
              .secondary {{ background: var(--btn2); color: var(--btn2Text); }}
              .status {{ margin-top: 10px; color: var(--muted); }}
              .hint {{ margin-top: 14px; color: var(--muted); font-size: 13px; }}
            </style>
          </head>
          <body>
            <div class="wrap">
              <div class="panel">
                <h1>{_html_escape(safe_title)}</h1>
                <p>{_html_escape(safe_message)}</p>
                <textarea id="cmd" spellcheck="false">{_html_escape(safe_command)}</textarea>
                <div class="row">
                  <button class="primary" id="copy">Copy Command</button>
                  <button class="secondary" id="select">Select All</button>
                </div>
                <div class="status" id="status"></div>
                <div class="hint">
                  If the Copy button fails (browser permissions), click Select All and copy manually.
                </div>
              </div>
            </div>
            <script>
              const cmd = document.getElementById('cmd');
              const status = document.getElementById('status');
              function setStatus(t) {{ status.textContent = t; }}
              document.getElementById('select').addEventListener('click', () => {{
                cmd.focus();
                cmd.select();
                setStatus('Selected.');
              }});
              document.getElementById('copy').addEventListener('click', async () => {{
                try {{
                  await navigator.clipboard.writeText(cmd.value);
                  setStatus('Copied to clipboard.');
                }} catch (e) {{
                  cmd.focus();
                  cmd.select();
                  setStatus('Clipboard blocked. Selected instead; copy manually.');
                }}
              }});
            </script>
          </body>
        </html>
        """
    )

    ts = int(time.time())
    out = Path(tempfile.gettempdir()) / f"mailtriage-command-{ts}.html"
    out.write_text(html, encoding="utf-8")
    try:
        open_uri(out.as_uri())
    except Exception:
        # Fall back to whatever notifications are available.
        notify(title, f"{message}\n\n{command}")


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
