# Scanner image for the ScanCode Toolkit adapter (step 1.7).
#
# License and package detection needs no network: the license rules ship inside
# the scancode-toolkit wheel, which is installed at build time. The adapter runs
# this image with --network none, a read-only snapshot mount, CPU and memory
# limits, and a timeout.
FROM python:3.12-slim-bookworm

ARG SCANCODE_VERSION=32.5.0

# libgomp1 is required by the compiled wheels scancode depends on.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# pluggy 1.6+ stops plugincode 32.0.0 from finding its hook specs, so the toolkit
# aborts at startup with "did not find any 'pre_scan' hooks". 1.5.0 is the last
# release that works with this toolkit version; drop the pin when it is fixed.
RUN pip install --no-cache-dir "scancode-toolkit==${SCANCODE_VERSION}" "pluggy==1.5.0"

RUN useradd --create-home --uid 10001 scanner

LABEL org.opencontainers.image.title="due-diligence-scancode" \
      org.opencontainers.image.description="ScanCode Toolkit for the due diligence scanner sandbox" \
      com.due-diligence.scanner.version="${SCANCODE_VERSION}"

# The scanner keeps its license index cache under $HOME, so the user needs one.
USER scanner

WORKDIR /repo

ENTRYPOINT ["scancode"]
