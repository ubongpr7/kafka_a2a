#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP="${BOOTSTRAP:-kafka:9092}"
CFG="${CFG:-/etc/kafka/secrets/admin.properties}"

APP_USER="${APP_USER:-ka2a}"
TOPIC_PREFIX="${TOPIC_PREFIX:-ka2a}"
GROUP_PREFIX="${GROUP_PREFIX:-ka2a}"

echo "[acl-init] waiting for broker at ${BOOTSTRAP}..."
for i in $(seq 1 120); do
  if kafka-topics --bootstrap-server "${BOOTSTRAP}" --command-config "${CFG}" --list >/dev/null 2>&1; then
    echo "[acl-init] broker is ready"
    break
  fi
  sleep 1
done

echo "[acl-init] ensuring registry topic (compacted) exists..."
if ! kafka-topics --bootstrap-server "${BOOTSTRAP}" --command-config "${CFG}" --describe --topic "${TOPIC_PREFIX}.agent_cards" >/dev/null 2>&1; then
  kafka-topics --bootstrap-server "${BOOTSTRAP}" --command-config "${CFG}" --create \
    --topic "${TOPIC_PREFIX}.agent_cards" \
    --partitions 1 \
    --replication-factor 1 \
    --config cleanup.policy=compact || true
fi

echo "[acl-init] granting ${APP_USER} access to ${TOPIC_PREFIX}* topics and ${GROUP_PREFIX}* groups..."
kafka-acls --bootstrap-server "${BOOTSTRAP}" --command-config "${CFG}" --add \
  --allow-principal "User:${APP_USER}" \
  --operation Read --operation Write --operation Describe --operation Create \
  --topic "${TOPIC_PREFIX}" --resource-pattern-type prefixed

kafka-acls --bootstrap-server "${BOOTSTRAP}" --command-config "${CFG}" --add \
  --allow-principal "User:${APP_USER}" \
  --operation Read --operation Describe \
  --group "${GROUP_PREFIX}" --resource-pattern-type prefixed

echo "[acl-init] done"

