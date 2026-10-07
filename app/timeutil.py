from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_epoch(v: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(v, tz=timezone.utc) if v else None


def overlaps(a_start: Optional[datetime], a_end: Optional[datetime], b_start: datetime, b_end: datetime) -> bool:
    """Open-ended intervals are treated as unbounded on that side."""
    return (a_start is None or a_start <= b_end) and (a_end is None or a_end >= b_start)


def fmt_utc(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime("%b %d %H:%MZ") if dt else "?"
