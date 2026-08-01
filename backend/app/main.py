from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import SessionLocal
from .models import ValRun
from .routers import dashboard, entities, rules, runs, violations

app = FastAPI(title="SMTC Data Validation Framework", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(entities.router)
app.include_router(rules.router)
app.include_router(runs.router)
app.include_router(dashboard.router)
app.include_router(violations.router)


@app.on_event("startup")
def reap_interrupted_runs():
    """
    Runs execute as in-process background tasks, so a restart or crash mid-run
    leaves rows stuck in 'running' forever -- which would silently poison the
    dashboard's "latest completed run" logic. Mark them failed at startup so a
    stranded run is visible instead of invisible.
    """
    db = SessionLocal()
    try:
        stranded = db.query(ValRun).filter(ValRun.status.in_(["running", "pending"])).all()
        for run in stranded:
            run.status = "failed"
            run.error_message = "Interrupted by a service restart; re-run this entity."
        if stranded:
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}
