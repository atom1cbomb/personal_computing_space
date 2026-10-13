#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from benchmarks.composite_heterogeneous_scheduling.run_composite import run_experiment


POLICIES = ("round_robin_v1", "least_loaded_v1", "earliest_finish_v1")
TOPOLOGIES = ("a-b", "a-b-c")


def experiment_order(repeats: int, seed: int) -> list[tuple[str, str, int]]:
    cells = [(topology, policy, repeat) for repeat in range(1, repeats + 1) for topology in TOPOLOGIES for policy in POLICIES]
    random.Random(seed).shuffle(cells)
    return cells


def configure_stack(*, compose_dir: Path, topology: str, policy: str, source_path: Path) -> None:
    env = {**os.environ, "LOOM_SCHEDULING_POLICY": policy, "LOOM_SOURCE_PATH": str(source_path)}
    base = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.heterogeneous.yml"]
    if topology == "a-b":
        subprocess.run(base + ["stop", "slave-c"], cwd=compose_dir, env=env, check=True)
    else:
        subprocess.run(base + ["up", "-d", "slave-c"], cwd=compose_dir, env=env, check=True)
    subprocess.run(base + ["up", "-d", "--no-deps", "--force-recreate", "driver"], cwd=compose_dir, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--compose-dir", type=Path, default=Path("deploy"))
    parser.add_argument("--source-path", type=Path, default=Path("loom_v2").resolve())
    parser.add_argument("--observer-url", default="http://localhost:18080")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--formal", action="store_true")
    args = parser.parse_args()
    images = 100 if args.formal else 3
    args.output_root.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    for topology, policy, repeat in experiment_order(args.repeats, args.seed):
        configure_stack(compose_dir=args.compose_dir, topology=topology, policy=policy, source_path=args.source_path)
        record = run_experiment(SimpleNamespace(archive=args.archive, manifest=args.manifest, images=images, observer_url=args.observer_url, timeout=args.timeout, poll_interval=0.5, policy=policy, topology=topology, target_slave=None))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = args.output_root / f"{topology}__{policy}__r{repeat}__{stamp}__{uuid4().hex[:8]}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        index.append({"path": str(path), "topology": topology, "policy_id": policy, "repeat": repeat, "run_id": record["metrics"]["run_id"], "state": record["metrics"]["state"]})
    index_path = args.output_root / f"index__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}__{uuid4().hex[:8]}.json"
    index_path.write_text(json.dumps({"seed": args.seed, "images": images, "runs": index}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
