from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from email.message import Message

__all__ = [
    "ExtractedContent",
    "select_body",
    "extract_new_text",
    "extract_attachment_names",
]

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_END_RE = re.compile(r"(?i)</(p|div|li|tr|h1|h2|h3|h4|h5|h6)>")

_HTML_MARKERS = (
    "<html",
    "<head",
    "<body",
    "<style",
    "<script",
    "<table",
    "<div",
    "<span",
    "<meta",
    "<!doctype",
)

_QUOTE_MARKERS = (
    ">",
    "on ",
)

_SIGNATURE_MARKERS = (
    "--",
    "thanks,",
    "thank you,",
    "best,",
    "regards,",
)


@dataclass(frozen=True)
class ExtractedContent:
    source: str  # "body" | "subject" | "none"
    text: str  # extracted_new_text
    trimmed_quote: bool
    trimmed_signature: bool
    has_structured_block: bool


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)

    if not isinstance(payload, (bytes, bytearray)):
        return ""

    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def looks_like_html(text: str) -> bool:
    if not text:
        return False

    sample = text.lstrip()[:2048].lower()

    return any(marker in sample for marker in _HTML_MARKERS)


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = _SCRIPT_STYLE_RE.sub("", s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_END_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    # normalize whitespace
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = "\n".join(line.strip() for line in s.splitlines())
    return s.strip()


def select_body(msg: Message) -> tuple[str, bool]:
    """
    Returns (text, is_html_source).
    Always returns *plain text* suitable for Markdown.
    """
    text_plain: str | None = None
    text_html: str | None = None

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()

            # skip attachments
            if "attachment" in disp:
                continue

            if ctype == "text/plain" and text_plain is None:
                text_plain = _decode_part(part)

            elif ctype == "text/html" and text_html is None:
                text_html = _decode_part(part)
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "text/plain":
            text_plain = _decode_part(msg)
        elif ctype == "text/html":
            text_html = _decode_part(msg)

    if text_plain and text_plain.strip():
        if not looks_like_html(text_plain):
            return text_plain, False

    if text_html and text_html.strip():
        return html_to_text(text_html), True

    return "", False


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip() == "":
            blank += 1
            if blank <= 2:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip()


def normalize_excerpt(
    s: str,
    *,
    max_lines: int = 5,
    max_chars: int = 700,
) -> str:
    if not s:
        return ""

    lines_out: list[str] = []
    for raw in s.splitlines():
        ln = raw.strip()
        if not ln:
            continue

        low = ln.lower()

        if ln.startswith(">"):
            break

        if low.startswith("on ") and "wrote:" in low:
            break

        if low in _SIGNATURE_MARKERS or low.startswith(_SIGNATURE_MARKERS):
            break

        lines_out.append(ln)
        if len(lines_out) >= max_lines:
            break

    text = "\n".join(lines_out)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"

    return text


def strip_structured_blocks(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    structured = False
    result: list[str] = []

    in_block = True
    for ln in lines:
        if in_block and (ln.startswith(" ") or ":" in ln[:20]):
            structured = True
            continue
        in_block = False
        result.append(ln)

    return "\n".join(result).strip(), structured


QUOTE_PREFIXES = ("on ", "from:", "sent:", "-----original message-----")


def strip_quotes(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        low = ln.lower().strip()
        if low.startswith(">"):
            return "\n".join(lines[:i]).strip(), True
        if any(low.startswith(p) for p in QUOTE_PREFIXES):
            return "\n".join(lines[:i]).strip(), True
    return text, False


def strip_signature(text: str) -> tuple[str, bool]:
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() in ("--", "-- "):
            return "\n".join(lines[:i]).strip(), True
    return text, False


def extract_new_text(*, subject: str, body: str | None) -> ExtractedContent:
    if body is None or body.strip() == "":
        if subject.strip():
            return ExtractedContent(
                source="subject",
                text=subject.strip(),
                trimmed_quote=False,
                trimmed_signature=False,
                has_structured_block=False,
            )
        return ExtractedContent("none", "", False, False, False)

    text = normalize_text(body)
    text, structured = strip_structured_blocks(text)
    text, trimmed_q = strip_quotes(text)
    text, trimmed_s = strip_signature(text)

    if not text and subject.strip():
        return ExtractedContent(
            source="subject",
            text=subject.strip(),
            trimmed_quote=trimmed_q,
            trimmed_signature=trimmed_s,
            has_structured_block=structured,
        )

    return ExtractedContent(
        source="body",
        text=text,
        trimmed_quote=trimmed_q,
        trimmed_signature=trimmed_s,
        has_structured_block=structured,
    )


def extract_attachment_names(msg: Message) -> list[str]:
    names: list[str] = []
    if not msg.is_multipart():
        return names
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            fn = part.get_filename()
            if fn:
                names.append(fn)
    return names
