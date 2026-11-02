"""Retries must not execute a Slave side effect twice, including after restart."""
import asyncio

import pytest
from sqlalchemy import select

from loom_v2.db.models import SlaveAttemptRow
from loom_v2.db.session import make_engine
from loom_v2.slave.service import SlaveService
from tests.support.run_code_fixture import make_run_code_fixture, provision_run_code_fixture


@pytest.mark.parametrize("interrupted", [False, True])
async def test_attempt_ledger_survives_restart(tmp_path, interrupted):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/slave.db")
    service = SlaveService("slave-a", engine=engine)
    await service.init_db()
    marker = tmp_path / "effects"
    program = (f"from pathlib import Path\np=Path({str(marker)!r})\np.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
               "import time,json\ntime.sleep(0.05)\nprint(json.dumps({'ok': True}))\n").encode()
    fixture = await make_run_code_fixture(service, action="ledger", program=program)
    await provision_run_code_fixture(service, fixture)
    kwargs = {"closure": fixture.closure(), "binding": fixture.binding}
    if interrupted:
        # A previous process started the side effect but never stored its outcome.
        async with service.sessions() as session, session.begin():
            session.add(SlaveAttemptRow(attempt_id="same", slave_id="slave-a", workspace_id=service.workspace_id,
                                       action=fixture.action, payload={}, state="running", result=None))
    else:
        results = await asyncio.gather(*(service.run("same", fixture.action, {}, **kwargs) for _ in range(2)))
        assert results[0].value == results[1].value == {"ok": True}
        assert marker.read_text() == "x"
    restarted = SlaveService("slave-a", engine=engine, content_store=service.content_store)
    await restarted.init_db()
    await provision_run_code_fixture(restarted, fixture)
    try:
        if interrupted:
            with pytest.raises(RuntimeError, match="attempt_outcome_unknown"):
                await restarted.run("same", fixture.action, {}, **kwargs)
            assert not marker.exists()
        else:
            assert (await restarted.run("same", fixture.action, {}, **kwargs)).value == {"ok": True}
            assert marker.read_text() == "x"
            async with restarted.sessions() as session:
                rows = (await session.scalars(select(SlaveAttemptRow))).all()
                assert len(rows) == 1 and rows[0].state == "completed"
    finally:
        await restarted.close()
        await service.close()
        await engine.dispose()
