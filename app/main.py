"""FastAPI application: fault-injection control plane and diagnosis API."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from .config import SERVICES, Settings, load_settings
from .coordinator import Coordinator, VersionConflictError
from .models import (
    BatchAccepted,
    BatchList,
    BatchSummary,
    DiagnosisBatch,
    ErrorResponse,
    FaultPatch,
    FaultSetResponse,
    HealthResponse,
    ResetFaultsResponse,
    StateResponse,
    TopologyResponse,
)
from .store import Store

API_DESCRIPTION = """
Locate which layer caused an entry-service outage: **gateway (entry)**,
**auth**, **order**, or the **shared cache**.

* `gateway` depends on `auth` and `order`; both depend on `cache`.
* Every simulated service exposes `GET /health/live` and `GET /health/ready`
  (local readiness + its own dependency checks) plus a `PUT /_fault` admin API.
* `PUT /faults/{service}` sets a local fault and **increments the global state
  version**; every service is synchronised to that version before probing.
* `POST /diagnosis` probes all four services concurrently and reports the
  failed source, services that failed only because of their dependencies
  (collateral), and the evidence. A cache fault that breaks auth, order and the
  gateway is counted as **one** independent fault.
* Mixed/foreign `state_version` values, changed configuration during a probe,
  or any probe timeout make the batch `needs_retest` — never a confident root
  cause.
* Batches are persisted in SQLite: completed results survive a restart,
  unfinished ones are reported as `needs_retest`. A late response from an old
  batch is flagged `superseded` and never overrides the newest result.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.database_path, settings.max_stored_batches)
    coordinator = Coordinator(settings, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await coordinator.recover_after_restart()
        coordinator.start_watcher()
        try:
            yield
        finally:
            await coordinator.stop_watcher()
            await coordinator.close()

    app = FastAPI(
        title="Outage Fault-Diagnosis API",
        version="1.0.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "diagnosis", "description": "Run diagnoses and read results"},
            {"name": "faults", "description": "Inject and clear local faults"},
            {"name": "state", "description": "State version, topology and service state"},
            {"name": "health", "description": "Liveness of this API"},
        ],
    )
    app.state.settings = settings
    app.state.store = store
    app.state.coordinator = coordinator

    # -- health --------------------------------------------------------
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
        summary="Liveness of the diagnosis API",
    )
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "state_version": request.app.state.coordinator.state_version,
        }

    # -- state / topology ---------------------------------------------
    @app.get(
        "/state",
        response_model=StateResponse,
        tags=["state"],
        summary="Global state version plus each service's adopted state",
    )
    async def get_state(request: Request) -> dict[str, Any]:
        coord: Coordinator = request.app.state.coordinator
        states = await coord.service_states()
        return {
            "state_version": coord.state_version,
            "batch_seq": coord.batch_seq,
            "known_config_epochs": coord.snapshot_epochs(),
            "watcher_running": coord._watcher_task is not None,
            "watcher_interval_seconds": request.app.state.settings.watcher_interval_seconds,
            "service_states": states,
        }

    @app.get(
        "/topology",
        response_model=TopologyResponse,
        tags=["state"],
        summary="Dependency topology",
    )
    async def topology(request: Request) -> dict[str, Any]:
        return request.app.state.coordinator.topology()

    # -- faults --------------------------------------------------------
    @app.put(
        "/faults/{service}",
        response_model=FaultSetResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["faults"],
        summary="Set local faults and increment the global state version",
    )
    async def set_fault(
        service: str,
        body: FaultPatch,
        request: Request,
        x_expected_state_version: int | None = Header(
            default=None, alias="X-Expected-State-Version"
        ),
        expected_state_version: int | None = Query(default=None),
    ) -> dict[str, Any]:
        if service not in SERVICES:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "unknown_service",
                    "service": service,
                    "known_services": list(SERVICES),
                },
            )
        expected = (
            x_expected_state_version
            if x_expected_state_version is not None
            else expected_state_version
        )
        patch = body.model_dump(exclude_none=True)
        try:
            return await request.app.state.coordinator.set_fault(
                service, patch, expected_version=expected
            )
        except VersionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "state_version_conflict",
                    "expected": exc.expected,
                    "current": exc.current,
                    "hint": "read GET /state, then retry with the current version",
                },
            ) from exc

    @app.post(
        "/faults/reset",
        response_model=ResetFaultsResponse,
        responses={409: {"model": ErrorResponse}},
        tags=["faults"],
        summary="Clear all local faults and increment the global state version",
    )
    async def reset_faults(
        request: Request,
        x_expected_state_version: int | None = Header(
            default=None, alias="X-Expected-State-Version"
        ),
        expected_state_version: int | None = Query(default=None),
    ) -> dict[str, Any]:
        expected = (
            x_expected_state_version
            if x_expected_state_version is not None
            else expected_state_version
        )
        try:
            return await request.app.state.coordinator.reset_faults(
                expected_version=expected
            )
        except VersionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "state_version_conflict",
                    "expected": exc.expected,
                    "current": exc.current,
                    "hint": "read GET /state, then retry with the current version",
                },
            ) from exc

    # -- diagnosis -----------------------------------------------------
    @app.post(
        "/diagnosis",
        response_model=None,
        status_code=status.HTTP_201_CREATED,
        tags=["diagnosis"],
        summary="Probe all services concurrently and diagnose the failure source",
        responses={
            201: {
                "model": DiagnosisBatch,
                "description": "Batch finished within the wait budget.",
            },
            202: {
                "model": BatchAccepted,
                "description": (
                    "wait=false, or the batch is still running. Poll "
                    "GET /diagnosis/{batch_id} for the verdict."
                ),
            },
        },
    )
    async def run_diagnosis(
        request: Request,
        wait: bool = Query(
            default=True,
            description=(
                "Wait for the verdict (bounded). If the batch is still running when "
                "the wait budget expires, the response is HTTP 202 with the batch id."
            ),
        ),
        wait_seconds: float | None = Query(
            default=None,
            ge=0.1,
            le=60.0,
            description="Override the wait budget for wait=true.",
        ),
    ) -> Any:
        coord: Coordinator = request.app.state.coordinator
        created = await coord.create_batch()
        accepted = BatchAccepted(**created)
        if not wait:
            return JSONResponse(
                content=accepted.model_dump(mode="json"),
                status_code=status.HTTP_202_ACCEPTED,
            )
        budget = (
            wait_seconds
            if wait_seconds is not None
            else request.app.state.settings.probe_timeout_seconds + 5.0
        )
        record = await _wait_for_batch(coord, created["batch_id"], budget)
        if record is None or record.get("status") == "running":
            # Not an error: the caller polls GET /diagnosis/{batch_id}.
            return JSONResponse(
                content=accepted.model_dump(mode="json"),
                status_code=status.HTTP_202_ACCEPTED,
            )
        return JSONResponse(
            content=DiagnosisBatch.model_validate(record).model_dump(mode="json"),
            status_code=status.HTTP_201_CREATED,
        )

    @app.get(
        "/diagnosis/latest",
        response_model=DiagnosisBatch,
        responses={404: {"model": ErrorResponse}},
        tags=["diagnosis"],
        summary="Newest diagnosis batch (including unfinished ones)",
    )
    async def latest_diagnosis(
        request: Request,
        completed_only: bool = Query(
            default=False,
            description=(
                "When true, return only batches that finished with a verdict, "
                "skipping running and interrupted ones."
            ),
        ),
    ) -> dict[str, Any]:
        record = request.app.state.coordinator.latest_batch(completed_only=completed_only)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "no_batches", "hint": "POST /diagnosis first"},
            )
        return record

    @app.get(
        "/diagnosis",
        response_model=BatchList,
        tags=["diagnosis"],
        summary="Recent diagnosis batches, newest first",
    )
    async def list_diagnosis(
        request: Request,
        limit: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        records = request.app.state.coordinator.list_batches(limit)
        return {
            "batches": [
                BatchSummary(
                    batch_id=r["batch_id"],
                    seq=r["seq"],
                    state_version=r["state_version"],
                    status=r["status"],
                    outcome=r.get("outcome"),
                    summary=r.get("summary"),
                    created_at=r["created_at"],
                    completed_at=r.get("completed_at"),
                )
                for r in records
            ]
        }

    @app.get(
        "/diagnosis/{batch_id}",
        response_model=DiagnosisBatch,
        responses={404: {"model": ErrorResponse}},
        tags=["diagnosis"],
        summary="One diagnosis batch by id",
    )
    async def get_diagnosis(batch_id: str, request: Request) -> dict[str, Any]:
        record = request.app.state.coordinator.get_batch(batch_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "batch_not_found", "batch_id": batch_id},
            )
        return record

    return app


async def _wait_for_batch(
    coordinator: Coordinator, batch_id: str, budget: float
) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        record = coordinator.get_batch(batch_id)
        if record is not None and record.get("status") != "running":
            return record
        await asyncio.sleep(0.02)
    return coordinator.get_batch(batch_id)


app = create_app()


__all__ = ["app", "create_app", "_wait_for_batch"]
