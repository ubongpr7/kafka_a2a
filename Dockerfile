FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install from the lockfile using uv.
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src

ARG KA2A_UV_EXTRAS="server,auth"
RUN set -eux; \
    EXTRA_FLAGS=""; \
    for e in $(echo "${KA2A_UV_EXTRAS}" | tr ',' ' '); do EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"; done; \
    uv sync --locked ${EXTRA_FLAGS}; \
    rm -rf /root/.cache/uv

ENV PATH="/app/.venv/bin:$PATH"

# Run as non-root.
RUN useradd -m -u 10001 ka2a
USER ka2a

EXPOSE 8000 8001

ENTRYPOINT ["ka2a"]
CMD ["--help"]
