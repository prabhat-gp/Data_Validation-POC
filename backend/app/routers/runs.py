"""
runs.py
-------
Batch + run orchestration.

A BATCH is what a user calls "a run": one trigger covering 1..N entities.
Each entity inside it gets its own val_runs row, executed sequentially. That
isolation is the point -- if Contact fails on a missing column, Account still
completes and keeps its results.

Runs execute as BACKGROUND TASKS. The HTTP call returns a batch_id in
milliseconds; a multi-minute validation would otherwise blow the request
timeout. The frontend polls the batch for status.

Batch status is DERIVED from its child runs, never stored -- a crash can't
leave a stored status permanently wrong.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import ENTITIES, SOURCE_SYSTEMS, ValBatch, ValRule, ValRun
from ..schemas import BatchOut, BatchTriggerDb, RunOut
from ..validation_engine import (
    clear_run_staging, finish_run, stage_run, validate_run,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])


def utcnow():
    return datetime.now(timezone.utc)


def _derive_batch_status(runs: list) -> str:
    """running -> completed_with_errors -> completed. Derived, never stored."""
    if not runs:
        return "empty"
    statuses = {r.status for r in runs}
    if statuses & {"pending", "running"}:
        return "running"
    if "failed" in statuses:
        return "completed_with_errors"
    return "completed"


def _batch_payload(db: Session, batch: ValBatch) -> BatchOut:
    runs = db.query(ValRun).filter(ValRun.batch_id == batch.batch_id).order_by(ValRun.run_id).all()
    return BatchOut(
        batch_id=batch.batch_id,
        batch_name=batch.batch_name,
        run_type=batch.run_type,
        triggered_by=batch.triggered_by,
        started_at=batch.started_at,
        status=_derive_batch_status(runs),
        entity_count=len(runs),
        runs=[RunOut.from_orm(r) for r in runs],
    )


def _execute_batch(batch_id: int, source_kind: str, files_by_entity: dict, rule_ids):
    """
    THREE PHASES, all strictly sequential -- nothing runs concurrently.

        1. stage every entity
        2. validate every entity
        3. clear all staging

    Phase 1 must finish before phase 2 starts because REFERENTIAL_INTEGRITY
    LEFT JOINs the lookup entity's staging table. Staging one entity and
    wiping it before the next made that join impossible.

    A failure in phase 1 for one entity does NOT stop the others -- that
    entity is marked failed and skipped in phase 2.
    """
    db = SessionLocal()
    staged_runs: dict = {}
    failed_runs: set = set()
    try:
        runs = db.query(ValRun).filter(ValRun.batch_id == batch_id).order_by(ValRun.run_id).all()

        # ---- PHASE 1: stage everything -------------------------------------
        for run in runs:
            try:
                stage_run(db, run.run_id, source_kind, files_by_entity.get(run.entity_name))
                staged_runs[run.entity_name] = run.run_id
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                finish_run(db, run.run_id, str(exc))
                failed_runs.add(run.run_id)

        # ---- PHASE 2: validate everything ----------------------------------
        for run in runs:
            if run.run_id in failed_runs:
                continue
            try:
                validate_run(db, run.run_id, staged_runs, rule_ids)
                finish_run(db, run.run_id)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                finish_run(db, run.run_id, str(exc))
                failed_runs.add(run.run_id)

        # ---- PHASE 3: staging is runtime-only ------------------------------
        for run in runs:
            try:
                clear_run_staging(db, run.run_id)
            except Exception:  # noqa: BLE001
                pass
    finally:
        for path in files_by_entity.values():
            try:
                os.unlink(path)
            except OSError:
                pass
        db.close()


@router.post("/upload", response_model=BatchOut)
async def trigger_file_batch(
    background_tasks: BackgroundTasks,
    entity_names: str = Form(...),          # comma-separated, paired to files by position
    files: list[UploadFile] = File(...),
    batch_name: Optional[str] = Form(None),
    triggered_by: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Multi-file upload: one file per entity, paired by position.
    Uploading accounts.csv + contacts.csv creates ONE batch with TWO runs.
    """
    entities = [e.strip() for e in entity_names.split(",") if e.strip()]
    if len(entities) != len(files):
        raise HTTPException(400, f"got {len(files)} files but {len(entities)} object names")
    unknown = [e for e in entities if e not in ENTITIES]
    if unknown:
        raise HTTPException(400, f"unknown objects: {unknown}")
    if len(set(entities)) != len(entities):
        raise HTTPException(400, "the same object cannot appear twice in one batch")

    batch = ValBatch(
        batch_name=batch_name, run_type="file_upload", source_system="File Dump",
        triggered_by=triggered_by or "system", started_at=utcnow(),
    )
    db.add(batch)
    db.flush()

    files_by_entity = {}
    for entity, upload in zip(entities, files):
        # Spooled to disk, never held in memory -- a 600MB+ CSV must not be
        # buffered. nginx client_max_body_size governs the real ceiling.
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
        shutil.copyfileobj(upload.file, tmp)
        tmp.close()
        files_by_entity[entity] = tmp.name
        db.add(ValRun(
            batch_id=batch.batch_id, entity_name=entity, run_type="file_upload",
            source_system="File Dump", status="pending", started_at=utcnow(),
            source_file_name=upload.filename,
        ))

    db.commit()
    db.refresh(batch)
    background_tasks.add_task(_execute_batch, batch.batch_id, "file_upload", files_by_entity, None)
    return _batch_payload(db, batch)


@router.post("/db-fetch", response_model=BatchOut)
def trigger_db_batch(
    payload: BatchTriggerDb,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    'Run from Database'. The user picks ENTITIES only.

    No connection string and no SQL crosses the API -- the server already
    knows both (SOURCE_DB_URL env var + the ENTITIES catalog). That keeps
    credentials out of the browser and out of request logs, and means no
    user-supplied SQL is ever run against the source system.
    """
    from .rules import require_role
    require_role(payload.role, "owner")

    entities = [e.strip() for e in payload.entity_names if e and e.strip()]
    if not entities:
        raise HTTPException(400, "select at least one object")
    unknown = [e for e in entities if e not in ENTITIES]
    if unknown:
        raise HTTPException(400, f"unknown objects: {unknown}")
    if len(set(entities)) != len(entities):
        raise HTTPException(400, "the same object cannot appear twice in one batch")

    src = payload.source_system or ENTITIES[entities[0]]["source_system"]
    batch = ValBatch(
        batch_name=payload.batch_name, run_type="db_fetch", source_system=src,
        triggered_by=payload.triggered_by or "system", started_at=utcnow(),
    )
    db.add(batch)
    db.flush()
    for entity in entities:
        db.add(ValRun(
            batch_id=batch.batch_id, entity_name=entity, run_type="db_fetch",
            source_system=src, status="pending", started_at=utcnow(),
        ))
    db.commit()
    db.refresh(batch)

    background_tasks.add_task(_execute_batch, batch.batch_id, "db_fetch", {}, payload.rule_ids)
    return _batch_payload(db, batch)


@router.get("/source/objects")
def list_source_objects(source_system: str = "MySQL", db: Session = Depends(get_db)):
    """
    What is ACTUALLY in the selected source, not what the catalog claims.

    The Runs page used to filter ENTITIES by source_system, which meant an
    object was invisible unless someone had hand-labelled it with the right
    system -- and "MySQL" showed nothing at all even though every table lives
    in MySQL. Source is a CONNECTION, so this introspects it.

    Each table is then matched to the catalog by source_object_name to answer
    the only question that matters: can this be run?

        runnable    declared in ENTITIES and has >=1 approved rule
        no_rules    declared, but nothing approved to run against it
        undeclared  a real table with no ENTITIES entry -- its CDE columns and
                    primary key are unknown, so there is nothing to validate.
                    Shown rather than hidden so the table is not a mystery.

    Staging tables are excluded: they are this app's own scratch space.
    """
    from sqlalchemy import create_engine, inspect as sa_inspect

    from ..ingestion import source_url_for
    from ..models import staging_table_name

    url = source_url_for(source_system)
    if not url:
        raise HTTPException(400, f"No connection configured for '{source_system}'.")

    try:
        eng = create_engine(url)
        tables = sorted(sa_inspect(eng).get_table_names())
        eng.dispose()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not read {source_system}: {exc}")

    counts = dict(db_config_counts())
    by_table = {m["source_object_name"].lower(): (name, m)
                for name, m in ENTITIES.items()}
    ours = {staging_table_name(n) for n in ENTITIES}

    out = []
    for t in tables:
        if t.lower() in ours or t.lower().startswith("stg_"):
            continue
        hit = by_table.get(t.lower())
        if hit is None:
            out.append({"table_name": t, "entity_name": None, "approved_rule_count": 0,
                        "element_count": 0, "status": "undeclared"})
            continue
        name, meta = hit
        n = counts.get(name, 0)
        out.append({
            "table_name": t, "entity_name": name, "approved_rule_count": n,
            "element_count": len(meta["columns"]),
            "status": "runnable" if n else "no_rules",
        })
    return out


def db_config_counts():
    """Approved, active rule count per entity. Rules live in CONFIG_DB."""
    from ..database import ConfigSession
    cdb = ConfigSession()
    try:
        return (cdb.query(ValRule.entity_name, func.count(ValRule.rule_id))
                .filter(ValRule.status == "APPROVED", ValRule.active == True)  # noqa: E712
                .group_by(ValRule.entity_name).all())
    finally:
        cdb.close()


@router.get("/source/check")
def check_source_connection(source_system: str = "MySQL"):
    """
    Tests the configured source database BEFORE a run is allowed.

    Catching a bad connection here means the user gets an immediate red light
    instead of a batch that starts, creates run rows, and fails N times over.
    """
    from sqlalchemy import create_engine, text as sql_text

    from ..ingestion import ENV_VAR_FOR, source_url_for

    url = source_url_for(source_system)
    if not url:
        var = ENV_VAR_FOR.get(source_system, "the connection")
        return {"ok": False,
                "detail": f"Not configured. Set {var} in backend/.env"}
    try:
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        eng.dispose()
        # never echo the URL back -- it contains credentials
        tables = ", ".join(sorted(
            m["source_object_name"] for m in ENTITIES.values()
            if m["source_system"] == source_system))
        return {"ok": True,
                "detail": f"Connected to {source_system}" + (f" · {tables}" if tables else "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connection failed: {str(exc)[:200]}"}


@router.get("/batches", response_model=list)
def list_batches(limit: int = 50, run_type: Optional[str] = None,
                 source_system: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ValBatch)
    if run_type:
        q = q.filter(ValBatch.run_type == run_type)
    if source_system:
        q = q.filter(ValBatch.source_system == source_system)
    batches = q.order_by(ValBatch.batch_id.desc()).limit(limit).all()
    return [_batch_payload(db, b) for b in batches]


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(ValBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    return _batch_payload(db, batch)


@router.get("", response_model=list)
def list_runs(entity_name: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ValRun)
    if entity_name:
        q = q.filter(ValRun.entity_name == entity_name)
    return q.order_by(ValRun.started_at.desc()).all()


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ValRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run
