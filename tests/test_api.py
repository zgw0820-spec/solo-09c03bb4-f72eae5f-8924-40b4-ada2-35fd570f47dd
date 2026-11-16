"""Integration tests against real uvicorn processes for all five apps."""
from __future__ import annotations

import time

import httpx

from conftest import Stack, reset, set_fault


def diagnose(client: httpx.Client, **params) -> dict:
    response = client.post("/diagnosis", params={"wait": "true", **params})
    assert response.status_code == 201, response.text
    return response.json()


def test_healthy_stack(client: httpx.Client):
    reset(client)
    body = diagnose(client)
    assert body["outcome"] == "healthy"
    assert body["result"]["independent_faults_count"] == 0
    assert body["result"]["root_causes"] == []
    assert set(body["result"]["probes"]) == {"gateway", "auth", "order", "cache"}
    assert body["result"]["probes"]["gateway"]["ready_ok"] is True


def test_cache_fault_is_one_root_cause_with_three_collateral_services(
    client: httpx.Client,
):
    reset(client)
    before = client.get("/state").json()["state_version"]
    response = set_fault(client, "cache", ready_ok=False)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["state_version"] == before + 1
    assert payload["previous_state_version"] == before
    assert payload["applied"] is True
    assert payload["all_versions_synced"] is True
    assert payload["warnings"] == []
    # Every service adopted the new version.
    assert {s["state_version"] for s in payload["service_states"]} == {before + 1}

    body = diagnose(client)
    assert body["outcome"] == "root_cause_found"
    result = body["result"]
    assert result["independent_faults_count"] == 1
    assert result["primary_root_cause"] == "cache"
    assert [r["service"] for r in result["root_causes"]] == ["cache"]
    affected = {a["service"]: a["caused_by"] for a in result["affected_services"]}
    assert affected == {"auth": ["cache"], "order": ["cache"], "gateway": ["cache"]}
    assert result["retest_reasons"] == []
    assert result["batch_id"] == body["batch_id"]
    assert result["state_version"] == body["state_version"]


def test_auth_fault_implicates_only_gateway(client: httpx.Client):
    reset(client)
    assert set_fault(client, "auth", ready_ok=False).status_code == 200
    result = diagnose(client)["result"]
    assert result["outcome"] == "root_cause_found"
    assert result["primary_root_cause"] == "auth"
    assert [a["service"] for a in result["affected_services"]] == ["gateway"]
    assert result["affected_services"][0]["dependency_paths"] == [["gateway", "auth"]]


def test_order_liveness_fault(client: httpx.Client):
    reset(client)
    assert set_fault(client, "order", live_ok=False).status_code == 200
    result = diagnose(client)["result"]
    assert result["outcome"] == "root_cause_found"
    assert result["independent_faults_count"] == 1
    assert result["root_causes"][0]["service"] == "order"
    assert result["root_causes"][0]["failure"] == "liveness"
    assert [a["service"] for a in result["affected_services"]] == ["gateway"]


def test_two_independent_faults_counted_separately(client: httpx.Client):
    reset(client)
    assert set_fault(client, "cache", ready_ok=False).status_code == 200
    assert set_fault(client, "gateway", live_ok=False).status_code == 200
    result = diagnose(client)["result"]
    assert result["outcome"] == "root_cause_found"
    assert result["independent_faults_count"] == 2
    assert sorted(r["service"] for r in result["root_causes"]) == ["cache", "gateway"]


def test_probe_delay_causing_timeout_returns_needs_retest(client: httpx.Client):
    reset(client)
    # Delay is longer than the probe budget (2s in the fixture).
    assert set_fault(client, "cache", probe_delay_ms=3500).status_code == 200
    started = time.monotonic()
    result = diagnose(client, wait_seconds=20)["result"]
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert result["outcome"] == "needs_retest"
    assert result["root_causes"] == []
    assert result["independent_faults_count"] == 0
    assert any("timed out" in reason for reason in result["retest_reasons"])
    # A timeout must not be converted into a confident auth/order fault: the
    # only timeouts allowed are cache's own probes and dependency checks that
    # timed out waiting on the slow cache (directly or through gateway->auth).
    # Only timeout/freshness reasons may appear; a slow cache never becomes a
    # confident auth/order root cause. Gateway->auth/order dependency checks
    # also time out, which is expected cascade evidence, not a verdict.
    assert all("timed out" in reason for reason in result["retest_reasons"])


def test_all_services_are_probed_concurrently(client: httpx.Client):
    """A 400ms per-probe delay must not accumulate across the four services."""
    reset(client)
    assert set_fault(client, "cache", probe_delay_ms=400).status_code == 200
    started = time.monotonic()
    result = diagnose(client, wait_seconds=20)["result"]
    elapsed = time.monotonic() - started
    assert result["outcome"] == "healthy"
    # Sequential probing would need at least 4 x 400ms = 1.6s plus nested
    # dependency checks; concurrent fan-out stays well under a second.
    assert elapsed < 1.3, f"diagnosis took {elapsed:.2f}s, probes look sequential"
    latencies = [p["live"]["latency_ms"] for p in result["probes"].values()]
    assert len(latencies) == 4
    assert min(latencies) < 350, "all four probed endpoints should overlap"


def test_dependency_check_timeout_returns_needs_retest(client: httpx.Client):
    """cache answers the API within budget but is too slow for auth/order.

    The API's own probe of cache succeeds, yet a dependency check timed out, so
    this batch may not claim a root cause: it must ask for a retest.
    """
    reset(client)
    # 1.5s: under the API's 2s probe budget, over the mocks' 1.2s dep budget.
    assert set_fault(client, "cache", probe_delay_ms=1500).status_code == 200
    result = diagnose(client, wait_seconds=20)["result"]
    assert result["outcome"] == "needs_retest"
    assert result["root_causes"] == []
    joined = " ".join(result["retest_reasons"])
    assert "timed out while checking its dependency cache" in joined


def test_mixed_state_version_returns_needs_retest(client: httpx.Client, stack):
    reset(client)
    version = client.get("/state").json()["state_version"]
    # Someone desynchronises one service behind the API's back.
    response = httpx.post(
        f"{stack.url('auth')}/_fault/sync", json={"state_version": version - 1}
    )
    assert response.status_code == 200
    result = diagnose(client)["result"]
    assert result["outcome"] == "needs_retest"
    assert result["root_causes"] == []
    assert any("state_version" in reason for reason in result["retest_reasons"])


def test_fault_set_via_service_directly_bumps_version_through_watcher(
    client: httpx.Client, stack
):
    reset(client)
    before = client.get("/state").json()["state_version"]
    response = httpx.put(f"{stack.url('cache')}/_fault", json={"ready_ok": False})
    assert response.status_code == 200

    deadline = time.monotonic() + 5
    after = before
    while time.monotonic() < deadline:
        after = client.get("/state").json()["state_version"]
        if after > before:
            break
        time.sleep(0.1)
    assert after > before

    result = diagnose(client)["result"]
    assert result["outcome"] == "root_cause_found"
    assert result["independent_faults_count"] == 1
    assert result["primary_root_cause"] == "cache"


def test_async_batch_and_supersession(client: httpx.Client, stack):
    reset(client)
    # Slow every probe so the first batch is still running when the second starts.
    assert set_fault(client, "cache", probe_delay_ms=600).status_code == 200

    first = client.post("/diagnosis", params={"wait": "false"})
    assert first.status_code == 202, first.text
    first_body = first.json()
    time.sleep(0.15)
    second = client.post("/diagnosis", params={"wait": "false"})
    assert second.status_code == 202
    second_body = second.json()
    assert second_body["seq"] > first_body["seq"]

    deadline = time.monotonic() + 20
    first_record = second_record = None
    while time.monotonic() < deadline:
        first_record = client.get(f"/diagnosis/{first_body['batch_id']}").json()
        second_record = client.get(f"/diagnosis/{second_body['batch_id']}").json()
        if first_record["status"] != "running" and second_record["status"] != "running":
            break
        time.sleep(0.1)

    assert first_record["status"] == "completed"
    assert second_record["status"] == "completed"
    # The older batch's late result is flagged and never becomes the latest.
    assert first_record["superseded"] is True
    assert first_record["superseded_by"] == second_body["seq"]
    assert second_record["superseded"] is False

    latest = client.get("/diagnosis/latest").json()
    assert latest["batch_id"] == second_body["batch_id"]
    assert latest["superseded"] is False

    # Older, unrelated batches are not relabelled: only a genuine
    # finish-after-newer-batch race earns the superseded flag.
    listing = client.get("/diagnosis", params={"limit": 5}).json()["batches"]
    for item in listing:
        if item["seq"] < first_body["seq"]:
            detail = client.get(f"/diagnosis/{item['batch_id']}").json()
            assert detail["superseded"] is False
            assert detail["superseded_by"] is None


def test_wait_budget_expiry_returns_202_then_poll_succeeds(client: httpx.Client):
    reset(client)
    assert set_fault(client, "cache", probe_delay_ms=700).status_code == 200
    response = client.post("/diagnosis", params={"wait": "true", "wait_seconds": 0.2})
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "running"
    assert accepted["poll_url"] == f"/diagnosis/{accepted['batch_id']}"

    deadline = time.monotonic() + 10
    record = None
    while time.monotonic() < deadline:
        record = client.get(f"/diagnosis/{accepted['batch_id']}").json()
        if record["status"] != "running":
            break
        time.sleep(0.1)
    assert record is not None and record["status"] == "completed"
    assert record["batch_id"] == accepted["batch_id"]


def test_results_survive_restart_and_interrupted_batch_needs_retest(
    client: httpx.Client, stack
):
    reset(client)
    first = diagnose(client)
    assert first["status"] == "completed"

    # Start a slow batch and kill the API before it can finish.
    assert set_fault(client, "cache", probe_delay_ms=1500).status_code == 200
    started = client.post("/diagnosis", params={"wait": "false"}).json()
    time.sleep(0.2)
    stack.restart_api(hard=True)

    deadline = time.monotonic() + 10
    record = None
    while time.monotonic() < deadline:
        response = httpx.get(f"{stack.api_url}/diagnosis/{started['batch_id']}")
        if response.status_code == 200:
            record = response.json()
            break
        time.sleep(0.1)
    assert record is not None
    assert record["status"] == "interrupted"
    assert record["outcome"] == "needs_retest"
    assert "restarted" in record["summary"]

    # The previously completed verdict is still readable after the restart.
    replay = httpx.get(f"{stack.api_url}/diagnosis/{first['batch_id']}").json()
    assert replay["status"] == "completed"
    assert replay["outcome"] == first["outcome"]
    assert replay["result"]["probes"]["gateway"]["live"]["ok"] is True

    # The newest terminal batch is still the completed verdict, even though the
    # newer interrupted batch now sits at the top of the seq order.
    latest_completed = httpx.get(
        f"{stack.api_url}/diagnosis/latest", params={"completed_only": True}
    ).json()
    assert latest_completed["status"] == "completed"
    latest_any = httpx.get(f"{stack.api_url}/diagnosis/latest").json()
    assert latest_any["batch_id"] == started["batch_id"]
    assert latest_any["outcome"] == "needs_retest"


def test_reset_clears_faults_and_bumps_version(client: httpx.Client):
    reset(client)
    assert set_fault(client, "gateway", probe_delay_ms=50).status_code == 200
    before = client.get("/state").json()["state_version"]
    response = client.post("/faults/reset")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state_version"] == before + 1
    assert payload["warnings"] == []
    result = diagnose(client)["result"]
    assert result["outcome"] == "healthy"


def test_version_conflict_guard(client: httpx.Client):
    reset(client)
    current = client.get("/state").json()["state_version"]
    response = set_fault(client, "order", ready_ok=False)  # bumps version
    assert response.status_code == 200
    conflict = client.put(
        "/faults/auth",
        json={"ready_ok": False},
        headers={"X-Expected-State-Version": str(current)},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current"] == current + 1
    # And the conflicting write did not take effect.
    result = diagnose(client)["result"]
    assert result["primary_root_cause"] == "order"


def test_unknown_service_is_404(client: httpx.Client):
    response = client.put("/faults/database", json={"ready_ok": False})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_service"


def test_swagger_and_openapi_available(client: httpx.Client):
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    schema = client.get("/openapi.json").json()
    for path in (
        "/diagnosis",
        "/diagnosis/latest",
        "/diagnosis/{batch_id}",
        "/faults/{service}",
        "/faults/reset",
        "/state",
        "/topology",
    ):
        assert path in schema["paths"], path
    assert "DiagnosisBatch" in schema["components"]["schemas"]


def test_topology_matches_requirements(client: httpx.Client):
    topology = client.get("/topology").json()
    edges = {e["service"]: e for e in topology["services"]}
    assert edges["gateway"]["depends_on"] == ["auth", "order"]
    assert edges["auth"]["depends_on"] == ["cache"]
    assert edges["order"]["depends_on"] == ["cache"]
    assert edges["cache"]["depends_on"] == []


def test_unreachable_cache_is_single_unreachable_root_cause(stack):
    """A cache process that refuses connections is reported once, not 3 times."""
    from pathlib import Path
    import tempfile

    standalone = Stack(Path(tempfile.mkdtemp()) / "unreachable.db", unavailable=("cache",))
    standalone.start()
    try:
        with httpx.Client(base_url=standalone.api_url, timeout=30.0) as standalone_client:
            reset(standalone_client)
            body = diagnose(standalone_client)
        result = body["result"]
        assert body["outcome"] == "root_cause_found"
        assert result["independent_faults_count"] == 1
        assert result["primary_root_cause"] == "cache"
        assert result["root_causes"][0]["failure"] == "unreachable"
        affected = {a["service"] for a in result["affected_services"]}
        assert {"auth", "order", "gateway"} <= affected
    finally:
        standalone.stop()
