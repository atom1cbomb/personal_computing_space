from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import ssl
import sys

from pydantic import ValidationError
from loom_orchestration import LoomClient

from .memory import Memory
from .model import Problem, brief
from .store import Store


def read_json(path):
    return json.loads(Path(path).read_text())


def summary(record):
    return {"id": record["draft"]["id"], "steps": len(record["draft"]["steps"]),
            "verification": {k: v["level"] for k, v in record["evidence"].items()},
            "status": record["last_check"]["status"]}


async def dispatch(args):
    store = Store(args.cache)
    if args.command == "find":
        return {"candidates": store.find(args.query, args.limit), "requires_live_check": True}
    if args.command == "history":
        return brief(store.get(args.id), historical=True)
    if args.command == "remove":
        store.remove(args.id)
        return {"removed": args.id}
    if args.command == "contract":
        record = store.get(args.id)
        value = record["contracts"][args.binding]["bodies"][args.field]
        # Contract bodies can be large: opt in to a single field, or use a file.
        output = json.dumps({"historical_only": True, "field": args.field, "value": value}, ensure_ascii=False)
        if args.output:
            write_result(args.output, output, store)
            return {"written": str(args.output), "historical_only": True}
        if len(output) > 8000:
            raise Problem("contract_requires_output_file", characters=len(output))
        return json.loads(output)
    if not args.observer or not args.token_file:
        raise Problem("connection_required")
    context = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else None
    bindings = read_json(args.bindings_file) if args.bindings_file else None
    token = args.token_file.read_text().strip()
    async with LoomClient(args.observer, token=token, workspace_id=args.workspace, grip_authority=args.authority,
                          allow_insecure_grip=args.allow_http, ssl_context=context, trusted_bindings=bindings,
                          timeout=60) as client:
        memory = Memory(store, client)
        if args.command == "learn":
            record = await memory.learn(read_json(args.draft))
            return {**summary(record), "metrics": record["learning_metrics"]}
        if args.command == "prepare":
            return await memory.prepare(args.id)
        if args.command == "check":
            return await memory.check(args.id)
        if args.command == "refresh":
            return summary(await memory.refresh(args.id, read_json(args.draft) if args.draft else None))
        if args.command == "rebind":
            return summary(await memory.rebind(args.id, args.new_id, read_json(args.map)))
        if args.command == "run":
            # Refuse an unsafe destination before dispatching a business action.
            import os
            destination = check_destination(args.result_file, store)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as output:
                execution, result = await memory.run(args.id, args.step, read_json(args.arguments),
                                                     idempotency_key=args.key, timeout=args.timeout)
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
            return {"execution": execution, "result_file": str(args.result_file), "acceptance_pending": execution["status"] == "completed"}
        if args.command == "accept":
            return summary(await memory.accept(args.id, args.step, args.execution))
    raise Problem("unknown_command")


def check_destination(path, store):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(store.root) or path.exists() or not path.parent.is_dir():
        raise Problem("result_destination_invalid")
    return path


def write_result(path, text, store):
    import os
    path = check_destination(path, store)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text + "\n")


def parser():
    p = argparse.ArgumentParser(description="Bounded, agent-local workflow memory. Public Loom API only.")
    p.add_argument("--cache", type=Path)
    p.add_argument("--observer")
    p.add_argument("--token-file", type=Path)
    p.add_argument("--workspace", default="workspace-default")
    p.add_argument("--authority", default="loom.local")
    p.add_argument("--allow-http", action="store_true")
    p.add_argument("--ca-file", type=Path)
    p.add_argument("--bindings-file", type=Path, help="operator-supplied trusted service bindings JSON; never cached")
    sub = p.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=3)
    learn = sub.add_parser("learn")
    learn.add_argument("--draft", type=Path, required=True)
    for name in ("history", "prepare", "check", "refresh", "remove", "contract", "rebind", "run", "accept"):
        cmd = sub.add_parser(name)
        cmd.add_argument("id")
        if name == "contract":
            cmd.add_argument("binding")
            cmd.add_argument("field", choices=["arguments_schema", "result_schema", "descriptor", "mcue"])
            cmd.add_argument("--output", type=Path)
        elif name == "refresh":
            cmd.add_argument("--draft", type=Path, help="reviewed replacement procedure; target bindings stay explicit")
        elif name == "rebind":
            cmd.add_argument("--new-id", required=True)
            cmd.add_argument("--map", type=Path, required=True)
        elif name == "run":
            cmd.add_argument("step")
            cmd.add_argument("--arguments", type=Path, required=True)
            cmd.add_argument("--key", required=True)
            cmd.add_argument("--result-file", type=Path, required=True)
            cmd.add_argument("--timeout", type=float, default=120)
        elif name == "accept":
            cmd.add_argument("step")
            cmd.add_argument("--execution", required=True)
    return p


def main():
    args = parser().parse_args()
    try:
        result = asyncio.run(dispatch(args))
    except Problem as exc:
        result = {"error": exc.code, **exc.details}
    except (ValidationError, ValueError, KeyError, OSError):
        result = {"error": "invalid_input_or_local_file"}
    except Exception:
        result = {"error": "operation_failed"}
    print(json.dumps(result, ensure_ascii=False))
    if "error" in result or result.get("status") in {"blocked", "stale", "offline", "needs_rebind"}:
        sys.exit(2)


if __name__ == "__main__":
    main()
