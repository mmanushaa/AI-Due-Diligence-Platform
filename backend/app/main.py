from fastapi import FastAPI

from app.api.audits import router as audits_router
from app.api.findings import router as findings_router
from app.api.health import router as health_router
from app.api.repositories import router as repositories_router
from app.core.logmask import install_log_masking

app = FastAPI(title="AI-Powered Software Due Diligence Platform", version="0.1.0")
app.include_router(health_router)
app.include_router(repositories_router)
app.include_router(audits_router)
app.include_router(findings_router)

# Every line this process emits is masked before it is written: a model reply or a
# scanner error can quote a repository's credential (step 2.11).
install_log_masking()
