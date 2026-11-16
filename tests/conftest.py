"""End-to-end tests: real uvicorn processes for all four mocks plus the API.

Run with:  python -m pytest -q
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("gateway", "auth", "order", "cache")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.5)
            if response.status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"{url} did not become ready: {last_error}")


class Stack:
    def __init__(
        self,
        db_path: Path,
        probe_timeout: float = 2.0,
        unavailable: tuple[str, ...] = (),
    ) -> None:
        self.proc: list[subprocess.Popen[bytes]] = []
        self.ports = {name: _free_port() for name in SERVICES}
        self.unavailable = set(unavailable)
        self.api_port = _free_port()
        self.db_path = db_path
        self.probe_timeout = probe_timeout
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "DIAG_DB_PATH": str(db_path),
            "DIAG_PROBE_TIMEOUT": str(probe_timeout),
            "DIAG_WATCH_INTERVAL": "0.2",
            **{f"{name.upper()}_URL": f"http://127.0.0.1:{port}" for name, port in self.ports.items()},
        }
        self._logs: list[Path] = []

    def start(self) -> None:
        for name in SERVICES:
            if name in self.unavailable:
                continue
            self._spawn(f"app.services.{name}:app", self.ports[name], f"mock-{name}")
        for name, port in self.ports.items():
            if name in self.unavailable:
                continue
            _wait_http(f"http://127.0.0.1:{port}/health/live")
        self._spawn("app.main:app", self.api_port, "diagnosis-api")
        _wait_http(self.api_url + "/health")

    def _spawn(self, target: str, port: int, label: str) -> None:
        log_path = self.db_path.parent / f"{label}.log"
        self._logs.append(log_path)
        handle = open(log_path, "wb")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                target,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=self.env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self.proc.append(proc)

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def url(self, service: str) -> str:
        return f"http://127.0.0.1:{self.ports[service]}"

    def stop(self) -> None:
        for proc in self.proc:
            proc.terminate()
        for proc in self.proc:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.proc.clear()

    def restart_api(self, *, hard: bool = False) -> None:
        # Index 4 is the diagnosis API (the four mocks are spawned first).
        proc = self.proc[4]
        if hard:
            proc.kill()
        else:
            proc.terminate()
        proc.wait(timeout=10)
        self._spawn("app.main:app", self.api_port, f"diagnosis-api-restart-{time.monotonic_ns()}")
        _wait_http(self.api_url + "/health")


@pytest.fixture(scope="session")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("fault-diagnosis")


@pytest.fixture()
def stack(tmp_root: Path) -> Iterator[Stack]:
    import uuid

    db_path = tmp_root / f"{uuid.uuid4().hex}.db"
    stack = Stack(db_path)
    stack.start()
    try:
        yield stack
    finally:
        stack.stop()


@pytest.fixture()
def client(stack: Stack) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=stack.api_url, timeout=30.0) as client:
        yield client


def reset(client: httpx.Client) -> None:
    response = client.post("/faults/reset")
    assert response.status_code == 200, response.text


def set_fault(client: httpx.Client, service: str, **patch) -> httpx.Response:
    return client.put(f"/faults/{service}", json=patch)
