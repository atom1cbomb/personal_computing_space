"""One routing policy for activation admission and public service discovery."""
from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from loom_orchestration.bindings import stream_endpoint, validate_endpoint
from loom_orchestration.grip import Binding, GripError
from loom_orchestration.streams import STREAM_PROTOCOL
from loom_v2.contracts.errors import error
from loom_v2.service_access import ServiceTokens, activation_uri, export_uri, interaction


class ServiceRoutes:
    def __init__(self, settings):
        self.settings = settings
        self.tokens = ServiceTokens(settings)
        self.names = self.tokens.names
        # Only the embedded test composition installs this in-process route.
        self.embedded: Callable[[str], bool] = lambda slave: False

    def _routes(self, slave, target, *, upstream=False):
        values = self.settings.grip_upstream_bindings if upstream else self.settings.grip_data_bindings
        return [Binding.model_validate(b) for b in values.get(target, values.get(self.names.name("slaves", slave), []))]

    @staticmethod
    def _supports(binding, mode):
        return binding.protocol == STREAM_PROTOCOL if mode == "byte-stream" else binding.protocol in {"https-json.v1", "loom-http-json.v1"}

    def _validate(self, binding):
        validate_endpoint(binding, allow_insecure=self.settings.grip_allow_insecure_services)
        if not binding.audience or binding.audience == self.names.root:
            raise GripError("untrusted_audience")

    def upstream(self, slave, target, mode):
        # An upstream for a different mode must not hide a usable direct route.
        for binding in [*self._routes(slave, target, upstream=True), *self._routes(slave, target)]:
            if self._supports(binding, mode):
                self._validate(binding)
                return binding
        return None

    def bindings(self, slave, target, mode):
        if not self.tokens.enabled:
            return []
        result = []
        for binding in self._routes(slave, target):
            if self._supports(binding, mode):
                self._validate(binding)
                result.append(binding.model_dump(exclude_none=True))
        relay = self.embedded(slave) or self.upstream(slave, target, mode) is not None
        if relay:
            base = self.settings.public_base_url.rstrip("/")
            protocol = STREAM_PROTOCOL if mode == "byte-stream" else "https-json.v1" if base.startswith("https:") else "loom-http-json.v1"
            binding = {"protocol": protocol, "audience": self.names.root}
            if base:
                binding["endpoint"] = stream_endpoint(base) if mode == "byte-stream" else base + "/.well-known/grip"
            if binding not in result:
                result.append(binding)
        return result

    def problem(self, slave, target, mode):
        if not self.tokens.enabled:
            return "service_signing_unavailable"
        try:
            return None if self.bindings(slave, target, mode) else "binding_unavailable"
        except (GripError, ValidationError):
            return "binding_invalid"

    def issues(self, package, slave, instance):
        target = self.names.name("activations", package.package_id, package.package_version, slave, instance)
        result = []
        for mode in sorted({interaction(e) for e in package.action_exports}):
            if reason := self.problem(slave, target, mode):
                result.append({"code": reason, "interaction": mode, "actions": [
                    export_uri(self.names, package, e) for e in package.action_exports if interaction(e) == mode]})
        return result

    def require(self, package, slave, instance):
        # A missing direct JSON route does not prevent task execution.
        if issues := [i for i in self.issues(package, slave, instance) if i["interaction"] == "byte-stream"]:
            raise error("activation_preflight_failed", issues=issues)

    def executions(self, package, export, activation=None, *, direct_supported=True):
        """Action-local execution conditions, independent of Pool availability."""
        if not package.is_executable or package.execution.kind == "container:python_orchestrator":
            return {}
        reason = ("activation_required" if activation is None else
                  "desired_state_" + activation.desired_state if activation.desired_state != "running" else
                  "activation_" + activation.activation_state if activation.activation_state != "ready" else None)
        direct_reason = reason
        if direct_reason is None:
            target = activation_uri(self.names, package, activation)
            direct_reason = self.problem(activation.target_slave, target, interaction(export))
        result = ({"direct": {"available": direct_reason is None, "unavailable_reason": direct_reason}}
                  if direct_supported else {})
        if interaction(export) == "request-reply":
            result["task"] = {"available": reason is None, "unavailable_reason": reason}
        return result

    def status(self, package, activation):
        issues = self.issues(package, activation.target_slave, activation.instance_name)
        runtime = activation.activation_state
        running = activation.desired_state == "running"
        available = any(mode["available"] for export in package.action_exports
                        for mode in self.executions(package, export, activation).values())
        reason = ("desired_state_" + activation.desired_state if not running else
                  "activation_" + runtime if runtime != "ready" else
                  issues[0]["code"] if issues and not available else None)
        return {"state": "degraded" if running and runtime == "ready" and issues else runtime,
                "runtime_state": runtime, "available": available, "unavailable_reason": reason}

    def node_metadata(self, slave):
        return {mode: {"available": (reason := self.problem(slave, None, mode)) is None,
                       "unavailable_reason": reason} for mode in ("request-reply", "byte-stream")}
