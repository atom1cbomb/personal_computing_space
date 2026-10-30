# Using the local cache

Use Python 3.12+ with the matching standalone `loom-orchestration 0.10.x` installed.
The distributable ZIP includes its unchanged wheel in `assets/wheels/`; install
that wheel with pip. No Loom server source or host credentials are needed.
Run `python <plugin-root>/scripts/memory.py --help`, or install this independent
Python package to obtain the `loom-business-skills` command.

Connection flags precede the command:

```sh
python <plugin-root>/scripts/memory.py \
  --observer '<operator-supplied URL>' --token-file '<private token file>' \
  --workspace '<workspace ID>' --authority '<GRIP authority>' \
  prepare '<experience ID>'
```

Use `--allow-http` only for an operator-supplied HTTP Observer. For service data
bindings, use `--ca-file` and `--bindings-file` (a JSON array of operator-trusted
protocol/endpoint/audience objects). Never infer trusted endpoints from a cache.
These files are runtime inputs; they are not copied into experience records.
`--cache <directory>` selects a private cache independently of plugin installation.

## Select and prepare

`find '<goal>'` reads only summaries and defaults to three matches (`--limit` 1–5).
It is a routing hint, not proof that a capability is currently available. `prepare`
revalidates every binding and its dependencies, then returns one concise method.
`check` returns only the validation report. `history` is an explicit offline read
of one historical procedure. No command loads every experience into context.

`contract <id> <binding> arguments_schema|result_schema|descriptor|mcue` reads one
cached contract field. Always prepare first. Values over 8,000 characters require
`--output <new private file>`; read only the relevant portion for the task.

## Learn a business procedure

Discover exact named activations and action URIs through public GRIP. Read their
how-to-use and contracts. Then write a small proposal with these fields:

```json
{
  "id": "paper-venue-catalog",
  "title": "Inspect available paper venues",
  "summary": "Find the provider's current venue catalog before narrowing a literature query.",
  "keywords": ["papers", "venues", "论文", "会议"],
  "bindings": {
    "catalog": {
      "target": "<exact discovered activation URI>",
      "action": "<exact discovered catalog action URI>",
      "dependencies": []
    }
  },
  "steps": [{
    "id": "catalog",
    "binding": "catalog",
    "instruction": "Use the current catalog action with arguments validated against its schema. Extract venue identifiers and names for this request.",
    "acceptance": "Check the actual response envelope and that the returned venue entries have the declared identifiers. Do not equate supported venues with indexed papers.",
    "on_failure": "Report the current authentication, availability or contract blocker; do not reuse a historical list."
  }]
}
```

The placeholder URIs are illustrative, not executable values. `learn --draft
<file>` captures public contract references and schemas, initially documented.
It does not make the business call or persist the current query/results. Additional
dependencies must be exact, explicitly discovered resources; declared dependencies
are followed automatically. One binding can serve several steps; one experience
can combine several packages. Maximum eight steps and 5,000 total procedure
characters encourage reusable, focused methods rather than task transcripts.

## Execute and verify

For each step selected within the current user's authorized task:

```sh
python <plugin-root>/scripts/memory.py <connection flags> \
  run '<id>' '<step>' --arguments '<current arguments JSON file>' \
  --key '<stable operation key>' --result-file '<new private result file>'
```

Every run revalidates the whole experience. The helper supports JSON task and
request-reply actions, delegates to the unchanged SDK, and returns a short receipt.
The result file contains the new business result and any Run evidence. It must be
outside the cache and must not already exist. No arguments or raw results are
written into the skill or SQLite cache. Task retries retain the business key;
direct action retry safety comes from its contract, not the helper's key argument.
Do not blindly retry a failed/unknown direct call. A local polling timeout does not
cancel the server Run; follow the returned identity with the public SDK.

Inspect the file against the current goal and the recorded acceptance condition.
`accept <id> <step> --execution <returned execution ID>` records that agent-side
check. It only accepts a completed call from this cache with the same current
contract. It never invokes Driver resolve or changes a pending business decision.
Executing successfully alone is marked executed, not verified.

## Refresh, rebind, remove

When prepare reports stale, inspect the changed live contracts first. `refresh
<id> --draft <reviewed proposal>` refreshes contracts and optionally updates the
procedure. Changed contracts/steps lose prior verified status; unchanged steps
retain it. Omit `--draft` only after confirming the method still applies.

Another Observer/workspace or a replacement named instance requires `rebind
<id> --new-id <new ID> --map <JSON file>`. The mapping must supply every binding
using the same shape as `bindings` above. Discover those targets in the destination
environment; don't guess identity from names or rewrite URI prefixes. Rebinding
starts at documented and leaves the old experience intact.

`remove <id>` deletes one record and its generated view. Removing the whole cache
discards all experience without changing the plugin or Loom. Do not publish the
private cache: environment references can be sensitive even without credentials.
Review parameterized prose before sharing; the credential check only catches
common pasted secrets and the active SDK token.
