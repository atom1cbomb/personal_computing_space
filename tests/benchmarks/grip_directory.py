"""Local SQLite/ASGI comparison; requires test dependencies, no live services.

PYTHONPATH=. .venv/bin/python tests/benchmarks/grip_directory.py --baseline-ref 10078fd
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import httpx
from moto import mock_aws
from sqlalchemy.ext.asyncio import create_async_engine

# The application module also constructs its default app at import time.
os.environ.update(LOOM_S3_ENDPOINT_URL="https://s3.amazonaws.com", LOOM_S3_BUCKET="loom-benchmark",
                  LOOM_S3_ACCESS_KEY="test", LOOM_S3_SECRET_KEY="test")

from loom_orchestration import LoomClient
from loom_v2.digest import digest_json
from loom_v2.observer.app import create_app
from loom_v2.observer.grip import GripService
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from tests.api.test_grip_directory import queries, seed_packages


class CountingClient(LoomClient):
    request_count = 0

    async def _grip_exchange(self, *args, **kwargs):
        self.request_count += 1
        return await super()._grip_exchange(*args, **kwargs)


async def benchmark(baseline_ref):
    code = subprocess.check_output(["git", "show", f"{baseline_ref}:loom_v2/observer/grip.py"], text=True)
    baseline = types.ModuleType("grip_directory_baseline")
    sys.modules[baseline.__name__] = baseline
    exec(compile(code, "grip_directory_baseline.py", "exec"), baseline.__dict__)
    with tempfile.TemporaryDirectory() as directory:
        engine = create_async_engine(f"sqlite+aiosqlite:///{directory}/directory.db")
        settings = Settings(grip_token="benchmark", public_base_url="http://observer",
                            s3_endpoint_url="https://s3.amazonaws.com", s3_bucket="loom-benchmark",
                            s3_access_key="test", s3_secret_key="test")
        repo = ObserverRepository(engine, settings=settings)
        await repo.init_db()
        await repo.ensure_mcue_definitions(settings.workspace_id)
        packages = await seed_packages(repo, 23, exports=6)
        for package in packages[:15]:
            activation, = await repo.list_package_activations(package)
            await repo._persist_workspace_package(package, activations=[activation, activation.model_copy(update={"instance_name": "instance-1"})])
        for i in range(85):
            await repo.open_run(f"history-{i}", f"history-{i}", "directory benchmark")
        for i in range(8):
            await repo.register_resource(settings.workspace_id, {
                "resource_ref": {"resource_id": repo.names.name("resources", "vm", f"vm-{i}")},
                "mcue_ref": repo.names.mcue("computing/vm"), "attributes": {"display_name": f"VM {i}"},
            })
        app = create_app(repo, settings=settings)
        output = {"baseline_ref": baseline_ref, "environment": "local SQLite + ASGI + mocked S3",
                  "packages": 23, "activations": 38, "runs": 85, "exports_per_package": 6, "measurements": {}}
        descriptions = {}
        try:
            for name, service in (("baseline", baseline.GripService), ("optimized", GripService)):
                app.state.grip = service(app, settings)
                async with CountingClient("http://observer", token=settings.grip_token, allow_insecure_grip=True,
                                          transport=httpx.ASGITransport(app=app)) as client:
                    operations = {
                        "resources": lambda: client.resources(),
                        "schema_read": lambda: client.invoke(client.names.schema("list-resources-input"), client.names.action("read"), {}),
                        "activation_resources": lambda: client.resources(mcue="capability/activation"),
                    }
                    output["measurements"][name] = {}
                    for operation, call in operations.items():
                        samples = []
                        for _ in range(4):
                            client.request_count = 0
                            start = time.perf_counter()
                            with queries(engine) as sql:
                                result = await call()
                            samples.append({"seconds": round(time.perf_counter() - start, 6),
                                            "sql_queries": len(sql), "grip_requests": client.request_count})
                        output["measurements"][name][operation] = {
                            "first_seconds": samples[0]["seconds"],
                            "warm_median_seconds": statistics.median(s["seconds"] for s in samples[1:]),
                            "samples": samples,
                        }
                        if operation == "resources":
                            output["resource_count"] = len(result)
                    catalog = await app.state.grip.catalog()
                    descriptions[name] = {
                        uri: entry.description if "revision" in entry.description else {
                            **entry.description, "revision": digest_json(entry.description, domain="loom/grip-description/v1"),
                        } for uri, entry in catalog.items()
                    }
            assert descriptions["baseline"] == descriptions["optimized"], "directory descriptions changed"
            output["all_descriptions_and_revisions_equal"] = True
            return output
        finally:
            await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with mock_aws():
        result = asyncio.run(benchmark(args.baseline_ref))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
