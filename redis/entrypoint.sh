#!/bin/sh
set -eu

CONF="${REDIS_CONF:-/usr/local/etc/redis/redis.conf}"

set -- redis-server "$CONF"

if [ -n "${REDIS_PASSWORD:-}" ]; then
  set -- "$@" --requirepass "$REDIS_PASSWORD"
fi

if [ -n "${REDIS_ARGS:-}" ]; then
  # shellcheck disable=SC2086
  set -- "$@" $REDIS_ARGS
fi

exec "$@"

