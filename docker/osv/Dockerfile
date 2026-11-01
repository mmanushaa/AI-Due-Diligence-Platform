# Scanner image for the OSV-Scanner adapter (step 1.6).
#
# The OSV vulnerability database is downloaded at build time and stored in the
# image, so the scan itself runs with no network. The database is one zip per
# ecosystem: https://google.github.io/osv-scanner/usage/offline-mode/
FROM debian:bookworm-slim

ARG OSV_VERSION=2.7.0
# Ecosystems needed by the audited repos. Add more here (Go, Maven, ...) if a
# target repository uses them; each zip is tens to hundreds of MB.
ARG OSV_ECOSYSTEMS="PyPI npm"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/osv-scanner \
        "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" \
    && chmod 0755 /usr/local/bin/osv-scanner

# 2.7.0 expects the databases under <cache>/osv-scalibr/<ecosystem>/all.zip.
RUN mkdir -p /osv-db/osv-scalibr \
    && for ecosystem in ${OSV_ECOSYSTEMS}; do \
         mkdir -p "/osv-db/osv-scalibr/${ecosystem}"; \
         curl -fsSL -o "/osv-db/osv-scalibr/${ecosystem}/all.zip" \
             "https://storage.googleapis.com/osv-vulnerabilities/${ecosystem}/all.zip"; \
       done \
    && chmod -R a+rX /osv-db

RUN useradd --create-home --uid 10001 scanner \
    && chown -R scanner:scanner /osv-db
USER scanner

# Read by the adapter for scanner_runs.version without starting a container.
LABEL com.due-diligence.scanner.version="${OSV_VERSION}"

# The scanner extracts the zips into this directory, so it must be writable.
ENV OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY=/osv-db

WORKDIR /repo

ENTRYPOINT ["osv-scanner"]
