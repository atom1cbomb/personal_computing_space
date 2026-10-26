"""Scoped credentials for Loom-managed service calls (not GRIP core auth)."""
from __future__ import annotations

import time
from pathlib import Path

import jwt

from loom_orchestration.grip import GripError, Names, Request, uri
from loom_orchestration.naming import resource_label


def interaction(export) -> str:
    binding = export.runtime_binding if hasattr(export, "runtime_binding") else export.get("runtime_binding", {})
    return binding.get("interaction", "task")


def export_uri(names: Names, package, export) -> str:
    ref = export.action_descriptor_ref
    return names.name("actions", "exports", package.package_id, package.package_version,
                      resource_label(ref.resource_id), "sha256", ref.digest)


def activation_uri(names: Names, package, activation) -> str:
    return names.name("activations", package.package_id, package.package_version,
                      activation.target_slave, activation.instance_name)


class ServiceTokens:
    def __init__(self, settings):
        self.settings = settings
        self.names = Names(settings.grip_authority, settings.workspace_id)
        self.issuer = self.names.root

    @property
    def enabled(self):
        return bool(self.settings.grip_signing_key_file
                    and self.settings.grip_signing_key_id in self.settings.grip_verification_keys)

    def issue(self, *, subject, audience, target, action, activation, revision,
              expires_at=None, caller_revision=None):
        if not self.enabled:
            raise GripError("authentication_required")
        now = int(time.time())
        expiry = min(now + min(self.settings.grip_access_token_seconds, 3600), expires_at or float("inf"))
        if expiry <= now:
            raise GripError("authentication_failed")
        claims = {"iss": self.issuer, "sub": uri(subject), "aud": uri(audience),
                  "workspace": self.names.root, "target": uri(target), "action": uri(action),
                  "activation": uri(activation), "revision": revision, "iat": now, "exp": int(expiry)}
        if caller_revision is not None:
            claims["caller_revision"] = caller_revision
        key = Path(self.settings.grip_signing_key_file).read_bytes()
        token = jwt.encode(claims, key, algorithm="EdDSA", headers={"kid": self.settings.grip_signing_key_id})
        return token, int(expiry)

    def verify(self, request: Request, audience: str):
        try:
            token = request.auth.credential
            header = jwt.get_unverified_header(token)
            key_file = self.settings.grip_verification_keys.get(header.get("kid"))
            if not key_file:
                raise GripError("authentication_failed")
            claims = jwt.decode(token, Path(key_file).read_bytes(), algorithms=["EdDSA"],
                issuer=self.issuer, audience=audience, options={"require": [
                    "iss", "sub", "aud", "workspace", "target", "action", "activation", "revision", "iat", "exp"]})
            if (claims["workspace"] != self.names.root or claims["target"] != request.target
                    or claims["action"] != request.action
                    or (request.auth.tenant is not None and request.auth.tenant != self.names.root)):
                raise GripError("forbidden")
            if type(claims["revision"]) is not int or claims["revision"] < 1:
                raise GripError("authentication_failed")
            if "caller_revision" in claims and (type(claims["caller_revision"]) is not int or claims["caller_revision"] < 1):
                raise GripError("authentication_failed")
            return claims
        except (jwt.PyJWTError, OSError, TypeError, ValueError) as exc:
            raise GripError("authentication_failed") from exc
