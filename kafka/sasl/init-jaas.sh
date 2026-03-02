#!/usr/bin/env bash
set -euo pipefail

JAAS_FILE="${JAAS_FILE:-/etc/kafka/secrets/kafka_server_jaas.conf}"
ENV_FILE="${ENV_FILE:-/etc/kafka/secrets/ka2a_env.sh}"
ADMIN_PROPS_FILE="${ADMIN_PROPS_FILE:-/etc/kafka/secrets/admin.properties}"

JAAS_LINE="${KAFKA_LISTENER_NAME_SASL_PLAINTEXT_PLAIN_SASL_JAAS_CONFIG:-}"
if [[ -z "${JAAS_LINE}" ]]; then
  JAAS_LINE="${KAFKA_LISTENER_NAME_SASL_PLAINTEXT_PUBLIC_PLAIN_SASL_JAAS_CONFIG:-}"
fi

if [[ -z "${JAAS_LINE}" ]]; then
  echo "[ka2a-sasl] Missing SASL JAAS config. Set KAFKA_LISTENER_NAME_SASL_PLAINTEXT_PLAIN_SASL_JAAS_CONFIG." >&2
  exit 1
fi

mkdir -p "$(dirname "${JAAS_FILE}")"

# Confluent's cp-kafka images require KAFKA_OPTS to be set when SASL_* listeners are present.
# Because this script is run as a *separate* process, we persist the computed env to a file
# that the container command can `source` before starting Kafka.
opts="${KAFKA_OPTS:-}"
if [[ -z "${opts}" ]]; then
  opts="-Djava.security.auth.login.config=${JAAS_FILE}"
elif [[ "${opts}" != *"java.security.auth.login.config"* ]]; then
  opts="${opts} -Djava.security.auth.login.config=${JAAS_FILE}"
fi

printf 'export KAFKA_OPTS=%q\n' "${opts}" >"${ENV_FILE}"

printf 'KafkaServer { %s };\nKafkaClient { %s };\n' "${JAAS_LINE}" "${JAAS_LINE}" >"${JAAS_FILE}"

echo "[ka2a-sasl] JAAS file written to ${JAAS_FILE}"
echo "[ka2a-sasl] Env file written to ${ENV_FILE}"

# Generate an admin client properties file for Kafka CLI tools used by the healthcheck and ACL init.
admin_user="$(printf '%s' "${JAAS_LINE}" | sed -n 's/.*username="\([^"]*\)".*/\1/p')"
admin_pass="$(printf '%s' "${JAAS_LINE}" | sed -n 's/.*password="\([^"]*\)".*/\1/p')"

if [[ -z "${admin_user}" || -z "${admin_pass}" ]]; then
  echo "[ka2a-sasl] Could not extract admin username/password from JAAS config." >&2
  exit 1
fi

cat >"${ADMIN_PROPS_FILE}" <<EOF
# Admin client properties for Kafka CLI tools (kafka-topics, kafka-acls, etc.)
security.protocol=SASL_PLAINTEXT
sasl.mechanism=PLAIN
sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="${admin_user}" password="${admin_pass}";
EOF

echo "[ka2a-sasl] Admin properties written to ${ADMIN_PROPS_FILE}"
