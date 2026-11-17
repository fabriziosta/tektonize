# tektonize — Jenkins shared-library Groovy → incomplete Tekton fragments
FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="tektonize" \
      org.opencontainers.image.description="Convert Jenkins shared-library Groovy to incomplete Tekton tasks and pipeline fragments"

COPY --from=builder /install /usr/local

# Default workspace for bind-mounted input/output
WORKDIR /work

ENTRYPOINT ["tektonize"]
CMD ["--help"]
