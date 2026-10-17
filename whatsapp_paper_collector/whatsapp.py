from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .models import ChatMessage, LinkOccurrence
from .urls import normalize_url


_FORMAT_MARKS = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"
_HEADER_RE = re.compile(
    rf"^[{_FORMAT_MARKS}]*\[(?P<timestamp>[^]]+)]\s*(?P<body>.*)$"
)
_URL_RE = re.compile(r"https?://[^\s<>\"“”]+", re.IGNORECASE)


def _parse_timestamp(raw: str, date_order: str) -> datetime | None:
    cleaned = re.sub(r"\s+", " ", raw.replace("\u202f", " ")).strip()
    date_formats = ("%d/%m/%y", "%d/%m/%Y") if date_order == "dmy" else ("%m/%d/%y", "%m/%d/%Y")
    time_formats = ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M")
    for date_format in date_formats:
        for time_format in time_formats:
            try:
                return datetime.strptime(cleaned, f"{date_format}, {time_format}")
            except ValueError:
                continue
    return None


def parse_chat_text(text: str, date_order: str = "dmy") -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    current: ChatMessage | None = None

    for line in text.splitlines():
        match = _HEADER_RE.match(line)
        if match:
            if current is not None:
                messages.append(current)

            timestamp_raw = match.group("timestamp")
            body = match.group("body").strip(_FORMAT_MARKS + " \t\u202f")
            sender, separator, message_text = body.partition(": ")
            if not separator:
                sender = ""
                message_text = body
            current = ChatMessage(
                timestamp_raw=timestamp_raw,
                timestamp=_parse_timestamp(timestamp_raw, date_order),
                sender=sender.strip(_FORMAT_MARKS + " \t\u202f"),
                text=message_text,
            )
        elif current is not None:
            current = ChatMessage(
                timestamp_raw=current.timestamp_raw,
                timestamp=current.timestamp,
                sender=current.sender,
                text=f"{current.text}\n{line}",
            )

    if current is not None:
        messages.append(current)
    return messages


def parse_chat_file(path: Path, date_order: str = "dmy") -> list[ChatMessage]:
    return parse_chat_text(path.read_text(encoding="utf-8-sig"), date_order=date_order)


def _trim_url(raw_url: str) -> str:
    url = raw_url.rstrip(".,;:!?")
    pairs = ((")", "("), ("]", "["), ("}", "{"))
    changed = True
    while changed and url:
        changed = False
        for closing, opening in pairs:
            if url.endswith(closing) and url.count(closing) > url.count(opening):
                url = url[:-1]
                changed = True
    return url


def extract_links(messages: list[ChatMessage]) -> list[LinkOccurrence]:
    links: list[LinkOccurrence] = []
    for message in messages:
        for match in _URL_RE.finditer(message.text):
            original_url = _trim_url(match.group(0))
            try:
                normalized_url = normalize_url(original_url)
            except ValueError:
                continue
            links.append(
                LinkOccurrence(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    message=message,
                )
            )
    return links
