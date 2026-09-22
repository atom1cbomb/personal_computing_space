"""Short-lived browser bindings and the scoped local-helper bridge."""

import asyncio
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from loom_v2.contracts.browser_auth import BrowserAuthMessage
from loom_v2.contracts.types import CapabilityHealthReport, ResourceRef


class BrowserAuth:
    path = "/api/v1/auth/browser"
    bridge_path = "/api/v1/auth/browser/bridge"
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.sessions = {}
        self.lock = asyncio.Lock()
        app.add_api_route(self.bridge_path, self.bridge, methods=["POST"])

    def prune(self):
        for key, value in list(self.sessions.items()):
            if value["expires_at"] <= time.time():
                self.sessions.pop(key)

    async def send(self, session, action, payload):
        ref = ResourceRef(resource_id=session["package_ref"], version_or_digest=session["package_digest"])
        package = await self.app.state.repo._assert_package_workspace(ref, self.settings.workspace_id)
        activations = await self.app.state.repo.list_package_activations(package)
        current = next((a for a in activations if a.target_slave == session["target_slave"]
                        and a.instance_name == session["instance_name"]), None)
        if (current is None or current.package_digest != session["package_digest"]
                or current.activation_revision != session["activation_revision"]
                or current.desired_state != "running"
                or current.activation_state in {"stopped", "lost", "not_installed"}):
            raise RuntimeError("capability_activation_revision_stale")
        args = dict(instance_name=session["instance_name"], activation_revision=session["activation_revision"])
        args["browser_auth" if action == "start" else "payload"] = payload
        if self.app.state.embedded:
            worker = self.app.state.workers.get(session["target_slave"])
            slave = self.app.state.slaves.get(session["target_slave"])
            method = "auth_start" if action == "start" else "auth_browser"
            if worker is not None:
                result = await getattr(worker, method)(package=package, target_slave=session["target_slave"],
                                                      workspace_id=self.settings.workspace_id, **args)
            elif slave is not None:
                result = await getattr(slave, method)(package, **args)
            else:
                raise RuntimeError("slave_not_found")
            if action == "start":
                await self.app.state.repo.record_capability_health(result)
                return {"health_reports": [result.model_dump(mode="json")]}
            return result
        drivers = await self.app.state.repo.list_agents(self.settings.workspace_id, role="driver")
        driver = next((item for item in drivers if item.get("lease_state") == "active"), None)
        if driver is None:
            raise RuntimeError("driver_unavailable")
        if action == "start":
            value = await self.app.state.slave_gateway.forward(
                session["target_slave"], "/worker/v1/capability/auth/start", {
                    "package": package.model_dump(mode="json"),
                    "target_slave": session["target_slave"],
                    "workspace_id": self.settings.workspace_id,
                    "instance_name": session["instance_name"],
                    "activation_revision": session["activation_revision"],
                    "browser_auth": payload,
                    "driver_id": str(driver["agent_id"]),
                    "driver_epoch": int(driver["epoch"]),
                })
            report = value.get("health_report")
            if not isinstance(report, dict):
                raise RuntimeError("browser_auth_start_failed")
            await self.app.state.repo.record_capability_health(CapabilityHealthReport.model_validate(report))
            return {"health_reports": [report]}
        return await self.app.state.slave_gateway.forward(
            session["target_slave"], "/worker/v1/capability/auth/browser", {
                "package": package.model_dump(mode="json"),
                "target_slave": session["target_slave"],
                "workspace_id": self.settings.workspace_id,
                "instance_name": session["instance_name"],
                "activation_revision": session["activation_revision"],
                "payload": payload,
                "driver_id": str(driver["agent_id"]),
                "driver_epoch": int(driver["epoch"]),
            })

    async def start(self, request, package, targets, instance_name):
        if len(targets) != 1:
            raise HTTPException(400, "browser_auth_single_target_required")
        activations = await self.app.state.repo.list_package_activations(package)
        activation = next((a for a in activations if a.target_slave == targets[0]
                           and a.instance_name == instance_name), None)
        if activation is None:
            raise HTTPException(409, "capability_activation_not_running")
        authentication = package.body.get("authentication") if isinstance(package.body, dict) else None
        if not isinstance(authentication, dict) or not isinstance(authentication.get("client"), dict):
            raise HTTPException(409, "browser_auth_client_not_declared")
        session = dict(package_ref=package.version_ref, package_digest=package.package_digest,
                       target_slave=targets[0], instance_name=instance_name,
                       activation_revision=activation.activation_revision,
                       expires_at=time.time() + 300, bound=None, phase="created",
                       bridge_token=secrets.token_urlsafe(32))
        async with self.lock:
            self.prune()
            for key, old in list(self.sessions.items()):
                if all(old[k] == session[k] for k in ("package_ref", "target_slave", "instance_name")):
                    self.sessions.pop(key)
            if len(self.sessions) >= 256:
                raise HTTPException(503, "browser_auth_capacity_exhausted")
            session_id = secrets.token_urlsafe(32)
            try:
                result = await self.send(session, "start", dict(session_id=session_id,
                    callback_uri="http://localhost:3000/login-callback", expires_at=session["expires_at"]))
                if not result.get("health_reports") or any(
                    r.get("activation_state") != "pending_auth" for r in result["health_reports"]
                ):
                    raise RuntimeError("browser_auth_start_failed")
            except (ValueError, RuntimeError, KeyError) as exc:
                raise HTTPException(409, "browser_auth_start_failed") from exc
            self.sessions[session_id] = session
        uri = "http://localhost:3000/login?session_id=" + session_id
        base_url = (self.settings.public_base_url or str(request.base_url)).rstrip("/")
        client = {**authentication["client"], "bridge_url": base_url + self.bridge_path,
                  "session_id": session_id, "session_token": session["bridge_token"]}
        return {"package": package.model_dump(mode="json"), **result, "browser_auth": [
            {"target_slave": targets[0], "instance_name": instance_name, "verification_uri": uri,
             "qr_payload": uri, "status": "auth_required", "expires_at": session["expires_at"],
             "client": client}]}

    async def _open_session(self, session_id: str) -> tuple[str, str]:
        async with self.lock:
            self.prune()
            session = self.sessions.get(session_id)
            if session is None:
                raise HTTPException(404, "browser_session_not_found")
            if session["phase"] != "created":
                raise HTTPException(409, "browser_session_already_open")
            session["phase"] = "opening"
            session["bound"] = secrets.token_urlsafe(32)
            try:
                value = await self.send(session, "open", {"action": "open", "session_id": session_id})
                location = value.get("location", "")
                parsed = urlsplit(location)
                if (value.get("status") != "redirect" or len(location) > 16384 or parsed.scheme != "https"
                        or not parsed.hostname or parsed.username or parsed.password
                        or any(ord(c) < 32 for c in location)):
                    raise ValueError("browser_auth_redirect_invalid")
            except (ValueError, RuntimeError, KeyError) as exc:
                self.sessions.pop(session_id, None)
                raise HTTPException(409, "browser_auth_open_failed") from exc
            session["phase"] = "bound"
            return location, session["bound"]

    async def _callback_session(self, session_id: str, binding: str, query: dict[str, list[str]]) -> None:
        try:
            message = BrowserAuthMessage(action="callback", session_id="x" * 32, query=query)
        except ValueError as exc:
            raise HTTPException(400, "browser_callback_invalid") from exc
        async with self.lock:
            self.prune()
            session = self.sessions.get(session_id)
            if (session is None or not secrets.compare_digest(binding, session.get("bound") or "")
                    or session["phase"] != "bound"):
                raise HTTPException(400, "browser_session_not_found")
            session = self.sessions.pop(session_id)
            message.session_id = session_id
            try:
                result = await self.send(session, "callback", message.model_dump(mode="json"))
                if result != {"status": "accepted"}:
                    raise RuntimeError("browser_callback_rejected")
            except (ValueError, RuntimeError, KeyError) as exc:
                raise HTTPException(409, "browser_callback_failed") from exc

    async def bridge(self, request: Request) -> JSONResponse:
        """Handle only the fixed open/callback messages from the local helper."""
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "browser_bridge_request_invalid") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "browser_bridge_request_invalid")
        action, session_id = payload.get("action"), payload.get("session_id")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        binding, query = payload.get("browser_binding"), payload.get("query", {})
        if (action not in {"open", "callback"} or not isinstance(session_id, str)
                or not token or not isinstance(binding, (str, type(None)))
                or not isinstance(query, dict)):
            raise HTTPException(400, "browser_bridge_request_invalid")
        async with self.lock:
            self.prune()
            session = self.sessions.get(session_id)
            if session is None or not secrets.compare_digest(token, session["bridge_token"]):
                raise HTTPException(404, "browser_session_not_found")
        if action == "open":
            if binding is not None:
                raise HTTPException(409, "browser_session_already_open")
            location, browser_binding = await self._open_session(session_id)
            return JSONResponse({"status": "redirect", "location": location,
                                 "browser_binding": browser_binding}, headers=self.headers)
        if binding is None:
            raise HTTPException(400, "browser_session_binding_required")
        await self._callback_session(session_id, binding, query)
        return JSONResponse({"status": "accepted"}, headers=self.headers)
