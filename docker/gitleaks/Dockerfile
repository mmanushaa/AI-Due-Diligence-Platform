# Scanner image for the Gitleaks adapter (step 1.4).
#
# The binary is pinned by version and fetched in a build stage, so the runtime
# layer holds only gitleaks and runs as an unprivileged user. The adapter runs
# this image with --network none, a read-only snapshot mount, CPU and memory
# limits, and a timeout.
FROM alpine:3.20 AS fetch

ARG GITLEAKS_VERSION=8.28.0

RUN apk add --no-cache curl tar \
 && curl -sSfL -o /tmp/gitleaks.tar.gz \
      "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
 && tar -xzf /tmp/gitleaks.tar.gz -C /tmp gitleaks

FROM alpine:3.20

LABEL org.opencontainers.image.title="due-diligence-gitleaks" \
      org.opencontainers.image.description="Gitleaks for the due diligence scanner sandbox"

COPY --from=fetch /tmp/gitleaks /usr/local/bin/gitleaks

RUN adduser -D -u 10001 scanner
USER scanner

WORKDIR /repo
ENTRYPOINT ["gitleaks"]
