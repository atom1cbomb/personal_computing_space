# Outline HTTP capability package

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

This gateway provides a bounded Outline API surface for the Loom
`service + container:http/1` runtime. It supports collection discovery,
document listing/retrieval/search, and ordinary document create/update/archive/
restore actions. It does not expose arbitrary URLs or Outline methods,
administration, API-key management, attachments, or permanent deletion.

Outline's API is JSON RPC over `POST /api/<method>` and authenticates with
`Authorization: Bearer <API key>`. The default endpoint is the configured
instance at `https://docs.cs.ac.cn/api`; set `OUTLINE_ENDPOINT` for another
HTTPS Outline instance.

The gateway reads the key from `/run/loom/credentials/outline_key`. The file is
operator-managed and is never copied into the image, package manifest,
Observer content, or responses. For local testing with the repository's
untracked secret file, run the process with
`OUTLINE_CREDENTIAL_ROOT=production/secrets`; do not commit or upload that
file.

For a Slave deployment, create a credential profile directory outside Git and
put the key in a file named `outline_key`, for example:

```text
/var/lib/loom/runtime-credentials/outline-prod/outline_key
```

The package manifest refers only to the non-sensitive profile name
`outline-prod`. The runtime mounts that profile read-only. Build the image and
replace the example image digest before registration; this repository does not
push the image or register the package automatically.

Each exported action has a small input/output schema in `schemas/` and an
IoContract in `contracts/`. The package example is updated from those files by
`python scripts/generate_service_contracts.py`; use its `--check` mode in CI.

The IoContracts keep `success_semantics` null because the gateway treats a
successful Outline JSON response matching the output schema as sufficient.
Stronger document-level criteria require a deterministic validator rather than
an unexecuted descriptive field.
