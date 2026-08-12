"""
violations.py
-------------
The worklist. Always paginated -- a single rule on a 1M-row object produced
499,520 violations in scale testing, so "give me the whole list" is never an
acceptable query.

WHY KEYSET AND NOT OFFSET
-------------------------
`LIMIT 100 OFFSET 400000` makes the database walk and discard 400,000 rows to
return 100. Cost grows with how deep you are, so paging through half a million
violations is quadratic -- and the CSV export, which walks every page, is where
that actually bites.

Keyset paging asks `WHERE violation_id > :after_id ORDER BY violation_id
LIMIT n` instead. That is an index range scan positioned directly at the
cursor: the same cost for page 5,000 as for page 1. It is also stable while
rows are being inserted, where OFFSET silently shifts and repeats rows.

The trade-off is that you cannot jump to "page 400" -- only next/previous. For
a worklist that is the right shape anyway.
"""

import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ValViolation
from ..schemas import ViolationOut

router = APIRouter(prefix="/api/violations", tags=["violations"])

EXPORT_CHUNK_SIZE = 5000
MAX_PAGE_SIZE = 500


def _filtered(db: Session, run_id: int, entity_name: Optional[str],
              severity: Optional[str], rule_id: Optional[str]):
    q = db.query(ValViolation).filter(ValViolation.run_id == run_id)
    if entity_name is not None:
        q = q.filter(ValViolation.entity_name == entity_name)
    if severity is not None:
        q = q.filter(ValViolation.severity == severity)
    if rule_id is not None:
        q = q.filter(ValViolation.rule_id == rule_id)
    return q.order_by(ValViolation.violation_id)


def _page(q, after_id: Optional[int], page_size: int):
    """
    One extra row is fetched to answer "is there more?" without a COUNT -- a
    COUNT over half a million filtered rows costs more than the page itself.
    """
    if after_id is not None:
        q = q.filter(ValViolation.violation_id > after_id)
    rows = q.limit(page_size + 1).all()
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    return rows, has_more


@router.get("")
def list_violations(
    run_id: int,
    entity_name: Optional[str] = None,
    severity: Optional[str] = None,
    rule_id: Optional[str] = None,
    after_id: Optional[int] = Query(
        None, description="violation_id of the last row you saw; omit for the first page"),
    page_size: int = 100,
    db: Session = Depends(get_db),
):
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    rows, has_more = _page(
        _filtered(db, run_id, entity_name, severity, rule_id), after_id, page_size)
    return {
        "items": [ViolationOut.from_orm(r) for r in rows],
        "page_size": page_size,
        "has_more": has_more,
        # feed this straight back as ?after_id= for the next page
        "next_cursor": rows[-1].violation_id if rows and has_more else None,
    }


@router.get("/export")
def export_violations(
    run_id: int,
    entity_name: Optional[str] = None,
    severity: Optional[str] = None,
    rule_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["record_key", "entity_name", "field_name", "rule_id",
                         "current_value", "violation_reason", "severity", "dimension"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        q = _filtered(db, run_id, entity_name, severity, rule_id)
        after_id = None
        while True:
            chunk, _ = _page(q, after_id, EXPORT_CHUNK_SIZE)
            if not chunk:
                break
            for v in chunk:
                writer.writerow([v.record_key, v.entity_name, v.field_name, v.rule_id,
                                 v.current_value, v.violation_reason, v.severity, v.dimension])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            after_id = chunk[-1].violation_id

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=violations_run_{run_id}.csv"},
    )
