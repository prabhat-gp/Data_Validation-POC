from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import objects, rules, runs, dashboard, violations

app = FastAPI(title="SMTC Data Validation Framework", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # Next.js dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(objects.router)
app.include_router(rules.router)
app.include_router(runs.router)
app.include_router(dashboard.router)
app.include_router(violations.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
