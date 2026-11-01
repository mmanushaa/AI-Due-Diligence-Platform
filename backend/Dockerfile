FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# The Celery worker runs the scanners, and every scanner is itself a container:
# the worker needs the docker CLI and the host daemon's socket (see compose).
FROM api AS worker

USER root

ARG DOCKER_CLI_VERSION=27.3.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /usr/local/bin --strip-components=1 docker/docker

CMD ["celery", "-A", "app.jobs.celery_app", "worker", "--loglevel=INFO", "--concurrency=2", "-Q", "audits"]
