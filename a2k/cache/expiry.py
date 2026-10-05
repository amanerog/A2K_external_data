"""Expiry rule (proposal Annex A8, with the base date refined in the working
session of 2026-10-01 -- vendors such as Cala capture data weeks or months
before serving it, so counting from that date left most entries expired on
arrival):
- base date: for a dated snapshot (news, report, a figure for a period) the
  date of the data, if the judge found one; for data the vendor presents as
  current (registry record, rule in force, current board), and whenever there
  is no date, now -- the moment it is admitted;
- validity: that of the primary domain; with two domains, the shorter one;
- historical data (absolute time anchor entirely in the past): the
  historical validity;
- if base + validity is already in the past, the entry is not activated.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .models import Classification
from .taxonomy import DOMAINS, HISTORICAL, HISTORICAL_VALIDITY_DAYS


def default_policy() -> dict[str, timedelta]:
    policy = {code: timedelta(days=d.default_validity_days) for code, d in DOMAINS.items()}
    policy[HISTORICAL] = timedelta(days=HISTORICAL_VALIDITY_DAYS)
    return policy


def _is_historical(classification: Classification, today: date) -> bool:
    # The classifier also returns entirely_past; recomputed here from the
    # period's end so a misjudged flag cannot make live data "historical".
    anchor = classification.time_anchor
    if anchor.get("kind") != "absolute":
        return False
    end = anchor.get("to") or anchor.get("from")
    try:
        return date.fromisoformat(str(end)[:10]) < today
    except (TypeError, ValueError):
        return False


def expires_at(
    classification: Classification,
    data_as_of: date | None,
    *,
    now: datetime,
    policy: dict[str, timedelta],
    data_kind: str = "snapshot",
) -> datetime:
    if _is_historical(classification, now.date()):
        validity = policy[HISTORICAL]
    else:
        validity = min(policy[code] for code in classification.domains)
    if data_kind == "snapshot" and data_as_of:
        base = datetime.combine(data_as_of, time.min, tzinfo=timezone.utc)
    else:
        base = now
    # A data date in the future is a misread, not fresher data.
    return min(base, now) + validity
