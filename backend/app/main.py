from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.audits import router as audits_router
from app.api.findings import router as findings_router
from app.api.health import router as health_router
from app.api.repositories import router as repositories_router
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
app.include_router(repositories_router)
app.include_router(audits_router)
app.include_router(findings_router)
