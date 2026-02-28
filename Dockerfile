FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install K-A2A (and selected extras) from source.
COPY pyproject.toml README.md /app/
COPY src /app/src

ARG KA2A_PIP_EXTRAS="server,auth"
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir ".[${KA2A_PIP_EXTRAS}]"

# Run as non-root.
RUN useradd -m -u 10001 ka2a
USER ka2a

EXPOSE 8000 8001

ENTRYPOINT ["ka2a"]
CMD ["--help"]

