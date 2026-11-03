import asyncio
import json
from pathlib import Path

import pytest

from loom_v2.slave.executor import ProcessJSONStdioV1Adapter


@pytest.mark.parametrize("cancel", [True, False])
async def test_process_timeout_and_cancellation_reap_the_process_group(tmp_path, cancel):
    ready = tmp_path / "processes.json"
    source = f'''
import os, sys, subprocess, json, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(ready)!r}).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(60)
'''
    adapter = ProcessJSONStdioV1Adapter(timeout_seconds=10 if cancel else .5)
    invocation = asyncio.create_task(adapter.invoke({}, program=source.encode()))
    async with asyncio.timeout(3):
        while not ready.exists():
            await asyncio.sleep(.01)
    parent, child = json.loads(ready.read_text())
    if cancel:
        invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
    else:
        with pytest.raises(RuntimeError, match="capability_timeout"):
            await invocation
    assert not Path(f"/proc/{parent}").exists(), "parent must be reaped"
    # An orphan may briefly be a zombie until PID 1 reaps it, but cannot run.
    child_stat = Path(f"/proc/{child}/stat")
    assert not child_stat.exists() or child_stat.read_text().split()[2] == "Z"
