#!/usr/bin/env python3
"""Entry point for ContainerLab GUI."""

import uvicorn
from app.config import settings
from app.services.shutdown_registry import shutdown_registry


class DnlabServer(uvicorn.Server):
    """Uvicorn server that asks long-lived sessions to close on SIGTERM."""

    def handle_exit(self, sig, frame) -> None:
        shutdown_registry.request_shutdown(f"signal {sig}")
        super().handle_exit(sig, frame)


if __name__ == "__main__":
    config = uvicorn.Config(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        log_level="info",
    )
    DnlabServer(config).run()
