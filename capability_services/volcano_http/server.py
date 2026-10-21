"""Read-only actions over an activation's Volcano Dashboard JSON API."""
from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_BODY = 128 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
PAGE_SIZE = 500
MAX_PAGES = 100
NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
RESOURCE_RE = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+$")
QUANTITY_RE = re.compile(r"^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?m?$")
ROUTES = {
    "/v1/namespaces/list": "/api/namespaces",
    "/v1/queues/list": "/api/queues",
    "/v1/podgroups/list": "/api/podgroups",
    "/v1/pods/list": "/api/pods",
    "/v1/jobs/list": "/api/jobs",
}


class GatewayError(Exception):
    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def https_origin(value: Any) -> str:
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or parsed.scheme != "https"
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError("invalid origin")
        parsed.port
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError) as exc:
        raise GatewayError("instance_config_invalid", 500) from exc


class VolcanoGateway:
    def __init__(self, *, instance_path: Path = Path("/run/loom/instance.json"),
                 transport: httpx.BaseTransport | None = None, timeout: float = 20):
        try:
            config = json.loads(instance_path.read_text())
            if set(config) - {"api_origin", "target_resource_ref"}:
                raise ValueError("unknown configuration field")
            origin = https_origin(config["api_origin"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GatewayError("instance_config_invalid", 500) from exc
        self.origin = origin
        self.timeout = timeout
        self.client = httpx.Client(base_url=self.origin, timeout=timeout, transport=transport,
                                   follow_redirects=False, trust_env=False,
                                   headers={"Accept": "application/json"})

    def close(self):
        self.client.close()

    def _get(self, path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            with self.client.stream("GET", path, params=params, timeout=timeout) as response:
                if response.status_code == 401 or response.is_redirect:
                    raise GatewayError("dashboard_authentication_required", 401)
                if response.status_code == 403:
                    raise GatewayError("dashboard_forbidden", 403)
                if not 200 <= response.status_code < 300:
                    raise GatewayError("dashboard_request_failed")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise GatewayError("dashboard_response_too_large")
            value = json.loads(raw)
        except GatewayError:
            raise
        except httpx.HTTPError as exc:
            raise GatewayError("dashboard_unreachable") from exc
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("dashboard_response_invalid") from exc
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise GatewayError("dashboard_response_invalid")
        return value

    def _items(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        items: dict[tuple[str, str], dict[str, Any]] = {}
        deadline = time.monotonic() + self.timeout
        for page in range(1, MAX_PAGES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError("dashboard_query_timeout")
            value = self._get(path, {**params, "page": page, "limit": PAGE_SIZE}, remaining)
            total = value.get("totalCount")
            if total is None and path == "/api/namespaces":
                total = len(value["items"])
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise GatewayError("dashboard_response_invalid")
            previous = len(items)
            for item in value["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
                    raise GatewayError("dashboard_response_invalid")
                meta = item["metadata"]
                if not isinstance(meta.get("name"), str) or not meta["name"]:
                    raise GatewayError("dashboard_response_invalid")
                items[(meta.get("namespace", ""), meta["name"])] = item
            if len(items) == total:
                return list(items.values())
            if len(items) > total or len(items) == previous:
                raise GatewayError("dashboard_list_incomplete")
        raise GatewayError("dashboard_list_incomplete")

    @staticmethod
    def _namespace(payload: dict[str, Any]) -> str:
        namespace = payload.get("namespace", "all")
        if not isinstance(namespace, str) or not NAME_RE.fullmatch(namespace):
            raise GatewayError("namespace_invalid", 400)
        return namespace

    def _filters(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        fields = ({"state", "search"} if path == "/api/queues" else
                  set() if path == "/api/namespaces" else {"namespace", "status", "search"})
        if path == "/api/jobs":
            fields.add("queue")
        if set(payload) - fields:
            raise GatewayError("parameters_invalid", 400)
        params: dict[str, Any] = {}
        if "namespace" in fields:
            namespace = self._namespace(payload)
            params["namespace"] = "All" if namespace == "all" else namespace
        for key, value in payload.items():
            if key == "namespace":
                continue
            if not isinstance(value, str) or len(value) > 256:
                raise GatewayError("parameters_invalid", 400)
            params[key] = value
        return params

    @staticmethod
    def _quantity(value: Any) -> int:
        text = str(value)
        if len(text) > 64 or not QUANTITY_RE.fullmatch(text):
            raise GatewayError("gpu_quantity_invalid")
        try:
            amount = Decimal(text[:-1]) / 1000 if text.endswith("m") else Decimal(text)
            if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value() or amount > 2**63 - 1:
                raise GatewayError("gpu_quantity_invalid")
            return int(amount)
        except InvalidOperation as exc:
            raise GatewayError("gpu_quantity_invalid") from exc

    @classmethod
    def _container_request(cls, container: dict[str, Any], resource: str) -> int:
        resources = container.get("resources") or {}
        requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
        return cls._quantity(requests.get(resource, limits.get(resource, 0)))

    @classmethod
    def _pod_request(cls, pod: dict[str, Any], resource: str) -> int:
        spec = pod.get("spec") or {}
        containers = spec.get("containers")
        if not isinstance(containers, list) or not containers:
            raise GatewayError("dashboard_pod_spec_missing")
        app = sum(cls._container_request(c, resource) for c in containers)
        sidecars, init_peak = 0, 0
        for container in spec.get("initContainers", []):
            amount = cls._container_request(container, resource)
            if container.get("restartPolicy") == "Always":
                sidecars += amount
                init_peak = max(init_peak, sidecars)
            else:
                init_peak = max(init_peak, sidecars + amount)
        overhead = cls._quantity((spec.get("overhead") or {}).get(resource, 0))
        return max(app + sidecars, init_peak) + overhead

    def _gpu_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"namespace", "resource_name"}:
            raise GatewayError("parameters_invalid", 400)
        namespace = self._namespace(payload)
        resource = payload.get("resource_name", "nvidia.com/gpu")
        if not isinstance(resource, str) or len(resource) > 253 or not RESOURCE_RE.fullmatch(resource):
            raise GatewayError("gpu_resource_name_invalid", 400)
        pods = self._items("/api/pods", {"namespace": "All" if namespace == "all" else namespace})
        totals = {"allocated": 0, "running_allocated": 0, "unscheduled_requested": 0}
        by_namespace = defaultdict(lambda: {"allocated": 0, "running_allocated": 0,
                                            "unscheduled_requested": 0, "gpu_pods": 0})
        gpu_pods = []
        terminal = 0
        for pod in pods:
            phase = (pod.get("status") or {}).get("phase", "Pending")
            if phase in {"Succeeded", "Failed"}:
                terminal += 1
                continue
            amount = self._pod_request(pod, resource)
            if amount == 0:
                continue
            meta, spec = pod["metadata"], pod["spec"]
            pod_namespace = meta.get("namespace")
            if not isinstance(pod_namespace, str) or not NAME_RE.fullmatch(pod_namespace):
                raise GatewayError("dashboard_pod_namespace_missing")
            if namespace != "all" and pod_namespace != namespace:
                raise GatewayError("dashboard_namespace_mismatch")
            row = by_namespace[pod_namespace]
            scheduled = bool(spec.get("nodeName"))
            amount_by_metric = {
                "allocated": amount if scheduled else 0,
                "running_allocated": amount if scheduled and phase == "Running" else 0,
                "unscheduled_requested": 0 if scheduled else amount,
            }
            for key, value in amount_by_metric.items():
                row[key] += value
                totals[key] += value
            row["gpu_pods"] += 1
            gpu_pods.append({"namespace": pod_namespace, "name": meta["name"],
                             "node_name": spec.get("nodeName"), "phase": phase,
                             "gpu_requests": amount, "scheduled": scheduled})
        return {"status": "ok", "complete": True, "metric": "scheduled_pod_requests",
                "collected_at": datetime.now(timezone.utc).isoformat(), "namespace": namespace,
                "resource_name": resource, "pod_count": len(pods), "terminal_pods": terminal,
                **totals, "by_namespace": [{"namespace": key, **value} for key, value in sorted(by_namespace.items())],
                "gpu_pods": gpu_pods}

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/gpu/usage":
            return self._gpu_usage(payload)
        upstream = ROUTES.get(path)
        if upstream is None:
            raise GatewayError("action_not_allowed", 404)
        items = self._items(upstream, self._filters(upstream, payload))
        return {"status": "ok", "items": items, "total_count": len(items), "complete": True}


class Handler(BaseHTTPRequestHandler):
    gateway: VolcanoGateway

    def _reply(self, status: int, value: Any):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"status": "ok"})
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise GatewayError("request_too_large", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise GatewayError("parameters_invalid", 400)
            self._reply(200, self.gateway.handle(self.path, payload))
        except GatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, TypeError, KeyError, AttributeError):
            self._reply(400, {"error": "request_or_provider_data_invalid"})

    def log_message(self, _format, *_args):
        return


def main():
    gateway = VolcanoGateway(instance_path=Path(os.getenv("VOLCANO_INSTANCE_PATH", "/run/loom/instance.json")))
    Handler.gateway = gateway
    try:
        ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()
    finally:
        gateway.close()


if __name__ == "__main__":
    main()
