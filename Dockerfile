FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_HTTP_TIMEOUT=600 \
    UV_HTTP_RETRIES=10

WORKDIR /app

# Install from the lockfile using uv.
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
COPY agent_cards /app/agent_cards
COPY prompts /app/prompts
COPY mcp-tools.example.json mcp-tools.local.json mcp-tools.dev.json mcp-tools.prod.json /app/
COPY ka2a-gateway-entrypoint.sh /usr/local/bin/ka2a-gateway-entrypoint.sh
COPY ka2a-proxy-entrypoint.sh /usr/local/bin/ka2a-proxy-entrypoint.sh

ARG KA2A_UV_EXTRAS="server,auth,lang"
RUN set -eux; \
    EXTRA_FLAGS=""; \
    for e in $(echo "${KA2A_UV_EXTRAS}" | tr ',' ' '); do EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"; done; \
    uv sync --locked ${EXTRA_FLAGS}; \
    rm -rf /root/.cache/uv

ENV PATH="/app/.venv/bin:$PATH"
ENV KA2A_LOAD_DOTENV=false

# Run as non-root.
RUN useradd -m -u 10001 ka2a
RUN mkdir -p /app/.data /tmp/ka2a-data && chown -R ka2a:ka2a /app/.data /tmp/ka2a-data
RUN chmod +x /usr/local/bin/ka2a-gateway-entrypoint.sh
RUN chmod +x /usr/local/bin/ka2a-proxy-entrypoint.sh
COPY --chown=ka2a:ka2a .data/control_plane.json /app/.data/control_plane.json
USER ka2a

EXPOSE 8000 8001

ENTRYPOINT ["ka2a"]
CMD ["--help"]
