#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

read_secret() {
  path="$1"
  if [ ! -r "$path" ]; then
    echo "missing secret: $path" >&2
    exit 1
  fi
  value=$(cat "$path")
  if [ -z "$value" ]; then
    echo "empty secret: $path" >&2
    exit 1
  fi
  printf '%s' "$value"
}

internal_api_secret=$(read_secret /run/secrets/internal_api_secret)
export LOOM_INTERNAL_API_SECRET="$internal_api_secret"
exec uvicorn loom_v2.driver.app:app --host 0.0.0.0 --port 8090
