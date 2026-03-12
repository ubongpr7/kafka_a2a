# Neo4j Server (Docker Compose)

This folder runs a self-hosted Neo4j server suitable for a knowledge graph demo.

## 1) Start Neo4j

```bash
cd /Users/ubongpr7/dev/pr7/inventory/kafka_a2a/Neo4jServer
cp .env.example .env
docker compose up -d
docker compose ps
docker compose logs -f neo4j
```

## 2) Access

- Neo4j Browser UI: `http://<host>:7474` (or your `NEO4J_HTTP_PORT`)
- Bolt endpoint for apps/tools: `bolt://<host>:7687` (or your `NEO4J_BOLT_PORT`)

Login with username `neo4j` and password from `NEO4J_AUTH`.

## 3) Quick test query

In Browser:

```cypher
CREATE (n:Person {name: 'Ubong'});
MATCH (n:Person) RETURN n LIMIT 10;
```

## 4) Domain and reverse proxy notes

If you use a domain like `neo4j.interaims.com`:

- Route web UI to Neo4j HTTP port (`7474`) with your reverse proxy.
- Bolt (`7687`) is not normal HTTP traffic; it needs a TCP/L4 proxy or direct port exposure.
- Update `NEO4J_HTTP_ADVERTISED_ADDRESS` and `NEO4J_BOLT_ADVERTISED_ADDRESS` in `.env` to match your public addresses.

## 5) Server deployment now (`neo4j.interaims.com`)

On server:

```bash
cd /path/to/Neo4jServer
cp .env.server.example .env
docker compose up -d
docker compose ps
```

This keeps Neo4j bound to localhost and publishes it through Caddy only.

Caddy:

```bash
# Merge the neo4j.interaims.com block from Caddyfile.example into your existing /etc/caddy/Caddyfile.
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Required DNS:
- `neo4j.interaims.com` A record -> your server public IP

Verify:

```bash
curl -I https://neo4j.interaims.com
docker compose logs --tail=150 neo4j
```

Browser:
- Open `https://neo4j.interaims.com`

Bolt access options:
- Same server tools: use `bolt://127.0.0.1:7687`
- External tools: open firewall for TCP `7687` and use `bolt://neo4j.interaims.com:7687`

## 6) MCP tool integration pattern

For your agent tool layer, use env vars like:

```env
NEO4J_URI=bolt://neo4j.interaims.com:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-strong-password
NEO4J_DATABASE=neo4j
```

Then your MCP tool can expose operations such as:
- `graph.upsert_nodes`
- `graph.upsert_relationships`
- `graph.cypher_query` (read-only for safety)

## 7) Stop and cleanup

Stop:

```bash
docker compose down
```

Stop and remove data:

```bash
docker compose down -v
```
