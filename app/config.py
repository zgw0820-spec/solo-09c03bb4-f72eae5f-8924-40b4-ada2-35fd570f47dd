"""Topology and runtime configuration for the fault-diagnosis stack."""
from __future__ import annotations

import os
from dataclasses import dataclass

# Dependency graph. The gateway (entry) depends on auth and order; both of
# those depend on the shared cache. A cache fault therefore explains failures
# observed at three services and must collapse into a single root failure.
DEPENDS_ON: dict[str, tuple[str, ...]] = {
    "gateway": ("auth", "order"),
    "auth": ("cache",),
    "order": ("cache",),
    "cache": (),
}
SERVICES: tuple[str, ...] = ("gateway", "auth", "order", "cache")
ROLES: dict[str, str] = {
    "gateway": "entry",
    "auth": "auth",
    "order": "order",
    "cache": "shared_cache",
}

# Reverse graph, computed once.
DEPENDENTS: dict[str, tuple[str, ...]] = {
    service: tuple(other for other in SERVICES if service in DEPENDS_ON[other])
    for service in SERVICES
}


def downstream_services(service: str) -> list[str]:
    """All services that transitively depend on ``service`` (BFS order)."""
    result: list[str] = []
    queue = list(DEPENDENTS.get(service, ()))
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result.append(current)
        queue.extend(DEPENDENTS[current])
    return result


def all_dependency_paths(
    start: str, target: str, *, max_paths: int = 8
) -> list[list[str]]:
    """All simple paths from ``start`` down dependency edges to ``target``."""
    results: list[list[str]] = []

    def walk(node: str, path: list[str]) -> None:
        if len(results) >= max_paths:
            return
        if node == target:
            results.append(list(path))
            return
        for dep in DEPENDS_ON.get(node, ()):
            if dep in path:
                continue
            path.append(dep)
            walk(dep, path)
            path.pop()

    walk(start, [start])
    return results


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Tests build this directly; compose uses env vars."""

    database_path: str = "data/diagnosis.db"
    # Per-probe budget. A probe exceeding this is recorded as a timeout, which
    # forces the whole batch to "needs_retest" instead of a confident verdict.
    probe_timeout_seconds: float = 2.0
    # Budget for the small admin calls used to snapshot/synchronise state.
    admin_timeout_seconds: float = 1.0
    # Number of terminal batches kept in SQLite; older ones are pruned.
    max_stored_batches: int = 200
    # Poll interval for detecting faults injected directly on a simulated
    # service (bypassing this API). <= 0 disables the watcher.
    watcher_interval_seconds: float = 0.5
    gateway_url: str = "http://gateway:8001"
    auth_url: str = "http://auth:8002"
    order_url: str = "http://order:8003"
    cache_url: str = "http://cache:8004"
    # Optional shared token for the simulated services' admin endpoints.
    admin_token: str = ""

    @property
    def service_urls(self) -> dict[str, str]:
        return {
            "gateway": self.gateway_url.rstrip("/"),
            "auth": self.auth_url.rstrip("/"),
            "order": self.order_url.rstrip("/"),
            "cache": self.cache_url.rstrip("/"),
        }


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def load_settings() -> Settings:
    """Build settings from the current environment (called per app instance)."""
    return Settings(
        database_path=os.getenv("DIAG_DB_PATH", "data/diagnosis.db"),
        probe_timeout_seconds=_env_float("DIAG_PROBE_TIMEOUT", 2.0),
        admin_timeout_seconds=_env_float("DIAG_ADMIN_TIMEOUT", 1.0),
        max_stored_batches=_env_int("DIAG_MAX_STORED_BATCHES", 200),
        watcher_interval_seconds=_env_float("DIAG_WATCH_INTERVAL", 0.5),
        gateway_url=os.getenv("GATEWAY_URL", "http://gateway:8001"),
        auth_url=os.getenv("AUTH_URL", "http://auth:8002"),
        order_url=os.getenv("ORDER_URL", "http://order:8003"),
        cache_url=os.getenv("CACHE_URL", "http://cache:8004"),
        admin_token=os.getenv("DIAG_ADMIN_TOKEN", ""),
    )


def mock_settings_from_env(
    service_name: str, service_urls: dict[str, str] | None = None
) -> dict:
    """Configuration for one simulated service container/process."""
    return {
        "service_name": service_name,
        "dependencies": DEPENDS_ON[service_name],
        "service_urls": service_urls or load_settings().service_urls,
        "admin_token": os.getenv("MOCK_ADMIN_TOKEN") or os.getenv("DIAG_ADMIN_TOKEN", ""),
        "admin_timeout_seconds": _env_float("MOCK_ADMIN_TIMEOUT", 1.0),
        "dependency_timeout_seconds": _env_float("MOCK_DEP_TIMEOUT", 1.2),
        "hang_seconds": _env_float("MOCK_HANG_SECONDS", 5.0),
    }


__all__ = [
    "DEPENDS_ON",
    "DEPENDENTS",
    "ROLES",
    "SERVICES",
    "Settings",
    "all_dependency_paths",
    "downstream_services",
    "load_settings",
    "mock_settings_from_env",
]
