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
OBSERVER_PORT="${LOOM_OBSERVER_PORT:-18080}"
curl -fsS "http://localhost:${OBSERVER_PORT}/healthz"
curl -fsS "http://localhost:${OBSERVER_PORT}/" | grep -q "Run drawer"
export LOOM_OBSERVER_URL="http://localhost:${OBSERVER_PORT}"
: "${LOOM_GRIP_TOKEN:?configure the same GRIP token as the Observer}"
python - <<'PYTHON'
import asyncio
import os
from loom_orchestration import LoomClient

async def check():
    async with LoomClient(os.environ["LOOM_OBSERVER_URL"], token=os.environ["LOOM_GRIP_TOKEN"],
                          grip_authority=os.getenv("LOOM_GRIP_AUTHORITY", "loom.local"),
                          workspace_id=os.getenv("LOOM_WORKSPACE_ID", "workspace-default"),
                          allow_insecure_grip=True) as client:
        slaves = await client.resources(kind="slave")
        available = {r["metadata"]["slave_id"] for r in slaves if r["metadata"]["available"]}
        assert {"slave-a", "slave-b"} <= available, available
        for slave in slaves:
            assert (await client.describe(slave["uri"]))["offers"]
asyncio.run(check())
PYTHON
"$DOCKER_BIN" compose -f deploy/docker-compose.yml ps --status running
echo "Loom v2 smoke checks passed"
