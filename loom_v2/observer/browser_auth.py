"""Short-lived browser bindings. Authorization codes are forwarded, never stored."""

import asyncio
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from loom_v2.contracts.browser_auth import BrowserAuthMessage
from loom_v2.contracts.types import CapabilityHealthReport, ResourceRef


class BrowserAuth:
    path = "/api/v1/auth/browser"
    cookie = "loom_browser_auth"
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.sessions = {}
        self.lock = asyncio.Lock()
        app.add_api_route("/login", self.tunnel_open, methods=["GET"])
        app.add_api_route("/login-callback", self.callback, methods=["GET"])
        app.add_api_route(self.path + "/result", self.result, methods=["GET"])

    def require_user(self, request):
        if not self.settings.browser_tunnel_target:
            raise HTTPException(503, "browser_tunnel_not_configured")

    @staticmethod
    def require_local_origin(request):
        # This is origin/Host validation, not user authorization. SSH access
        # plus the short-lived session obtained from auth/start authorize use.
        if request.headers.get("host") != "localhost:3000":
            raise HTTPException(403, "browser_localhost_required")
        if request.headers.get("origin") not in {None, "http://localhost:3000"}:
            raise HTTPException(403, "browser_origin_invalid")

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
        body = {"package_ref": ref.model_dump(mode="json"),
                "target_slave": session["target_slave"], **args}
        if action == "start":
            body["approved_digest"] = package.package_digest
        if action == "start":
            drivers = await self.app.state.repo.list_agents(
                self.settings.workspace_id, role="driver"
            )
            driver = next(
                (item for item in drivers if item.get("lease_state") == "active"),
                None,
            )
            if driver is None:
                raise RuntimeError("driver_unavailable")
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
                }
            )
            # Slave's Worker envelope is intentionally small.  Keep the
            # BrowserAuth contract independent of that transport envelope.
            report = value.get("health_report")
            if not isinstance(report, dict):
                raise RuntimeError("browser_auth_start_failed")
            await self.app.state.repo.record_capability_health(
                CapabilityHealthReport.model_validate(report)
            )
            return {"health_reports": [report]}
        drivers = await self.app.state.repo.list_agents(
            self.settings.workspace_id, role="driver"
        )
        driver = next(
            (item for item in drivers if item.get("lease_state") == "active"),
            None,
        )
        if driver is None:
            raise RuntimeError("driver_unavailable")
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
            }
        )

    async def start(self, request, package, targets, instance_name):
        if not self.settings.browser_tunnel_target:
            raise HTTPException(503, "browser_tunnel_not_configured")
        if len(targets) != 1:
            raise HTTPException(400, "browser_auth_single_target_required")
        activations = await self.app.state.repo.list_package_activations(package)
        activation = next((a for a in activations if a.target_slave == targets[0]
                           and a.instance_name == instance_name), None)
        if activation is None:
            raise HTTPException(409, "capability_activation_not_running")
        session = dict(package_ref=package.version_ref, package_digest=package.package_digest,
                       target_slave=targets[0], instance_name=instance_name,
                       activation_revision=activation.activation_revision,
                       expires_at=time.time() + 300, bound=None, phase="created")
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
        return {"package": package.model_dump(mode="json"), **result, "browser_auth": [
            {"target_slave": targets[0], "instance_name": instance_name, "verification_uri": uri,
             "qr_payload": uri, "tunnel_target": self.settings.browser_tunnel_target,
             "tunnel_command": f"ssh -N -L 3000:{self.settings.browser_tunnel_target} <observer-host>",
             "status": "auth_required", "expires_at": session["expires_at"]}]}

    async def open(self, session_id: str, request: Request) -> Response:
        async with self.lock:
            self.prune()
            session = self.sessions.get(session_id)
            if session is None:
                raise HTTPException(404, "browser_session_not_found")
            binding = request.cookies.get(self.cookie)
            if binding and any(s["bound"] == binding for s in self.sessions.values()):
                raise HTTPException(409, "browser_auth_in_progress")
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
            response = RedirectResponse(location, status_code=302, headers=self.headers)
            # The registered opkssh redirect is localhost HTTP. The SSH tunnel
            # is the transport boundary; Secure would prevent the browser from
            # returning this binding cookie over localhost.
            response.set_cookie(self.cookie, session["bound"], secure=False, httponly=True,
                                samesite="lax", path="/",
                                max_age=max(1, int(session["expires_at"] - time.time())))
            return response

    async def tunnel_open(self, request: Request) -> Response:
        self.require_local_origin(request)
        session_id = request.query_params.get("session_id")
        if not session_id:
            raise HTTPException(400, "browser_session_required")
        return await self.open(session_id, request)

    async def callback(self, request: Request) -> Response:
        self.require_local_origin(request)
        if len(request.scope.get("query_string", b"")) > 65536:
            raise HTTPException(400, "browser_callback_invalid")
        try:
            query = {}
            for key, value in request.query_params.multi_items():
                query.setdefault(key, []).append(value)
            message = BrowserAuthMessage(action="callback", session_id="x" * 32, query=query)
        except ValueError as exc:
            raise HTTPException(400, "browser_callback_invalid") from exc
        async with self.lock:
            self.prune()
            binding = request.cookies.get(self.cookie)
            key = next((k for k, s in self.sessions.items()
                        if binding and s["bound"] == binding and s["phase"] == "bound"), None)
            if key is None:
                raise HTTPException(400, "browser_session_not_found")
            # Consume before forwarding, including when the response is lost.
            session = self.sessions.pop(key)
            message.session_id = key
            try:
                result = await self.send(session, "callback", message.model_dump(mode="json"))
                if result != {"status": "accepted"}:
                    raise RuntimeError("browser_callback_rejected")
            except (ValueError, RuntimeError, KeyError) as exc:
                raise HTTPException(409, "browser_callback_failed") from exc
        response = RedirectResponse(self.path + "/result", status_code=303, headers=self.headers)
        response.delete_cookie(self.cookie, path="/", secure=False, httponly=True, samesite="lax")
        return response

    async def result(self) -> Response:
        return Response("Authentication callback received. Check instance status in Loom.",
                        media_type="text/plain", headers=self.headers)
