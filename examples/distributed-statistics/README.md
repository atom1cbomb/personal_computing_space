# distributed-statistics

A multi-node, map-reduce style distributed application executed by the Loom v2
runtime. It splits an input dataset into partitions, fans the partitions out to
compute nodes running on **both** WorkspaceReplica Slaves (`slave-a` and
`slave-b`), and reduces the per-partition summaries into one aggregate result.

## Node roles

| Node | Role in this app | Hosted by |
| --- | --- | --- |
| `grip://loom.local/workspaces/workspace-default/actions/orchestrate` | Parent node: reads the input, emits one summarize child per partition, awaits them, then emits a single merge child | Driver (container `container:python_orchestrator`) |
| `summarize` | Map node: computes `count/sum/min/max/sumsq` for one partition | `slave-a` / `slave-b` (`process:json_stdio`) |
| `merge-summaries` | Reduce node: combines all summaries into `mean/stddev` | `slave-a` / `slave-b` (`process:json_stdio`) |

The Observer is the state authority, the Driver owns the orchestration, and
each Slave keeps its own PostgreSQL ledger. Content (schemas, programs, input,
results) lives only in MinIO/S3 and is referenced by immutable
`grip://loom.local/workspaces/workspace-default/content/<readable-purpose>/sha256/<digest>` references.

## Layout

- `io/*.schema.json` — JSON Schema 2020-12 subset documents for every node input/output.
- `programs/summarize.py` — map program (reads JSON from stdin, prints JSON).
- `programs/merge.py` — reduce program.
- `programs/orchestrate.py.tpl` — orchestration program template; the E2E harness
  substitutes the materialized package `ResourceRef`s before upload.
- `data/partitions/p1.json`…`p4.json` — 4 content-addressed partitions; the E2E harness
  uploads each, then binds a parent input `{"partitions": [ref…]}`. Expected aggregate:
  `count=12`, `sum=57`, `min=0`, `max=10`, `mean=4.75`, `stddev≈3.139`.
- `closure.json` — the high-level `ClosureContract` used to open the Run.

## Execution flow (end-to-end)

1. Discover available Slaves through `client.resources(mcue="computing/slave")`; upload schemas and IoContracts with `client.upload_content`.
2. Register `summarize` and `merge-summaries` workspace capability packages
   (`process:json_stdio`).
3. Register the `orchestrate` capability package
   (`container:python_orchestrator`, `allowed_node_package_refs=[summarize, merge]`,
   `max_nodes=6`, `max_live_nodes=2`).
4. Submit one complete `ClosureContract` through `client.submit` and poll its queued execution. Full plans use the authoring/submission interface; individual resource actions use [GRIP](../grip/README.md).
5. The Driver runs the orchestration program in Docker; it emits summarize nodes
   round-robin across `slave-a` and `slave-b` (2 live nodes max), then a merge
   node over the collected summaries.
6. Observer persists the terminal result as a content reference.

Run the live end-to-end test with:

```bash
.venv/bin/pytest -q tests/e2e/test_live_distributed_statistics.py -s
```

Requires the Compose stack to be up and the Observer reachable at
`http://localhost:18080` (see `scripts/test-e2e.sh`).

The band-gap acceptance harness uses the same public workflow:

```sh
python scripts/accept-bandgap.py --observer "$LOOM_OBSERVER_URL"
```

It discovers Slaves through GRIP, uploads immutable artifacts with the SDK, submits
a complete plan, reads the Run through GRIP and checks statistics and provenance.
Set the operator-provided GRIP token/authority/workspace first; local HTTP needs
`--allow-http`. `--inject-failure` explicitly opts into operator Docker stop/start
testing. The removed server-agent/MCP refinement modes are no longer supported.

All resource references use GRIP, including program/schema content. Generate the orchestration from `orchestrate.py.tpl` with references returned by the current deployment; generated programs with historical package digests are not checked in.
