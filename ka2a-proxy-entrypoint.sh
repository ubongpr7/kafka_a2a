#!/bin/sh
set -eu

host="${KA2A_BOOTSTRAP_HOST:-kafka}"
port="${KA2A_BOOTSTRAP_PORT:-9092}"
attempt=1
max_attempts="${KA2A_STARTUP_MAX_ATTEMPTS:-60}"

until python3 - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

with socket.create_connection((host, port), 5):
    pass
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

exec ka2a proxy --agent-name host --host 0.0.0.0 --port "${KA2A_PROXY_PORT:-7007}"
