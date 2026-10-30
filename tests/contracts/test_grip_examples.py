import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/accept-bandgap.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("accept_bandgap", SCRIPT_PATH)
SCRIPT_MODULE = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(SCRIPT_MODULE)

NATIVE_SPEC = importlib.util.spec_from_file_location(
    "native_task", Path(__file__).resolve().parents[2] / "examples/grip/native-task.py",
)
NATIVE_MODULE = importlib.util.module_from_spec(NATIVE_SPEC)
NATIVE_SPEC.loader.exec_module(NATIVE_MODULE)


async def test_bandgap_harness_prepares_and_submits_current_public_contract():
    from loom_orchestration import LoomClient
    from loom_v2.observer.app import create_app
    from loom_v2.observer.repository import ObserverRepository
    from loom_v2.settings import Settings
    from loom_v2.testing.observer import seed_embedded_slaves
    settings = Settings(grip_token="bandgap-test", public_base_url="http://observer", coding_agent_backend="none")
    repo = ObserverRepository(settings=settings)
    seed_embedded_slaves(repo)
    repo.orchestrator_runtime_available = True
    async with LoomClient("http://observer", token=settings.grip_token, transport=httpx.ASGITransport(app=create_app(repo, settings=settings)), allow_insecure_grip=True) as client:
        plan = await SCRIPT_MODULE.prepare_plan(client, SCRIPT_MODULE.make_corpus(7, 12), 3, "bandgap-test")
        queued = await client.submit(request_id="bandgap-test", plan=plan)
        state = await client.invoke(client.names.name("runs", queued["run_id"]), client.names.action("read"), {})
        assert queued["execution_request_state"] == "queued"
        assert state["state"] == "committed"
        assert len(await client.resources(mcue="capability/package")) == 3


async def test_native_task_example_publishes_and_queues_through_discovered_driver(monkeypatch):
    from loom_orchestration import LoomClient
    from loom_v2.observer.app import create_app
    from loom_v2.observer.repository import ObserverRepository
    from loom_v2.settings import Settings
    from loom_v2.testing.observer import seed_embedded_slaves

    settings = Settings(grip_token="native-example-test", public_base_url="http://observer")
    repo = ObserverRepository(settings=settings)
    seed_embedded_slaves(repo)
    app = create_app(repo, settings=settings)
    monkeypatch.setenv("LOOM_GRIP_TOKEN", settings.grip_token)
    monkeypatch.setattr(NATIVE_MODULE, "LoomClient", lambda *args, **kwargs: LoomClient(
        *args, **kwargs, transport=httpx.ASGITransport(app=app),
    ))
    args = SimpleNamespace(observer="http://observer", authority="loom.local", workspace="workspace-default",
                           allow_http=True, request_id="native-example-1", value=21, wait=0)
    first = await NATIVE_MODULE.main(args)
    again = await NATIVE_MODULE.main(args)
    assert first["driver_available"] is False
    assert first["submission"]["execution_request_state"] == "queued"
    assert first["submission"]["run"] == again["submission"]["run"]
    assert first["state"]["plan"] == first["submission"]["plan"]
    assert first["frozen_plan"]["plan"] == first["submission"]["closure_contract"]
    assert first["frozen_plan"]["plan"]["body"]["program_systems"]["execution"] == {
        "kind": "container:python_orchestrator", "version": "1",
    }
    assert len(repo.execution_requests) == 1
