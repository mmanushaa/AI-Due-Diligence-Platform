# Scanner image for the Semgrep adapter (step 1.5).
#
# The Python, JavaScript and TypeScript registry rulesets are fetched at build
# time and stored in the image, so the scanner runs with no network at all.
# The base tag is the non-root variant (user `semgrep`).
FROM semgrep/semgrep:1.182.1-nonroot

USER root

# Read by the adapter with `docker image inspect`; no container start needed.
LABEL com.due-diligence.scanner.tool="semgrep" \
      com.due-diligence.scanner.version="1.182.1"

RUN mkdir -p /rules/local

# Registry rulesets, pinned by the registry content at build time.
ADD https://semgrep.dev/c/p/python /rules/python.yaml
ADD https://semgrep.dev/c/p/javascript /rules/javascript.yaml
ADD https://semgrep.dev/c/p/typescript /rules/typescript.yaml

# Rules the registry does not carry, such as string-formatted SQL.
COPY rules/sql.yaml /rules/local/sql.yaml

# ADD over a URL writes mode 600; the scanner user has to be able to read them.
RUN chmod -R a+rX /rules

USER semgrep

WORKDIR /repo

ENTRYPOINT ["semgrep"]
