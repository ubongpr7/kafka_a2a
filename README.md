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
- **Kafka SASL/TLS (optional)**: connect to secured Kafka by setting `KA2A_KAFKA_SECURITY_PROTOCOL` + SASL/TLS env vars.
- **JSON-RPC 2.0 surface** over Kafka (`message/send`, `message/stream`, `tasks/*`, `agent/getCard`, etc.).
- **Streaming**: multiple JSON-RPC responses published to the client reply topic for `message/stream` and `tasks/resubscribe`.
- **Registry + discovery**: agents publish AgentCards to a Kafka topic (`ka2a.agent_cards`).
- **Topic strategy (optional)**: namespace/prefix topics via `KA2A_TOPIC_NAMESPACE` / `KA2A_*_PREFIX` for multi-deploy + ACL-friendly setups.
- **Gateway HTTP endpoints**: `/agents`, `/tasks`, `/tasks/{id}`, `/tasks/{id}/events` (SSE), plus `/chat` + `/upload` + `/stream`.
- **Task stores**: in-memory (default) and Redis (`KA2A_TASK_STORE=redis`).
- **Long-session memory (optional)**: per-context summary/profile stored in memory or Redis (`KA2A_CONTEXT_MEMORY_*`).
- **Push notifications** (optional): `tasks/pushNotificationConfig/*` + delivery to `http(s)://...` webhooks or `kafka://topic`.
- **SaaS / multi-tenant isolation** (optional): per-task principal enforcement via request `metadata`.
- **JWT verification hook** (optional): FastAPI gateway/proxy can verify Bearer JWTs (HS/RS/ES, JWKS supported) and forward a `Principal` in metadata.
- **Local/dev credentials** (optional): resolve a single set of LLM credentials from `.env` / process env.
- **LangGraph processor** (optional): `langgraph-chat` with pluggable `KA2A_LLM_FACTORY` (default: OpenAI-compatible adapter).
- **Tool calling (MCP-ready, optional)**: `langgraph-chat` can execute tools via `ToolCallPart`/`ToolResultPart` (built-in MCP HTTP executor available).
- **Router processor** (optional): `router` host agent that selects downstream agents by card/skills and delegates over Kafka.
- **Multimodal (partial)**: `/upload` sends file bytes; Gemini adapter can pass vision parts and return file artifacts.
- **Ops helpers**: `ka2a ensure-topics` for clusters with Kafka auto-topic-creation disabled.
- **Ops (optional)**: DLQ publishing for malformed Kafka messages (`KA2A_DLQ_ENABLED`), `/metrics` endpoint (`KA2A_METRICS_ENABLED`), and trace propagation (`KA2A_TRACE_ENABLED`).

## Architecture (Kafka topics)

K-A2A uses a small set of topics (defaults shown):

- Agent request topic: `ka2a.req.<agent_name>`
- Client reply topic: `ka2a.reply.<client_id>`
- Task events topic: `ka2a.evt.<task_id>` (used for push-to-Kafka notifications)
- Agent card registry topic: `ka2a.agent_cards`

Optional topic namespacing/prefixing:
- Set `KA2A_TOPIC_NAMESPACE=<name>` to automatically prefix the default topics (and DLQ) with `<name>.`
- Or override `KA2A_REQUESTS_PREFIX`, `KA2A_REPLIES_PREFIX`, `KA2A_EVENTS_PREFIX`, `KA2A_REGISTRY_TOPIC` explicitly

Wire format:
- All messages are `KafkaEnvelope` objects (`EnvelopeType.request|response|event|registry`)
- The `payload` is a JSON-RPC request/response dict (or a StreamResponse event for notifications)

## Quickstart (Docker Compose)

This repo ships a single `docker-compose.yml` that starts:
- The real Intera agents (`host-agent`, `product-agent`, `inventory-agent`, and `pos-agent`)
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

If your Kafka cluster has **auto-topic-creation disabled**, create the required topics first:

```bash
# Uses KA2A_BOOTSTRAP_SERVERS from .env
docker compose run --rm gateway ensure-topics --agents host,product,inventory,pos --client-ids gateway,proxy
```

Optional: if you want to run Kafka locally for development, you can use `kafka/docker-compose.yml` and then point
the app stack at it (on macOS this is typically `host.docker.internal:9094`):

```bash
docker compose -f kafka/docker-compose.yml up -d
# In .env, set:
#   KA2A_BOOTSTRAP_SERVERS=host.docker.internal:9094
docker compose up -d --build
```

Optional: if you want a **secured local Kafka** (SASL/PLAIN + ACLs), use `kafka/sasl/docker-compose.yml`:

```bash
cd kafka/sasl
cp .env.example .env
docker compose up -d
```

For server deployment (TLS + SASL + ACLs), see `kafka/sasl/README.md` and the TCP-proxy example `kafka/sasl/Caddyfile.example`.

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

## Playground UI (Next.js + Redux)

This repo includes a simple UI in `frontend/` that:
- Sends streaming chat requests (`/stream`)
- Reuses `contextId` so the agent remembers the session
- Shows the full event timeline (task/status/artifact updates)

Run it:

```bash
cd frontend
yarn install
cp .env.example .env.local
yarn dev
```

Open `http://localhost:3000`. Make sure the gateway is reachable at `NEXT_PUBLIC_KA2A_GATEWAY_URL` (default `http://localhost:8000`).
For a hosted backend, set `NEXT_PUBLIC_KA2A_GATEWAY_URL` in `frontend/.env.local` to the remote URL, for example `https://dev.agents.interaims.com`.
If your browser client sends `credentials: "include"`, set `KA2A_CORS_ALLOW_CREDENTIALS=true` on the gateway and use a specific origin or origin regex instead of `*`.
To disable gateway/proxy request timeouts to downstream agents, set `KA2A_GATEWAY_REQUEST_TIMEOUT_S=0` (and `KA2A_PROXY_REQUEST_TIMEOUT_S=0` if you use the A2A HTTP proxy).

Test the A2A HTTP proxy:

```bash
curl -sS 'http://localhost:8001/.well-known/agent-card.json' | jq

curl -sS -X POST 'http://localhost:8001/' \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"hello"}]}}}' | jq
```

To make the host agent act as an orchestrator, set:

```bash
KA2A_AGENT_PROCESSOR=router
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
ka2a agent --agent-name host --agent-card-path ./agent_cards/host.agent-card.json
```

4) Run the gateway:

```bash
KA2A_DEFAULT_AGENT=host ka2a gateway
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
- `ka2a ensure-topics` — creates required Kafka topics when auto-topic-creation is disabled

## Agent configuration (env)

Common:
- `KA2A_BOOTSTRAP_SERVERS` (default `localhost:9092`)

Kafka topics (optional):
- `KA2A_TOPIC_NAMESPACE` (optional; prefixes default topics with `<namespace>.`)
- `KA2A_REQUESTS_PREFIX` (default `ka2a.req`)
- `KA2A_REPLIES_PREFIX` (default `ka2a.reply`)
- `KA2A_EVENTS_PREFIX` (default `ka2a.evt`)
- `KA2A_REGISTRY_TOPIC` (default `ka2a.agent_cards`)

Kafka security (optional):
- `KA2A_KAFKA_SECURITY_PROTOCOL` = `PLAINTEXT` | `SSL` | `SASL_PLAINTEXT` | `SASL_SSL` (default `PLAINTEXT`)
- `KA2A_KAFKA_SASL_MECHANISM` = `PLAIN` | `SCRAM-SHA-256` | `SCRAM-SHA-512` (required for SASL)
- `KA2A_KAFKA_SASL_USERNAME`, `KA2A_KAFKA_SASL_PASSWORD` (or `KA2A_KAFKA_SASL_PASSWORD_ENV`)
- `KA2A_KAFKA_SSL_CA_FILE`, `KA2A_KAFKA_SSL_CERT_FILE`, `KA2A_KAFKA_SSL_KEY_FILE`
- `KA2A_KAFKA_SSL_KEY_PASSWORD` (or `KA2A_KAFKA_SSL_KEY_PASSWORD_ENV`)
- `KA2A_KAFKA_SSL_CHECK_HOSTNAME` (default true)
- `KA2A_KAFKA_SSL_INSECURE_SKIP_VERIFY` (default false)

Ops (optional):
- `KA2A_DLQ_ENABLED` (`true|false`, default false)
- `KA2A_DLQ_TOPIC` (default `ka2a.dlq`)
- `KA2A_DLQ_MAX_VALUE_BYTES` (default `16384`)
- `KA2A_METRICS_ENABLED` (`true|false`, default false; enables `/metrics`)
- `KA2A_TRACE_ENABLED` (`true|false`, default false; propagates `urn:ka2a:trace` in metadata)

Agent runtime:
- `KA2A_AGENT_NAME` (default: `<prefix><hostname>`)
- `KA2A_AGENT_NAME_PREFIX` (default: empty; used when `KA2A_AGENT_NAME` is unset)
- `KA2A_AGENT_DESCRIPTION`
- `KA2A_AGENT_URL`
- `KA2A_AGENT_VERSION` (default `0.1.0`)
- `KA2A_AGENT_PROCESSOR` = `langgraph-chat` | `router` | `pkg.module:callable` (default `langgraph-chat`)
- `KA2A_SYSTEM_PROMPT` or `KA2A_SYSTEM_PROMPT_PATH`
- `KA2A_AGENT_PUSH_NOTIFICATIONS` (`true|false`)
- `KA2A_TASK_STORE` = `memory` | `redis` (default `memory`)
- `KA2A_REDIS_URL` (required when `KA2A_TASK_STORE=redis`)
- `KA2A_REDIS_NAMESPACE` (optional; default `ka2a`)
- `KA2A_CONTEXT_HISTORY_TURNS` (default `20`; max turns injected into the LLM prompt per context)

Multi-tenant isolation (optional):
- `KA2A_TENANT_ISOLATION` (`true|false`; defaults to true when `KA2A_LLM_CREDENTIALS_SOURCE!=env`)
- `KA2A_REQUIRE_TENANT_MATCH` (`true|false`, default true)
- `KA2A_PRINCIPAL_METADATA_KEY` (default `urn:ka2a:principal`)
- `KA2A_STORE_PRINCIPAL_SECRETS` (`true|false`, default false; if true, persists `claims`/`bearerToken` to stored Tasks)

AgentCard override (optional):
- `KA2A_AGENT_CARD_PATH=/path/to/agent-card.json` (mount it in Docker and the agent will merge Kafka transport info)
  - Example cards are in `agent_cards/host.agent-card.json`, `agent_cards/product.agent-card.json`,
    `agent_cards/inventory.agent-card.json`, and `agent_cards/pos.agent-card.json`

Long-session memory (optional):
- `KA2A_CONTEXT_MEMORY_STORE` = `off` | `memory` | `redis` (default `off`)
- `KA2A_CONTEXT_MEMORY_SUMMARY` (`true|false`, default false)
- `KA2A_CONTEXT_MEMORY_PROFILE` (`true|false`, default false)
- `KA2A_CONTEXT_MEMORY_UPDATE_EVERY` (default `1`; update every N turns)
- `KA2A_CONTEXT_MEMORY_HISTORY_ITEMS` (default `12`; items included in memory update prompt)
- `KA2A_CONTEXT_MEMORY_MAX_SUMMARY_CHARS` (default `1200`)
- `KA2A_CONTEXT_MEMORY_TTL_S` (optional; Redis only)

Upstream LLM robustness (optional):
- `KA2A_LLM_MAX_CONCURRENCY` (default `5`; set `0` to disable limiter)
- `KA2A_LLM_RETRY_MAX_RETRIES` (default `3`)
- `KA2A_LLM_RETRY_BASE_DELAY_S` (default `0.5`)
- `KA2A_LLM_RETRY_MAX_DELAY_S` (default `8`)

Router processor (optional):
- `KA2A_ROUTER_LLM_SELECTION` (`true|false`, default true)
- `KA2A_ROUTER_FALLBACK_AGENT` (optional; if routing is ambiguous)
- `KA2A_DIRECTORY_AUTO_OFFSET_RESET` (default `earliest`; used by the router directory watcher)

Tools / MCP (optional):
- Requires installing the MCP extra: `uv sync --extra mcp`
- `KA2A_TOOLS_ENABLED` (`true|false`, default false; only used by `langgraph-chat`)
- `KA2A_TOOLS_SOURCE` = `mcp` (built-in multi-MCP executor) or leave unset + set `KA2A_TOOL_EXECUTOR` for a fully custom executor
- `KA2A_TOOLS_MAX_STEPS` (default `5`)
- `KA2A_MCP_CONFIG_PATH` (optional JSON file with per-agent MCP server/tool config)
- `KA2A_MCP_AGENT_NAME` (optional override for config lookup; defaults to `KA2A_AGENT_NAME`)
- `KA2A_TOOL_EXECUTOR` (optional extra/local executor import path; when `KA2A_TOOLS_SOURCE=mcp`, this is composed with the built-in MCP executor)
- `KA2A_MCP_SERVER_URL` (Streamable HTTP endpoint, e.g. `http://localhost:8005/mcp`)
- `KA2A_MCP_TOKEN` (optional; or `KA2A_MCP_TOKEN_ENV`)
- `KA2A_MCP_TIMEOUT_S` (default `30`)
- `KA2A_MCP_TOOLS_CACHE_S` (default `60`)
- Legacy single-server envs (`KA2A_MCP_SERVER_URL`, `KA2A_MCP_TOKEN`) still work when `KA2A_MCP_CONFIG_PATH` is unset.

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
- `KA2A_JWT_KEY` (HS shared secret, or RS/ES public key PEM; optional if `KA2A_JWT_JWKS_URL` is set)
  - or `KA2A_JWT_KEY_PATH=/run/secrets/jwt_public.pem`
- `KA2A_JWT_JWKS_URL=https://.../.well-known/jwks.json` (recommended for RS256/ES256 + key rotation)
- `KA2A_JWT_JWKS_CACHE_LIFESPAN_S=300`
- `KA2A_JWT_JWKS_TIMEOUT_S=30`
- `KA2A_JWT_JWKS_HEADERS={"Authorization":"Bearer ..."}`
- `KA2A_JWT_ALGORITHMS=RS256` (or `HS256`, `ES256`, comma-separated)
- `KA2A_JWT_USER_CLAIM=sub` (your user id claim)
- `KA2A_JWT_TENANT_CLAIM=tenant_id` (optional)
- `KA2A_JWT_FORWARD_BEARER_TOKEN=true` (required if MCP tools should run with the end-user token)
- `KA2A_JWT_INCLUDE_CLAIMS=true` (if you want agents/tools to read custom claims like `ka2a`)

For the `intera_users` token shape used in this inventory platform:
- Set `KA2A_JWT_USER_CLAIM=user_id`
- Set `KA2A_JWT_TENANT_CLAIM=profile_id`
- Enable `KA2A_JWT_FORWARD_BEARER_TOKEN=true`
- Enable `KA2A_JWT_INCLUDE_CLAIMS=true`

JWT secret decryption (agent side):
- `KA2A_SECRET_DECRYPTOR=kafka_a2a.secrets:decrypt_fernet_secret`
- `KA2A_FERNET_KEY=<same key used by your identity service to encrypt secrets>`
- Optional key rotation fallback: `KA2A_FERNET_KEYS=<comma-separated old keys>`

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
      "apiKey": { "ciphertext": "BASE64(...)", "alg": "fernet" }
    },
    "tavily": {
      "apiKey": { "ciphertext": "BASE64(...)", "alg": "fernet" }
    }
  }
}
```

Important:
- JWT payloads are **not confidential** by default (signature != encryption). Only put encrypted blobs in claims.
- By default, agents do **not** persist `Principal.bearerToken`/`Principal.claims` to stored Tasks.
- If your issuer cannot embed nested JSON objects, K-A2A also accepts flattened claim keys like
  `ka2a.llm.provider`, `ka2a.llm.apiKey`, `ka2a.tavily.apiKey`, `ka2a.mcp.*`.

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

Example multi-MCP setup for specialist agents:

```bash
KA2A_AGENT_PROCESSOR=langgraph-chat
KA2A_TOOLS_ENABLED=true
KA2A_TOOLS_SOURCE=mcp
KA2A_MCP_CONFIG_PATH=./mcp-tools.example.json
KA2A_JWT_ENABLED=true
KA2A_JWT_USER_CLAIM=user_id
KA2A_JWT_TENANT_CLAIM=profile_id
KA2A_JWT_FORWARD_BEARER_TOKEN=true
KA2A_JWT_INCLUDE_CLAIMS=true
```

Example `mcp-tools.example.json`:

```json
{
  "version": 1,
  "agents": {
    "product": {
      "servers": [
        {
          "id": "products",
          "serverUrl": "http://products-mcp:8000/mcp/",
          "toolNamePrefix": "product.",
          "auth": { "mode": "forward_bearer" },
          "tools": ["search_products", "get_product_details", "search_product_variants"]
        }
      ]
    },
    "inventory": {
      "servers": [
        {
          "id": "inventory",
          "serverUrl": "http://inventory-mcp:8000/mcp/",
          "toolNamePrefix": "inventory.",
          "auth": { "mode": "forward_bearer" },
          "tools": ["search_inventories", "get_inventory_alerts", "search_stock_movements"]
        }
      ]
    },
    "pos": {
      "servers": [
        {
          "id": "pos",
          "serverUrl": "http://pos-mcp:8000/mcp/",
          "toolNamePrefix": "pos.",
          "auth": { "mode": "forward_bearer" },
          "tools": ["get_current_pos_session", "search_pos_orders", "list_held_pos_orders"]
        }
      ]
    }
  }
}
```

Supported MCP auth modes in the JSON config:
- `none`: no auth header
- `static`: use a configured token or `tokenEnv`
- `context`: use request-scoped `ka2a.mcp.token` credentials
- `forward_bearer`: forward the end-user bearer token from the gateway

## Roadmap / known gaps

- Token-level streaming UX in the Playground UI.
- True multimodal across more providers (bytes/uris in, structured artifacts out).
- More built-in processors and tool patterns (e.g., richer planning/execution styles).
