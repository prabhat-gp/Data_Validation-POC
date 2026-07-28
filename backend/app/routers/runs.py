"""
runs.py
-------
Triggers a validation run and lets the frontend poll its status. The actual
validation NEVER runs inline in the request -- it's handed to FastAPI's
BackgroundTasks so the HTTP call returns immediately with a run_id. A run
against millions of rows can take minutes; nothing about that should ever
sit inside a request/response cycle.
"""

import os
import shutil
import tempfile
from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..models import DQRun
from ..schemas import RunOut
from ..validation_engine import run_validation

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _execute_in_background(run_id: int, source_kind: str, file_path: Optional[str] = None,
                            db_source_url: Optional[str] = None, db_source_query: Optional[str] = None):
    """Runs in its own DB session -- the request's session is already closed by now."""
    db = SessionLocal()
    try:
        run_validation(
            db, run_id, source_kind,
            file_path=file_path, db_source_url=db_source_url, db_source_query=db_source_query,
        )
    finally:
        db.close()
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


@router.post("/upload", response_model=RunOut)
async def trigger_file_run(
    background_tasks: BackgroundTasks,
    object_id: int = Form(...),
    run_name: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    run = DQRun(object_id=object_id, run_name=run_name, run_type="file_upload",
                status="running", source_file_name=file.filename)
    db.add(run)
    db.commit()
    db.refresh(run)

    # stream the upload straight to a temp file -- never hold a 700MB upload in memory
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    with tmp as out:
        shutil.copyfileobj(file.file, out)

    background_tasks.add_task(_execute_in_background, run.run_id, "file_upload", file_path=tmp.name)
    return run


@router.post("/db-fetch", response_model=RunOut)
def trigger_db_run(
    background_tasks: BackgroundTasks,
    object_id: int = Form(...),
    run_name: Optional[str] = Form(None),
    connection_url: str = Form(...),
    query: str = Form(...),
    db: Session = Depends(get_db),
):
    run = DQRun(object_id=object_id, run_name=run_name, run_type="db_fetch", status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

    background_tasks.add_task(
        _execute_in_background, run.run_id, "db_fetch",
        db_source_url=connection_url, db_source_query=query,
    )
    return run


@router.get("", response_model=list[RunOut])
def list_runs(object_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(DQRun)
    if object_id is not None:
        q = q.filter(DQRun.object_id == object_id)
    return q.order_by(DQRun.started_at.desc()).all()


@router.get("/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(DQRun, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run
