"""Activation-scoped, renewable dependency context mounted by the operator."""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from .bindings import AccessCredential
from .client import LoomClient
from .grip import GripError


class DependencyContext:
    def __init__(self, path="/run/loom/dependencies/context.json"):
        self.path = Path(path)

    def read(self, name):
        try:
            context = json.loads(self.path.read_text())
            return context, context["dependencies"][name]
        except (OSError, ValueError, KeyError) as exc:
            raise GripError("authentication_required") from exc

    @asynccontextmanager
    async def client(self, name, *, min_valid_seconds=0):
        context, dependency = self.read(name)
        async def credential(binding, target, action, description):
            fresh, dep = self.read(name)
            if fresh["revision"] != context["revision"]:
                raise GripError("forbidden")
            for grant in dep["credentials"]:
                if (grant["target"], grant["action"], grant["audience"]) == (target, action, binding.audience):
                    value = AccessCredential.from_response(grant)
                    if value.expires_at <= time.time() + min_valid_seconds:
                        raise GripError("authentication_required")
                    return value
            raise GripError("forbidden")
        # Each new channel reads the atomic snapshot; an established channel
        # keeps its original bounded grant and is never transparently replayed.
        async with LoomClient(context["observer_url"], workspace_id=context["workspace_id"],
                grip_authority=context["grip_authority"], allow_insecure_grip=context["allow_insecure_grip"],
                trusted_bindings=dependency["bindings"], binding_preference=[b["endpoint"] for b in dependency["bindings"]],
                credential_provider=credential, descriptions={dependency["target"]: dependency["description"]},
                schemas=dependency["schemas"]) as client:
            yield client, dependency
