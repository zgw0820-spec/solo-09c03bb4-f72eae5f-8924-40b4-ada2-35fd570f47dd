"""Pydantic request/response models (drive the OpenAPI/Swagger schema)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ServiceName = Literal["gateway", "auth", "order", "cache"]
Outcome = Literal["healthy", "root_cause_found", "needs_retest"]
BatchStatus = Literal["running", "completed", "interrupted", "failed"]


class FaultPatch(BaseModel):
    """Mutable local fault settings for one simulated service."""

    live_ok: bool | None = Field(
        default=None, description="Whether the service's local liveness check passes."
    )
    ready_ok: bool | None = Field(
        default=None,
        description="Whether the service's local readiness check passes (ignoring dependencies).",
    )
    probe_delay_ms: int | None = Field(
        default=None,
        ge=0,
        le=120_000,
        description="Artificial delay injected into /health/live and /health/ready.",
    )

    @model_validator(mode="after")
    def _at_least_one(self) -> "FaultPatch":
        if self.live_ok is None and self.ready_ok is None and self.probe_delay_ms is None:
            raise ValueError("at least one of live_ok, ready_ok, probe_delay_ms is required")
        return self


class MockState(BaseModel):
    """Point-in-time state of one simulated service (``None`` = unreachable)."""

    service: str
    role: str
    dependencies: list[str] = Field(default_factory=list)
    live_ok: bool | None = None
    ready_ok: bool | None = None
    probe_delay_ms: int | None = None
    config_epoch: int | None = None
    state_version: int | None = None
    reachable: bool = True
    error: str | None = None


class FaultSetResponse(BaseModel):
    state_version: int
    previous_state_version: int
    service: str
    applied: bool
    all_versions_synced: bool
    service_states: list[MockState]
    warnings: list[str] = Field(default_factory=list)


class ResetFaultsResponse(BaseModel):
    state_version: int
    previous_state_version: int
    service_states: list[MockState]
    warnings: list[str] = Field(default_factory=list)


class DependencyEdge(BaseModel):
    service: str
    depends_on: list[str]
    role: str


class TopologyResponse(BaseModel):
    services: list[DependencyEdge]
    probe_timeout_seconds: float
    state_version: int


class StateResponse(BaseModel):
    state_version: int
    batch_seq: int
    known_config_epochs: dict[str, int | None]
    watcher_running: bool
    watcher_interval_seconds: float
    service_states: list[MockState]


class ProbeEvidence(BaseModel):
    kind: str = Field(description="ok | http_error | timeout | unreachable | invalid_response")
    ok: bool
    http_status: int | None = None
    latency_ms: float = 0.0
    timed_out: bool = False
    unreachable: bool = False
    reason: str | None = None
    state_version: int | None = None
    config_epoch: int | None = None
    blocking_dependencies: list[str] = Field(default_factory=list)
    timed_out_dependencies: list[str] = Field(default_factory=list)
    local_ready_ok: bool | None = None
    detail: str | None = None


class ServiceProbe(BaseModel):
    service: str
    role: str
    live: ProbeEvidence
    ready: ProbeEvidence
    live_ok: bool
    ready_ok: bool
    failed: bool
    failure_kind: str | None = None
    state_version: int | None = None
    config_epoch: int | None = None
    blocking_dependencies: list[str] = Field(default_factory=list)
    timed_out_dependencies: list[str] = Field(default_factory=list)
    local_ready_ok: bool | None = None


class RootCause(BaseModel):
    service: str
    role: str
    failure: str = Field(description="liveness | readiness")
    observation: str


class AffectedService(BaseModel):
    service: str
    role: str
    status: str
    caused_by: list[str]
    dependency_paths: list[list[str]]
    observation: str


class DiagnosisResult(BaseModel):
    batch_id: str
    seq: int
    state_version: int
    status: str
    outcome: Outcome
    summary: str
    root_causes: list[RootCause] = Field(default_factory=list)
    primary_root_cause: str | None = None
    independent_faults_count: int = 0
    affected_services: list[AffectedService] = Field(default_factory=list)
    probes: dict[str, ServiceProbe] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    retest_reasons: list[str] = Field(default_factory=list)
    probe_timeout_seconds: float
    duration_ms: float = 0.0
    superseded: bool = False
    superseded_by: int | None = Field(
        default=None, description="seq of the newer batch that superseded this one"
    )


class DiagnosisBatch(BaseModel):
    batch_id: str
    seq: int
    state_version: int
    status: BatchStatus | str
    outcome: Outcome | str | None = None
    summary: str | None = None
    created_at: str
    completed_at: str | None = None
    superseded: bool = False
    superseded_by: int | None = Field(
        default=None, description="seq of the newer batch that superseded this one"
    )
    result: DiagnosisResult | None = None


class BatchAccepted(BaseModel):
    batch_id: str
    seq: int
    state_version: int
    status: str
    poll_url: str


class BatchSummary(BaseModel):
    batch_id: str
    seq: int
    state_version: int
    status: str
    outcome: str | None = None
    summary: str | None = None
    created_at: str
    completed_at: str | None = None


class BatchList(BaseModel):
    batches: list[BatchSummary]


class HealthResponse(BaseModel):
    status: str
    state_version: int


class ErrorResponse(BaseModel):
    detail: Any
