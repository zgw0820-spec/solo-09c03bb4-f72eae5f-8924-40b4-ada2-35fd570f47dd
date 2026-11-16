"""End-to-end tests for the single-root-cause clear API (real processes).

The drill scenario: cache readiness fault + gateway liveness fault are
injected together, two independent root causes are confirmed, then only the
cache root cause is cleared while the gateway fault must stay in place.
"""
from __future__ import annotations

import time

import httpx

from conftest import reset, set_fault


def diagnose(client: httpx.Client) -> dict:
    response = client.post("/diagnosis", params={"wait": "true"})
    assert response.status_code == 201, response.text
    return response.json()


def clear(
    client: httpx.Client,
    batch: dict,
    target: str,
    *,
    wait_seconds: float | None = None,
) -> httpx.Response:
    body = {
        "batch_id": batch["batch_id"],
        "state_version": batch["state_version"],
        "target_service": target,
    }
    params = {"wait_seconds": str(wait_seconds)} if wait_seconds is not None else {}
    return client.post("/faults/clear-root-cause", json=body, params=params)


def test_clear_one_of_two_independent_root_causes_keeps_the_other(client: httpx.Client):
    reset(client)
    # The drill: two independent local faults at once.
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    assert set_fault(client, "gateway", live_ok=False).status_code == 200

    batch = diagnose(client)
    assert batch["result"]["outcome"] == "root_cause_found"
    assert batch["result"]["independent_faults_count"] == 2
    assert sorted(r["service"] for r in batch["result"]["root_causes"]) == [
        "cache",
        "gateway",
    ]

    response = clear(client, batch, "cache")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "cleared"
    assert payload["target_service"] == "cache"
    assert payload["submitted_batch_id"] == batch["batch_id"]
    assert payload["clear_applied"] is True
    assert payload["reasons"] == []
    # The clear itself bumps the global version.
    assert payload["state_version"] == batch["state_version"] + 1
    assert payload["previous_state_version"] == batch["state_version"]
    assert payload["verification_outcome"] == "root_cause_found"
    assert payload["target_still_root_cause"] is False
    # The other root cause is still reported — only cache was cleared.
    assert [r["service"] for r in payload["remaining_root_causes"]] == ["gateway"]

    # And a fresh independent diagnosis says the same thing.
    follow_up = diagnose(client)["result"]
    assert follow_up["outcome"] == "root_cause_found"
    assert follow_up["independent_faults_count"] == 1
    assert [r["service"] for r in follow_up["root_causes"]] == ["gateway"]
    cache_probe = follow_up["probes"]["cache"]
    assert cache_probe["live_ok"] is True and cache_probe["ready_ok"] is True
    gateway_probe = follow_up["probes"]["gateway"]
    assert gateway_probe["live_ok"] is False

    # Only the cache fault settings were reset; gateway's live_ok stays false.
    states = {s["service"]: s for s in client.get("/state").json()["service_states"]}
    assert states["gateway"]["live_ok"] is False
    assert states["cache"]["ready_ok"] is True
    assert states["cache"]["live_ok"] is True


def test_verification_batch_is_pollable_and_auditable(client: httpx.Client):
    reset(client)
    assert set_fault(client, "order", ready_ok=False).status_code == 200
    batch = diagnose(client)

    payload = clear(client, batch, "order").json()
    verification = client.get(
        f"/diagnosis/{payload['verification_batch_id']}"
    ).json()
    assert verification["status"] == "completed"
    assert verification["seq"] > batch["seq"]
    assert verification["state_version"] == payload["state_version"]
    assert verification["result"]["outcome"] == "healthy"


def test_replaying_the_same_request_after_success_changes_nothing(client: httpx.Client):
    reset(client)
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    batch = diagnose(client)

    first = clear(client, batch, "cache")
    assert first.status_code == 200
    version_after_clear = first.json()["state_version"]

    # Re-inject a *new* fault after the clear, then replay the old request.
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    replay = clear(client, batch, "cache")
    assert replay.status_code == 409
    assert replay.json()["detail"]["error"] == "stale_batch"

    # The newly set fault survives the replay untouched.
    result = diagnose(client)["result"]
    assert result["primary_root_cause"] == "cache"
    assert client.get("/state").json()["state_version"] > version_after_clear


def test_stale_state_version_is_refused_without_side_effects(client: httpx.Client):
    reset(client)
    assert set_fault(client, "auth", ready_ok=False).status_code == 200
    batch = diagnose(client)
    version_at_batch = batch["state_version"]

    # Something else advances the global version before the clear arrives.
    assert set_fault(client, "order", ready_ok=False).status_code == 200
    current = client.get("/state").json()["state_version"]
    assert current > version_at_batch

    response = clear(client, batch, "auth")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "state_version_conflict"

    # Both faults are still present: the refused request wrote nothing.
    result = diagnose(client)["result"]
    assert sorted(r["service"] for r in result["root_causes"]) == ["auth", "order"]


def test_target_config_changed_directly_on_service_is_refused(client, stack):
    reset(client)
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    batch = diagnose(client)
    batch_epoch = batch["result"]["probes"]["cache"]["config_epoch"]

    # Re-set the fault directly on the mock service (bypassing the API): clear
    # it locally first, then inject it again so the service's config_epoch
    # actually moves; the watcher then bumps the global version as well.
    assert httpx.post(f"{stack.url('cache')}/_fault/reset").status_code == 200
    direct = httpx.put(f"{stack.url('cache')}/_fault", json={"ready_ok": False})
    assert direct.status_code == 200
    deadline = time.monotonic() + 5
    states = {}
    while time.monotonic() < deadline:
        states = {
            s["service"]: s
            for s in client.get("/state").json()["service_states"]
        }
        if states["cache"]["config_epoch"] != batch_epoch:
            break
        time.sleep(0.1)
    assert states["cache"]["config_epoch"] != batch_epoch

    response = clear(client, batch, "cache")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] in (
        "target_config_changed",
        "state_version_conflict",
        "stale_batch",
    )
    # The direct fault is still there — the old request must not reset it.
    result = diagnose(client)["result"]
    assert result["primary_root_cause"] == "cache"


def test_clearing_a_collateral_service_is_refused(client: httpx.Client):
    reset(client)
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    batch = diagnose(client)

    response = clear(client, batch, "auth")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "target_not_root_cause"
    # Nothing was modified: version unchanged and cache still the root cause.
    assert client.get("/state").json()["state_version"] == batch["state_version"]
    assert diagnose(client)["result"]["primary_root_cause"] == "cache"


def test_unreachable_target_in_diagnosis_cannot_be_cleared(client, stack):
    reset(client)
    assert set_fault(client, "auth", ready_ok=False).status_code == 200
    batch = diagnose(client)
    stack.stop_service("auth")

    response = clear(client, batch, "auth")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "target_unreachable"
    # The version was not advanced: refused before any write.
    assert client.get("/state").json()["state_version"] == batch["state_version"]


def test_unknown_batch_and_service_are_rejected(client: httpx.Client):
    reset(client)
    batch = diagnose(client)

    missing = client.post(
        "/faults/clear-root-cause",
        json={
            "batch_id": "does-not-exist",
            "state_version": batch["state_version"],
            "target_service": "cache",
        },
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "batch_not_found"

    bad_service = client.post(
        "/faults/clear-root-cause",
        json={
            "batch_id": batch["batch_id"],
            "state_version": batch["state_version"],
            "target_service": "database",
        },
    )
    assert bad_service.status_code == 422


def test_pending_verification_when_wait_budget_expires_then_poll_confirms(
    client: httpx.Client,
):
    reset(client)
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    batch = diagnose(client)

    # A budget shorter than one probe round: the receipt must stay pending.
    response = clear(client, batch, "cache", wait_seconds=0.05)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "pending_verification"
    assert payload["clear_applied"] is True
    assert payload["reasons"]
    assert any("wait budget" in reason for reason in payload["reasons"])

    # The verification batch still completes in the background and confirms it.
    deadline = time.monotonic() + 15
    verification = None
    while time.monotonic() < deadline:
        verification = client.get(
            f"/diagnosis/{payload['verification_batch_id']}"
        ).json()
        if verification["status"] != "running":
            break
        time.sleep(0.1)
    assert verification is not None and verification["status"] == "completed"
    assert verification["result"]["outcome"] == "healthy"

    # And the fault really was cleared despite the pending receipt.
    assert diagnose(client)["result"]["outcome"] == "healthy"


def test_pending_verification_when_verification_probes_time_out(client, stack):
    """A timeout during verification must never be reported as cleared."""
    reset(client)
    # Two independent faults; the clear targets cache — gateway is never touched.
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    assert set_fault(client, "gateway", live_ok=False).status_code == 200
    batch = diagnose(client)
    assert sorted(r["service"] for r in batch["result"]["root_causes"]) == [
        "cache",
        "gateway",
    ]

    # Freeze the *non-target* service before the clear is sent. SIGSTOP changes
    # no fault settings and bumps no version; the target's guard still passes,
    # the single-service reset still works, but the verification probes of the
    # frozen gateway time out -> needs_retest -> pending_verification.
    stack.pause_service("gateway")
    try:
        response = clear(client, batch, "cache", wait_seconds=20)
    finally:
        stack.resume_service("gateway")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["status"] == "pending_verification"
    assert payload["clear_applied"] is True
    assert any("needs_retest" in reason for reason in payload["reasons"])
    assert any("timed out" in reason for reason in payload["reasons"])
    verification = client.get(
        f"/diagnosis/{payload['verification_batch_id']}"
    ).json()
    assert verification["result"]["outcome"] == "needs_retest"

    # Once probes work again: the target fault really was cleared, while the
    # gateway's own liveness fault survived untouched throughout.
    follow_up = diagnose(client)["result"]
    assert follow_up["outcome"] == "root_cause_found"
    assert [r["service"] for r in follow_up["root_causes"]] == ["gateway"]
    assert follow_up["probes"]["cache"]["ready_ok"] is True
    assert follow_up["probes"]["gateway"]["live_ok"] is False


def test_clear_endpoint_is_in_openapi(client: httpx.Client):
    schema = client.get("/openapi.json").json()
    assert "/faults/clear-root-cause" in schema["paths"]
    assert "RootCauseClearResponse" in schema["components"]["schemas"]
    assert "ClearRootCauseRequest" in schema["components"]["schemas"]
