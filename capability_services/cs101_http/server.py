"""Read-only grade analysis against one configured CS101 HTTPS origin."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
from statistics import fmean, median, pstdev
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator


MAX_BODY = 128 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
MAX_RECORDS = 10000
PAGE_SIZE = 200
REQUEST_TIMEOUT = 20.0
ANALYSIS_TIMEOUT = 60.0
TOKEN_FILENAME = "cs101_token"
STORAGE_KEY = "token"
READ_PATH = re.compile(r"/api/v2/(?:users/me|papers|papers/[1-9][0-9]*/statistics|paper-statuses)")
ROUTES = {
    "/v1/papers/list": "papers-list",
    "/v1/paper/statistics": "paper-statistics",
    "/v1/grades/list": "grades-list",
    "/v1/grades/analyze": "grades-analyze",
    "/v1/student/analyze": "student-analyze",
}


class GatewayError(Exception):
    """A stable, credential-free error suitable for the runtime."""

    def __init__(self, code: str, status: int = 502):
        super().__init__(code)
        self.code, self.status = code, status


def https_origin(value: Any) -> str:
    try:
        if not isinstance(value, str) or not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("invalid origin")
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or "?" in value or "#" in value or "\\" in value):
            raise ValueError("invalid origin")
        host = parsed.hostname.lower().rstrip(".")
        if host == "iaaa.gbu.edu.cn" or host.endswith(".iaaa.gbu.edu.cn"):
            raise ValueError("prohibited identity provider")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid port")
        return str(httpx.URL(value)).rstrip("/")
    except (TypeError, ValueError, httpx.InvalidURL) as exc:
        raise GatewayError("instance_config_invalid", 500) from exc


def integer(value: Any) -> bool:
    return type(value) is int


def invalid_json_constant(_value: str):
    raise ValueError("nonfinite JSON number")


def valid_token(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 8192 and value.isascii()
            and not any(c.isspace() or ord(c) < 33 for c in value))


class CS101Gateway:
    def __init__(self, *, instance_path: Path = Path("/run/loom/instance.json"),
                 credential_root: Path = Path("/run/loom/credentials"),
                 state_root: Path = Path("/var/lib/loom/service"),
                 transport: httpx.BaseTransport | None = None):
        try:
            config = json.loads(instance_path.read_text())
            if not isinstance(config, dict) or set(config) - {"api_origin", "target_resource_ref"}:
                raise ValueError("unknown configuration field")
            self.origin = https_origin(config["api_origin"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GatewayError("instance_config_invalid", 500) from exc
        self.credential_root = credential_root
        self.state_path = state_root / "cs101-session.json"
        self._browser_token: str | None = None
        self._browser_session: dict[str, Any] | None = None
        self._lock = threading.RLock()
        try:
            saved = json.loads(self.state_path.read_text())
            if isinstance(saved, dict) and saved.get("origin") == self.origin and valid_token(saved.get("token")):
                self._browser_token = saved["token"]
        except (FileNotFoundError, ValueError):
            pass
        except OSError as exc:
            raise GatewayError("credential_state_unavailable", 503) from exc
        # No proxy discovery, redirect following, OAuth discovery or remote schema
        # resolution. All requests are constructed from this origin and READ_PATH.
        self.client = httpx.Client(base_url=self.origin, transport=transport,
                                   follow_redirects=False, trust_env=False,
                                   timeout=REQUEST_TIMEOUT, headers={"Accept": "application/json"})
        schemas = Path(__file__).with_name("schemas")
        self.validators = {action: Draft202012Validator(json.loads(
            (schemas / f"{action}-input.json").read_text())) for action in ROUTES.values()}

    def close(self):
        self.client.close()

    def _mounted_token(self) -> str | None:
        path = self.credential_root / TOKEN_FILENAME
        try:
            if path.is_symlink():
                raise GatewayError("credential_profile_invalid", 401)
            if not path.exists():
                return None
            if not path.is_file():
                raise GatewayError("credential_profile_invalid", 401)
            token = path.read_text().strip()
        except UnicodeError as exc:
            raise GatewayError("credential_profile_invalid", 401) from exc
        except OSError as exc:
            raise GatewayError("credential_profile_unavailable", 401) from exc
        if not valid_token(token):
            raise GatewayError("credential_profile_invalid", 401)
        return token

    def _token(self) -> str:
        with self._lock:
            mounted = self._mounted_token()
            if mounted is not None and self._browser_token is not None and mounted != self._browser_token:
                raise GatewayError("credential_sources_conflict", 503)
            if self._browser_session is not None:
                raise GatewayError("upstream_auth_required", 401)
            token = mounted if mounted is not None else self._browser_token
            if token is None:
                raise GatewayError("credential_profile_unavailable", 401)
            return token

    def _get(self, path: str, params: dict[str, Any], deadline: float, *, token: str | None = None) -> dict[str, Any]:
        if not READ_PATH.fullmatch(path):
            raise GatewayError("upstream_path_not_allowed", 400)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayError("analysis_timeout", 504)
        try:
            with self.client.stream("GET", self.origin + path, params=params,
                                    headers={"Authorization": "Bearer " + (token if token is not None else self._token())},
                                    timeout=min(REQUEST_TIMEOUT, remaining)) as response:
                if 300 <= response.status_code < 400:
                    raise GatewayError("upstream_redirect_blocked")
                if response.status_code == 401:
                    raise GatewayError("upstream_auth_required", 401)
                if response.status_code == 403:
                    raise GatewayError("upstream_forbidden", 403)
                if response.status_code == 404:
                    raise GatewayError("upstream_not_found", 404)
                if response.status_code == 429:
                    raise GatewayError("upstream_rate_limited", 429)
                if not 200 <= response.status_code < 300:
                    raise GatewayError("upstream_request_failed")
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise GatewayError("upstream_response_invalid")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise GatewayError("upstream_response_too_large")
                    if time.monotonic() >= deadline:
                        raise GatewayError("analysis_timeout", 504)
            result = json.loads(raw, parse_constant=invalid_json_constant)
        except httpx.TimeoutException as exc:
            raise GatewayError("upstream_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise GatewayError("upstream_unreachable") from exc
        except (UnicodeError, ValueError) as exc:
            raise GatewayError("upstream_response_invalid") from exc
        if not isinstance(result, dict):
            raise GatewayError("upstream_response_invalid")
        return result

    def _verify(self, token: str):
        user = self._get("/api/v2/users/me", {}, time.monotonic() + REQUEST_TIMEOUT, token=token)
        if (not integer(user.get("uid")) or user["uid"] <= 0
                or not integer(user.get("role"))
                or not all(isinstance(user.get(k), str) for k in ("name", "group"))):
            raise GatewayError("upstream_response_invalid")

    def auth_status(self) -> dict[str, Any]:
        with self._lock:
            if self._browser_session is not None:
                if self._browser_session["expires_at"] > time.time():
                    return {"status": "auth_required"}
                self._browser_session = None
            try:
                self._verify(self._token())
            except GatewayError as exc:
                return {"status": "auth_required" if exc.code in {
                    "credential_profile_unavailable", "credential_profile_invalid", "upstream_auth_required",
                } else "auth_failed"}
            return {"status": "authenticated"}

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
            # Profile-bound instances remain operator-managed. Interactive
            # instances omit the mounted token instead of silently switching accounts.
            if self._mounted_token() is not None:
                raise GatewayError("operator_credentials_configured", 409)
            try:
                self.state_path.unlink(missing_ok=True)
            except OSError as exc:
                raise GatewayError("credential_state_unavailable", 503) from exc
            self._browser_token = None
            self._browser_session = {**payload, "phase": "created"}
        return {"status": "auth_required"}

    def _persist_token(self, token: str):
        temporary = None
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state_path.parent, delete=False) as output:
                temporary = Path(output.name)
                json.dump({"origin": self.origin, "token": token}, output)
            os.replace(temporary, self.state_path)
        except OSError as exc:
            raise GatewayError("credential_state_unavailable", 503) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

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
                # Only the visible browser visits this link and follows the
                # site's SSO flow. The gateway never fetches any login/IdP URL.
                return {"status": "redirect", "location": self.origin + "/login?manual=show"}
            values = query.get(STORAGE_KEY)
            if (set(query) != {STORAGE_KEY} or not isinstance(values, list) or len(values) != 1
                    or not isinstance(values[0], str) or not 0 < len(values[0]) <= 4096):
                raise GatewayError("browser_callback_invalid", 400)
            # CS101's Mantine storage adapter serializes the token as a JSON
            # string. Accept a raw string as well, without evaluating its contents.
            try:
                token = json.loads(values[0])
            except ValueError:
                token = values[0]
            # Mantine initializes the logged-out state as JSON "". This is an
            # anonymous browser candidate, so retain the challenge and window.
            if token is None or token == "":
                return {"status": "pending"}
            if not valid_token(token):
                raise GatewayError("browser_callback_invalid", 400)
            session["phase"] = "verifying"
            try:
                if self._mounted_token() is not None:
                    raise GatewayError("credential_sources_conflict", 503)
                self._verify(token)
                if session["expires_at"] <= time.time():
                    raise GatewayError("browser_session_expired", 409)
                self._persist_token(token)
            except GatewayError as exc:
                if exc.code == "upstream_auth_required":
                    session["phase"] = "bound"
                    return {"status": "pending"}
                self._browser_session = None
                raise GatewayError("browser_login_verification_failed", 409) from None
            self._browser_token = token
            self._browser_session = None
            return {"status": "accepted"}

    def _page(self, path: str, params: dict[str, Any], deadline: float) -> tuple[list[dict], int]:
        result = self._get(path, params, deadline)
        items, count = result.get("items"), result.get("count")
        if "items" in result and items is None:
            items = []
        if (not isinstance(items, list) or not integer(count) or count < 0
                or len(items) > params["limit"] or not all(isinstance(x, dict) for x in items)
                or len(items) > max(0, count - params["offset"])):
            raise GatewayError("upstream_response_invalid")
        return items, count

    @staticmethod
    def _paper(item: dict[str, Any]) -> dict[str, Any]:
        categories = item.get("category")
        if (not integer(item.get("paper_id")) or item["paper_id"] <= 0
                or not isinstance(item.get("title"), str)
                or (categories is not None and (not isinstance(categories, list)
                    or not all(isinstance(c, str) for c in categories)))):
            raise GatewayError("upstream_response_invalid")
        return {"paper_id": item["paper_id"], "title": item["title"], "category": categories or []}

    @classmethod
    def _grade(cls, item: dict[str, Any], *, include_context: bool = False) -> dict[str, Any]:
        instance = item.get("instance")
        if not isinstance(instance, dict) or not isinstance(instance.get("paper"), dict):
            raise GatewayError("upstream_grade_identity_missing")
        paper = cls._paper(instance["paper"])
        user = instance.get("user")
        if (not isinstance(user, dict) or not integer(user.get("uid")) or user["uid"] <= 0
                or not all(isinstance(user.get(k), str) for k in ("name", "group"))):
            raise GatewayError("upstream_grade_identity_missing")
        if (not all(integer(item.get(k)) for k in ("status_id", "time", "point"))
                or item["status_id"] <= 0 or not integer(instance.get("paper_instance_id"))
                or instance["paper_instance_id"] <= 0):
            raise GatewayError("upstream_response_invalid")
        grade = {**paper, "status_id": item["status_id"], "time": item["time"], "point": item["point"],
                 "paper_instance_id": instance["paper_instance_id"], "user_id": user["uid"],
                 "user_name": user["name"], "group": user["group"]}
        if include_context:
            context = item.get("context", "")
            if not isinstance(context, str):
                raise GatewayError("upstream_response_invalid")
            grade["context"] = context
        return grade

    @staticmethod
    def _scope(grade: dict, filters: dict):
        if ("paper_id" in filters and grade["paper_id"] != filters["paper_id"]
                or "group" in filters and grade["group"] != filters["group"]
                or "category" in filters and filters["category"] not in grade["category"]):
            raise GatewayError("upstream_scope_mismatch")

    def _collect(self, filters: dict, deadline: float) -> tuple[list[dict], int]:
        records: list[dict] = []
        seen: set[int] = set()
        expected = None
        while True:
            params = {**filters, "all_user": True, "latest": True, "offset": len(records), "limit": PAGE_SIZE}
            items, count = self._page("/api/v2/paper-statuses", params, deadline)
            if count > MAX_RECORDS:
                raise GatewayError("analysis_record_limit_exceeded")
            if expected is not None and expected != count:
                raise GatewayError("upstream_list_changed")
            expected = count
            if not items and len(records) != count:
                raise GatewayError("upstream_list_incomplete")
            for item in items:
                grade = self._grade(item)
                self._scope(grade, filters)
                if grade["status_id"] in seen:
                    raise GatewayError("upstream_list_incomplete")
                seen.add(grade["status_id"])
                records.append(grade)
            if len(records) == count:
                break
        # latest=true can still return different generated instances of a paper.
        # Select one record for each stable user ID and paper ID, never by name.
        selected: dict[tuple[int, int], dict] = {}
        for grade in records:
            key = (grade["paper_id"], grade["user_id"])
            previous = selected.get(key)
            if previous is None or (grade["time"], grade["status_id"]) > (previous["time"], previous["status_id"]):
                selected[key] = grade
        return list(selected.values()), len(records)

    @staticmethod
    def _summary(grades: list[dict], *, pass_score: float | None = None,
                 max_score: float | None = None) -> dict[str, Any]:
        points = [g["point"] for g in grades]
        passed = sum(p >= pass_score for p in points) if pass_score is not None else None
        return {"count": len(points), "min": min(points) if points else None,
                "max": max(points) if points else None, "mean": fmean(points) if points else None,
                "median": median(points) if points else None, "stddev": pstdev(points) if points else None,
                "pass_score": pass_score, "max_score": max_score, "pass_count": passed,
                "pass_rate_percent": passed / len(points) * 100 if points and passed is not None else None,
                "mean_percent": fmean(points) / max_score * 100 if points and max_score is not None else None,
                "distribution": [{"point": point, "count": count} for point, count in sorted(Counter(points).items())]}

    def _analyze(self, action: str, payload: dict, deadline: float) -> dict:
        filters = {key: payload[key] for key in ("paper_id", "category", "group") if key in payload}
        grades, source_count = self._collect(filters, deadline)
        common = {"complete": True, "collected_at": datetime.now(timezone.utc).isoformat(),
                  "source_record_count": source_count, "selected_record_count": len(grades),
                  "selection": "latest_per_student_paper", "filters": filters}
        if action == "student-analyze":
            by_paper = defaultdict(list)
            for grade in grades:
                by_paper[grade["paper_id"]].append(grade)
            papers = []
            for grade in sorted(grades, key=lambda g: (g["time"], g["status_id"])):
                if grade["user_id"] != payload["user_id"]:
                    continue
                peers = by_paper[grade["paper_id"]]
                papers.append({"grade": grade, "rank": 1 + sum(p["point"] > grade["point"] for p in peers),
                               "cohort": self._summary(peers)})
            return {**common, "user_id": payload["user_id"], "paper_count": len(papers), "papers": papers}
        by_group = defaultdict(list)
        for grade in grades:
            by_group[grade["group"]].append(grade)
        options = {key: payload[key] for key in ("pass_score", "max_score") if key in payload}
        students = []
        if payload.get("include_students", False):
            previous_point, rank = None, 0
            for index, grade in enumerate(sorted(grades, key=lambda g: (-g["point"], g["user_id"])), 1):
                if grade["point"] != previous_point:
                    rank = index
                previous_point = grade["point"]
                students.append({**grade, "rank": rank})
        return {**common, "paper_id": payload["paper_id"], "summary": self._summary(grades, **options),
                "by_group": [{"group": group, "summary": self._summary(items, **options)}
                             for group, items in sorted(by_group.items())],
                "students_included": payload.get("include_students", False), "students": students}

    def handle(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise GatewayError("parameters_invalid", 400)
        if path == "/v1/auth/start":
            return self.auth_start(payload)
        if path == "/v1/auth/browser":
            return self.browser(payload)
        if path == "/v1/auth/status":
            if payload:
                raise GatewayError("parameters_invalid", 400)
            return self.auth_status()
        action = ROUTES.get(path)
        if action is None:
            raise GatewayError("action_not_allowed", 404)
        if not self.validators[action].is_valid(payload):
            raise GatewayError("parameters_invalid", 400)
        for key in ("pass_score", "max_score"):
            if key in payload and not math.isfinite(payload[key]):
                raise GatewayError("parameters_invalid", 400)
        if "max_score" in payload and payload.get("pass_score", 0) > payload["max_score"]:
            raise GatewayError("parameters_invalid", 400)
        deadline = time.monotonic() + ANALYSIS_TIMEOUT
        if action in {"grades-analyze", "student-analyze"}:
            result = self._analyze(action, payload, deadline)
        elif action == "paper-statistics":
            result = self._get(f"/api/v2/papers/{payload['paper_id']}/statistics", {}, deadline)
            users, problems = result.get("users"), result.get("problems")
            if "users" not in result or "problems" not in result:
                raise GatewayError("upstream_response_invalid")
            if not isinstance(users, (list, type(None))) or not isinstance(problems, (list, type(None))):
                raise GatewayError("upstream_response_invalid")
            for user in users or []:
                if (not isinstance(user, dict) or not integer(user.get("point"))
                        or not all(isinstance(user.get(k), str) for k in ("name", "context"))):
                    raise GatewayError("upstream_response_invalid")
            for problem in problems or []:
                if (not isinstance(problem, dict) or not integer(problem.get("problem_id"))
                        or not all(isinstance(problem.get(k), str) for k in ("statement", "render_type", "config"))):
                    raise GatewayError("upstream_response_invalid")
            result = {"paper_id": payload["paper_id"], "users": [
                {key: u[key] for key in ("name", "context", "point")} for u in users or []], "problems": [
                {key: p[key] for key in ("problem_id", "statement", "render_type", "config")} for p in problems or []]}
        else:
            params = {key: value for key, value in payload.items() if key != "include_context"}
            params.setdefault("offset", 0)
            params.setdefault("limit", 20)
            upstream = "/api/v2/papers" if action == "papers-list" else "/api/v2/paper-statuses"
            if action == "grades-list":
                params.setdefault("all_user", True)
                params.setdefault("latest", True)
            items, count = self._page(upstream, params, deadline)
            if action == "papers-list":
                records = [self._paper(item) for item in items]
            else:
                records = [self._grade(item, include_context=payload.get("include_context", False)) for item in items]
                for grade in records:
                    self._scope(grade, params)
            next_offset = params["offset"] + len(items)
            if not items and next_offset < count:
                raise GatewayError("upstream_list_incomplete")
            result = {"items": records, "count": count, "offset": params["offset"],
                      "next_offset": next_offset if next_offset < count else None}
        return {"action": action.replace("-", "_"), "result": result}


class Handler(BaseHTTPRequestHandler):
    gateway: CS101Gateway

    def _reply(self, status: int, value: Any):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        self._reply(200, {"status": "ok"}) if self.path == "/healthz" else self._reply(404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_BODY:
                raise GatewayError("request_too_large", 413)
            payload = json.loads(self.rfile.read(length) or b"{}", parse_constant=invalid_json_constant)
            if not isinstance(payload, dict):
                raise GatewayError("parameters_invalid", 400)
            self._reply(200, self.gateway.handle(self.path, payload))
        except GatewayError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (ValueError, UnicodeError):
            self._reply(400, {"error": "request_json_invalid"})
        except Exception:
            self._reply(500, {"error": "gateway_error"})

    def log_message(self, _format, *_args):
        return


def main():
    gateway = CS101Gateway(
        instance_path=Path(os.getenv("CS101_INSTANCE_PATH", "/run/loom/instance.json")),
        credential_root=Path(os.getenv("CS101_CREDENTIAL_ROOT", "/run/loom/credentials")),
        state_root=Path(os.getenv("CS101_STATE_ROOT", "/var/lib/loom/service")),
    )
    Handler.gateway = gateway
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        gateway.close()


if __name__ == "__main__":
    main()
