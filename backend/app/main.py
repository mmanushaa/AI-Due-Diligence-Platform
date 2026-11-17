from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.audits import router as audits_router
from app.api.auth import router as auth_router
from app.api.catalog import router as catalog_router
from app.api.events import router as events_router
from app.api.findings import router as findings_router
from app.api.health import router as health_router
from app.api.repositories import router as repositories_router
from app.api.reports import router as reports_router
from app.api.reviews import router as reviews_router
from app.core.logmask import install_log_masking


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Install log masking once the server's own logging is in place.

    At import time there is nothing to mask: uvicorn configures its handlers
    afterwards, and the ones this call would touch are replaced. Starting here
    means every line the running process emits is masked.
    """
    install_log_masking()
    yield


app = FastAPI(
    title="AI-Powered Software Due Diligence Platform", version="0.1.0", lifespan=lifespan
)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(repositories_router)
app.include_router(audits_router)
app.include_router(events_router)
app.include_router(findings_router)
app.include_router(catalog_router)
app.include_router(reviews_router)
app.include_router(reports_router)
