# Redis for K-A2A (Task Store)

This folder contains a standalone Redis container setup you can deploy on a server and point K-A2A agents at.

## Run

```bash
cd redis
cp .env.example .env
docker compose up -d --build
```

## Configure K-A2A to use it

In your K-A2A `.env`:

```env
KA2A_TASK_STORE=redis
KA2A_REDIS_URL=redis://:change-me@<redis-host>:6379/0
KA2A_REDIS_NAMESPACE=ka2a
```

Security recommendation: do not expose port 6379 publicly; restrict it via firewall/security group/VPC.

