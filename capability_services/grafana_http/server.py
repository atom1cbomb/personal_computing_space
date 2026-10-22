"""Read-only Grafana and Prometheus queries for one named activation."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_BODY = 2 * 1024 * 1024
MAX_RESPONSE = 32 * 1024 * 1024
PAGE_SIZE = 1000
MAX_PAGES = 100
UID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
METRIC_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\Z")
LABEL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class GatewayError(Exception):
    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def https_origin(value: Any) -> str:
    try:
        parsed = urlsplit(value)
        if (not isinstance(value, str) or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError("origin")
        parsed.port
        return value.rstrip("/")
    except (ValueError, TypeError, AttributeError) as exc:
        raise GatewayError("instance_config_invalid", 500) from exc


def uid(value: Any) -> str:
    if not isinstance(value, str) or not UID_RE.fullmatch(value):
        raise GatewayError("uid_invalid", 400)
    return value


def timestamp(value: Any) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("time")
        try:
            result = float(value)
        except ValueError:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("timezone")
            result = date.timestamp()
        if not math.isfinite(result) or result < 0:
            raise ValueError("time")
        datetime.fromtimestamp(result, timezone.utc)
        return result
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise GatewayError("time_invalid", 400) from exc


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def gpu_metric_config(value: Any) -> dict[str, str]:
    required = {"name", "node_label", "index_label", "unit"}
    if (not isinstance(value, dict) or not required.issubset(value)
            or set(value) - required - {"uuid_label"}
            or not isinstance(value["name"], str) or not METRIC_RE.fullmatch(value["name"])
            or value["unit"] not in {"percent", "ratio"}):
        raise GatewayError("instance_config_invalid", 500)
    for key in ("node_label", "index_label", "uuid_label"):
        if key in value and (not isinstance(value[key], str) or not LABEL_RE.fullmatch(value[key])):
            raise GatewayError("instance_config_invalid", 500)
    if value["node_label"] == value["index_label"]:
        raise GatewayError("instance_config_invalid", 500)
    return dict(value)


class GrafanaGateway:
    def __init__(self, *, instance_path: Path = Path("/run/loom/instance.json"),
                 credential_root: Path = Path("/run/loom/credentials"),
                 state_root: Path = Path("/var/lib/loom/service"),
                 transport: httpx.BaseTransport | None = None, timeout: float = 20):
        try:
            config = json.loads(instance_path.read_text())
            if not isinstance(config, dict) or set(config) - {
                    "api_origin", "browser_origin", "datasource_uid", "gpu_metric", "target_resource_ref"}:
                raise ValueError("config")
            self.origin = https_origin(config["api_origin"])
            self.browser_origin = https_origin(config.get("browser_origin", config["api_origin"]))
            self.default_datasource = uid(config["datasource_uid"]) if "datasource_uid" in config else None
            self.gpu_metric = gpu_metric_config(config["gpu_metric"]) if "gpu_metric" in config else None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise GatewayError("instance_config_invalid", 500) from exc
        self.credential_root = credential_root
        self.state_path = state_root / "grafana-session.json"
        self._session_source: str | None = None
        self._session_cookie: str | None = None
        self._browser_session: dict[str, Any] | None = None
        self._lock = threading.RLock()
        self.client = httpx.Client(base_url=self.origin, transport=transport, timeout=timeout,
                                   follow_redirects=False, trust_env=False)

    def close(self):
        self.client.close()

    def _load_session(self, source: str, original: str | None):
        self._session_source, self._session_cookie = source, original
        try:
            saved = json.loads(self.state_path.read_text())
            if saved.get("source_sha256") == source and self._valid_cookie(saved.get("cookie")):
                self._session_cookie = saved["cookie"]
        except (OSError, ValueError, AttributeError):
            pass

    @staticmethod
    def _valid_cookie(value: Any) -> bool:
        return (isinstance(value, str) and 0 < len(value) <= 16384
                and all(33 <= ord(c) <= 126 and c not in ';,"\\' for c in value))

    def _credential_headers(self) -> dict[str, str]:
        sources = [self.credential_root / name for name in ("grafana_token", "grafana_session")]
        present = [p for p in sources if p.exists()]
        if len(present) > 1:
            raise GatewayError("credential_profile_invalid", 503)
        headers = {"Accept": "application/json"}
        if not present:
            if self._session_source != "browser":
                self._load_session("browser", None)
            if self._session_cookie:
                headers["Cookie"] = "grafana_session=" + self._session_cookie
            return headers
        path = present[0]
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                raise ValueError("credential")
            value = path.read_text().strip()
            if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("credential")
        except (OSError, ValueError) as exc:
            raise GatewayError("credential_profile_invalid", 503) from exc
        if path.name == "grafana_token":
            headers["Authorization"] = "Bearer " + value
        else:
            if not self._valid_cookie(value):
                raise GatewayError("credential_profile_invalid", 503)
            source = hashlib.sha256(value.encode()).hexdigest()
            if source != self._session_source:
                self._load_session(source, value)
            headers["Cookie"] = "grafana_session=" + self._session_cookie
        return headers

    def _persist_session(self):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.state_path.with_suffix(".tmp")
            with open(temporary, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
                json.dump({"source_sha256": self._session_source, "cookie": self._session_cookie}, stream)
            temporary.replace(self.state_path)
        except OSError as exc:
            raise GatewayError("credential_state_unavailable", 503) from exc

    def _rotate_session(self, response: httpx.Response):
        if self._session_source is None:
            return
        for cookie in response.cookies.jar:
            if cookie.name != "grafana_session" or not cookie.value:
                continue
            if cookie.value == self._session_cookie:
                continue
            if not self._valid_cookie(cookie.value):
                raise GatewayError("grafana_response_invalid")
            self._session_cookie = cookie.value
            if not self._browser_session or self._browser_session["phase"] != "verifying":
                self._persist_session()

    @staticmethod
    def _response_json(response: httpx.Response) -> Any:
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE:
                raise GatewayError("grafana_response_too_large")
        return json.loads(raw)

    def _request_json(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            headers = self._credential_headers()
            self.client.cookies.clear()
            with self.client.stream(method, path, params=params, headers=headers) as r:
                if r.status_code == 401:
                    if "Cookie" in headers:
                        try:
                            challenge = self._response_json(r)
                        except (ValueError, UnicodeError):
                            challenge = None
                        if isinstance(challenge, dict) and challenge.get("messageId") == "session.token.rotate":
                            raise GatewayError("grafana_session_rotation_required", 401)
                    raise GatewayError("grafana_authentication_required", 401)
                if r.is_redirect:
                    raise GatewayError("grafana_authentication_required", 401)
                if r.status_code == 403:
                    raise GatewayError("grafana_forbidden", 403)
                if r.status_code == 429:
                    raise GatewayError("grafana_rate_limited", 429)
                if not 200 <= r.status_code < 300:
                    raise GatewayError("grafana_request_failed")
                value = self._response_json(r)
                self._rotate_session(r)
                return value
        except GatewayError:
            raise
        except httpx.HTTPError as exc:
            raise GatewayError("grafana_unreachable") from exc
        except (ValueError, UnicodeError) as exc:
            raise GatewayError("grafana_response_invalid") from exc

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            try:
                return self._request_json("GET", path, params)
            except GatewayError as exc:
                if exc.code != "grafana_session_rotation_required":
                    raise
            # Grafana's normal short-lived token rotation needs no human login.
            # Serialize it with the original read and retry only once.
            try:
                self._request_json("POST", "/api/user/auth-tokens/rotate")
                return self._request_json("GET", path, params)
            except GatewayError as exc:
                if exc.code == "grafana_session_rotation_required":
                    raise GatewayError("grafana_session_rotation_failed", 503) from exc
                raise

    def _datasources(self) -> list[dict[str, Any]]:
        value = self._get("/api/datasources")
        if not isinstance(value, list) or any(not isinstance(x, dict) or not all(
                isinstance(x.get(k), str) and x[k] for k in ("uid", "name", "type")) for x in value):
            raise GatewayError("grafana_response_invalid")
        if len(value) >= 5000:
            raise GatewayError("grafana_datasource_list_truncated")
        return [{"uid": uid(x["uid"]), "name": x["name"], "type": x["type"],
                 "is_default": x.get("isDefault") is True} for x in value]

    def auth_status(self) -> dict[str, Any]:
        with self._lock:
            if self._browser_session:
                if self._browser_session["expires_at"] > time.time():
                    return {"status": "auth_required", "poll_after_seconds": 2}
                self._browser_session = None
            try:
                self._datasources()
                return {"status": "authenticated"}
            except GatewayError as exc:
                if exc.code == "grafana_authentication_required":
                    return {"status": "auth_required", "poll_after_seconds": 5}
                return {"status": "auth_failed"}

    def auth_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload:
            return self.auth_status()
        session_id, expiry = payload.get("session_id"), payload.get("expires_at")
        if (set(payload) != {"session_id", "callback_uri", "expires_at"}
                or not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session_id)
                or payload.get("callback_uri") != "http://localhost:3000/login-callback"
                or isinstance(expiry, bool) or not isinstance(expiry, (float, int))
                or not time.time() < expiry <= time.time() + 310):
            raise GatewayError("browser_context_invalid", 400)
        with self._lock:
            if any((self.credential_root / name).exists() for name in ("grafana_token", "grafana_session")):
                raise GatewayError("browser_login_profile_conflict", 409)
            self._browser_session = {**payload, "phase": "created"}
        return {"status": "auth_required", "poll_after_seconds": 2}

    def browser(self, payload: dict[str, Any]) -> dict[str, Any]:
        action, query = payload.get("action"), payload.get("query", {})
        if (set(payload) - {"action", "session_id", "query"} or action not in {"open", "callback"}
                or not isinstance(query, dict) or action == "open" and query):
            raise GatewayError("browser_request_invalid", 400)
        with self._lock:
            session = self._browser_session
            if session is None or session["session_id"] != payload.get("session_id"):
                raise GatewayError("browser_session_not_found", 404)
            if session["expires_at"] <= time.time():
                self._browser_session = None
                raise GatewayError("browser_session_expired", 409)
            if session["phase"] != ("created" if action == "open" else "bound"):
                raise GatewayError("browser_session_consumed", 409)
            if action == "open":
                session["phase"] = "bound"
                return {"status": "redirect", "location": self.browser_origin + "/login/generic_oauth"}
            values = query.get("grafana_session")
            if (set(query) != {"grafana_session"} or not isinstance(values, list) or len(values) != 1
                    or not self._valid_cookie(values[0]) or len(values[0]) > 4096):
                raise GatewayError("browser_callback_invalid", 400)
            session["phase"] = "verifying"
            previous = self._session_source, self._session_cookie
            self._session_source, self._session_cookie = "browser", values[0]
            try:
                self._datasources()
                self._persist_session()
            except GatewayError:
                self._session_source, self._session_cookie = previous
                raise GatewayError("browser_login_verification_failed", 409) from None
            finally:
                self._browser_session = None
            return {"status": "accepted"}

    @staticmethod
    def _parameters(payload: dict[str, Any], allowed: set[str], required: set[str] | None = None):
        if set(payload) - allowed or not (required or set()).issubset(payload):
            raise GatewayError("parameters_invalid", 400)

    def _datasource(self, requested: Any = None) -> str:
        wanted = requested if requested is not None else self.default_datasource
        values = self._datasources()
        if wanted is not None:
            wanted = uid(wanted)
            matched = [x for x in values if x["uid"] == wanted]
            if not matched:
                raise GatewayError("datasource_not_found", 404)
            if matched[0]["type"] != "prometheus":
                raise GatewayError("prometheus_datasource_required", 400)
            return wanted
        values = [x for x in values if x["type"] == "prometheus"]
        defaults = [x for x in values if x["is_default"]]
        if len(defaults) == 1:
            return defaults[0]["uid"]
        if len(values) == 1:
            return values[0]["uid"]
        raise GatewayError("datasource_ambiguous" if values else "prometheus_datasource_unavailable", 400)

    def _query(self, query: Any, *, datasource_uid: Any = None, at: Any = None,
               start: Any = None, end: Any = None, step: Any = None) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query.encode()) > 32768:
            raise GatewayError("query_invalid", 400)
        params: dict[str, Any] = {"query": query}
        if start is None:
            params["time"] = timestamp(at) if at is not None else time.time()
            route = "query"
        else:
            first, last = timestamp(start), timestamp(end)
            if (isinstance(step, bool) or not isinstance(step, int) or step < 1
                    or first >= last or (last - first) / step + 1 > 11000):
                raise GatewayError("query_range_invalid", 400)
            params.update(start=first, end=last, step=step)
            route = "query_range"
        selected = self._datasource(datasource_uid)
        value = self._get(f"/api/datasources/proxy/uid/{selected}/api/v1/{route}", params)
        if not isinstance(value, dict):
            raise GatewayError("prometheus_response_invalid")
        if value.get("status") == "error":
            raise GatewayError("prometheus_query_failed")
        data = value.get("data")
        warnings, infos = value.get("warnings", []), value.get("infos", [])
        if (value.get("status") != "success" or not isinstance(data, dict)
                or data.get("resultType") not in {"vector", "matrix", "scalar", "string"}
                or not isinstance(data.get("result"), list)
                or any(not isinstance(x, list) or any(not isinstance(y, str) for y in x)
                       for x in (warnings, infos))):
            raise GatewayError("prometheus_response_invalid")
        return {"status": "ok", "complete": not bool(warnings), "datasource_uid": selected,
                "queried_at": iso(time.time()), "query": query, "parameters": params,
                "result_type": data["resultType"], "result": data["result"],
                "warnings": warnings, "infos": infos}

    def _gpu_utilization(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._parameters(payload, {"hostname", "gpu_index", "time", "gpu_metric", "datasource_uid"}, {"hostname"})
        hostname, index = payload["hostname"], payload.get("gpu_index")
        if (not isinstance(hostname, str) or not hostname or len(hostname) > 253
                or any(ord(c) < 32 for c in hostname)
                or index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 0)):
            raise GatewayError("gpu_selector_invalid", 400)
        config = self.gpu_metric
        if "gpu_metric" in payload:
            try:
                config = gpu_metric_config(payload["gpu_metric"])
            except GatewayError as exc:
                raise GatewayError("gpu_metric_invalid", 400) from exc
        if config is None:
            raise GatewayError("gpu_metric_not_configured", 400)
        selectors = [config["node_label"] + "=" + json.dumps(hostname)]
        if index is not None:
            selectors.append(config["index_label"] + "=" + json.dumps(str(index)))
        query = config["name"] + "{" + ",".join(selectors) + "}"
        result = self._query(query, datasource_uid=payload.get("datasource_uid"), at=payload.get("time"))
        if result["result_type"] != "vector":
            raise GatewayError("gpu_metric_vector_required")
        samples = []
        for item in result["result"]:
            try:
                labels, point = item["metric"], item["value"]
                if (not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                         for k, v in labels.items())
                        or not isinstance(point, list) or len(point) != 2
                        or labels.get(config["node_label"]) != hostname):
                    raise ValueError("sample")
                raw_index = labels[config["index_label"]]
                if not re.fullmatch(r"[0-9]+", raw_index):
                    raise ValueError("index")
                device = int(raw_index)
                if index is not None and device != index:
                    raise ValueError("index mismatch")
                utilization = float(point[1]) * (100 if config["unit"] == "ratio" else 1)
                if not math.isfinite(utilization) or not 0 <= utilization <= 100:
                    raise ValueError("utilization")
                samples.append({"hostname": hostname, "gpu_index": device,
                                "gpu_uuid": labels.get(config.get("uuid_label", "")),
                                "utilization_percent": utilization, "sampled_at": iso(timestamp(point[0])),
                                "labels": labels})
            except (KeyError, TypeError, ValueError, OverflowError, GatewayError) as exc:
                raise GatewayError("gpu_metric_sample_invalid") from exc
        return {"status": "ok" if samples else "no_data", "complete": result["complete"],
                "hostname": hostname, "unit": "percent", "metric": config["name"],
                "datasource_uid": result["datasource_uid"], "queried_at": result["queried_at"],
                "query_time": iso(result["parameters"]["time"]), "samples": sorted(samples, key=lambda x: x["gpu_index"]),
                "warnings": result["warnings"], "infos": result["infos"]}

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/auth/browser":
            return self.browser(payload)
        if path == "/v1/auth/status":
            self._parameters(payload, set())
            return self.auth_status()
        if path == "/v1/datasources/list":
            self._parameters(payload, set())
            return {"status": "ok", "items": self._datasources(), "complete": True}
        if path == "/v1/dashboards/list":
            self._parameters(payload, {"search"})
            search = payload.get("search", "")
            if not isinstance(search, str) or len(search) > 256:
                raise GatewayError("parameters_invalid", 400)
            items = {}
            for page in range(1, MAX_PAGES + 1):
                batch = self._get("/api/search", {"type": "dash-db", "query": search,
                                                  "limit": PAGE_SIZE, "page": page})
                if not isinstance(batch, list):
                    raise GatewayError("grafana_response_invalid")
                previous = len(items)
                for item in batch:
                    if not isinstance(item, dict) or not isinstance(item.get("title"), str):
                        raise GatewayError("grafana_response_invalid")
                    identity = uid(item.get("uid"))
                    items[identity] = {k: item[k] for k in ("uid", "title", "url", "folderUid", "folderTitle", "tags") if k in item}
                if len(batch) < PAGE_SIZE:
                    return {"status": "ok", "items": list(items.values()), "total_count": len(items), "complete": True}
                if len(items) == previous:
                    break
            raise GatewayError("grafana_dashboard_list_truncated")
        if path == "/v1/dashboards/get":
            self._parameters(payload, {"uid"}, {"uid"})
            value = self._get("/api/dashboards/uid/" + uid(payload["uid"]))
            if not isinstance(value, dict) or not isinstance(value.get("dashboard"), dict):
                raise GatewayError("grafana_response_invalid")
            if value["dashboard"].get("uid") != payload["uid"]:
                raise GatewayError("grafana_dashboard_identity_mismatch")
            return {"status": "ok", "dashboard": value["dashboard"]}
        if path == "/v1/metrics/query":
            self._parameters(payload, {"query", "datasource_uid", "time"}, {"query"})
            return self._query(payload["query"], datasource_uid=payload.get("datasource_uid"), at=payload.get("time"))
        if path == "/v1/metrics/query-range":
            self._parameters(payload, {"query", "datasource_uid", "start", "end", "step"}, {"query", "start", "end", "step"})
            return self._query(payload["query"], datasource_uid=payload.get("datasource_uid"),
                               start=payload["start"], end=payload["end"], step=payload["step"])
        if path == "/v1/gpu/utilization":
            return self._gpu_utilization(payload)
        raise GatewayError("action_not_allowed", 404)


class Handler(BaseHTTPRequestHandler):
    gateway: GrafanaGateway

    def log_message(self, *_args):
        return

    def _reply(self, status: int, value: Any):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._reply(200 if self.path == "/healthz" else 404,
                    {"status": "ok"} if self.path == "/healthz" else {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise GatewayError("request_too_large", 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise GatewayError("request_json_invalid", 400)
            self._reply(200, self.gateway.handle(self.path, payload))
        except GatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, UnicodeError):
            self._reply(400, {"error": "request_json_invalid"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})


def main():
    gateway = GrafanaGateway()
    Handler.gateway = gateway
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        gateway.close()


if __name__ == "__main__":
    main()
