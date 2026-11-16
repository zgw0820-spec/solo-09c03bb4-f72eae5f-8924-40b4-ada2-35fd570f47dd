"""HTTP client for the simulated services: admin calls and probe calls."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import ROLES, SERVICES, Settings


class ServiceCallError(RuntimeError):
    """The simulated service rejected or failed an admin operation."""


def _latency_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def _evidence(
    *,
    kind: str,
    ok: bool,
    http_status: int | None = None,
    latency_ms: float = 0.0,
    timed_out: bool = False,
    unreachable: bool = False,
    reason: str | None = None,
    detail: str | None = None,
    state_version: int | None = None,
    config_epoch: int | None = None,
    local_ready_ok: bool | None = None,
    blocking_dependencies: list[str] | None = None,
    timed_out_dependencies: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ok": ok,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "timed_out": timed_out,
        "unreachable": unreachable,
        "reason": reason,
        "detail": detail,
        "state_version": state_version,
        "config_epoch": config_epoch,
        "local_ready_ok": local_ready_ok,
        "blocking_dependencies": blocking_dependencies or [],
        "timed_out_dependencies": timed_out_dependencies or [],
    }


class ServiceClient:
    """Talks to the four simulated services on behalf of the diagnosis API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.urls = settings.service_urls

    # -- helpers -------------------------------------------------------
    @property
    def _admin_headers(self) -> dict[str, str]:
        if self.settings.admin_token:
            return {"X-Admin-Token": self.settings.admin_token}
        return {}

    def _timeout(self, seconds: float) -> httpx.Timeout:
        return httpx.Timeout(seconds)

    async def _admin_request(
        self,
        method: str,
        service: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.urls[service]}{path}"
        try:
            response = await self.client.request(
                method,
                url,
                json=json,
                headers=self._admin_headers,
                timeout=self._timeout(self.settings.admin_timeout_seconds),
            )
        except httpx.HTTPError as exc:
            raise ServiceCallError(
                f"{method} {url} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ServiceCallError(
                f"{method} {url} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ServiceCallError(f"{method} {url} returned non-JSON body") from exc
        if not isinstance(body, dict):
            raise ServiceCallError(f"{method} {url} returned a non-object body")
        return body

    # -- fault administration -----------------------------------------
    async def set_fault(self, service: str, patch: dict[str, Any]) -> dict[str, Any]:
        return await self._admin_request("PUT", service, "/_fault", json=patch)

    async def reset_fault(self, service: str) -> dict[str, Any]:
        return await self._admin_request("POST", service, "/_fault/reset")

    async def sync_version(self, service: str, state_version: int) -> dict[str, Any]:
        """Set the service's adopted global version; returns a MockState dict."""
        try:
            body = await self._admin_request(
                "POST", service, "/_fault/sync", json={"state_version": state_version}
            )
        except ServiceCallError as exc:
            return {
                "service": service,
                "role": ROLES[service],
                "synced": False,
                "reachable": False,
                "error": str(exc),
                **_default_state_fields(),
            }
        return {
            "service": service,
            "role": ROLES[service],
            "synced": True,
            "reachable": True,
            "error": None,
            **body,
        }

    async def get_fault_state(self, service: str) -> dict[str, Any]:
        try:
            body = await self._admin_request("GET", service, "/_fault")
        except ServiceCallError as exc:
            return {
                "service": service,
                "role": ROLES[service],
                "reachable": False,
                "error": str(exc),
                **_default_state_fields(),
            }
        return {
            "service": service,
            "role": ROLES[service],
            "reachable": True,
            "error": None,
            **body,
        }

    async def snapshot_states(self) -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(*(self.get_fault_state(s) for s in SERVICES))
        )

    async def sync_all(self, state_version: int) -> list[dict[str, Any]]:
        return list(
            await asyncio.gather(*(self.sync_version(s, state_version) for s in SERVICES))
        )

    async def reset_all(self) -> list[dict[str, Any]]:
        async def one(service: str) -> dict[str, Any]:
            try:
                await self.reset_fault(service)
            except ServiceCallError as exc:
                return {"service": service, "reset": False, "error": str(exc)}
            return {"service": service, "reset": True, "error": None}

        return list(await asyncio.gather(*(one(s) for s in SERVICES)))

    # -- probe ---------------------------------------------------------
    async def probe_endpoint(self, service: str, endpoint: str) -> dict[str, Any]:
        """Probe one endpoint. Never raises; transport problems become evidence."""
        url = f"{self.urls[service]}/health/{endpoint}"
        started = time.perf_counter()
        try:
            response = await self.client.get(
                url, timeout=self._timeout(self.settings.probe_timeout_seconds)
            )
        except httpx.TimeoutException as exc:
            return _evidence(
                kind="timeout",
                ok=False,
                latency_ms=_latency_ms(started),
                timed_out=True,
                detail=f"{type(exc).__name__}: {exc}",
            )
        except httpx.HTTPError as exc:
            return _evidence(
                kind="unreachable",
                ok=False,
                latency_ms=_latency_ms(started),
                unreachable=True,
                detail=f"{type(exc).__name__}: {exc}",
            )

        latency = _latency_ms(started)
        try:
            body = response.json()
        except ValueError:
            return _evidence(
                kind="invalid_response",
                ok=False,
                http_status=response.status_code,
                latency_ms=latency,
                detail="response body is not valid JSON",
            )
        if not isinstance(body, dict):
            return _evidence(
                kind="invalid_response",
                ok=False,
                http_status=response.status_code,
                latency_ms=latency,
                detail="response body is not a JSON object",
            )

        state_version = body.get("state_version")
        if not isinstance(state_version, int) or isinstance(state_version, bool):
            state_version = None
        config_epoch = body.get("config_epoch")
        if not isinstance(config_epoch, int) or isinstance(config_epoch, bool):
            config_epoch = None

        if endpoint == "live":
            ok = response.status_code == 200 and body.get("status") == "alive"
            return _evidence(
                kind="ok" if ok else "http_error",
                ok=ok,
                http_status=response.status_code,
                latency_ms=latency,
                reason=body.get("reason") or body.get("status"),
                state_version=state_version,
                config_epoch=config_epoch,
            )

        blocking = body.get("blocking_dependencies")
        if not isinstance(blocking, list):
            blocking = []
        blocking = [str(dep) for dep in blocking]
        timed_out_deps = body.get("timed_out_dependencies")
        if not isinstance(timed_out_deps, list):
            timed_out_deps = []
        timed_out_deps = [str(dep) for dep in timed_out_deps]
        local_ready_ok = body.get("local_ready_ok")
        if not isinstance(local_ready_ok, bool):
            local_ready_ok = None
        ok = response.status_code == 200 and body.get("status") == "ready"
        return _evidence(
            kind="ok" if ok else "http_error",
            ok=ok,
            http_status=response.status_code,
            latency_ms=latency,
            reason=body.get("reason") or body.get("status"),
            state_version=state_version,
            config_epoch=config_epoch,
            local_ready_ok=local_ready_ok,
            blocking_dependencies=blocking,
            timed_out_dependencies=timed_out_deps,
        )

    async def probe_service(self, service: str) -> dict[str, Any]:
        live, ready = await asyncio.gather(
            self.probe_endpoint(service, "live"),
            self.probe_endpoint(service, "ready"),
        )
        versions = [ev["state_version"] for ev in (live, ready) if ev["state_version"] is not None]
        epochs = [ev["config_epoch"] for ev in (live, ready) if ev["config_epoch"] is not None]
        if live["ok"] and ready["ok"]:
            failure_kind = None
        elif not live["ok"]:
            failure_kind = "liveness"
        else:
            failure_kind = "readiness"
        return {
            "service": service,
            "role": ROLES[service],
            "live": live,
            "ready": ready,
            "live_ok": live["ok"],
            "ready_ok": ready["ok"],
            "failed": not (live["ok"] and ready["ok"]),
            "state_version": versions[0] if versions else None,
            "config_epoch": epochs[0] if epochs else None,
            "blocking_dependencies": ready["blocking_dependencies"],
            "timed_out_dependencies": ready["timed_out_dependencies"],
            "local_ready_ok": ready["local_ready_ok"],
            "failure_kind": failure_kind,
        }

    async def probe_all(self) -> dict[str, dict[str, Any]]:
        """Probe all services concurrently, with a hard outer deadline.

        Each individual HTTP call already has a timeout, but the outer deadline
        also protects against a transport that ignores timeouts. Partial
        results are preserved: anything unfinished is reported as a timeout.
        """
        tasks = {
            service: asyncio.create_task(self.probe_service(service))
            for service in SERVICES
        }
        outer_budget = self.settings.probe_timeout_seconds + 0.5
        done, pending = await asyncio.wait(
            tasks.values(), timeout=outer_budget
        )
        for task in pending:
            task.cancel()
        results: dict[str, dict[str, Any]] = {}
        for service, task in tasks.items():
            if task in pending:
                results[service] = _timed_out_service(service, outer_budget)
                continue
            if task.cancelled():
                results[service] = _timed_out_service(service, outer_budget)
                continue
            try:
                results[service] = task.result()
            except Exception as exc:  # defensive: never fail a whole batch
                results[service] = _unreachable_service(service, str(exc))
        return results


def _default_state_fields() -> dict[str, Any]:
    return {
        "live_ok": None,
        "ready_ok": None,
        "probe_delay_ms": None,
        "config_epoch": None,
        "state_version": None,
    }


def _timed_out_service(service: str, budget: float) -> dict[str, Any]:
    ev = _evidence(
        kind="timeout",
        ok=False,
        latency_ms=round(budget * 1000.0, 3),
        timed_out=True,
        detail="probe did not finish within the batch deadline",
    )
    return {
        "service": service,
        "role": ROLES[service],
        "live": dict(ev),
        "ready": dict(ev),
        "live_ok": False,
        "ready_ok": False,
        "failed": True,
        "state_version": None,
        "config_epoch": None,
        "blocking_dependencies": [],
        "timed_out_dependencies": [],
        "local_ready_ok": None,
        "failure_kind": None,
    }


def _unreachable_service(service: str, detail: str) -> dict[str, Any]:
    ev = _evidence(
        kind="unreachable",
        ok=False,
        unreachable=True,
        detail=detail,
    )
    return {
        "service": service,
        "role": ROLES[service],
        "live": dict(ev),
        "ready": dict(ev),
        "live_ok": False,
        "ready_ok": False,
        "failed": True,
        "state_version": None,
        "config_epoch": None,
        "blocking_dependencies": [],
        "timed_out_dependencies": [],
        "local_ready_ok": None,
        "failure_kind": None,
    }


__all__ = ["ServiceCallError", "ServiceClient"]
