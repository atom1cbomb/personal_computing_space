"""Observe existing SSH activations and periodically execute a harmless probe.

No activation, authentication or policy changes are made. Durable JSON evidence
records readiness samples and actual completed business Runs. A running receipt
is not a passed validation. Use a duration spanning multiple configured refresh intervals.
"""
import argparse
import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loom_orchestration import CallLedger, LoomClient, execute_action


def now():
    return datetime.now(timezone.utc).isoformat()


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    run_key = uuid.uuid4().hex
    state = {"validation_id": run_key, "status": "awaiting_auth", "created_at": now(),
             "duration_seconds": args.duration, "activations": args.activation,
             "samples": 0, "probes": 0, "runs": []}

    def save(event=None):
        state["updated_at"] = now()
        tmp = args.output / "status.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(args.output / "status.json")
        if event is not None:
            with (args.output / "events.jsonl").open("a") as f:
                f.write(json.dumps({"at": now(), **event}, ensure_ascii=False) + "\n")

    ledger = CallLedger(args.output / "calls.sqlite",
                        max_calls=len(args.activation) * (math.ceil(args.duration / args.probe_interval) + 3))
    save()
    try:
        async with LoomClient(args.observer, token=args.token_file.read_text().strip(),
                              allow_insecure_grip=args.allow_http,
                              grip_authority=args.authority, timeout=60) as client:
            ready_at = None
            last_probe = float("-inf")
            while True:
                observations = []
                for target in args.activation:
                    m = (await client.describe(target, refresh=True))["resource"]["metadata"]
                    observations.append({"activation": target, "state": m["state"],
                                         "authentication": m.get("authentication", {}).get("status"),
                                         "revision": m["revision"]})
                ready = all(o["state"] == "ready" and o["authentication"] == "authenticated"
                            for o in observations)
                save({"event": "sample", "observations": observations})
                if ready_at is None:
                    if not ready:
                        if time.monotonic() - started > args.auth_timeout:
                            raise RuntimeError("authentication_wait_timeout")
                        await asyncio.sleep(args.interval)
                        continue
                    ready_at = time.monotonic()
                    state.update(status="running", started_at=now(),
                                 expected_finish_epoch=time.time() + args.duration,
                                 revisions={o["activation"]: o["revision"] for o in observations})
                if not ready:
                    raise RuntimeError("activation_lost_authenticated_readiness")
                if any(state["revisions"][o["activation"]] != o["revision"] for o in observations):
                    raise RuntimeError("activation_revision_changed")
                elapsed = time.monotonic() - ready_at
                state["samples"] += 1
                state["elapsed_seconds"] = round(elapsed, 1)
                if time.monotonic() - last_probe >= args.probe_interval or elapsed >= args.duration:
                    for index, target in enumerate(args.activation):
                        result = await execute_action(client, target, "exec",
                            {"command": "printf loom-ssh-soak-ok", "timeout_seconds": 30},
                            idempotency_key=f"{run_key}-{state['probes']}-{index}", ledger=ledger)
                        value = result.get("value") or {}
                        if (result.get("status") != "completed" or value.get("exit_code") != 0
                                or value.get("timed_out") is not False
                                or value.get("stdout") != "loom-ssh-soak-ok"):
                            raise RuntimeError("ssh_probe_failed")
                        receipt = result.get("receipt") or {}
                        state["runs"].append(receipt)
                        state["probes"] += 1
                        save({"event": "probe_completed", "activation": target, "receipt": receipt})
                    last_probe = time.monotonic()
                if elapsed >= args.duration:
                    state.update(status="passed", completed_at=now())
                    save({"event": "completed", "ledger": ledger.summary()})
                    return
                save()
                await asyncio.sleep(min(args.interval, max(1, args.duration - elapsed)))
    except Exception as exc:
        state.update(status="failed", error_type=type(exc).__name__)
        # Only our fixed codes are persisted; upstream errors may contain secrets.
        known_codes = {"authentication_wait_timeout", "activation_lost_authenticated_readiness",
                       "activation_revision_changed", "ssh_probe_failed"}
        if type(exc) is RuntimeError and str(exc) in known_codes:
            state["error_code"] = str(exc)
        save({"event": "failed"})
        raise SystemExit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--observer", required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--authority", default="loom.local")
    p.add_argument("--activation", action="append", required=True)
    p.add_argument("--duration", type=int, default=600)
    p.add_argument("--probe-interval", type=int, default=60)
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--auth-timeout", type=int, default=1800)
    p.add_argument("--allow-http", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if min(args.duration, args.interval, args.probe_interval, args.auth_timeout) <= 0:
        p.error("durations must be positive")
    asyncio.run(main(args))
