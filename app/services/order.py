"""Entrypoint for the simulated order service."""
from __future__ import annotations

from ..mock_service import create_mock_app

app = create_mock_app("order")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8003)
