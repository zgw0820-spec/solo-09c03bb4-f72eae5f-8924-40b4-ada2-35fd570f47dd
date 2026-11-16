"""Pure diagnosis reduction: probe evidence -> root cause / collateral / retest.

The reduction is deliberately conservative:

* Any timed-out probe, unusable response, mixed/foreign ``state_version`` or a
  configuration epoch that moved mid-probe means the batch cannot support a
  confident verdict: the outcome is ``needs_retest`` and root causes are empty.
* A service failing readiness purely because a dependency is not ready is
  *collateral*, not a root cause. Because the cache sits under both auth and
  order, a single cache fault produces failures at cache, auth, order and the
  gateway but exactly **one** independent fault.
* A failed service that cannot be explained by an observed local fault or a
  dependency path to one also forces ``needs_retest`` rather than a guess.
"""
from __future__ import annotations

from typing import Any

from .config import (
    DEPENDS_ON,
    ROLES,
    SERVICES,
    all_dependency_paths,
    downstream_services,
)

ENDPOINTS = ("live", "ready")


def _root_cause(service: str, failure: str, observation: str) -> dict[str, Any]:
    return {
        "service": service,
        "role": ROLES[service],
        "failure": failure,
        "observation": observation,
    }


def _describe_probe(service: str, endpoint: str, ev: dict[str, Any]) -> str:
    latency = ev.get("latency_ms") or 0.0
    version = ev.get("state_version")
    version_text = f", state_version={version}" if version is not None else ", state_version=unknown"
    if ev.get("kind") == "ok":
        return f"{service} /health/{endpoint}: ok in {latency}ms{version_text}"
    if ev.get("kind") == "timeout":
        return f"{service} /health/{endpoint}: TIMEOUT after {latency}ms{version_text}"
    if ev.get("kind") == "unreachable":
        return f"{service} /health/{endpoint}: unreachable ({ev.get('detail') or 'transport error'})"
    if ev.get("kind") == "invalid_response":
        return f"{service} /health/{endpoint}: unusable response ({ev.get('detail')})"
    status = ev.get("http_status")
    reason = ev.get("reason") or "failed"
    extra = ""
    if endpoint == "ready":
        extra = (
            f", local_ready_ok={ev.get('local_ready_ok')}"
            f", blocking_dependencies={ev.get('blocking_dependencies') or []}"
            f", timed_out_dependencies={ev.get('timed_out_dependencies') or []}"
        )
    return (
        f"{service} /health/{endpoint}: HTTP {status} {reason} in {latency}ms"
        f"{version_text}{extra}"
    )


def evaluate(
    probes: dict[str, dict[str, Any]],
    *,
    expected_version: int,
    current_version: int,
    baseline_epochs: dict[str, int | None],
    probe_timeout_seconds: float,
) -> dict[str, Any]:
    """Reduce one batch of probe results into a diagnosis verdict."""
    evidence: list[str] = []
    retest_reasons: list[str] = []

    # -- 1. Freshness / consistency gate -------------------------------
    for service in SERVICES:
        probe = probes.get(service)
        if probe is None:
            retest_reasons.append(f"{service}: no probe result was produced")
            continue
        observed_versions: set[int] = set()
        for endpoint in ENDPOINTS:
            ev = probe.get(endpoint) or {}
            evidence.append(_describe_probe(service, endpoint, ev))
            if ev.get("kind") == "timeout":
                retest_reasons.append(
                    f"{service} /health/{endpoint} timed out "
                    f"(probe budget {probe_timeout_seconds:.2f}s)"
                )
            elif ev.get("kind") == "invalid_response":
                retest_reasons.append(
                    f"{service} /health/{endpoint} returned an unusable response"
                )
            elif ev.get("kind") in ("ok", "http_error"):
                version = ev.get("state_version")
                if version is None:
                    retest_reasons.append(
                        f"{service} /health/{endpoint} did not report a state_version"
                    )
                else:
                    observed_versions.add(version)
                    if version != expected_version:
                        retest_reasons.append(
                            f"{service} /health/{endpoint} reported state_version "
                            f"{version}, expected {expected_version}"
                        )
            baseline = baseline_epochs.get(service)
            epoch = ev.get("config_epoch")
            if baseline is not None and epoch is not None and epoch != baseline:
                retest_reasons.append(
                    f"{service} /health/{endpoint} configuration changed during the "
                    f"probe (config_epoch {baseline} -> {epoch})"
                )
            if endpoint == "ready":
                for dep in ev.get("timed_out_dependencies") or []:
                    retest_reasons.append(
                        f"{service} timed out while checking its dependency {dep}; "
                        "the dependency's responsiveness is not proven for this batch"
                    )
        if len(observed_versions) > 1:
            retest_reasons.append(
                f"{service} live/ready disagreed on state_version: "
                f"{sorted(observed_versions)}"
            )

    if current_version != expected_version:
        retest_reasons.append(
            f"global state version changed from {expected_version} to "
            f"{current_version} while the diagnosis was running"
        )

    if retest_reasons:
        return _retest_result(
            evidence=evidence,
            reasons=_dedupe(retest_reasons),
            expected_version=expected_version,
        )

    # -- 2. Classify failures ------------------------------------------
    roots: dict[str, dict[str, Any]] = {}
    collateral_candidates: dict[str, list[str]] = {}
    unexplained: list[str] = []

    for service in SERVICES:
        probe = probes[service]
        live = probe["live"]
        ready = probe["ready"]
        if live["ok"] and ready["ok"]:
            continue

        if not live["ok"]:
            if live.get("unreachable"):
                roots[service] = _root_cause(
                    service,
                    "unreachable",
                    f"{service} liveness probe could not connect "
                    f"({live.get('detail') or 'transport error'}); the process is "
                    "not serving and its state_version could not be verified",
                )
            else:
                roots[service] = _root_cause(
                    service,
                    "liveness",
                    f"{service} local liveness probe reports failure "
                    f"({live.get('reason') or 'failed'}); readiness fails as a "
                    "consequence of the same local fault",
                )
            continue

        # Liveness is fine; readiness failed.
        if ready.get("unreachable"):
            unexplained.append(
                f"{service}: readiness probe was unreachable although its "
                "liveness probe succeeded (inconsistent evidence)"
            )
            continue

        blocking = list(ready.get("blocking_dependencies") or [])
        local_ready_ok = ready.get("local_ready_ok")
        if local_ready_ok is False:
            roots[service] = _root_cause(
                service,
                "readiness",
                f"{service} local readiness probe reports failure "
                f"({ready.get('reason') or 'not ready'}); its own dependency "
                "checks are not the cause",
            )
        elif blocking:
            collateral_candidates[service] = blocking
        else:
            unexplained.append(
                f"{service}: readiness failed with no local failure and no "
                "blocking dependency reported"
            )

    # -- 3. Attribute collateral failures to root causes ---------------
    affected: list[dict[str, Any]] = []
    for service, blocking in collateral_candidates.items():
        for dep in blocking:
            if dep not in DEPENDS_ON[service]:
                unexplained.append(
                    f"{service}: reported blocking dependency {dep!r} that is not "
                    "a declared dependency"
                )
            elif probes.get(dep, {}).get("ready", {}).get("ok") is True:
                unexplained.append(
                    f"{service}: reports dependency {dep} as not ready, but "
                    f"{dep}'s own readiness probe passed (inconsistent evidence)"
                )

        caused_by: list[str] = []
        paths: list[list[str]] = []
        for root_service in roots:
            root_paths = all_dependency_paths(service, root_service)
            if root_paths:
                caused_by.append(root_service)
                for path in root_paths:
                    if path not in paths:
                        paths.append(path)
        if not caused_by:
            unexplained.append(
                f"{service}: readiness failed because dependencies {blocking} are "
                "not ready, but no observed local root cause reaches it"
            )
            continue
        affected.append(
            {
                "service": service,
                "role": ROLES[service],
                "status": "collateral",
                "caused_by": caused_by,
                "dependency_paths": paths,
                "observation": (
                    f"{service} /health/ready reports not_ready with local readiness "
                    f"ok and blocking dependencies {blocking}; "
                    + "; ".join(
                        f"path {' -> '.join(path)} explains the failure" for path in paths
                    )
                ),
            }
        )

    if unexplained:
        return _retest_result(
            evidence=evidence,
            reasons=_dedupe(unexplained),
            expected_version=expected_version,
        )

    # -- 4. Verdict -----------------------------------------------------
    if not roots:
        return {
            "outcome": "healthy",
            "summary": (
                f"All {len(SERVICES)} services passed liveness and readiness at "
                f"state version {expected_version}; no fault detected."
            ),
            "root_causes": [],
            "primary_root_cause": None,
            "independent_faults_count": 0,
            "affected_services": [],
            "evidence": evidence,
            "retest_reasons": [],
        }

    root_list = sorted(
        roots.values(),
        key=lambda item: (-len(downstream_services(item["service"])), item["service"]),
    )
    root_names = [item["service"] for item in root_list]
    affected_names = [item["service"] for item in affected]
    cache_is_root = "cache" in roots
    independent_count = len(root_list)

    if independent_count == 1:
        root = root_list[0]
        summary = (
            f"Root cause: {root['service']} ({root['role']}) {root['failure']} "
            f"failure. "
        )
    else:
        summary = (
            f"{independent_count} independent root causes: "
            + ", ".join(f"{item['service']} ({item['failure']})" for item in root_list)
            + ". "
        )

    if affected_names:
        summary += (
            f"Collateral services (failures fully explained by those root "
            f"causes): {', '.join(affected_names)}. "
        )
    else:
        summary += "No collateral services. "

    if cache_is_root and (len(root_list) > 1 or affected_names):
        summary += (
            "The shared cache fault is counted once, not once per affected "
            "caller; auth and order are collateral, not independent faults."
        )
    else:
        summary += f"Independent fault count: {independent_count}."

    return {
        "outcome": "root_cause_found",
        "summary": summary.strip(),
        "root_causes": root_list,
        "primary_root_cause": root_names[0] if independent_count == 1 else None,
        "independent_faults_count": independent_count,
        "affected_services": affected,
        "evidence": evidence,
        "retest_reasons": [],
    }


def assess_clear_request(
    *,
    record: dict[str, Any] | None,
    latest_completed_batch_id: str | None,
    expected_version: int,
    current_state_version: int,
    target: str,
    service_states: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure guard for clearing one diagnosed local root cause.

    All the data this function needs is fetched by the caller while holding the
    coordinator's state lock; keeping the decision itself pure makes every
    staleness rule unit-testable without processes or network.

    Returns ``{"allowed": bool, "error": str | None, "message": str | None,
    "batch_config_epoch": int | None, "current_config_epoch": int | None}``.
    """

    def reject(error: str, message: str) -> dict[str, Any]:
        return {
            "allowed": False,
            "error": error,
            "message": message,
            "batch_config_epoch": None,
            "current_config_epoch": None,
        }

    if record is None:
        return reject(
            "batch_not_found",
            "the submitted diagnosis batch does not exist; run POST /diagnosis first",
        )
    if record.get("status") != "completed":
        return reject(
            "batch_not_completed",
            f"batch {record.get('batch_id')} is {record.get('status')}, not a completed "
            "diagnosis; only a completed verdict can authorise clearing a root cause",
        )
    if latest_completed_batch_id != record.get("batch_id"):
        return reject(
            "stale_batch",
            "a newer completed diagnosis exists; the submitted batch no longer "
            "represents the current situation, so its clear request is refused",
        )
    if record.get("state_version") != expected_version:
        return reject(
            "stale_batch",
            f"the submitted state_version {expected_version} does not match batch "
            f"{record.get('batch_id')} (version {record.get('state_version')})",
        )
    if current_state_version != expected_version:
        return reject(
            "state_version_conflict",
            f"global state version changed from {expected_version} to "
            f"{current_state_version} after the submitted diagnosis; the old clear "
            "request is refused and nothing was modified",
        )

    target_state = next(
        (state for state in service_states if state.get("service") == target), None
    )
    if target_state is None or not target_state.get("reachable"):
        return reject(
            "target_unreachable",
            f"service {target} is currently unreachable, so its local fault cannot be "
            "cleared through the admin API; nothing was modified",
        )

    result = record.get("result") or {}
    probes = result.get("probes") or {}
    target_probe = probes.get(target) or {}
    batch_epoch = target_probe.get("config_epoch")
    current_epoch = target_state.get("config_epoch")
    if batch_epoch is None or current_epoch is None or batch_epoch != current_epoch:
        return {
            "allowed": False,
            "error": "target_config_changed",
            "message": (
                f"{target} configuration epoch changed since the submitted diagnosis "
                f"(batch config_epoch={batch_epoch}, current config_epoch={current_epoch}); "
                "the fault may have been re-set directly on the service, so the old "
                "clear request is refused and nothing was modified"
            ),
            "batch_config_epoch": batch_epoch,
            "current_config_epoch": current_epoch,
        }

    ok, error, message = _classify_clearable_root(result, target, target_probe)
    if not ok:
        return reject(error, message or "target is not a clearable local root cause")
    return {
        "allowed": True,
        "error": None,
        "message": None,
        "batch_config_epoch": batch_epoch,
        "current_config_epoch": current_epoch,
    }


def _classify_clearable_root(
    result: dict[str, Any], target: str, target_probe: dict[str, Any]
) -> tuple[bool, str | None, str | None]:
    """Only reachable local liveness/readiness root causes may be cleared."""
    if result.get("outcome") != "root_cause_found":
        return False, "not_root_cause_verdict", (
            f"the latest diagnosis outcome is {result.get('outcome')!r}, not "
            "root_cause_found; there is no confirmed local root cause to clear"
        )
    root = next(
        (item for item in result.get("root_causes", []) if item.get("service") == target),
        None,
    )
    if root is None:
        if any(
            item.get("service") == target
            for item in result.get("affected_services", [])
        ):
            hint = "it is only a collateral service whose failure is explained by a dependency"
        else:
            hint = "it is not reported as a root cause in the submitted diagnosis"
        return False, "target_not_root_cause", (
            f"{target} cannot be cleared from this diagnosis: {hint}"
        )
    failure = root.get("failure")
    if failure == "unreachable":
        return False, "target_unreachable", (
            f"{target} was unreachable when diagnosed; an unreachable process has no "
            "admin endpoint to clear, so this root cause cannot be removed here"
        )
    if failure not in ("liveness", "readiness"):
        return False, "target_not_clearable", (
            f"{target} root cause has unsupported failure kind {failure!r}"
        )
    # Defence in depth: the evidence behind the verdict must be a reachable,
    # parseable local failure — never a timeout or an unusable response.
    for endpoint in ENDPOINTS:
        ev = target_probe.get(endpoint) or {}
        if ev.get("kind") not in ("ok", "http_error") or ev.get("unreachable"):
            return False, "target_evidence_not_clearable", (
                f"{target} /health/{endpoint} evidence is {ev.get('kind')!r}; only a "
                "reachable local liveness/readiness failure can be cleared"
            )
    return True, None, None


def _retest_result(
    *, evidence: list[str], reasons: list[str], expected_version: int
) -> dict[str, Any]:
    return {
        "outcome": "needs_retest",
        "summary": (
            f"No confident root cause at state version {expected_version}: "
            f"{len(reasons)} freshness/consistency problem(s) require a retest."
        ),
        "root_causes": [],
        "primary_root_cause": None,
        "independent_faults_count": 0,
        "affected_services": [],
        "evidence": evidence,
        "retest_reasons": reasons,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


__all__ = ["assess_clear_request", "evaluate"]
