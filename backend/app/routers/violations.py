"""
violations.py
-------------
The worklist. Always paginated -- a run can produce 1.5-2M violation rows,
so "give me the whole list" is never an acceptable query. CSV export streams
in chunks for the same reason.
"""

import csv
import io
from typing import Optional
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DQViolation
from ..schemas import ViolationOut

router = APIRouter(prefix="/api/violations", tags=["violations"])

EXPORT_CHUNK_SIZE = 5000


def _filtered(db: Session, run_id: int, object_id: Optional[int], severity: Optional[str],
              rule_id: Optional[int]):
    q = db.query(DQViolation).filter(DQViolation.run_id == run_id)
    if object_id is not None:
        q = q.filter(DQViolation.object_id == object_id)
    if severity is not None:
        q = q.filter(DQViolation.severity == severity)
    if rule_id is not None:
        q = q.filter(DQViolation.rule_id == rule_id)
    return q


@router.get("", response_model=list[ViolationOut])
def list_violations(
    run_id: int,
    object_id: Optional[int] = None,
    severity: Optional[str] = None,
    rule_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 100,
    db: Session = Depends(get_db),
):
    page_size = min(page_size, 500)  # hard cap -- never let a client ask for everything
    q = _filtered(db, run_id, object_id, severity, rule_id)
    return q.order_by(DQViolation.violation_id).offset((page - 1) * page_size).limit(page_size).all()


@router.get("/export")
def export_violations(
    run_id: int,
    object_id: Optional[int] = None,
    severity: Optional[str] = None,
    rule_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    def generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["record_key", "element_id", "rule_id", "current_value",
                          "violation_reason", "severity", "dimension"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)

        q = _filtered(db, run_id, object_id, severity, rule_id).order_by(DQViolation.violation_id)
        offset = 0
        while True:
            chunk = q.offset(offset).limit(EXPORT_CHUNK_SIZE).all()
            if not chunk:
                break
            for v in chunk:
                writer.writerow([v.record_key, v.element_id, v.rule_id, v.current_value,
                                  v.violation_reason, v.severity, v.dimension])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            offset += EXPORT_CHUNK_SIZE

    return StreamingResponse(
        generate(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=violations_run_{run_id}.csv"},
    )
