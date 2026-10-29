from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.security import mask_secrets
from app.db.session import get_engine

router = APIRouter()


def _check_database() -> dict:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - the check reports any failure as degraded
        return {"status": "error", "detail": mask_secrets(f"{type(exc).__name__}: {exc}")}


def _check_redis() -> dict:
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        try:
            client.ping()
        finally:
            client.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - the check reports any failure as degraded
        return {"status": "error", "detail": mask_secrets(f"{type(exc).__name__}: {exc}")}


@router.get("/health")
def health() -> JSONResponse:
    checks = {"database": _check_database(), "redis": _check_redis()}
    healthy = all(check["status"] == "ok" for check in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
