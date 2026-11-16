"""Entrypoint for the simulated gateway service."""
from __future__ import annotations

from ..mock_service import create_mock_app

app = create_mock_app("gateway")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
