"""Run the API with uvicorn on API_HOST:API_PORT (`make api`)."""

from __future__ import annotations

import uvicorn

from ..config import get_settings
from ..log import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "signal_scout.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # keep our JSON logging
        access_log=False,  # http_request_seconds covers request timing
    )


if __name__ == "__main__":
    main()
