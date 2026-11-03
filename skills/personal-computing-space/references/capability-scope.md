# Run capability scope and workspace sharing

The workspace directory lists workspace packages and activations. A package
created inside one Run is private to that Run, even when its action descriptor
or package digest matches a shared package. Other Runs cannot discover or bind
its activation. Slave registration does not publish private actions.

Use `client.packages(run_id=owner_run_id)` to inspect packages available to a
Run. `GET /api/v1/runs/{run_id}/capability-packages` includes private package and
activation records. A specific private package is read with
`GET /api/v1/capability-packages/{package_uri}?run_id={owner_run_id}`; omitting
the Run context does not search other Runs. The same scope rule applies to
promotion and abandonment. Encode URI path parameters and query values using
the HTTP client's URL handling.

For cross-Run sharing, the external agent performs these steps:

1. After the source Run is terminal, POST
   `/api/v1/capability-packages/{package_uri}/promote?run_id={owner_run_id}`
   with the returned `package_digest` as `approved_digest` and a stable
   `idempotency_key`. Save the returned workspace package `resource`. The
   derived version has the `-reusable` suffix and retains provenance. An
   independently authored manifest can instead be uploaded using
   `client.register_package()`. Publication creates no activation.
2. Discover the destination Slave's `activate-package` action. Activate the
   workspace package with a **new `instance_name`**, copying the intended
   target resource, instance configuration, credential profile and dependency
   bindings explicitly. Reusing the private instance name with the same digest
   is rejected while that instance is running, because the container provider
   otherwise aliases the two activations. Run-private package coordinates must
   also be distinct when different Runs activate them on the same Slave.
3. Check the new activation's readiness and run a suitable business check.
   Save its resource URI and revision. Point subsequent Runs at the workspace
   package and activation. If activation or validation fails, keep the private
   instance available while investigating.
4. Stop the old instance through
   `POST /api/v1/runs/{owner_run_id}/capability-packages/{private_package_uri}/stop`
   with `target_slave`, `instance_name`, its current `expected_revision`, and a
   stable `idempotency_key`. Verify that `health_reports` confirms `stopped`;
   HTTP success alone does not prove the Slave accepted the stop. A stale
   revision requires reading the current private activation before deciding
   whether to repeat the request.

This migration creates a new runtime instance. Copying the package does not
transfer live connections, in-memory state, or service data. Follow the
capability package's own instructions when data or authentication state also
needs to move. Stopping the old activation does not stop the new one.
