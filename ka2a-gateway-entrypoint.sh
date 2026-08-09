#!/bin/sh
set -eu

host="${KA2A_BOOTSTRAP_HOST:-kafka}"
port="${KA2A_BOOTSTRAP_PORT:-9092}"
attempt=1
max_attempts="${KA2A_STARTUP_MAX_ATTEMPTS:-60}"

until python3 - "$host" "$port" <<'PY'
import asyncio
import sys

from aiokafka import AIOKafkaProducer

host = sys.argv[1]
port = int(sys.argv[2])


async def main() -> None:
    producer = AIOKafkaProducer(bootstrap_servers=f"{host}:{port}")
    await producer.start()
    await producer.stop()


asyncio.run(main())
PY
do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "Kafka still unavailable after ${attempt} attempts" >&2
    exit 1
  fi
  echo "Waiting for Kafka at ${host}:${port} (${attempt}/${max_attempts})"
  attempt=$((attempt + 1))
  sleep 5
done

exec ka2a gateway "$@"
