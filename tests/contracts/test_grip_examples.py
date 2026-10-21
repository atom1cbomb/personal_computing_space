import importlib.util
from pathlib import Path

import httpx

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts/accept-bandgap.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("accept_bandgap", SCRIPT_PATH)
SCRIPT_MODULE = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(SCRIPT_MODULE)


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
