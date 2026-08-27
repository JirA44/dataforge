"""Console entry point for the development server."""

from __future__ import annotations

import os


def run() -> None:
    import uvicorn

    uvicorn.run(
        "dataforge.api:app",
        host=os.getenv("DATAFORGE_HOST", "127.0.0.1"),
        port=int(os.getenv("DATAFORGE_PORT", "8010")),
        reload=False,
    )


if __name__ == "__main__":
    run()

