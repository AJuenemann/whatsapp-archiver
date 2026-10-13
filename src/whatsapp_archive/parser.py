"""Parse the text file produced by WhatsApp's chat export."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_DATE = r"(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
_MESSAGE_LINE = re.compile(
    rf"^(?P<stamp>\[?{_DATE},\s*\d{{1,2}}:\d{{2}}(?::\d{{2}})?(?:\s?[APap][Mm])?\]?)"
    r"\s*(?:-\s*)?(?P<content>.*)$"
)


@dataclass(frozen=True)
class ExportMessage:
    timestamp: datetime
    sender: str | None
    text: str


@dataclass(frozen=True)
class ParseResult:
    messages: list[ExportMessage]
    warnings: list[str]


def _parse_timestamp(value: str, date_order: str) -> datetime:
    stamp = value.strip("[] ")
    date, time = (part.strip() for part in stamp.split(",", 1))
    time = re.sub(r"(?i)(\d)(AM|PM)$", r"\1 \2", time)

    if re.match(r"^\d{4}", date):
        date_formats = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d")
    else:
        separator = next((char for char in "/.-" if char in date), "/")
        first, second, year = date.split(separator)
        if len(first) == 4:
            date_formats = ("%Y" + separator + "%m" + separator + "%d",)
        elif date_order == "day-first":
            date_formats = ("%d" + separator + "%m" + separator + ("%y" if len(year) == 2 else "%Y"),)
        else:
            date_formats = ("%m" + separator + "%d" + separator + ("%y" if len(year) == 2 else "%Y"),)

    time_formats = ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p")
    for date_format in date_formats:
        for time_format in time_formats:
            try:
                return datetime.strptime(f"{date}, {time}", f"{date_format}, {time_format}")
            except ValueError:
                continue
    raise ValueError(f"Unsupported WhatsApp timestamp: {value}")


def parse_export(text: str, *, date_order: str = "day-first") -> ParseResult:
    """Parse export records, joining continuation lines to their message."""
    if date_order not in {"day-first", "month-first"}:
        raise ValueError("date_order must be 'day-first' or 'month-first'")

    messages: list[ExportMessage] = []
    warnings: list[str] = []
    current: ExportMessage | None = None
    ignored_prefix_lines = 0

    def flush() -> None:
        if current is not None:
            messages.append(current)

    for line in text.splitlines():
        match = _MESSAGE_LINE.match(line)
        if match:
            flush()
            content = match.group("content")
            sender, separator, body = content.partition(": ")
            current = ExportMessage(
                timestamp=_parse_timestamp(match.group("stamp"), date_order),
                sender=sender if separator else None,
                text=body if separator else content,
            )
        elif current is not None:
            current = ExportMessage(current.timestamp, current.sender, f"{current.text}\n{line}")
        elif line.strip():
            ignored_prefix_lines += 1

    flush()
    if ignored_prefix_lines:
        warnings.append(f"Ignored {ignored_prefix_lines} non-message line(s) before the first timestamp")
    if not messages:
        warnings.append("No timestamped messages were found")
    return ParseResult(messages, warnings)