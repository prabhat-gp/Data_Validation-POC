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
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import ENTITIES, ValBatch, ValRun
from ..schemas import BatchOut, BatchTriggerDb, RunOut
from ..validation_engine import run_validation

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
    Runs every entity in the batch SEQUENTIALLY, in its own DB session.

    Sequential on purpose: four heavy jobs at once would compete for the same
    connection pool and write throughput, and concurrent bulk loads inside the
    API process can starve the UI. One entity failing does NOT abort the rest.
    """
    db = SessionLocal()
    try:
        runs = db.query(ValRun).filter(ValRun.batch_id == batch_id).order_by(ValRun.run_id).all()
        for run in runs:
            try:
                run_validation(
                    db, run.run_id, source_kind,
                    file_path=files_by_entity.get(run.entity_name),
                    rule_ids=rule_ids,
                )
            except Exception:  # noqa: BLE001 -- already recorded on the run row
                continue
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
        raise HTTPException(400, f"got {len(files)} files but {len(entities)} entity names")
    unknown = [e for e in entities if e not in ENTITIES]
    if unknown:
        raise HTTPException(400, f"unknown entities: {unknown}")
    if len(set(entities)) != len(entities):
        raise HTTPException(400, "the same entity cannot appear twice in one batch")

    batch = ValBatch(
        batch_name=batch_name, run_type="file_upload",
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
            status="pending", started_at=utcnow(), source_file_name=upload.filename,
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
        raise HTTPException(400, "select at least one entity")
    unknown = [e for e in entities if e not in ENTITIES]
    if unknown:
        raise HTTPException(400, f"unknown entities: {unknown}")
    if len(set(entities)) != len(entities):
        raise HTTPException(400, "the same entity cannot appear twice in one batch")

    batch = ValBatch(
        batch_name=payload.batch_name, run_type="db_fetch",
        triggered_by=payload.triggered_by or "system", started_at=utcnow(),
    )
    db.add(batch)
    db.flush()
    for entity in entities:
        db.add(ValRun(
            batch_id=batch.batch_id, entity_name=entity, run_type="db_fetch",
            status="pending", started_at=utcnow(),
        ))
    db.commit()
    db.refresh(batch)

    background_tasks.add_task(_execute_batch, batch.batch_id, "db_fetch", {}, payload.rule_ids)
    return _batch_payload(db, batch)


@router.get("/source/check")
def check_source_connection():
    """
    Tests the configured source database BEFORE a run is allowed.

    Catching a bad connection here means the user gets an immediate red light
    instead of a batch that starts, creates run rows, and fails N times over.
    """
    from sqlalchemy import create_engine, text as sql_text

    from ..ingestion import SOURCE_DB_URL

    if not SOURCE_DB_URL:
        return {
            "ok": False,
            "detail": "No source database configured. Set SOURCE_DB_URL on the server.",
        }
    try:
        eng = create_engine(SOURCE_DB_URL)
        with eng.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
        eng.dispose()
        # never echo the URL back -- it contains credentials
        return {"ok": True, "detail": "Connection successful."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"Connection failed: {str(exc)[:200]}"}


@router.get("/batches", response_model=list)
def list_batches(limit: int = 50, run_type: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ValBatch)
    if run_type:
        q = q.filter(ValBatch.run_type == run_type)
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
