from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.repositories import router as repositories_router

app = FastAPI(title="AI-Powered Software Due Diligence Platform", version="0.1.0")
app.include_router(health_router)
app.include_router(repositories_router)
