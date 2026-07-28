"""
dashboard.py
------------
Every read here comes from DQ_METRIC only (never re-aggregates DQ_VIOLATION
directly) -- the dashboard is a set of cheap GROUP BY queries over a table
that's at most a few hundred rows per run, not a live scan of millions of
violations.

The OVERVIEW endpoints (kpis / heatmap / top-failing / trend / fix-profile)
aggregate across every ACTIVE object's most-recently-completed run -- the
schema always supported multiple objects (DQ_OBJECT is a table, not a
singleton), this is just the query layer catching up to the approved Mock A
design, which shows one row per object in the heatmap. The DRILLDOWN endpoint
stays single-object, as it always was.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import DQMetric, DQRun, DQElement, DQObject

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _latest_completed_run(db: Session, object_id: Optional[int] = None) -> Optional[DQRun]:
    q = db.query(DQRun).filter(DQRun.status == "completed")
    if object_id is not None:
        q = q.filter(DQRun.object_id == object_id)
    return q.order_by(DQRun.finished_at.desc()).first()


def _current_state(db: Session):
    """
    One row per active object: its latest completed run + that run's
    metrics. This is "the current state of the whole database" the overview
    page reads -- every object contributes its own most recent pass.
    """
    objects = db.query(DQObject).filter(DQObject.active_flag == True).all()  # noqa: E712
    state = []
    for obj in objects:
        run = _latest_completed_run(db, obj.object_id)
        if run is None:
            continue
        metrics = db.query(DQMetric).filter(DQMetric.run_id == run.run_id).all()
        state.append({"object": obj, "run": run, "metrics": metrics})
    return state


@router.get("/kpis")
def kpis(db: Session = Depends(get_db)):
    state = _current_state(db)
    if not state:
        raise HTTPException(404, "no completed runs found for any object")

    all_metrics = [m for s in state for m in s["metrics"]]
    total_checked = sum(m.records_checked for m in all_metrics)
    total_failed = sum(m.records_failed for m in all_metrics)
    overall_score = round((total_checked - total_failed) / total_checked * 100, 1) if total_checked else None
    critical_failed = sum(m.records_failed for m in all_metrics if m.severity == "Critical")
    records_scanned = sum(s["run"].records_scanned for s in state)

    total_elements = 0
    elements_with_rule = 0
    for s in state:
        total_elements += db.query(func.count(DQElement.element_id)).filter(
            DQElement.object_id == s["object"].object_id, DQElement.active_flag == True  # noqa: E712
        ).scalar() or 0
        elements_with_rule += len({m.element_id for m in s["metrics"]})
    rule_coverage_pct = round(elements_with_rule / total_elements * 100, 1) if total_elements else 0

    return {
        "overall_dq_score": overall_score,
        "objects_checked": len(state),
        "critical_failed_checks": critical_failed,
        "records_scanned": records_scanned,
        "rule_coverage_pct": rule_coverage_pct,
    }


@router.get("/heatmap")
def heatmap(db: Session = Depends(get_db)):
    """One row per active object (its latest run), one column per dimension."""
    state = _current_state(db)
    rows = []
    for s in state:
        by_dim = {}
        for m in s["metrics"]:
            d = by_dim.setdefault(m.dimension, {"checked": 0, "failed": 0})
            d["checked"] += m.records_checked
            d["failed"] += m.records_failed
        dims = {
            dim: round((v["checked"] - v["failed"]) / v["checked"] * 100, 1) if v["checked"] else 0
            for dim, v in by_dim.items()
        }
        checked = sum(v["checked"] for v in by_dim.values())
        failed = sum(v["failed"] for v in by_dim.values())
        overall = round((checked - failed) / checked * 100, 1) if checked else 0
        rows.append({
            "object_id": s["object"].object_id, "object_name": s["object"].object_name,
            "run_id": s["run"].run_id, "dimensions": dims, "overall": overall,
        })
    return rows


@router.get("/top-failing")
def top_failing(limit: int = 5, db: Session = Depends(get_db)):
    state = _current_state(db)
    items = []
    for s in state:
        elements = {e.element_id: e.element_name for e in
                    db.query(DQElement).filter(DQElement.object_id == s["object"].object_id)}
        for m in s["metrics"]:
            if m.records_failed > 0:
                items.append({
                    "element_name": elements.get(m.element_id, "?"),
                    "object_name": s["object"].object_name,
                    "score_pct": m.score_pct, "records_failed": m.records_failed,
                    "severity": m.severity,
                })
    items.sort(key=lambda x: x["score_pct"])
    return items[:limit]


@router.get("/trend")
def trend(limit: int = 10, db: Session = Depends(get_db)):
    """
    Aggregates by run_name (e.g. "Run #1"/"Run #2"/"Run #3") across every
    object -- each object validates on its own run_id, but they share a
    logical run sequence for the whole-database trend line.
    """
    objects = db.query(DQObject).filter(DQObject.active_flag == True).all()  # noqa: E712
    by_name: dict[str, dict] = {}
    for obj in objects:
        runs = (
            db.query(DQRun)
            .filter(DQRun.object_id == obj.object_id, DQRun.status == "completed")
            .order_by(DQRun.finished_at.desc())
            .limit(limit)
            .all()
        )
        for run in runs:
            key = run.run_name or f"run-{run.run_id}"
            bucket = by_name.setdefault(key, {"checked": 0, "failed": 0, "critical_failed": 0, "order": run.run_id})
            metrics = db.query(DQMetric).filter(DQMetric.run_id == run.run_id).all()
            bucket["checked"] += sum(m.records_checked for m in metrics)
            bucket["failed"] += sum(m.records_failed for m in metrics)
            bucket["critical_failed"] += sum(m.records_failed for m in metrics if m.severity == "Critical")
            bucket["order"] = min(bucket["order"], run.run_id)

    out = []
    for name, b in sorted(by_name.items(), key=lambda kv: kv[1]["order"]):
        score = round((b["checked"] - b["failed"]) / b["checked"] * 100, 1) if b["checked"] else None
        out.append({"run_name": name, "dq_score": score, "critical_failed_checks": b["critical_failed"]})
    return out


@router.get("/fix-profile")
def fix_profile(db: Session = Depends(get_db)):
    """
    V1 has no fix_action/auto-fix concept on DQ_RULE yet -- reporting the
    Critical vs Warning split instead of an Auto/Manual split, since that's
    the only classification the V1 schema actually captures.
    """
    state = _current_state(db)
    all_metrics = [m for s in state for m in s["metrics"]]
    counts = {}
    for m in all_metrics:
        counts[m.severity] = counts.get(m.severity, 0) + m.records_failed
    total = sum(counts.values()) or 1
    return {
        "critical_pct": round(counts.get("Critical", 0) / total * 100, 1),
        "warning_pct": round(counts.get("Warning", 0) / total * 100, 1),
        "critical_count": counts.get("Critical", 0),
        "warning_count": counts.get("Warning", 0),
    }


@router.get("/critical-by-dimension")
def critical_by_dimension(db: Session = Depends(get_db)):
    """Critical-severity failed checks, grouped by dimension, across every object's latest run."""
    state = _current_state(db)
    by_dim: dict[str, int] = {}
    for s in state:
        for m in s["metrics"]:
            if m.severity == "Critical":
                by_dim[m.dimension] = by_dim.get(m.dimension, 0) + m.records_failed
    total = sum(by_dim.values()) or 1
    rows = sorted(by_dim.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "total": sum(by_dim.values()),
        "breakdown": [
            {"dimension": dim, "count": count, "pct": round(count / total * 100, 1)}
            for dim, count in rows
        ],
    }


@router.get("/object/{object_id}/drilldown")
def object_drilldown(object_id: int, run_id: Optional[int] = None, db: Session = Depends(get_db)):
    run = db.get(DQRun, run_id) if run_id else _latest_completed_run(db, object_id)
    if run is None:
        raise HTTPException(404, "no completed run found for this object")

    rows = (
        db.query(DQMetric, DQElement.element_name)
        .join(DQElement, DQElement.element_id == DQMetric.element_id)
        .filter(DQMetric.run_id == run.run_id, DQMetric.object_id == object_id)
        .order_by(DQMetric.score_pct.asc())
        .all()
    )
    elements = [
        {
            "element_name": name, "dimension": m.dimension, "score_pct": m.score_pct,
            "records_failed": m.records_failed, "severity": m.severity,
        }
        for m, name in rows
    ]
    checked = sum(m.records_checked for m, _ in rows)
    failed = sum(m.records_failed for m, _ in rows)
    overall = round((checked - failed) / checked * 100, 1) if checked else 0

    dims = {}
    for m, _ in rows:
        d = dims.setdefault(m.dimension, {"checked": 0, "failed": 0})
        d["checked"] += m.records_checked
        d["failed"] += m.records_failed
    dimension_scores = {
        d: round((v["checked"] - v["failed"]) / v["checked"] * 100, 1) if v["checked"] else 0
        for d, v in dims.items()
    }

    obj = db.get(DQObject, object_id)
    return {
        "run_id": run.run_id, "object_id": object_id, "object_name": obj.object_name,
        "overall_score": overall, "elements_checked": len(rows), "records_scanned": run.records_scanned,
        "dimension_scores": dimension_scores, "elements": elements,
    }
