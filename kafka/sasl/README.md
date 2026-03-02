# Secured Kafka (SASL + ACLs) for K-A2A

This folder provides a **single-node Kafka (KRaft) broker** configured for:

- **SASL/PLAIN authentication** (no TLS in this compose; see notes below for TLS)
- **ACL authorization** (KRaft `StandardAuthorizer`)

It is meant for local/dev and for validating that K-A2A can connect to a secured broker.

## 1) Start the broker

```bash
cd kafka/sasl
cp .env.example .env
# Edit .env if you want different usernames/passwords or an external advertised host/port.
docker compose up -d
```

Note: if you change the `admin` username/password in `.env`, also update `kafka/sasl/admin.properties`
so the init/CLI tools can authenticate.

## Server + TLS (recommended)

If you run this on a public server, **do not expose SASL_PLAINTEXT to the internet** (credentials would be sent
in cleartext). You have two common options:

### Option A (recommended): Kafka terminates TLS (`SASL_SSL`)

Kafka itself presents a certificate (for `kafka.interaims.com`) and your clients connect with
`security.protocol=SASL_SSL`. This is the cleanest approach for multi-broker later, but it requires
setting up Kafka keystores/truststores.

### Option B (simple): TLS terminates at a TCP proxy (Caddy/HAProxy/nginx stream), Kafka stays `SASL_PLAINTEXT`

- Use a **TCP proxy** (not HTTP). Your example:

```caddyfile
kafka.interaims.com {
  reverse_proxy localhost:9092
}
```

won’t work for Kafka because that `reverse_proxy` is HTTP-only.

If you want to use Caddy, you need the **Layer 4** app/plugin (`caddy-l4`) so Caddy can proxy raw TCP.
See `kafka/sasl/Caddyfile.example`.

- Bind Kafka’s public listener to localhost only:

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d
```

If you get `address already in use` for port `9094`, set `KA2A_KAFKA_HOST_PORT` in `kafka/sasl/.env` (example: `19094`)
and update your TCP proxy upstream accordingly.

- Decide what port you want the public endpoint to be:
  - If your proxy listens on `:443`, you must advertise `kafka.interaims.com:443`
  - If your proxy listens on `:9094`, you must advertise `kafka.interaims.com:9094`

In this compose, the host-facing broker listener is `9094` (the internal Docker listener is `9092`), so:
- If your proxy runs on the **host**, proxy to `127.0.0.1:9094` (or `127.0.0.1:$KA2A_KAFKA_HOST_PORT` if you changed it)
- If your proxy runs in **Docker on the same network**, proxy to `kafka:9092`

Then configure K-A2A to connect to the proxy endpoint using `SASL_SSL` (TLS to the proxy) + SASL creds.

#### Minimal “server” checklist (Option B)

1) In `kafka/sasl/.env`, set:
- `KAFKA_ADVERTISED_LISTENERS` to your public DNS/port, e.g. `kafka.interaims.com:443`
- strong passwords (don’t keep the defaults)

2) Start Kafka bound to localhost only:

```bash
cd kafka/sasl
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d
```

3) Run your TCP TLS proxy on the host (Caddy L4 / nginx stream / HAProxy):
- open the proxy port to the internet (commonly `443`)
- keep `9094` closed to the internet (localhost-only)

4) Point K-A2A at it (root `.env` used by your agent/gateway containers):

```env
KA2A_BOOTSTRAP_SERVERS=kafka.interaims.com:443
KA2A_KAFKA_SECURITY_PROTOCOL=SASL_SSL
KA2A_KAFKA_SASL_MECHANISM=PLAIN
KA2A_KAFKA_SASL_USERNAME=ka2a
KA2A_KAFKA_SASL_PASSWORD=ka2a-secret
```

## 2) Point K-A2A at this broker

If K-A2A is running on your host machine:

```bash
KA2A_BOOTSTRAP_SERVERS=localhost:9094
KA2A_KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT
KA2A_KAFKA_SASL_MECHANISM=PLAIN
KA2A_KAFKA_SASL_USERNAME=ka2a
KA2A_KAFKA_SASL_PASSWORD=ka2a-secret
```

If K-A2A is running in Docker on macOS and your Kafka broker is started via this compose on the same machine:

```bash
KA2A_BOOTSTRAP_SERVERS=host.docker.internal:9094
KA2A_KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT
KA2A_KAFKA_SASL_MECHANISM=PLAIN
KA2A_KAFKA_SASL_USERNAME=ka2a
KA2A_KAFKA_SASL_PASSWORD=ka2a-secret
```

## 3) TLS (SASL_SSL) notes

This compose uses `SASL_PLAINTEXT` to keep setup minimal. For **encryption in transit**, you need:

- a broker listener using `SASL_SSL`
- broker keystore/truststore (and optionally client cert auth)
- client-side CA (`KA2A_KAFKA_SSL_CA_FILE`) and (optionally) client cert/key

If you want, I can add a `docker-compose.ssl.yml` + a small script that generates dev certificates
and enables `SASL_SSL` on a separate port.
