"""The four simulated services.

Every simulated service exposes the same surface:

* ``GET /health/live``        - local process liveness, honouring ``live_ok``.
* ``GET /health/ready``       - local readiness (``ready_ok``) plus checks of its
                                own dependencies, honouring ``probe_delay_ms``.
* ``GET /_fault``             - current fault settings and this process's epochs.
* ``PUT /_fault``             - set ``live_ok`` / ``ready_ok`` / ``probe_delay_ms``.
* ``POST /_fault/reset``      - clear local faults.
* ``POST /_fault/sync``       - adopt a global state version (the diagnosis API
                                calls this after bumping the version).
* ``GET /admin/upstream``     - what this service currently sees of its deps.

Why this shape matters: a cache fault makes both auth and order readiness fail,
which in turn makes gateway readiness fail. The diagnosis API's reduction logic
(not these services) is responsible for reporting that as one root cause.

Every probe answer carries ``state_version`` and ``config_epoch`` so a stale or
unsynchronised answer can never quietly masquerade as current.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .config import DEPENDS_ON, ROLES, SERVICES, mock_settings_from_env


class FaultState:
    def __init__(self, initial_state_version: int = 1) -> None:
        self.live_ok = True
        self.ready_ok = True
        self.probe_delay_ms = 0
        self.config_epoch = 1
        # Global state version this process has adopted. The diagnosis API is
        # the only writer in normal operation (via POST /_fault/sync).
        self.state_version = initial_state_version
        self.lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "live_ok": self.live_ok,
            "ready_ok": self.ready_ok,
            "probe_delay_ms": self.probe_delay_ms,
            "config_epoch": self.config_epoch,
            "state_version": self.state_version,
        }


class FaultUpdate(BaseModel):
    live_ok: bool | None = None
    ready_ok: bool | None = None
    probe_delay_ms: int | None = Field(default=None, ge=0, le=120_000)


class SyncRequest(BaseModel):
    state_version: int = Field(ge=0)


class DependencyClient:
    """Readiness-only dependency checks performed by a simulated service."""

    def __init__(self, service_name: str, urls: dict[str, str], timeout: float) -> None:
        self.deps = DEPENDS_ON[service_name]
        self.urls = urls
        self.timeout = timeout

    async def check(self, client: httpx.AsyncClient) -> dict[str, Any]:
        if not self.deps:
            return {}
        tasks = [self._check_one(client, dep) for dep in self.deps]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, Any] = {}
        for dep, result in zip(self.deps, results):
            if isinstance(result, BaseException):  # defensive; _check_one catches
                out[dep] = {"reachable": False, "healthy": False, "error": str(result)}
            else:
                out[dep] = result
        return out

    async def _check_one(self, client: httpx.AsyncClient, dep: str) -> dict[str, Any]:
        url = f"{self.urls[dep]}/health/ready"
        try:
            response = await client.get(url, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            return {
                "reachable": True,
                "healthy": False,
                "timed_out": True,
                "error": f"dependency check timed out ({type(exc).__name__})",
            }
        except httpx.HTTPError as exc:
            return {
                "reachable": False,
                "healthy": False,
                "timed_out": False,
                "error": f"dependency unreachable: {type(exc).__name__}",
            }
        body: dict[str, Any] = {}
        try:
            body = response.json()
        except ValueError:
            pass
        healthy = response.status_code == 200 and body.get("status") == "ready"
        return {
            "reachable": True,
            "healthy": healthy,
            "timed_out": False,
            "http_status": response.status_code,
            "dependency_state": body.get("status"),
            "state_version": body.get("state_version"),
            "error": None if healthy else "dependency not ready",
        }


def _admin_guard(token: str) -> Callable[..., None]:
    def dependency(x_admin_token: str | None = Header(default=None)) -> None:
        if token and x_admin_token != token:
            raise HTTPException(status_code=401, detail="invalid admin token")

    return dependency


def create_mock_app(
    service_name: str,
    *,
    service_urls: dict[str, str] | None = None,
    initial_state_version: int = 1,
) -> FastAPI:
    if service_name not in SERVICES:
        raise ValueError(f"unknown service {service_name!r}")
    cfg = mock_settings_from_env(service_name, service_urls)
    state = FaultState(initial_state_version)
    dep_client = DependencyClient(
        service_name, cfg["service_urls"], cfg["dependency_timeout_seconds"]
    )
    http_client: dict[str, httpx.AsyncClient] = {}
    admin_guard = _admin_guard(cfg["admin_token"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http_client["client"] = httpx.AsyncClient()
        try:
            yield
        finally:
            await http_client["client"].aclose()

    app = FastAPI(
        title=f"Mock {service_name} service ({ROLES[service_name]})",
        version="1.0.0",
        description=(
            f"Simulated **{ROLES[service_name]}** service. Depends on: "
            f"{', '.join(DEPENDS_ON[service_name]) or 'nothing'}. "
            "Local faults are injected via `PUT /_fault`; in normal operation "
            "the diagnosis API's fault proxy does this and bumps the global "
            "state version for every service."
        ),
        lifespan=lifespan,
    )
    app.state.service_name = service_name
    app.state.fault_state = state

    async def _apply_probe_delay() -> int:
        delay = state.probe_delay_ms
        if delay:
            await asyncio.sleep(delay / 1000.0)
        return delay

    @app.get("/health/live", summary="Local liveness probe")
    async def health_live(response: Response) -> dict[str, Any]:
        started = time.perf_counter()
        delay = await _apply_probe_delay()
        snap = state.snapshot()
        if not state.live_ok:
            response.status_code = 503
            return {
                "status": "failed",
                "service": service_name,
                "state_version": snap["state_version"],
                "config_epoch": snap["config_epoch"],
                "injected_probe_delay_ms": delay,
                "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "reason": "local liveness fault injected",
            }
        return {
            "status": "alive",
            "service": service_name,
            "state_version": snap["state_version"],
            "config_epoch": snap["config_epoch"],
            "injected_probe_delay_ms": delay,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }

    @app.get("/health/ready", summary="Readiness probe including dependencies")
    async def health_ready(response: Response) -> dict[str, Any]:
        started = time.perf_counter()
        delay = await _apply_probe_delay()
        snap = state.snapshot()
        dependencies = await dep_client.check(http_client["client"])
        blocking = [dep for dep, res in dependencies.items() if not res.get("healthy")]
        timed_out_dependencies = [
            dep for dep, res in dependencies.items() if res.get("timed_out")
        ]
        healthy = state.live_ok and state.ready_ok and not blocking
        if not healthy:
            response.status_code = 503
        return {
            "status": "ready" if healthy else "not_ready",
            "service": service_name,
            "state_version": snap["state_version"],
            "config_epoch": snap["config_epoch"],
            "live_ok": state.live_ok,
            "local_ready_ok": state.ready_ok,
            "blocking_dependencies": blocking,
            "timed_out_dependencies": timed_out_dependencies,
            "dependencies": dependencies,
            "injected_probe_delay_ms": delay,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "reason": (
                None
                if healthy
                else "local liveness fault injected"
                if not state.live_ok
                else "local readiness fault injected"
                if not state.ready_ok
                else "dependency not ready"
            ),
        }

    # -- admin / fault surface -----------------------------------------
    @app.get("/_fault", summary="Read local fault state")
    async def get_fault(_: None = Depends(admin_guard)) -> dict[str, Any]:
        return {
            "service": service_name,
            "role": ROLES[service_name],
            "dependencies": list(DEPENDS_ON[service_name]),
            **state.snapshot(),
        }

    @app.put("/_fault", summary="Set local fault settings")
    async def put_fault(
        update: FaultUpdate, _: None = Depends(admin_guard)
    ) -> dict[str, Any]:
        async with state.lock:
            changed = False
            for attr in ("live_ok", "ready_ok", "probe_delay_ms"):
                value = getattr(update, attr)
                if value is not None and value != getattr(state, attr):
                    setattr(state, attr, value)
                    changed = True
            if changed:
                state.config_epoch += 1
            snap = state.snapshot()
        return {"service": service_name, "changed": changed, **snap}

    @app.post("/_fault/reset", summary="Clear local faults")
    async def reset_fault(_: None = Depends(admin_guard)) -> dict[str, Any]:
        async with state.lock:
            state.live_ok = True
            state.ready_ok = True
            state.probe_delay_ms = 0
            state.config_epoch += 1
            snap = state.snapshot()
        return {"service": service_name, **snap}

    @app.post("/_fault/sync", summary="Adopt a global state version")
    async def sync_fault(
        request: SyncRequest, _: None = Depends(admin_guard)
    ) -> dict[str, Any]:
        async with state.lock:
            state.state_version = request.state_version
            snap = state.snapshot()
        return {"service": service_name, **snap}

    @app.get("/admin/upstream", summary="What this service sees of its deps")
    async def upstream(_: None = Depends(admin_guard)) -> dict[str, Any]:
        dependencies = await dep_client.check(http_client["client"])
        return {
            "service": service_name,
            "state_version": state.state_version,
            "dependencies": dependencies,
        }

    @app.get("/admin/whoami", summary="Service identity and state")
    async def whoami() -> dict[str, Any]:
        return {
            "service": service_name,
            "role": ROLES[service_name],
            "dependencies": list(DEPENDS_ON[service_name]),
            "state": state.snapshot(),
        }

    return app


def build_mock_apps(service_urls: dict[str, str] | None = None) -> dict[str, FastAPI]:
    """All four mock apps in one process, used by tests and local runs."""
    return {
        name: create_mock_app(name, service_urls=service_urls) for name in SERVICES
    }


__all__ = ["FaultState", "build_mock_apps", "create_mock_app"]
