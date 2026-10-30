"""Overleaf's authenticated web endpoints; Git tokens are never sent here."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .common import ORIGIN, OverleafError, fingerprint, unpack

COOKIE = "overleaf.sid"
PROJECT_ID = re.compile(r"[0-9a-f]{24}")


def valid_cookie(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}', value))


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "ol-csrfToken":
            self.token = values.get("content")


class WebSession:
    def __init__(self, root: Path, journal, *, transport=None):
        self.path = root / "web-session.json"
        self.journal = journal
        self.lock = threading.RLock()
        self.cookie, self.challenge = None, None
        self.imports = set()
        self.client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)
        try:
            saved = json.loads(self.path.read_text())
            if saved.get("origin") == ORIGIN and valid_cookie(saved.get("cookie")):
                self.cookie = saved["cookie"]
        except (FileNotFoundError, ValueError, AttributeError):
            pass

    def close(self):
        self.client.close()

    def persist(self, cookie):
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            json.dump({"origin": ORIGIN, "cookie": cookie}, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)
        self.cookie = cookie

    def request(self, method, path, *, cookie=None, headers=None, **kwargs):
        with self.lock:
            current = cookie or self.cookie
            if not current or (cookie is None and self.challenge):
                raise OverleafError("web_authentication_required", 401)
        request = httpx.Request(method, ORIGIN + path,
            headers={"Accept": "application/json", "Cookie": f"{COOKIE}={current}", **(headers or {})},
            extensions={"timeout": httpx.Timeout(120 if method == "POST" else 10).as_dict()}, **kwargs)
        try:
            response = self.client.send(request, stream=True)
            try:
                redirect = urlsplit(urljoin(ORIGIN, response.headers.get("location", "")))
                login_redirect = response.is_redirect and redirect.scheme == "https" and redirect.netloc == "overleaf.cs.ac.cn" and redirect.path == "/login"
                if response.status_code == 401 or login_redirect:
                    raise OverleafError("web_authentication_required", 401)
                if response.status_code == 403:
                    raise OverleafError("web_forbidden", 403)
                if not 200 <= response.status_code < 300:
                    raise OverleafError("web_request_failed", response.status_code if response.status_code in (400, 413, 422, 429) else 502)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 8 * 1024 * 1024:
                        raise OverleafError("web_response_too_large", 502)
                updated = current
                for candidate in response.cookies.jar:
                    if (candidate.name == COOKIE and candidate.path == "/"
                            and (not candidate.domain_specified or candidate.domain.lstrip(".") == "overleaf.cs.ac.cn")
                            and valid_cookie(candidate.value)):
                        updated = candidate.value
                with self.lock:
                    if self.cookie == current and not self.challenge and updated != current:
                        self.persist(updated)
                return bytes(body), updated
            finally:
                response.close()
        except httpx.HTTPError as exc:
            raise OverleafError("web_unreachable", 502) from exc

    def projects(self, *, cookie=None):
        raw, updated = self.request("GET", "/user/projects", cookie=cookie)
        try:
            value = json.loads(raw)
            rows = value["projects"]
            if not isinstance(rows, list):
                raise ValueError()
            projects = []
            for row in rows:
                identity, name = row["_id"], row["name"]
                if not isinstance(identity, str) or not PROJECT_ID.fullmatch(identity) or not isinstance(name, str):
                    raise ValueError()
                project = {"project_id": identity, "name": name, "url": f"{ORIGIN}/project/{identity}"}
                if isinstance(row.get("accessLevel"), str):
                    project["access_level"] = row["accessLevel"]
                projects.append(project)
            return projects, updated
        except (ValueError, TypeError, KeyError) as exc:
            raise OverleafError("web_response_invalid", 502) from exc

    def list_projects(self, args):
        rows, _ = self.projects()
        rows = sorted((r for r in rows if args.get("query", "").casefold() in r["name"].casefold()), key=lambda r: r["project_id"])
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        return {"projects": rows[offset:offset+limit], "total": len(rows),
                "next_offset": offset+limit if offset+limit < len(rows) else None}

    def auth_status(self):
        with self.lock:
            if self.challenge and self.challenge["expires_at"] <= time.time():
                self.challenge = None
            if self.challenge or not self.cookie:
                return {"status": "auth_required", "poll_after_seconds": 2}
        try:
            self.projects()
            return {"status": "authenticated"}
        except OverleafError as exc:
            return {"status": "auth_required", "poll_after_seconds": 2} if exc.code == "web_authentication_required" else {"status": "auth_failed"}

    def auth_start(self, args):
        if not args:
            return self.auth_status()
        sid, expiry = args.get("session_id"), args.get("expires_at")
        if (set(args) != {"session_id", "callback_uri", "expires_at"}
                or not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", sid)
                or args["callback_uri"] != "http://localhost:3000/login-callback"
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not time.time() < expiry <= time.time()+310):
            raise OverleafError("browser_context_invalid")
        with self.lock:
            self.path.unlink(missing_ok=True)
            self.cookie = None
            self.challenge = {**args, "phase": "created"}
        return {"status": "auth_required", "poll_after_seconds": 2}

    def browser(self, args):
        action, query = args.get("action"), args.get("query", {})
        if (set(args) - {"action", "session_id", "query"} or action not in ("open", "callback")
                or not isinstance(query, dict) or action == "open" and query):
            raise OverleafError("browser_request_invalid")
        with self.lock:
            session = self.challenge
            if not session or session["session_id"] != args.get("session_id"):
                raise OverleafError("browser_session_not_found", 404)
            if session["expires_at"] <= time.time():
                self.challenge = None
                raise OverleafError("browser_session_expired", 409)
            if session["phase"] != ("created" if action == "open" else "bound"):
                raise OverleafError("browser_session_consumed", 409)
            if action == "open":
                session["phase"] = "bound"
                return {"status": "redirect", "location": ORIGIN + "/login"}
            values = query.get(COOKIE)
            if set(query) != {COOKIE} or not isinstance(values, list) or len(values) != 1 or not valid_cookie(values[0]):
                raise OverleafError("browser_callback_invalid")
            try:
                _, updated = self.projects(cookie=values[0])
                self.persist(updated)
            except OverleafError as exc:
                if exc.code == "web_authentication_required":
                    return {"status": "pending"}
                self.challenge = None
                raise OverleafError("browser_login_verification_failed", 409) from None
            self.challenge = None
            return {"status": "accepted"}

    def import_project(self, args, data):
        if not unpack(data):
            raise OverleafError("empty_project")
        key, fp = "import:" + args["idempotency_key"], fingerprint(args)
        with self.journal.lock:
            saved = self.journal.get(key, fp)
            if saved:
                if key in self.imports:
                    return {"status": "pending"}
                return saved.get("result", {"status": "error", "error_code": "import_outcome_unknown"})
        # Safe reads happen before the durable sending marker.
        with self.lock:
            if not self.cookie or self.challenge:
                raise OverleafError("web_authentication_required", 401)
            cookie = self.cookie
        _, cookie = self.projects(cookie=cookie)
        page, cookie = self.request("GET", "/project", cookie=cookie, headers={"Accept": "text/html"})
        parser = CSRFParser()
        parser.feed(page.decode("utf-8"))
        if not parser.token or len(parser.token) > 4096:
            raise OverleafError("csrf_token_unavailable", 502)
        with self.journal.lock:
            saved = self.journal.get(key, fp)
            if saved:
                return {"status": "pending"} if key in self.imports else saved.get("result", {"status": "error", "error_code": "import_outcome_unknown"})
            self.journal.put(key, fp, {"state": "sending"})
            self.imports.add(key)
        try:
            try:
                raw, _ = self.request("POST", "/project/new/upload", cookie=cookie,
                    headers={"X-CSRF-Token": parser.token, "Origin": ORIGIN, "Referer": ORIGIN + "/project"},
                    data={"name": args["name"]}, files={"qqfile": ("project.zip", data, "application/zip")})
                value = json.loads(raw)
                identity = value.get("project_id")
                if value.get("success") is not True or not isinstance(identity, str) or not PROJECT_ID.fullmatch(identity):
                    raise ValueError()
                result = {"status": "imported", "project_id": identity, "url": f"{ORIGIN}/project/{identity}"}
            except OverleafError as exc:
                # Auth/validation/rate-limit rejection precedes creation. Server
                # errors and transport failures can follow partial side effects.
                result = exc.result() if exc.status in (400, 401, 403, 413, 422, 429) else {"status": "error", "error_code": "import_outcome_unknown"}
            except (ValueError, TypeError, AttributeError):
                result = {"status": "error", "error_code": "import_outcome_unknown"}
            self.journal.put(key, fp, {"state": "done", "result": result})
            return result
        finally:
            with self.journal.lock:
                self.imports.discard(key)
