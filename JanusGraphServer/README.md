# JanusGraph Server (Docker)

This folder provides a self-hosted JanusGraph server you can run with Docker.

## 1) Quick start (single container, persistent local storage)

```bash
cd /Users/ubongpr7/dev/pr7/inventory/kafka_a2a/JanusGraphServer
cp .env.example .env
docker compose up -d
docker compose ps
docker compose logs -f janusgraph
```

Server endpoint:
- Gremlin Server WebSocket: `ws://<host>:${JANUSGRAPH_HOST_PORT}` (default `8182`)

## 2) Smoke test from Gremlin Console

Open console inside the running container:

```bash
docker compose exec janusgraph bin/gremlin.sh
```

Run inside console:

```groovy
:remote connect tinkerpop.server conf/remote.yaml session
:remote console
g.addV('person').property('name','Ubong').next()
g.V().hasLabel('person').count().next()
```

## 3) Optional Cassandra-backed setup

Use this if you want JanusGraph storage in Cassandra instead of local BerkeleyJE.

```bash
cd /Users/ubongpr7/dev/pr7/inventory/kafka_a2a/JanusGraphServer
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.cql.yml up -d
docker compose -f docker-compose.yml -f docker-compose.cql.yml ps
```

This starts:
- `janusgraph` (Gremlin Server)
- `cassandra` (storage backend)

## 4) Stop / cleanup

Stop stack:

```bash
docker compose down
```

Stop + remove data volumes:

```bash
docker compose down -v
```

## 5) Server deployment notes

- Open inbound TCP port `8182` (or your `JANUSGRAPH_HOST_PORT`) for Gremlin clients.
- JanusGraph traffic on `8182` is Gremlin protocol (WebSocket), not plain HTTP API.
- If you put it behind a proxy, use TCP/WebSocket-capable passthrough.
- For production security, restrict IPs with firewall/VPC rules and prefer TLS termination at an L4-capable edge.

## 6) Next step for K-A2A integration

Recommended pattern:
- keep JanusGraph isolated in its own stack,
- expose one internal service that executes Gremlin queries,
- call that service from your tool layer (later MCP integration) instead of letting agents query JanusGraph directly.
