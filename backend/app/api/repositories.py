"""Repository upload API (step 1.2)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.deps import get_current_user
from app.db.models import Repository, User
from app.db.session import get_session
from app.intake.errors import IntakeError
from app.intake.service import store_repository

router = APIRouter(prefix="/api/v1", tags=["repositories"])


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    source_type: str
    source_ref: str | None
    storage_path: str
    sha256: str | None
    size_bytes: int | None
    created_at: datetime


class RepositoryCreated(RepositoryOut):
    file_count: int
    uncompressed_bytes: int


@router.post(
    "/repositories",
    response_model=RepositoryCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_repository(
    file: UploadFile = File(..., description="zip archive of the repository"),
    name: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> RepositoryCreated:
    """Accept a zip upload, extract it safely and record the snapshot."""
    try:
        repository, extraction = await store_repository(
            upload=file,
            name=name,
            uploaded_by_id=user.id,
            session=session,
        )
    except IntakeError as exc:
        # The message is written for the user; it never contains a secret or a path
        # outside the upload directory.
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return RepositoryCreated(
        **RepositoryOut.model_validate(repository).model_dump(),
        file_count=extraction.file_count,
        uncompressed_bytes=extraction.total_bytes,
    )
