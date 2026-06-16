"""Pure text-processing utilities shared between api.py and tests.

No FastAPI / slowapi / network imports — safe to import in unit tests.
"""
from __future__ import annotations

import re
from datetime import datetime


def _clean_response(text: str) -> str:
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\*(?!\s)(.+?)(?<!\s)\*", r"\1", text)
    text = re.sub(r"_(?!\s)(.+?)(?<!\s)_", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\n[-*_]{3,}\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_time_slots(text: str) -> tuple[str | None, list[str]]:
    slot_pattern = re.compile(
        r"\b(\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM))\b"
    )
    slots = slot_pattern.findall(text)
    date_pattern = re.compile(
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?)",
        re.IGNORECASE,
    )
    date_match = date_pattern.search(text)
    date_label = date_match.group(1) if date_match else None
    return date_label, [s.strip() for s in slots]


def _strip_slots_from_text(text: str) -> str:
    text = re.sub(
        r"\n\s*[-•]\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(
        r"\n\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*[–\-]\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
        "",
        text,
    )
    text = re.sub(r"Which of these works best for you\?\s*", "", text)
    return text.strip()


def _enhance_slots_for_voice(slots_text: str, tenant_id: str = "default", _today=None) -> str:
    """Append ISO timestamps to each slot line so the voice agent can pass them to book_appointment.

    Input line:  "Tuesday, May 27 10:00 AM - 10:30 AM"
    Output line: "... | start_iso=2026-05-27T10:00:00-04:00 end_iso=2026-05-27T10:30:00-04:00"

    Year-boundary: if slot month < current month, infers next year.
    """
    from datetime import date as _date

    import pytz

    from tenants import load_tenant

    tz = pytz.timezone(load_tenant(tenant_id).business_tz)
    today = _today or _date.today()
    year = today.year
    slot_re = re.compile(
        r"^(.+?,\s+\w+\s+\d+)\s+(\d{1,2}:\d{2}\s*(?:AM|PM))\s*[–\-]\s*(\d{1,2}:\d{2}\s*(?:AM|PM))$",
        re.IGNORECASE,
    )
    lines = []
    for line in slots_text.splitlines():
        m = slot_re.match(line.strip())
        if m:
            date_part, start_str, end_str = m.group(1), m.group(2).strip(), m.group(3).strip()
            try:
                # Infer year: if slot month < current month the slot is in the next year.
                probe = datetime.strptime(f"{date_part} {year}", "%A, %B %d %Y")
                inferred_year = year + 1 if probe.month < today.month else year
                start_dt = datetime.strptime(
                    f"{date_part} {start_str} {inferred_year}", "%A, %B %d %I:%M %p %Y"
                )
                start_dt = tz.localize(start_dt)
                end_dt = start_dt.replace(
                    hour=datetime.strptime(end_str, "%I:%M %p").hour,
                    minute=datetime.strptime(end_str, "%I:%M %p").minute,
                )
                lines.append(
                    f"{line.strip()} | start_iso={start_dt.isoformat()} end_iso={end_dt.isoformat()}"
                )
                continue
            except Exception:
                pass
        lines.append(line)
    return "\n".join(lines)
