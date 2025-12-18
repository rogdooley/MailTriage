from __future__ import annotations

from dataclasses import dataclass
from email.message import Message

__all__ = [
    "ExtractedContent",
    "select_body",
    "extract_new_text",
    "extract_attachment_names",
]


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


def select_body(msg: Message) -> tuple[str | None, bool]:
    """
    Returns (text, is_html).
    """
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue
            subtype = part.get_content_subtype()
            if subtype == "plain":
                return _decode_part(part), False
            if subtype == "html":
                return _decode_part(part), True
        return None, False
    else:
        if msg.get_content_type() == "text/plain":
            return _decode_part(msg), False
        if msg.get_content_type() == "text/html":
            return _decode_part(msg), True
    return None, False


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
