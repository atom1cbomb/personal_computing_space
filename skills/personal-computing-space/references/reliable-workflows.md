# Public workflow helpers (SDK 0.9)

Install the wheel included in this skill. Load the operator's connection data
without printing credentials, encoded credentials, environment dumps or config
files. A test endpoint may be a local authentication proxy: use it as supplied.

## Discover and call

`await inventory(client)` gives compact `resources`, `activations` and `slaves`.
It omits protocol records, historical Runs and stopped instances, but retains
pending/degraded instances and all business categories. An activation can offer
useful actions without a separate named business resource. Match explicit
resource attributes and activation targets; a VM's address alone does not prove
it is a cloud-provider instance. Use `include_stopped=True` when investigating
history. DESCRIBE remains authoritative for each selected action.

When a task depends on ownership, provenance, or a relationship between
resources, enumerate the relevant MCUE resources and inspect their returned
system metadata (`package`, `managed_by`, `state`, and `activations`) together
with the public attributes and the selected capability package's descriptors.
Use relationship and identity fields only when the deployed MCUE or capability
package contract declares their meaning, then match those identities exactly
against the corresponding public action result. Keep distinct resource URIs
distinct; do not infer a relationship from an address, display name, package
name, or URI spelling. The project skill defines this discovery and matching
procedure; provider-specific fields and lifecycle semantics belong in the
published MCUE or capability package description.

```python
from loom_orchestration import inventory, action_contract, execute_action, CallLedger

catalog = await inventory(client)
# Choose an exact discovered activation URI and action name for the user's goal.
contract = await action_contract(client, activation_uri, action_name)
# Inspect contract["arguments_schema"] and contract["result_schema"].
ledger = CallLedger("evidence/calls.sqlite", max_calls=10,
                    max_text_characters=1500, max_text_length=600)
result = await execute_action(client, activation_uri, action_name, arguments,
                              idempotency_key=operation_key, ledger=ledger, timeout=120)
```

Set the ledger limits from the actual task; the numbers above are examples.
Use one ledger for the whole task, including exploration, fallbacks and retries.
Character limits count every string in the business `texts` input across calls.
The helper rejects schema or budget violations before dispatch. It is a client
guard, not an authorization boundary against code bypassing the helper.

`execute_action` selects the exact current action, validates business arguments,
handles the task/direct wrapper, and polls task Runs until the server reports a
terminal state. `status=completed` is only execution success: check the user's
acceptance conditions too. `value` is the unmodified business response,
including any provider `result`/`data` envelopes; inspect its schema and keys
rather than assuming a uniform result path. Task results also retain `receipt`,
full `run`, `resource_ref` and `digest_verified`. Direct results have a receipt
but no invented Run. The optional helper `timeout` is only a local polling
watchdog and does not cancel or replace the Run's server-owned execution or
cleanup budget; the default follows the Run until it reaches a terminal state.
Keep its identity and report its actual state.

Use `ledger.summary()` to account for all dispatched calls and text characters.
Prior overspending or synthetic results are not erased by a later successful
retry. Do not substitute generated text for requested real documents. Derive
the final report and chat numbers from the same verified values. Record every
published package, content reference, Run and retained activation, even when no
mutable business object was created.

## Distributed map and reduce

For a JSON-stdio computation, `publish_function(...)` implements the public
publication contract in [function-packages.md](function-packages.md).
Publish a worker and a separate reducer with their actual schemas. The reducer
accepts `{"inputs": [worker_result, ...]}` and returns the final result schema.

```python
from loom_orchestration import publish_function, submit_map_reduce, wait_run

# worker and reducer are the dictionaries returned by publish_function.
submitted = await submit_map_reduce(
    client, worker_package=worker["resource"], worker_action=worker_action_name,
    reducer_package=reducer["resource"], reducer_action=reducer_action_name,
    jobs=[{"target": first_slave_uri, "arguments": first_partition},
          {"target": second_slave_uri, "arguments": second_partition}],
    reducer_target=first_slave_uri, package_id=unique_orchestrator_id,
    request_id=stable_request_id, goal=user_goal,
    max_live_nodes=2, execution_timeout_seconds=600,
)
# The receipt is the normal submission response. Preserve its run_id.
result = await wait_run(client, submitted["receipt"]["run_id"])
```

The helper emits all map nodes, passes their completed result references to a
real remote reduction node, and returns that node's result. It also assigns the
reducer's output schema to the orchestrator. It checks and activates these
user-owned function packages on the chosen Slaves and returns those activation
receipts; retain them in the created-object ledger. `target_resource_ref` is for
a managed business resource, not a Slave placement hint. It does not compute the answer in
the client or return a locally materialized final answer. Inspect remote node
targets/results and independently verify the user's expected value. For other
DAGs, use the lower-level documented orchestration protocol.
