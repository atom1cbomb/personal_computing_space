# Reuse an existing named activation

Authentication belongs to a particular package/Slave/instance/revision, not to
the external client's machine. A user's local `loom-auth` can authenticate an
instance used by a remote agent. Do not transfer provider cookies to that agent.
Instances with different names or Slaves do not share authentication implicitly.

For a ready instance, invoke its exact offered action on its activation URI using
the declared interaction. `Slave.run-package` has no instance-name argument and
uses `default`; it does not mean “run on whichever named instance I discovered”.
Missing business offers are readiness/authentication evidence, not a reason to
switch to the Slave route. A failed default-instance Run does not prove that the
named instance's authentication recovery failed.

## A persistent task that waits for existing-instance login

Use an externally authored Python orchestrator when the user needs one Run to
wait for authentication and then continue. SDK 0.8.1 has
`await client.activation_ref(discovered_activation_uri)`: a read-only resolver
that copies the package digest, Slave, instance name, revision and optional target
from public descriptions into a runtime reference. It neither creates nor changes
the instance. Preserve its returned fields; a bare URI is insufficient for the
orchestration runtime. Do not call `ensure_activation` just to obtain this reference,
because that primitive can change an existing activation's desired configuration.

Prepare input containing the resolved activation reference, published package
reference, selected action descriptor reference, and uploaded business arguments
reference. These values must come from public descriptions/publication responses.
Use `ResourceRef.model_dump(mode="json")` when uploading references as JSON.
Publish an `OrchestratorPackage` that allows that exact node package and submit
with the selected export's exact permission strings and a login-sized execution
deadline, using [observer-workflow.md](observer-workflow.md).

The generic program shape is:

```python
async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    data = await ctx.read_json(input_ref)
    await ctx.wait_activation(step_id="wait-login", activation_ref=data["activation"],
                              ready_state="authenticated")
    node = ctx.emit_node("query", data["package"], data["descriptor"], [data["arguments"]],
                         activation_ref=data["activation"])
    return await ctx.result(node)
```

`pending_auth` becomes `awaiting_auth` on that Run. Record its waiting instance
and revision, have the user complete login via the unified portal, and poll the
same Run. Reauthentication does not require a new business Run or activation.
`degraded/auth_failed` is a failure state and is not automatically an auth wait;
start reauthentication through the portal and confirm `pending_auth` before
testing this transition. Do not revoke a working login to manufacture a test.
Provider failure, expired challenges and a changed activation revision must be
reported separately. Once a Run is terminal, do not claim that it resumed.
