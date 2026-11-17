FROM python:3.12-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# WeasyPrint (step 3.9) renders the PDF report through pango/cairo, which are system
# libraries rather than Python packages. DejaVu fonts are what the report's CSS names.
RUN apt-get update     && apt-get install -y --no-install-recommends        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libcairo2        libgdk-pixbuf-2.0-0 shared-mime-info fonts-dejavu-core     && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
# scripts/seed_users.py runs inside this image (docker compose run --rm seed).
COPY scripts ./scripts

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
