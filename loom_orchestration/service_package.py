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
        source_files: list[str] | None = None,
    ) -> str:
        """Return the package's immutable GRIP URI supplied by Observer.

        dependencies maps declared package coordinates to already-published GRIP
        URIs. No resource use or container activation happens during publication.
        """
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image_ref):
            raise ValueError("immutable_image_ref_required")
        source_ref = None
        if source_files is not None:
            # Explicit opt-in: never upload a directory recursively, credentials,
            # build caches, or the operator's surrounding checkout implicitly.
            contents = {}
            root = self.directory.resolve()
            for name in source_files:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("source_path_invalid")
                file = self.directory / path
                if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root):
                    raise ValueError("source_path_invalid")
                contents[path.as_posix()] = file.read_text(encoding="utf-8")
            if not contents or sum(len(v.encode()) for v in contents.values()) > 2 * 1024 * 1024:
                raise ValueError("source_bundle_size_invalid")
            source_ref = await client.upload_content({
                "format": "loom.source-bundle.v1", "image_ref": image_ref, "files": contents,
            }, media_type="application/json", name=f"{self.manifest['package_id']}/diagnostic-source")
        refs: dict[str, dict[str, Any]] = {}
        for coordinate, target in (dependencies or {}).items():
            description = await client.describe(target)
            if description["resource"]["mcue"] != "capability/package":
                raise ValueError("package_dependency_required")
            package = await client.invoke(target, client.names.action("read"), {})
            # Match the source template's coordinate; target is the published URI.
            if Names().name("packages", package["package_id"], package["package_version"]) != coordinate:
                raise ValueError("package_dependency_coordinate_mismatch")
            refs[coordinate] = {"resource_id": target, "version_or_digest": package["package_digest"]}

        prefix = self.manifest["package_id"]
        definitions = {
            Names().root + item["uri"].removeprefix(client.names.root): item["uri"]
            for item in await client.resources(mcue="grip/mcue-type")
        }

        def wire(ref):
            return ref.model_dump(mode="json", include={"resource_id", "version_or_digest", "identity_criterion"}, exclude_none=True)

        async def upload(source, content, media_type, name):
            raw = rfc8785.dumps(source) if media_type.endswith("json") else source.encode("utf-8")
            original_uri = content_uri(hashlib.sha256(raw).hexdigest(), name)
            ref = await client.upload_content(content, media_type=media_type, name=name)
            refs[original_uri] = wire(ref)
            return ref

        sources: dict[str, dict] = {}
        for path in sorted((self.directory / "descriptors").glob("*.json")):
            descriptor = json.loads(path.read_text())
            identity = descriptor["resource_id"]
            if not identity.startswith(Names().root + "/"):
                raise ValueError("service_template_namespace_required")
            if identity in sources:
                raise ValueError("duplicate_descriptor_source")
            sources[identity] = descriptor

        config_schemas = {descriptor["instance_config_schema_ref"]["resource_id"]
                          for descriptor in sources.values() if descriptor.get("instance_config_schema_ref")}
        for path in sorted((self.directory / "schemas").glob("*.json")):
            source = json.loads(path.read_text())
            name = f"{prefix}/schemas/{path.stem}"
            original_uri = content_uri(hashlib.sha256(rfc8785.dumps(source)).hexdigest(), name)
            media_type = ("application/vnd.loom.instance-config-schema+json" if original_uri in config_schemas
                          else "application/schema+json")
            await upload(source, source, media_type, name)
        for path in sorted((self.directory / "validators").glob("*.py")):
            source = path.read_text()
            await upload(source, source, "text/x-python", f"{prefix}/validators/{path.stem}")
        usage_path = self.directory / "HOW_TO_USE.md"
        usage_ref = None
        if usage_path.is_file():
            usage = usage_path.read_text(encoding="utf-8")
            usage_ref = await upload(usage, usage, "text/markdown", f"{prefix}/how-to-use")

        class PendingDependency(Exception):
            pass

        def resolve(value):
            if isinstance(value, str) and value.startswith(Names().root + "/mcue-types/"):
                if value not in definitions:
                    raise ValueError(f"service_mcue_definition_missing:{value}")
                return definitions[value]
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
                if source_ref is not None:
                    descriptor["diagnostics"] = {"source_bundle_ref": wire(source_ref), "image_ref": image_ref}
                ref = await client.upload_content(descriptor, media_type="application/vnd.loom.action-descriptor+json", name=f"{prefix}/descriptors/{identity.rsplit('/', 1)[-1]}")
                refs[identity] = ref.model_dump(mode="json", include={"resource_id", "version_or_digest", "identity_criterion"}, exclude_none=True)
                del pending[identity]
                progress = True
            if not progress:
                raise ValueError("service_descriptor_dependency_cycle")
        manifest = deepcopy(self.manifest)
        manifest["body"]["image_ref"] = image_ref
        if usage_ref is not None:
            manifest["how_to_use_ref"] = wire(usage_ref)
        for export in manifest["action_exports"]:
            export["action_descriptor_ref"] = resolve(export["action_descriptor_ref"])
            export["io_contract_ref"] = resolve(export["io_contract_ref"])
        package = await client.register_package(manifest)
        return package["resource"]
