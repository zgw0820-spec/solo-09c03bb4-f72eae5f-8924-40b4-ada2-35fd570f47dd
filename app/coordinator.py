"""Batch coordinator: state versioning, fault injection, probe runs, storage.

Concurrency model (one API process):

* ``_state_lock`` serialises global state-version bump plus the
  version snapshot taken when a batch is created and when it is evaluated.
* ``_run_lock`` admits one probe phase at a time. Probe phases are short
  (bounded by explicit timeouts) and serialising them prevents a slow, older
  batch's admin calls from clobbering a newer batch's version synchronisation.
* ``_batch_lock`` guards the in-memory map of active batches.

Ordering guarantees:

* Every batch gets a monotonically increasing ``seq`` (persisted in SQLite).
* A batch finishing after a younger batch exists is stored but flagged
  ``superseded`` with the younger batch's seq; unrelated older batches are not
  relabelled. A late response therefore stays auditable yet can never become
  ``GET /diagnosis/latest``.
* ``state_version`` is persisted so a restart never reissues a used version.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import DEPENDS_ON, ROLES, SERVICES, Settings
from .diagnosis import evaluate
from .probes import ServiceCallError, ServiceClient
from .store import Store


class UnknownServiceError(ValueError):
    pass


class VersionConflictError(RuntimeError):
    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            f"state version changed from {expected} to {current} while applying the fault"
        )
        self.expected = expected
        self.current = current


class Coordinator:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._state_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()
        # state_version and batch_seq are persisted so a restart can never
        # hand out a version or sequence that was already used.
        self.state_version = self._load_int("state_version", 1)
        self.batch_seq = self._load_int("batch_seq", store.max_seq())
        self._active: dict[str, asyncio.Task[Any]] = {}
        # Epochs last observed/commanded per service. Used by the watcher to
        # notice faults injected directly on a simulated service, bypassing
        # this API. ``None`` means "not known yet".
        self._expected_epochs: dict[str, int | None] = {name: None for name in SERVICES}
        self._watcher_task: asyncio.Task[None] | None = None

    # -- persisted counters -------------------------------------------
    def _load_int(self, key: str, default: int) -> int:
        try:
            return max(int(self.store.get_meta(key) or default), int(default))
        except (TypeError, ValueError):
            return int(default)

    def _bump_version_locked(self) -> tuple[int, int]:
        previous = self.state_version
        self.state_version += 1
        self.store.set_meta("state_version", str(self.state_version))
        return previous, self.state_version

    def _next_seq(self) -> int:
        self.batch_seq += 1
        self.store.set_meta("batch_seq", str(self.batch_seq))
        return self.batch_seq

    # -- startup -------------------------------------------------------
    async def recover_after_restart(self) -> int:
        """Mark batches abandoned by the previous process; re-adopt version.

        Completed results stay readable. Unfinished batches are relabelled
        ``interrupted`` / ``needs_retest`` so no one mistakes them for a verdict.
        """
        count = self.store.mark_all_running_interrupted(
            "diagnosis API restarted before this batch finished; its in-flight "
            "probes are unknown and the batch must be re-run"
        )
        try:
            await asyncio.wait_for(
                self.sync_all_services(self.state_version),
                timeout=self.settings.admin_timeout_seconds + 0.5,
            )
            for state in await self.service_states():
                if state.get("reachable") and state.get("config_epoch") is not None:
                    self._expected_epochs[state["service"]] = state["config_epoch"]
        except (TimeoutError, asyncio.TimeoutError):
            pass
        except ServiceCallError:
            pass
        return count

    # -- external fault watcher ----------------------------------------
    def start_watcher(self) -> None:
        if self.settings.watcher_interval_seconds <= 0 or self._watcher_task is not None:
            return
        self._watcher_task = asyncio.create_task(
            self._watch_loop(), name="fault-watcher"
        )

    async def stop_watcher(self) -> None:
        task = self._watcher_task
        self._watcher_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _watch_loop(self) -> None:
        interval = max(0.05, self.settings.watcher_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.check_external_fault_changes()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep watching; never take the API down
                continue

    async def check_external_fault_changes(self) -> bool:
        """Detect faults set directly on a service and bump the state version.

        The diagnosis API is the intended writer, but somebody may call a
        simulated service's ``PUT /_fault`` directly. Because every service
        reports its ``config_epoch``, such a change is still detectable: the
        global state version is bumped and re-synchronised exactly as the API
        would have done.

        Skipped while a probe phase is in progress; the next tick picks it up.
        The whole check runs under the state lock so it cannot interleave with
        a fault set through this API and cause a spurious double bump.
        """
        if self._run_lock.locked():
            return False
        async with self._state_lock:
            states = await self.service_states()
            changed = [
                state["service"]
                for state in states
                if state.get("reachable")
                and state.get("config_epoch") is not None
                and self._expected_epochs.get(state["service"]) is not None
                and state["config_epoch"] != self._expected_epochs[state["service"]]
            ]
            if changed:
                _, current = self._bump_version_locked()
                await self.sync_all_services(current)
            # Always refresh what we believe the epochs are, so a failure to
            # bump is retried only once (a stuck service is reported by its
            # probes instead).
            for state in states:
                if state.get("reachable") and state.get("config_epoch") is not None:
                    self._expected_epochs[state["service"]] = state["config_epoch"]
            return bool(changed)

    # -- fault injection ----------------------------------------------
    async def set_fault(
        self,
        service: str,
        patch: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if service not in SERVICES:
            raise UnknownServiceError(f"unknown service {service!r}")
        patch = {key: value for key, value in patch.items() if value is not None}
        if not patch:
            raise ValueError("at least one fault setting is required")

        async with self._state_lock:
            if expected_version is not None and expected_version != self.state_version:
                raise VersionConflictError(expected_version, self.state_version)
            previous, current = self._bump_version_locked()

            # Advance every service's adopted version first, so probes taken
            # after the fault see one consistent version.
            client = await self._make_client()
            try:
                sync_states = await client.sync_all(current)
                applied = True
                error: str | None = None
                try:
                    target = await client.set_fault(service, patch)
                    if target.get("config_epoch") is not None:
                        self._expected_epochs[service] = target["config_epoch"]
                except ServiceCallError as exc:
                    applied = False
                    error = str(exc)
                states_after = await client.snapshot_states()
                for state in states_after:
                    if state.get("reachable") and state.get("config_epoch") is not None:
                        self._expected_epochs[state["service"]] = state["config_epoch"]
            finally:
                await self._close_client(client)

        warnings: list[str] = []
        unsynced = [s["service"] for s in sync_states if not s.get("synced")]
        if unsynced:
            warnings.append(
                "state version sync failed for: "
                + ", ".join(unsynced)
                + "; those services will answer probes without the current version, "
                "so a diagnosis either reports them as unreachable or asks for a retest"
            )
        if not applied:
            warnings.append(
                f"global state version was already advanced to {current}, but the "
                f"fault could not be applied to {service}: {error}"
            )
        return {
            "state_version": current,
            "previous_state_version": previous,
            "service": service,
            "applied": applied,
            "all_versions_synced": not unsynced,
            "service_states": await self.service_states(),
            "warnings": warnings,
        }

    async def reset_faults(self, *, expected_version: int | None = None) -> dict[str, Any]:
        async with self._state_lock:
            if expected_version is not None and expected_version != self.state_version:
                raise VersionConflictError(expected_version, self.state_version)
            previous, current = self._bump_version_locked()
            client = await self._make_client()
            try:
                results = await client.reset_all()
                sync_states = await client.sync_all(current)
                states_after = await client.snapshot_states()
                for state in states_after:
                    if state.get("reachable") and state.get("config_epoch") is not None:
                        self._expected_epochs[state["service"]] = state["config_epoch"]
            finally:
                await self._close_client(client)

        warnings = [
            f"could not reset {item['service']}: {item['error']}"
            for item in results
            if not item.get("reset")
        ]
        unsynced = [s["service"] for s in sync_states if not s.get("synced")]
        if unsynced:
            warnings.append("state version sync failed for: " + ", ".join(unsynced))
        return {
            "state_version": current,
            "previous_state_version": previous,
            "service_states": await self.service_states(),
            "warnings": warnings,
        }

    async def sync_all_services(self, version: int) -> list[dict[str, Any]]:
        client = await self._make_client()
        try:
            return await client.sync_all(version)
        finally:
            await self._close_client(client)

    async def service_states(self) -> list[dict[str, Any]]:
        client = await self._make_client()
        try:
            states = await client.snapshot_states()
        finally:
            await self._close_client(client)
        for state in states:
            state["dependencies"] = list(DEPENDS_ON[state["service"]])
            state["reachable"] = bool(state.get("reachable", True))
        return states

    # -- diagnosis -----------------------------------------------------
    async def create_batch(self) -> dict[str, Any]:
        """Create a batch record and start its probes in the background."""
        batch_id = uuid.uuid4().hex
        async with self._batch_lock:
            async with self._state_lock:
                expected_version = self.state_version
                seq = self._next_seq()
                self.store.create_running_batch(batch_id, seq, expected_version)
                # Register the task before any await can let it run and finish,
                # otherwise a fast batch would be added after its own cleanup.
                task = asyncio.create_task(
                    self._run_batch(batch_id, seq, expected_version),
                    name=f"diagnosis-{batch_id}",
                )
                self._active[batch_id] = task
        return {
            "batch_id": batch_id,
            "seq": seq,
            "state_version": expected_version,
            "status": "running",
            "poll_url": f"/diagnosis/{batch_id}",
        }

    async def _run_batch(self, batch_id: str, seq: int, expected_version: int) -> None:
        started = datetime.now(timezone.utc)
        try:
            async with self._run_lock:
                pre_reasons: list[str] = []
                async with self._state_lock:
                    version_before = self.state_version
                if version_before != expected_version:
                    pre_reasons.append(
                        f"global state version changed from {expected_version} to "
                        f"{version_before} before probing began"
                    )
                    baseline_epochs: dict[str, int | None] = {}
                    probes: dict[str, Any] = {}
                else:
                    client = await self._make_client()
                    try:
                        # Read baseline epochs without mutating services: the
                        # fault path already synchronised versions. A service
                        # whose admin endpoint is down is not itself a reason
                        # to retest: its probe evidence (typically a refused
                        # connection) still speaks for itself, and a reachable
                        # probe carries its own state_version.
                        states = await client.snapshot_states()
                        baseline_epochs = {
                            state["service"]: state.get("config_epoch")
                            for state in states
                            if state.get("reachable")
                        }
                        probes = await client.probe_all()
                    finally:
                        await self._close_client(client)
                    async with self._state_lock:
                        version_after = self.state_version
                    if version_after != expected_version:
                        pre_reasons.append(
                            f"global state version changed from {expected_version} to "
                            f"{version_after} while probing"
                        )

                if probes:
                    assessment = evaluate(
                        probes,
                        expected_version=expected_version,
                        current_version=self.state_version,
                        baseline_epochs=baseline_epochs,
                        probe_timeout_seconds=self.settings.probe_timeout_seconds,
                    )
                else:
                    assessment = _empty_assessment(expected_version)

                if pre_reasons:
                    reasons = _dedupe([*pre_reasons, *assessment["retest_reasons"]])
                    assessment = {
                        **assessment,
                        "outcome": "needs_retest",
                        "root_causes": [],
                        "primary_root_cause": None,
                        "independent_faults_count": 0,
                        "affected_services": [],
                        "retest_reasons": reasons,
                        "summary": (
                            f"No confident root cause at state version "
                            f"{expected_version}: {len(reasons)} freshness/"
                            "consistency problem(s) require a retest."
                        ),
                    }

            duration_ms = round(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000.0, 3
            )
            result = {
                "batch_id": batch_id,
                "seq": seq,
                "state_version": expected_version,
                "status": "completed",
                "outcome": assessment["outcome"],
                "summary": assessment["summary"],
                "root_causes": assessment["root_causes"],
                "primary_root_cause": assessment["primary_root_cause"],
                "independent_faults_count": assessment["independent_faults_count"],
                "affected_services": assessment["affected_services"],
                "probes": probes,
                "evidence": assessment["evidence"],
                "retest_reasons": assessment["retest_reasons"],
                "probe_timeout_seconds": self.settings.probe_timeout_seconds,
                "duration_ms": duration_ms,
                "superseded": False,
                "superseded_by": None,
            }
            superseder = self._younger_batch_seq(seq)
            if superseder is not None:
                result["superseded"] = True
                result["superseded_by"] = superseder
                result["summary"] = (
                    f"Superseded by newer diagnosis batch #{superseder}; this late "
                    "response is kept for audit only and does not override the "
                    "current result. " + result["summary"]
                )
            self.store.finish_batch(
                batch_id,
                state_version=expected_version,
                status="completed",
                outcome=result["outcome"],
                summary=result["summary"],
                result=result,
                superseded_by=superseder,
            )
        except asyncio.CancelledError:
            self.store.finish_batch(
                batch_id,
                state_version=expected_version,
                status="interrupted",
                outcome="needs_retest",
                summary=(
                    "diagnosis API shut down before this batch finished; its probes "
                    "are incomplete and the batch must be re-run"
                ),
                result=None,
            )
            raise
        except Exception as exc:  # never leave a batch silently running
            self.store.finish_batch(
                batch_id,
                state_version=expected_version,
                status="failed",
                outcome="needs_retest",
                summary=(
                    f"batch failed before a verdict ({type(exc).__name__}: {exc}); "
                    "retest required"
                ),
                result=None,
            )
        finally:
            async with self._batch_lock:
                if self._active.get(batch_id) is asyncio.current_task():
                    self._active.pop(batch_id, None)

    def _younger_batch_seq(self, seq: int) -> int | None:
        latest = self.store.latest_batch()
        if latest is not None and latest["seq"] > seq:
            return latest["seq"]
        return None

    # -- reads ---------------------------------------------------------
    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._enrich(self.store.get_batch(batch_id))

    def latest_batch(self, *, completed_only: bool = False) -> dict[str, Any] | None:
        return self._enrich(self.store.latest_batch(completed_only=completed_only))

    def list_batches(self, limit: int) -> list[dict[str, Any]]:
        return [self._enrich(record) for record in self.store.list_batches(limit)]

    def _enrich(self, record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        record = dict(record)
        record["seq"] = record.get("seq") or 0
        record["superseded"] = bool(record.get("superseded") or False)
        if record["status"] == "running":
            record["outcome"] = None
        return record

    def snapshot_epochs(self) -> dict[str, int | None]:
        """Last known configuration epoch per service (public read view)."""
        return dict(self._expected_epochs)

    # -- helpers -------------------------------------------------------
    async def _make_client(self) -> ServiceClient:
        return ServiceClient(self.settings, httpx.AsyncClient())

    async def _close_client(self, client: ServiceClient) -> None:
        await client.client.aclose()

    def topology(self) -> dict[str, Any]:
        return {
            "services": [
                {
                    "service": service,
                    "role": ROLES[service],
                    "depends_on": list(DEPENDS_ON[service]),
                }
                for service in SERVICES
            ],
            "probe_timeout_seconds": self.settings.probe_timeout_seconds,
            "state_version": self.state_version,
        }

    async def close(self) -> None:
        async with self._batch_lock:
            tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _empty_assessment(expected_version: int) -> dict[str, Any]:
    return {
        "outcome": "needs_retest",
        "summary": (
            f"No confident root cause at state version {expected_version}: the batch "
            "was invalidated before probing could run; retest required."
        ),
        "root_causes": [],
        "primary_root_cause": None,
        "independent_faults_count": 0,
        "affected_services": [],
        "evidence": [],
        "retest_reasons": ["batch invalidated before probing could run"],
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


__all__ = ["Coordinator", "UnknownServiceError", "VersionConflictError"]
