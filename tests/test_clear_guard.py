"""Unit tests for the optimistic single-root-cause clear guard (pure function)."""
from __future__ import annotations

import pytest

from app.diagnosis import assess_clear_request


def evidence(version: int = 5, epoch: int = 3, *, kind: str = "ok", **extra) -> dict:
    base = {
        "kind": kind,
        "ok": kind == "ok",
        "http_status": 200 if kind == "ok" else 503,
        "latency_ms": 1.0,
        "timed_out": kind == "timeout",
        "unreachable": kind == "unreachable",
        "reason": "alive" if kind == "ok" else "failed",
        "detail": None,
        "state_version": version,
        "config_epoch": epoch,
        "local_ready_ok": True,
        "blocking_dependencies": [],
        "timed_out_dependencies": [],
    }
    base.update(extra)
    return base


def probe(epoch: int = 3, *, live_kind: str = "ok", ready_kind: str = "ok", **ready_extra) -> dict:
    live = evidence(epoch=epoch, kind=live_kind)
    ready = evidence(epoch=epoch, kind=ready_kind, **ready_extra)
    return {
        "live": live,
        "ready": ready,
        "config_epoch": epoch,
        "state_version": 5,
        "live_ok": live["ok"],
        "ready_ok": ready["ok"],
        "failed": not (live["ok"] and ready["ok"]),
    }


def root(service: str, failure: str = "readiness") -> dict:
    return {"service": service, "role": service, "failure": failure, "observation": "..."}


def affected(service: str) -> dict:
    return {
        "service": service,
        "role": service,
        "status": "collateral",
        "caused_by": ["cache"],
        "dependency_paths": [[service, "cache"]],
        "observation": "...",
    }


def make_record(
    *,
    batch_id: str = "b1",
    status: str = "completed",
    version: int = 5,
    outcome: str = "root_cause_found",
    roots: list[dict] | None = None,
    collateral: list[dict] | None = None,
    cache_probe: dict | None = None,
    gateway_probe: dict | None = None,
    epochs: dict[str, int] | None = None,
) -> dict:
    epochs = epochs or {"gateway": 2, "auth": 2, "order": 2, "cache": 3}
    probes = {name: probe(epochs[name]) for name in epochs}
    if cache_probe is not None:
        probes["cache"] = cache_probe
    if gateway_probe is not None:
        probes["gateway"] = gateway_probe
    return {
        "batch_id": batch_id,
        "seq": 7,
        "state_version": version,
        "status": status,
        "outcome": outcome,
        "result": {
            "outcome": outcome,
            "root_causes": roots if roots is not None else [root("cache")],
            "affected_services": collateral
            if collateral is not None
            else [affected("auth"), affected("order"), affected("gateway")],
            "probes": probes,
        },
    }


def states(epochs: dict[str, int] | None = None, *, unreachable: set[str] | None = None) -> list[dict]:
    epochs = epochs or {"gateway": 2, "auth": 2, "order": 2, "cache": 3}
    unreachable = unreachable or set()
    return [
        {
            "service": name,
            "reachable": name not in unreachable,
            "config_epoch": None if name in unreachable else epoch,
        }
        for name, epoch in epochs.items()
    ]


def assess(record, *, target="cache", expected=5, current=5, service_states=None, latest="b1"):
    return assess_clear_request(
        record=record,
        latest_completed_batch_id=latest,
        expected_version=expected,
        current_state_version=current,
        target=target,
        service_states=service_states or states(),
    )


def test_happy_path_readiness_root_is_allowed():
    outcome = assess(make_record())
    assert outcome["allowed"] is True
    assert outcome["error"] is None
    assert outcome["batch_config_epoch"] == 3
    assert outcome["current_config_epoch"] == 3


def test_liveness_root_is_allowed():
    gp = probe(2, live_kind="http_error", ready_kind="http_error", local_ready_ok=True)
    record = make_record(
        roots=[root("gateway", "liveness")],
        collateral=[],
        gateway_probe=gp,
    )
    outcome = assess(record, target="gateway")
    assert outcome["allowed"] is True


def test_missing_batch_is_refused():
    outcome = assess(None)
    assert outcome == {
        "allowed": False,
        "error": "batch_not_found",
        "message": outcome["message"],
        "batch_config_epoch": None,
        "current_config_epoch": None,
    }


def test_running_batch_is_refused():
    outcome = assess(make_record(status="running"))
    assert outcome["allowed"] is False
    assert outcome["error"] == "batch_not_completed"


def test_superseded_by_newer_completed_batch_is_refused():
    outcome = assess(make_record(batch_id="old"), latest="newer")
    assert outcome["allowed"] is False
    assert outcome["error"] == "stale_batch"


def test_submitted_version_not_matching_batch_is_refused():
    outcome = assess(make_record(version=5), expected=4)
    assert outcome["allowed"] is False
    assert outcome["error"] == "stale_batch"


def test_global_version_moved_is_refused():
    outcome = assess(make_record(), current=6)
    assert outcome["allowed"] is False
    assert outcome["error"] == "state_version_conflict"


def test_target_epoch_changed_is_refused():
    # The service was re-faulted directly after the diagnosis: epoch moved 3 -> 4.
    outcome = assess(
        make_record(),
        service_states=states({"gateway": 2, "auth": 2, "order": 2, "cache": 4}),
    )
    assert outcome["allowed"] is False
    assert outcome["error"] == "target_config_changed"
    assert outcome["batch_config_epoch"] == 3
    assert outcome["current_config_epoch"] == 4


def test_other_services_epoch_changes_are_allowed():
    # Only the target's configuration is part of the guard.
    outcome = assess(
        make_record(),
        service_states=states({"gateway": 99, "auth": 2, "order": 2, "cache": 3}),
    )
    assert outcome["allowed"] is True


def test_target_currently_unreachable_is_refused():
    outcome = assess(
        make_record(),
        service_states=states(unreachable={"cache"}),
    )
    assert outcome["allowed"] is False
    assert outcome["error"] == "target_unreachable"


def test_collateral_target_is_refused():
    outcome = assess(make_record(), target="auth")
    assert outcome["allowed"] is False
    assert outcome["error"] == "target_not_root_cause"


def test_unreachable_root_cause_is_not_clearable():
    cp = probe(3, live_kind="unreachable", ready_kind="unreachable")
    record = make_record(
        roots=[root("cache", "unreachable")], cache_probe=cp
    )
    outcome = assess(record)
    assert outcome["allowed"] is False
    assert outcome["error"] == "target_unreachable"


def test_needs_retest_verdict_has_no_clearable_root():
    record = make_record(outcome="needs_retest", roots=[], collateral=[])
    outcome = assess(record)
    assert outcome["allowed"] is False
    assert outcome["error"] == "not_root_cause_verdict"


def test_timeout_evidence_blocks_clearing():
    cp = probe(3, live_kind="timeout", ready_kind="http_error", local_ready_ok=False)
    record = make_record(roots=[root("cache", "readiness")], cache_probe=cp)
    outcome = assess(record)
    assert outcome["allowed"] is False
    assert outcome["error"] == "target_evidence_not_clearable"


def test_two_independent_roots_each_clearable_but_only_target_checked():
    gp = probe(2, live_kind="http_error", ready_kind="http_error", local_ready_ok=True)
    record = make_record(
        roots=[root("cache", "readiness"), root("gateway", "liveness")],
        collateral=[affected("auth"), affected("order")],
        gateway_probe=gp,
    )
    cache_outcome = assess(record, target="cache")
    gateway_outcome = assess(record, target="gateway")
    assert cache_outcome["allowed"] is True
    assert gateway_outcome["allowed"] is True
