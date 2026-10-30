---
name: loom-business-memory
description: Learn and reuse business workflows for Loom tasks through an agent-local experience cache, with fresh public-contract checks before use. Use for repeated Loom business goals or explicitly requested experience capture.
---

# Business workflow memory

This plugin supplements the existing public Loom workflow. It does not replace
current user authorization, contracts, authentication, budgets, or cleanup rules.
Use the unchanged `personal-computing-space` skill for cold discovery when available;
otherwise discover through the installed public SDK and deployed how-to-use offers.

Only this entry skill is registered. Never register, glob-read, or inject all cached
experiences. Run `python <plugin-root>/scripts/memory.py find '<business goal>'`:
it returns at most three short candidates by default. Load one selected experience
at a time. Read [the command reference](../../references/workflow.md) only when
learning, rebinding, calling a step, or configuring the connection is needed.

For a candidate, use `prepare <id>` with the current operator-supplied connection.
Only `ready` returns its concise procedure. Read at most the selected binding's
needed contract field; large schemas go to a file. Fresh dynamic state and exact
named instances matter even when contract references are unchanged. A ready check
does not grant permissions or replace the server's checks at invocation time.

For `stale`, inspect the changed contracts before `refresh`; adapt the procedure if
semantics changed. For another environment, discover targets there and supply a
complete explicit `rebind` map. Never rewrite historical URIs or choose the first
of several instances. `blocked` and `offline` are current observations, not permanent
capability absence. `history` is explicitly historical and never execution authority.

During an authorized task, execute one selected step with `run`, current arguments,
a stable operation key, and a private result file outside the cache. Inspect the new
result against the stored acceptance condition and the user's goal; then `accept`
that exact execution if it passes. Handle awaiting_auth/awaiting_decision through
the normal public workflow. Never auto-accept a pending Driver decision.

After a useful task, learn a compact parameterized procedure or refresh an existing
one. Preserve documented versus executed versus verified evidence. Cache methods,
contracts, and evidence references, not credentials, raw business data, or historical
answers. Don't make one experience per invocation: update a reusable business goal.
Delete obsolete experiences with `remove`; normal discovery remains available.
