# K-A2A (Kafka A2A)

K-A2A is a Kafka-transport implementation of the **Agent-to-Agent (A2A)** protocol concepts:
Agent Cards, capabilities, tasks, streaming task updates, and (optional) push notifications.

Design constraints:
- All **agent-to-agent** communication is Kafka-only (no HTTP between agents).
- HTTP (FastAPI) is for user/client entrypoints and observability.
- LLM-agnostic: the core library defines protocol + transport + runtime, not a specific model provider.

## What’s implemented

- **A2A-like models**: AgentCard (+ signatures field), capabilities, skills, tasks, artifacts, streaming updates.
- **Kafka transport** (`aiokafka`): request/response envelopes with correlation IDs.
- **JSON-RPC 2.0 surface** over Kafka (`message/send`, `message/stream`, `tasks/*`, `agent/getCard`, etc.).
- **Streaming**: multiple JSON-RPC responses published to the client reply topic for `message/stream` and `tasks/resubscribe`.
- **Registry + discovery**: agents publish AgentCards to a Kafka topic (`ka2a.agent_cards`).
- **Gateway HTTP endpoints**: `/agents`, `/tasks`, `/tasks/{id}`, `/tasks/{id}/events` (SSE), plus `/chat` + `/upload` + `/stream`.
- **Task stores**: in-memory (default) and Redis (`KA2A_TASK_STORE=redis`).
- **Push notifications** (optional): `tasks/pushNotificationConfig/*` + delivery to `http(s)://...` webhooks or `kafka://topic`.
- **SaaS / multi-tenant isolation** (optional): per-task principal enforcement via request `metadata`.
- **JWT verification hook** (optional): FastAPI gateway/proxy can verify Bearer JWTs and forward a `Principal` in metadata.
- **Local/dev credentials** (optional): resolve a single set of LLM credentials from `.env` / process env.
- **LangGraph processor** (optional): `langgraph-chat` with pluggable `KA2A_LLM_FACTORY` (default: OpenAI-compatible adapter).

## Architecture (Kafka topics)

K-A2A uses a small set of topics (defaults shown):

- Agent request topic: `ka2a.req.<agent_name>`
- Client reply topic: `ka2a.reply.<client_id>`
- Task events topic: `ka2a.evt.<task_id>` (used for push-to-Kafka notifications)
- Agent card registry topic: `ka2a.agent_cards`

Wire format:
- All messages are `KafkaEnvelope` objects (`EnvelopeType.request|response|event|registry`)
- The `payload` is a JSON-RPC request/response dict (or a StreamResponse event for notifications)

## Quickstart (Docker Compose)

This repo ships a single `docker-compose.yml` that starts:
- Two example agents (`host-agent`, `echo-agent`)
- A gateway (`/chat`, `/upload`, `/stream`)
- An A2A-compatible HTTP proxy (JSON-RPC POST `/` + SSE streaming + Agent Card endpoint)

Kafka is expected to be provisioned separately (remote broker, MSK, Confluent Cloud, or your own server).

1) Create a `.env` file (required):

```bash
cp .env.example .env
# Edit .env and set KA2A_BOOTSTRAP_SERVERS (e.g. 3.217.248.209:9092)
```

2) Build + start the services:

```bash
docker compose up -d --build
```

Optional: if you want to run Kafka locally for development, you can use `kafka/docker-compose.yml` and then point
the app stack at it (on macOS this is typically `host.docker.internal:9094`):

```bash
docker compose -f kafka/docker-compose.yml up -d
# In .env, set:
#   KA2A_BOOTSTRAP_SERVERS=host.docker.internal:9094
docker compose up -d --build
```

Optional: if you want to run Redis locally for development (task persistence), use `redis/docker-compose.yml` and
point agents at it:

```bash
docker compose -f redis/docker-compose.yml up -d --build
# In your K-A2A .env, set:
#   KA2A_TASK_STORE=redis
#   KA2A_REDIS_URL=redis://:change-me@host.docker.internal:6379/0
```

Test the gateway:

```bash
curl -sS -X POST 'http://localhost:8000/chat' \
  -H 'content-type: application/json' \
  -d '{"text":"hello"}' | jq

curl -sS 'http://localhost:8000/agents' | jq

curl -sS 'http://localhost:8000/tasks' | jq

curl -N -X POST 'http://localhost:8000/stream' \
  -H 'content-type: application/json' \
  -d '{"text":"hello"}'
```

To stream task events (SSE), capture the `id` from `/chat` and then:

```bash
curl -N "http://localhost:8000/tasks/<task_id>/events"
```

Test the A2A HTTP proxy:

```bash
curl -sS 'http://localhost:8001/.well-known/agent-card.json' | jq

curl -sS -X POST 'http://localhost:8001/' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hello"}]}}}' | jq
```

### Multiple agents

You can add more agent services by copying `echo-agent` and pointing it at a different AgentCard JSON file
(`agent_cards/*.agent-card.json`). For a simple worker pool, scale a single agent service:

```bash
docker compose up -d --scale echo-agent=3
```

## Quickstart (Python / dev)

1) Start (or choose) a Kafka broker, then set `KA2A_BOOTSTRAP_SERVERS` in your `.env`:

```bash
cp .env.example .env
# Edit .env and set KA2A_BOOTSTRAP_SERVERS
```

2) Install (uv):

```bash
uv sync --locked --extra server --extra auth --extra dev
# Optional:
#   --extra lang   (LangGraph processor)
#   --extra redis  (Redis task store)
```

3) Run an agent:

```bash
ka2a agent --agent-name echo
```

4) Run the gateway:

```bash
KA2A_DEFAULT_AGENT=echo ka2a gateway
```

## Repo layout

- `src/kafka_a2a/` — library
- `docker-compose.yml` — agents + gateway + proxy (requires external Kafka)
- `kafka/docker-compose.yml` — optional local Kafka (dev convenience)
- `agent_cards/` — sample AgentCard JSON files

## Docker image

Build a local image:

```bash
docker build -t kafka-a2a:local .
```

Run components:

```bash
docker run --rm --env-file .env kafka-a2a:local --help
docker run --rm --env-file .env kafka-a2a:local agent
docker run --rm -p 8000:8000 --env-file .env kafka-a2a:local gateway
docker run --rm -p 8001:8001 --env-file .env kafka-a2a:local proxy --agent-name host
```

Notes:
- `KA2A_BOOTSTRAP_SERVERS` must be reachable from the container. If your broker runs on your host machine, use a
  host-reachable address (on macOS: `host.docker.internal:<port>`).

## CLI

Installed as `ka2a` (also available via `python -m kafka_a2a.cli`).

Commands:
- `ka2a agent` — runs an agent that consumes `ka2a.req.<agent>` and replies to client reply topics
- `ka2a gateway` — runs the FastAPI gateway on `KA2A_GATEWAY_PORT` (default `8000`)
- `ka2a proxy` — runs the A2A HTTP proxy on `KA2A_PROXY_PORT` (default `8001`)

## Agent configuration (env)

Common:
- `KA2A_BOOTSTRAP_SERVERS` (default `localhost:9092`)

Agent runtime:
- `KA2A_AGENT_NAME` (default: `<prefix><hostname>`)
- `KA2A_AGENT_NAME_PREFIX` (default: empty; used when `KA2A_AGENT_NAME` is unset)
- `KA2A_AGENT_DESCRIPTION`
- `KA2A_AGENT_URL`
- `KA2A_AGENT_VERSION` (default `0.1.0`)
- `KA2A_AGENT_PROCESSOR` = `echo` | `prompted-echo` | `langgraph-chat` | `pkg.module:callable` (default `echo`)
- `KA2A_SYSTEM_PROMPT` (used by `prompted-echo`)
- `KA2A_AGENT_PUSH_NOTIFICATIONS` (`true|false`)
- `KA2A_TASK_STORE` = `memory` | `redis` (default `memory`)
- `KA2A_REDIS_URL` (required when `KA2A_TASK_STORE=redis`)
- `KA2A_REDIS_NAMESPACE` (optional; default `ka2a`)

Multi-tenant isolation (optional):
- `KA2A_TENANT_ISOLATION` (`true|false`)
- `KA2A_REQUIRE_TENANT_MATCH` (`true|false`, default true)
- `KA2A_PRINCIPAL_METADATA_KEY` (default `urn:ka2a:principal`)
- `KA2A_STORE_PRINCIPAL_SECRETS` (`true|false`, default false; if true, persists `claims`/`bearerToken` to stored Tasks)

AgentCard override (optional):
- `KA2A_AGENT_CARD_PATH=/path/to/agent-card.json` (mount it in Docker and the agent will merge Kafka transport info)
  - Example cards are in `agent_cards/host.agent-card.json` and `agent_cards/echo.agent-card.json`

## Push notifications (optional)

If the agent runs with push notifications enabled, it supports:
`tasks/pushNotificationConfig/set|get|list|delete`.

Delivery:
- `http(s)://...` URLs: `POST` JSON payload with optional auth headers
- `kafka://<topic>` URLs: publish `EnvelopeType.event` to that topic on the same cluster

The payload is a `StreamResponse` object with exactly one of `statusUpdate` or `artifactUpdate`.

## SaaS / JWT mode (optional)

If you run a gateway/proxy in front of agents and you want **per-user task isolation**, configure JWT verification
at the edge and forward a `Principal` into request metadata (`urn:ka2a:principal`).

JWT env vars (gateway/proxy):
- `KA2A_JWT_ENABLED=true`
- `KA2A_JWT_KEY` (HS shared secret, or RS/ES public key PEM)
  - or `KA2A_JWT_KEY_PATH=/run/secrets/jwt_public.pem`
- `KA2A_JWT_ALGORITHMS=RS256` (or `HS256`, `ES256`, comma-separated)
- `KA2A_JWT_USER_CLAIM=sub` (your user id claim)
- `KA2A_JWT_TENANT_CLAIM=tenant_id` (optional)
- `KA2A_JWT_INCLUDE_CLAIMS=true` (if you want agents/tools to read custom claims like `ka2a`)

### JWT claim schema (optional)

K-A2A reserves a top-level namespaced claim key: `ka2a` (see `src/kafka_a2a/credentials.py`).

Example JWT payload:

```json
{
  "sub": "user-123",
  "tenant_id": "acme",
  "ka2a": {
    "v": 1,
    "llm": {
      "provider": "openai",
      "model": "gpt-4.1-mini",
      "apiKey": { "ciphertext": "BASE64(...)", "alg": "A256GCM", "kid": "enc-key-2026-02" }
    }
  }
}
```

Important:
- JWT payloads are **not confidential** by default (signature != encryption). Only put encrypted blobs in claims.
- By default, agents do **not** persist `Principal.bearerToken`/`Principal.claims` to stored Tasks.

## Local/dev LLM credentials from `.env` (optional)

If you are not using JWT claims, you can store a single set of credentials in environment variables.
This is mainly for local development and simple single-tenant deployments.

```bash
KA2A_LOAD_DOTENV=true
KA2A_LLM_CREDENTIALS_SOURCE=env
KA2A_LLM_PROVIDER=openai
KA2A_LLM_API_KEY=sk-...
KA2A_LLM_MODEL=gpt-4.1-mini
```

Load and resolve:

```python
from kafka_a2a.settings import Ka2aSettings

settings = Ka2aSettings.from_env()
creds = settings.resolve_llm_credentials()
```

### Using the built-in LangGraph processor

Set the agent processor to `langgraph-chat` and configure an LLM factory.
By default, K-A2A uses an OpenAI-compatible Chat Completions adapter:
`kafka_a2a.llms.openai_compat:create_chat_model`.

Example:

```bash
KA2A_AGENT_PROCESSOR=langgraph-chat
KA2A_LLM_CREDENTIALS_SOURCE=env
KA2A_LLM_PROVIDER=gemini
KA2A_LLM_MODEL=gemini-1.5-flash
GOOGLE_API_KEY=your-api-key
```

## Roadmap / known gaps

- More built-in processors (tool calling, MCP wiring patterns).
- Authenticated Kafka (SASL/TLS) + ACL-aware topic strategy for production.
- First-class “tool/MCP auth” patterns (claims + metadata plumbing exists, but tools are app-specific).
