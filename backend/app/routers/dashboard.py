"""
dashboard.py
------------
Every read comes from val_metrics ONLY -- never a live scan of
val_violations. Metrics are a few hundred rows per run; violations run to
millions. That is why the dashboard is instant regardless of data volume.

Since val_metrics now carries entity_name / field_name / dimension
denormalized at write time, NOTHING here joins back to val_rules. The
dashboard can therefore run against a physically separate results database
with no cross-database join -- exactly what the 3-database split needs.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import elements_in, ENTITIES, ValBatch, ValMetric, ValRun

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Severity codes come from the reference workbook: INFO / WARNING / ERROR /
# CRITICAL. ERROR and CRITICAL both mean "this must be fixed", so the
# dashboard's blocking counts cover both.
BLOCKING = {"CRITICAL", "ERROR"}
NON_BLOCKING = {"WARNING", "INFO"}
_SEV_RANK = {"INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def _covered_elements(state_row, meta=None) -> set:
    """
    The distinct data elements an object's rules actually covered.

    NOT the number of metrics -- there is one metric per RULE, and several
    rules commonly target the same element (email has Completeness, Validity
    and Uniqueness rules). Counting metrics reported 13 "elements checked" for
    an object with 5 columns.

    A multi-field rule's label ("customer_name + sbg_id") is expanded into the
    real columns it covers, so it counts as the two elements it checks rather
    than as one column of that name. Anything that still is not a declared
    column of the object is dropped -- a rule-name fallback label is not an
    element.
    """
    cols = set(meta["columns"]) if meta else None
    out = set()
    for m in state_row["metrics"]:
        for name in elements_in(m.field_name):
            if cols is None or name in cols:
                out.add(name)
    return out


def _latest_completed_run(db: Session, entity_name: Optional[str] = None,
                          source_system: Optional[str] = None) -> Optional[ValRun]:
    q = db.query(ValRun).filter(ValRun.status == "completed")
    if entity_name is not None:
        q = q.filter(ValRun.entity_name == entity_name)
    if source_system:
        q = q.filter(ValRun.source_system == source_system)
    return q.order_by(ValRun.finished_at.desc()).first()


def _current_state(db: Session, batch_id: Optional[int] = None,
                   source_system: Optional[str] = None):
    """
    Latest completed run PER ENTITY + that run's metrics.

    With no batch_id: batches are deliberately ignored, so if Account last ran
    in batch 5 and Product in batch 3, you see each entity's most recent known
    state. That is what keeps the heatmap correct when different batches cover
    different entities.

    With a batch_id: scoped to just that batch, so the user can inspect one
    specific run.

    Fixed at 2 queries regardless of entity count.
    """
    q = db.query(ValRun).filter(ValRun.status == "completed")
    if batch_id is not None:
        q = q.filter(ValRun.batch_id == batch_id)
    if source_system:
        # without this the heatmap mixes entities from every source, because
        # "latest completed run per entity" spans all of them
        q = q.filter(ValRun.source_system == source_system)
    runs = q.order_by(ValRun.finished_at.desc()).all()
    latest = {}
    for r in runs:
        latest.setdefault(r.entity_name, r)
    if not latest:
        return []

    run_ids = [r.run_id for r in latest.values()]
    by_run = {}
    for m in db.query(ValMetric).filter(ValMetric.run_id.in_(run_ids)).all():
        by_run.setdefault(m.run_id, []).append(m)

    return [
        {"entity_name": name, "run": run, "metrics": by_run.get(run.run_id, [])}
        for name, run in latest.items()
    ]


@router.get("/kpis")
def kpis(batch_id: Optional[int] = None, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    state = _current_state(db, batch_id, source_system)
    if not state:
        raise HTTPException(404, "no completed runs found for any entity")

    all_metrics = [m for s in state for m in s["metrics"]]
    checked = sum(m.records_checked for m in all_metrics)
    failed = sum(m.records_failed for m in all_metrics)
    overall = round((checked - failed) / checked * 100, 1) if checked else None
    critical_failed = sum(m.records_failed for m in all_metrics if m.severity in BLOCKING)

    # Rule coverage: distinct ELEMENTS with a rule run, over declared elements.
    # Counted per object, so sbg_id on Customer and on Product are two CDEs.
    total_fields, covered_fields = 0, 0
    for s in state:
        meta = ENTITIES.get(s["entity_name"])
        if meta:
            total_fields += len(meta["columns"])
        covered_fields += len(_covered_elements(s, meta))
    coverage = round(covered_fields / total_fields * 100, 1) if total_fields else 0

    return {
        "overall_dq_score": overall,
        "objects_checked": len(state),
        # distinct CDEs that actually had a rule run against them
        "cdes_checked": covered_fields,
        "records_scanned": sum(s["run"].records_scanned or 0 for s in state),
        # RECORDS with at least one violation -- a different question from
        # "how many checks failed". One row breaking three rules is 3 failed
        # checks but 1 bad record. Precomputed on the run; see finish_run().
        "records_affected": sum(s["run"].records_affected or 0 for s in state),
        "critical_failed_checks": critical_failed,
        # Rule Coverage's denominator: declared elements of the objects that
        # ACTUALLY RAN. Running Account alone gives 16, not 33 -- an object
        # that was not in the batch cannot drag its coverage down.
        "elements_declared": total_fields,
        # The DQ score's denominator. Published so the UI can show
        # "38 of 890 checks" rather than inviting 38/128.
        "checks_run": checked,
        "checks_failed": failed,
        "rule_coverage_pct": min(coverage, 100.0),
    }


@router.get("/heatmap")
def heatmap(batch_id: Optional[int] = None, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    rows = []
    for s in _current_state(db, batch_id, source_system):
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
        rows.append({
            "object_id": s["entity_name"],          # entity name is the identifier now
            "object_name": s["entity_name"],
            "run_id": s["run"].run_id,
            "dimensions": dims,
            "overall": round((checked - failed) / checked * 100, 1) if checked else 0,
        })
    return rows


@router.get("/top-failing")
def top_failing(limit: int = 5, batch_id: Optional[int] = None, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Worst ELEMENTS, one row each -- not one row per rule.

    Several rules commonly target the same element (email has Completeness,
    Validity and Uniqueness rules), so emitting a row per metric listed the
    same element two or three times. Checks are pooled per element, the same
    way every other score in the app is computed, and the severity shown is
    the worst one among that element's failing rules.
    """
    agg = {}
    for s in _current_state(db, batch_id, source_system):
        for m in s["metrics"]:
            key = (m.entity_name, m.field_name)
            a = agg.setdefault(key, {"checked": 0, "failed": 0, "sev": "INFO"})
            a["checked"] += m.records_checked
            a["failed"] += m.records_failed
            if m.records_failed > 0 and _SEV_RANK.get(m.severity, 0) > _SEV_RANK.get(a["sev"], 0):
                a["sev"] = m.severity

    items = [
        {
            "element_name": field,
            "object_name": entity,
            "score_pct": round((a["checked"] - a["failed"]) / a["checked"] * 100, 2),
            "records_failed": a["failed"],
            "severity": a["sev"],
        }
        for (entity, field), a in agg.items()
        if a["failed"] > 0 and a["checked"] > 0
    ]
    items.sort(key=lambda x: (x["score_pct"], -x["records_failed"]))
    return items[:limit]


@router.get("/trend")
def trend(limit: int = 10, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Grouped by BATCH, not by a run_name string.

    CAVEAT: if batch 1 covered only Account and batch 2 covered four entities,
    the aggregate score changes because the COMPOSITION changed, not because
    quality did. entity_count is returned so the UI can show that rather than
    implying a trend that isn't there.
    """
    tq = db.query(ValRun).filter(ValRun.status == "completed")
    if source_system:
        tq = tq.filter(ValRun.source_system == source_system)
    runs = tq.order_by(ValRun.batch_id.desc()).all()
    if not runs:
        return []

    metrics_by_run = {}
    for m in db.query(ValMetric).filter(ValMetric.run_id.in_([r.run_id for r in runs])).all():
        metrics_by_run.setdefault(m.run_id, []).append(m)

    buckets = {}
    for run in runs:
        b = buckets.setdefault(run.batch_id, {
            "checked": 0, "failed": 0, "critical_failed": 0, "entities": set(),
        })
        b["entities"].add(run.entity_name)
        for m in metrics_by_run.get(run.run_id, []):
            b["checked"] += m.records_checked
            b["failed"] += m.records_failed
            if m.severity in BLOCKING:
                b["critical_failed"] += m.records_failed

    out = []
    for batch_id in sorted(buckets)[-limit:]:
        b = buckets[batch_id]
        score = round((b["checked"] - b["failed"]) / b["checked"] * 100, 1) if b["checked"] else None
        out.append({
            "run_name": f"Run #{batch_id}",
            "dq_score": score,
            "critical_failed_checks": b["critical_failed"],
            "entity_count": len(b["entities"]),
        })
    return out


@router.get("/fix-profile")
def fix_profile(batch_id: Optional[int] = None, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    """Critical vs Warning split -- the only classification V1 actually captures."""
    all_metrics = [m for s in _current_state(db, batch_id, source_system) for m in s["metrics"]]
    counts = {}
    for m in all_metrics:
        counts[m.severity] = counts.get(m.severity, 0) + m.records_failed
    blocking = sum(v for k, v in counts.items() if k in BLOCKING)
    non_blocking = sum(v for k, v in counts.items() if k in NON_BLOCKING)
    total = (blocking + non_blocking) or 1
    return {
        "critical_pct": round(blocking / total * 100, 1),
        "warning_pct": round(non_blocking / total * 100, 1),
        "critical_count": blocking,
        "warning_count": non_blocking,
    }


@router.get("/critical-by-dimension")
def critical_by_dimension(batch_id: Optional[int] = None, source_system: Optional[str] = None, db: Session = Depends(get_db)):
    by_dim = {}
    for s in _current_state(db, batch_id, source_system):
        for m in s["metrics"]:
            if m.severity in BLOCKING:
                by_dim[m.dimension] = by_dim.get(m.dimension, 0) + m.records_failed
    total = sum(by_dim.values()) or 1
    return {
        "total": sum(by_dim.values()),
        "breakdown": [
            {"dimension": dim, "count": count, "pct": round(count / total * 100, 1)}
            for dim, count in sorted(by_dim.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


@router.get("/object/{entity_name}/drilldown")
def entity_drilldown(entity_name: str, run_id: Optional[int] = None,
                     source_system: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Single-entity detail. Note there is NO join to val_rules -- field_name and
    dimension come straight off val_metrics.
    """
    run = db.get(ValRun, run_id) if run_id else _latest_completed_run(db, entity_name, source_system)
    if run is None:
        raise HTTPException(404, "no completed run found for this entity")

    metrics = (
        db.query(ValMetric)
        .filter(ValMetric.run_id == run.run_id, ValMetric.entity_name == entity_name)
        .order_by(ValMetric.score_pct.asc())
        .all()
    )
    checked = sum(m.records_checked for m in metrics)
    failed = sum(m.records_failed for m in metrics)

    dims = {}
    for m in metrics:
        d = dims.setdefault(m.dimension, {"checked": 0, "failed": 0})
        d["checked"] += m.records_checked
        d["failed"] += m.records_failed

    return {
        "run_id": run.run_id,
        "object_id": entity_name,
        "object_name": entity_name,
        "overall_score": round((checked - failed) / checked * 100, 1) if checked else 0,
        # distinct elements covered, NOT the rule count -- several rules
        # commonly target the same element
        "elements_checked": len(_covered_elements(
            {"metrics": metrics}, ENTITIES.get(entity_name))),
        "checks_run": len(metrics),
        "records_scanned": run.records_scanned or 0,
        "dimension_scores": {
            d: round((v["checked"] - v["failed"]) / v["checked"] * 100, 1) if v["checked"] else 0
            for d, v in dims.items()
        },
        "elements": [
            {
                "element_name": m.field_name, "dimension": m.dimension,
                "score_pct": m.score_pct, "records_failed": m.records_failed,
                "records_checked": m.records_checked,
                "severity": m.severity,
                # lets the UI open the exact rule behind this number
                "rule_id": m.rule_id,
            }
            for m in metrics
        ],
    }


@router.get("/batch-options")
def batch_options(run_type: Optional[str] = None, source_system: Optional[str] = None,
                  db: Session = Depends(get_db)):
    """
    Batches that actually have completed runs, optionally filtered by data
    source. Feeds the dashboard's Data Source -> Run ID cascade: pick db_fetch
    and only db_fetch batches are offered.
    """
    q = db.query(ValBatch)
    if run_type:
        q = q.filter(ValBatch.run_type == run_type)
    if source_system:
        q = q.filter(ValBatch.source_system == source_system)
    out = []
    for b in q.order_by(ValBatch.batch_id.desc()).all():
        done = db.query(ValRun).filter(
            ValRun.batch_id == b.batch_id, ValRun.status == "completed"
        ).count()
        if done:
            out.append({
                "batch_id": b.batch_id,
                "batch_name": b.batch_name or f"Run #{b.batch_id}",
                "run_type": b.run_type,
                "source_system": b.source_system,
                "entity_count": done,
                "started_at": b.started_at,
            })
    return out


@router.get("/summary")
def summary(top_limit: int = 5, batch_id: Optional[int] = None,
            source_system: Optional[str] = None, db: Session = Depends(get_db)):
    """
    Everything the overview page needs, in ONE request.

    NOTE the trend is deliberately NOT filtered by batch_id. A trend exists to
    show change over time; scoping it to a single batch would leave one point
    and destroy the thing it is for. Everything else respects the filter.
    """
    if not _current_state(db, batch_id, source_system):
        raise HTTPException(404, "no completed runs found for this selection")
    return {
        "kpis": kpis(batch_id, source_system, db),
        "heatmap": heatmap(batch_id, source_system, db),
        "top_failing": top_failing(top_limit, batch_id, source_system, db),
        "trend": trend(10, source_system, db),   # history for THIS source
        "fix_profile": fix_profile(batch_id, source_system, db),
        "critical_by_dimension": critical_by_dimension(batch_id, source_system, db),
    }
