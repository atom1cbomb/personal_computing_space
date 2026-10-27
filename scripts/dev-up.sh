#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! command -v "$DOCKER_BIN" >/dev/null 2>&1 || ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  for candidate in "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe" "/usr/bin/docker.exe"; do
    if [[ -x "$candidate" ]] && "$candidate" info >/dev/null 2>&1; then DOCKER_BIN="$candidate"; break; fi
  done
fi

INTERNAL_SECRET_FILE="${LOOM_INTERNAL_API_SECRET_FILE:-$ROOT_DIR/secrets/internal_api_secret}"
if [[ ! -s "$INTERNAL_SECRET_FILE" ]]; then
  echo "Missing non-empty secret file: $INTERNAL_SECRET_FILE" >&2
  echo "Create it before starting the stack (see README.md)." >&2
  exit 1
fi

export LOOM_INTERNAL_API_SECRET_FILE="$INTERNAL_SECRET_FILE"

GRIP_TOKEN_FILE="${LOOM_GRIP_TOKEN_FILE:-$ROOT_DIR/secrets/grip_token}"
if [[ -s "$GRIP_TOKEN_FILE" ]]; then
  export LOOM_GRIP_TOKEN="$(<"$GRIP_TOKEN_FILE")"
  export LOOM_GRIP_TOKEN_FILE="$GRIP_TOKEN_FILE"
fi

# The direct service protocols (request-reply and byte-stream) use a separate
# short-lived credential signed by Observer.  Keep the development key pair in
# the local secrets directory and reuse it across restarts so existing
# activations remain verifiable.  Production deployments must provide
# operator-managed keys instead.
SERVICE_SIGNING_KEY_FILE="${LOOM_SERVICE_SIGNING_KEY_FILE:-$ROOT_DIR/secrets/service-signing.pem}"
SERVICE_VERIFY_KEY_FILE="${LOOM_SERVICE_VERIFY_KEY_FILE:-$ROOT_DIR/secrets/service-verify.pem}"
mkdir -p "$(dirname "$SERVICE_SIGNING_KEY_FILE")" "$(dirname "$SERVICE_VERIFY_KEY_FILE")"
if [[ ! -s "$SERVICE_SIGNING_KEY_FILE" && ! -s "$SERVICE_VERIFY_KEY_FILE" ]]; then
  command -v openssl >/dev/null 2>&1 || {
    echo "openssl is required to create local service signing keys" >&2
    exit 1
  }
  umask 077
  openssl genpkey -algorithm Ed25519 -out "$SERVICE_SIGNING_KEY_FILE"
  openssl pkey -in "$SERVICE_SIGNING_KEY_FILE" -pubout -out "$SERVICE_VERIFY_KEY_FILE"
elif [[ ! -s "$SERVICE_SIGNING_KEY_FILE" || ! -s "$SERVICE_VERIFY_KEY_FILE" ]]; then
  echo "Service signing key pair is incomplete; provide both files or remove both to regenerate" >&2
  exit 1
fi
export LOOM_SERVICE_SIGNING_KEY_FILE="$SERVICE_SIGNING_KEY_FILE"
export LOOM_SERVICE_VERIFY_KEY_FILE="$SERVICE_VERIFY_KEY_FILE"

# AutoPaper declares network_profile=egress.  The runtime plugin attaches its
# containers to this operator-approved network after creation, so Compose
# cannot create it merely from the service list.
if ! "$DOCKER_BIN" network inspect loom-egress >/dev/null 2>&1; then
  "$DOCKER_BIN" network create loom-egress >/dev/null
fi

"$DOCKER_BIN" compose -f deploy/docker-compose.yml up -d --build --remove-orphans minio minio-init observer-db slave-a-db slave-b-db observer driver slave-a slave-b

echo "Loom v2 Observer: http://localhost:18080"
echo "Driver and Slaves run on the internal Compose network (Observer is the public entry point)"
