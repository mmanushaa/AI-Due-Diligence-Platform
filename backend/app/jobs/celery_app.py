"""Celery application (step 1.9).

Redis is both the broker and the result backend. ``task_acks_late`` is the point of
this configuration: the worker acknowledges a task only after it finishes, so a worker
killed mid-run leaves the message unacknowledged and it is delivered again instead of
being lost. ``reject_on_worker_lost`` requeues it rather than treating the death as a
failure, and a prefetch of one keeps a killed worker from holding a batch of tasks it
never started.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "due_diligence",
    broker=settings.celery_broker_url or settings.redis_url,
    backend=settings.celery_result_backend or settings.redis_url,
    include=["app.jobs.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_default_queue="audits",
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": settings.celery_visibility_timeout},
    result_expires=24 * 60 * 60,
    timezone="UTC",
    enable_utc=True,
)
