"""Publish reviewable service artifacts using Observer-returned references."""
from __future__ import annotations

import hashlib
import rfc8785
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from .client import LoomClient
from .grip import Names
from .naming import content_uri


class ServicePackage:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.manifest = json.loads((self.directory / "package.example.json").read_text())

    async def publish(
        self, client: LoomClient, *, image_ref: str,
        dependencies: dict[str, str] | None = None,
    ) -> str:
        """Return the package's immutable GRIP URI supplied by Observer.

        dependencies maps declared package coordinates to already-published GRIP
        URIs. No resource use or container activation happens during publication.
        """
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image_ref):
            raise ValueError("immutable_image_ref_required")
        refs: dict[str, dict[str, Any]] = {}
        for coordinate, target in (dependencies or {}).items():
            description = await client.describe(target)
            if description["resource"]["kind"] != "capability-package":
                raise ValueError("package_dependency_required")
            package = await client.invoke(target, client.names.action("read"), {})
            # Match the source template's coordinate; target is the published URI.
            if Names().name("packages", package["package_id"], package["package_version"]) != coordinate:
                raise ValueError("package_dependency_coordinate_mismatch")
            refs[coordinate] = {"resource_id": target, "version_or_digest": package["package_digest"]}

        prefix = self.manifest["package_id"]

        def wire(ref):
            return ref.model_dump(mode="json", include={"resource_id", "version_or_digest", "identity_criterion"}, exclude_none=True)

        async def upload(source, content, media_type, name):
            raw = rfc8785.dumps(source) if media_type.endswith("json") else source.encode("utf-8")
            original_uri = content_uri(hashlib.sha256(raw).hexdigest(), name)
            ref = await client.upload_content(content, media_type=media_type, name=name)
            refs[original_uri] = wire(ref)
            return ref

        for path in sorted((self.directory / "schemas").glob("*.json")):
            source = json.loads(path.read_text())
            await upload(source, source, "application/schema+json", f"{prefix}/schemas/{path.stem}")
        for path in sorted((self.directory / "validators").glob("*.py")):
            source = path.read_text()
            await upload(source, source, "text/x-python", f"{prefix}/validators/{path.stem}")

        sources: dict[str, dict] = {}
        for path in sorted((self.directory / "descriptors").glob("*.json")):
            descriptor = json.loads(path.read_text())
            identity = descriptor["resource_id"]
            if not identity.startswith(Names().root + "/"):
                raise ValueError("service_template_namespace_required")
            if identity in sources:
                raise ValueError("duplicate_descriptor_source")
            sources[identity] = descriptor

        class PendingDependency(Exception):
            pass

        def resolve(value):
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if not isinstance(value, dict):
                return value
            if "resource_id" in value:
                identity = value["resource_id"]
                if identity in refs:
                    return refs[identity]
                if identity in sources:
                    raise PendingDependency(identity)
                raise ValueError(f"service_artifact_dependency_missing:{identity}")
            return {key: resolve(item) for key, item in value.items()}

        for path in sorted((self.directory / "contracts").glob("*.io.json")):
            original = json.loads(path.read_text())
            # Content refs supplied by the generator must match uploaded bytes;
            # changing a schema requires regenerating its contracts first.
            contract = resolve(original)
            await upload(original, contract, "application/vnd.loom.io-contract+json", f"{prefix}/contracts/{path.name.removesuffix('.io.json')}")
        pending = dict(sources)
        while pending:
            progress = False
            for identity, source in list(pending.items()):
                try:
                    descriptor = {key: client.names.root + value.removeprefix(Names().root) if key == "resource_id" else resolve(value) for key, value in source.items()}
                except PendingDependency:
                    continue
                ref = await client.upload_content(descriptor, media_type="application/vnd.loom.action-descriptor+json", name=f"{prefix}/descriptors/{identity.rsplit('/', 1)[-1]}")
                refs[identity] = ref.model_dump(mode="json", include={"resource_id", "version_or_digest", "identity_criterion"}, exclude_none=True)
                del pending[identity]
                progress = True
            if not progress:
                raise ValueError("service_descriptor_dependency_cycle")
        manifest = deepcopy(self.manifest)
        manifest["body"]["image_ref"] = image_ref
        for export in manifest["action_exports"]:
            export["action_descriptor_ref"] = resolve(export["action_descriptor_ref"])
            export["io_contract_ref"] = resolve(export["io_contract_ref"])
        package = await client.register_package(manifest)
        return package["resource"]
